"""Live model discovery and a read-only audit of the catalogue and default policy.

Codex exposes capabilities through the App Server without starting a turn.
Claude has no list endpoint: only an explicit, tool-free one-turn probe can
establish availability and resolve an alias. Its efforts remain unverified.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from lazytools.routing.catalogue import StepModel, TierCatalogue, load_default_tiers, load_tiers
from lazytools.routing.policy import DEFAULT_POLICY, ModelPolicy

CODEX_TIMEOUT = 30.0
CLAUDE_TIMEOUT = 60.0
CLAUDE_PROMPT = "Reply with just: ok"
_PROBE_WINDOWS = os.name == "nt"
_PROBE_CLEANUP_SECONDS = 5.0


@dataclass(frozen=True)
class ModelInfo:
    engine: str
    model: str
    available: bool | None
    efforts: tuple[str, ...] | None = None
    is_default: bool | None = None
    default_effort: str | None = None
    hidden: bool = False
    answered_models: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    error: str | None = None


def _parse_codex(result: dict[str, Any]) -> list[ModelInfo]:
    data = result.get("data")
    if not isinstance(data, list):
        raise RuntimeError("codex model/list returned no data array")
    models = []
    for entry in data:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"].strip():
            raise RuntimeError("codex model/list returned a model without an id")
        efforts = entry.get("supportedReasoningEfforts")
        if not isinstance(efforts, list) or not efforts or any(
            not isinstance(item, dict) or not isinstance(item.get("reasoningEffort"), str) or not item["reasoningEffort"]
            for item in efforts
        ):
            raise RuntimeError(f"codex model/list returned no usable efforts for {entry['id']}")
        models.append(ModelInfo(
            engine="codex", model=entry["id"], available=True,
            efforts=tuple(dict.fromkeys(item["reasoningEffort"] for item in efforts)),
            is_default=bool(entry.get("isDefault", False)),
            default_effort=entry.get("defaultReasoningEffort"), hidden=bool(entry.get("hidden", False)),
        ))
    return models


async def fetch_codex_models(*, executable: str | None = None, timeout: float = CODEX_TIMEOUT) -> list[ModelInfo]:
    """Use the quota reader's initialize/initialized lifecycle, then model/list.

    No thread or turn is created. The deadline covers the handshake and all
    pages, and the ephemeral App Server is reaped even on refusal or timeout.
    """
    from lazybridge.engines.codex.app_server import codex_executable

    process = await asyncio.create_subprocess_exec(
        executable or codex_executable(), "app-server",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, limit=1024 * 1024,
    )

    async def send(message: dict[str, Any]) -> None:
        assert process.stdin is not None
        process.stdin.write((json.dumps(message) + "\n").encode())
        await process.stdin.drain()

    async def answer(wanted: int) -> dict[str, Any]:
        assert process.stdout is not None
        while True:
            line = await process.stdout.readline()
            if not line:
                raise RuntimeError("codex app-server closed the stream before answering model/list")
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict) or message.get("id") != wanted:
                continue
            if "error" in message:
                raise RuntimeError(f"codex app-server refused request {wanted}: {message['error']}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise RuntimeError(f"codex app-server returned no result for request {wanted}")
            return result

    try:
        async with asyncio.timeout(timeout):
            await send({
                "method": "initialize", "id": 1,
                "params": {"clientInfo": {"name": "lazytools", "title": "LazyTools", "version": "1"},
                           "capabilities": {"experimentalApi": True}},
            })
            await answer(1)
            await send({"method": "initialized", "params": {}})
            models: list[ModelInfo] = []
            params: dict[str, Any] = {}
            request_id = 2
            cursors: set[str] = set()
            while True:
                await send({"method": "model/list", "id": request_id, "params": params})
                result = await answer(request_id)
                models.extend(_parse_codex(result))
                cursor = result.get("nextCursor")
                if cursor is None:
                    break
                if not isinstance(cursor, str) or not cursor or cursor in cursors:
                    raise RuntimeError("codex model/list returned an invalid or repeated cursor")
                cursors.add(cursor)
                params = {"cursor": cursor}
                request_id += 1
            if not models:
                raise RuntimeError("codex model/list offered no models")
            if len({model.model for model in models}) != len(models):
                raise RuntimeError("codex model/list returned duplicate model ids")
            return models
    except TimeoutError as exc:
        raise RuntimeError(f"codex app-server did not answer model/list within {timeout:g}s") from exc
    finally:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


def _claude_executable() -> str:
    # Match the bridge engine's SDK preference for its bundled native CLI.
    name = "claude.exe" if os.name == "nt" else "claude"
    try:
        bundled = files("claude_agent_sdk").joinpath("_bundled", name)
        if bundled.is_file():
            return str(bundled)
    except ModuleNotFoundError:
        pass
    found = shutil.which(name)
    if found is not None:
        return found
    native = Path.home() / ".local" / "bin" / name
    if native.is_file():
        return str(native)
    raise FileNotFoundError("Claude Code CLI not found; install the native CLI or the claude-agent-sdk extra")


def _kill_probe_tree(process: subprocess.Popen[str]) -> None:
    """Stop a POSIX group, or a suspended Windows child whose job attachment failed."""
    if not _PROBE_WINDOWS:
        with contextlib.suppress(OSError):
            os.killpg(process.pid, signal.SIGKILL)  # type: ignore[attr-defined]  # POSIX-only branch
    with contextlib.suppress(OSError):
        process.kill()


def _run_claude(argv: list[str], *, cwd: Path, timeout: float) -> subprocess.CompletedProcess[str]:
    # Avoid subprocess.run's Windows timeout path: it re-communicates without
    # a deadline, and grandchildren may still hold stdout/stderr pipe handles.
    from lazytools.code_bridge._probe_process import WindowsProbeJob

    job = WindowsProbeJob() if _PROBE_WINDOWS else None
    try:
        process = subprocess.Popen(
            argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            start_new_session=not _PROBE_WINDOWS,
            # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | CREATE_SUSPENDED
            creationflags=(0x08000000 | 0x00000200 | 0x00000004) if _PROBE_WINDOWS else 0,
        )
    except BaseException:
        if job is not None:
            job.close()
        raise
    try:
        if job is not None:
            job.attach_and_resume(process.pid)
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    except BaseException:
        if job is not None:
            job.close()  # Stops descendants even if the root CLI has already exited.
        _kill_probe_tree(process)
        # Every teardown wait is bounded. In particular, never call communicate()
        # without a timeout, even after killing the tree or receiving EOF.
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            process.communicate(timeout=_PROBE_CLEANUP_SECONDS)
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            process.wait(timeout=_PROBE_CLEANUP_SECONDS)
        raise
    finally:
        if job is not None:
            job.close()


def probe_claude_model(model: str, *, cwd: Path, timeout: float = CLAUDE_TIMEOUT) -> ModelInfo:
    """One paid turn, without tools, MCP, hooks or persistence, in an empty directory."""
    try:
        result = _run_claude(
            [_claude_executable(), "-p", CLAUDE_PROMPT, "--model", model,
             "--output-format", "json", "--max-turns", "1", "--tools", "", "--no-session-persistence",
             "--strict-mcp-config", "--setting-sources", "", "--safe-mode"],
            cwd=cwd, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ModelInfo("claude", model, False, error=f"{type(exc).__name__}: {exc}")
    warnings = tuple(line.strip() for line in result.stderr.splitlines() if "unrecognized_model" in line)
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return ModelInfo("claude", model, False, warnings=warnings, error="Claude probe returned no JSON object")
    usage = payload.get("modelUsage")
    answered = tuple(key for key in usage if isinstance(key, str)) if isinstance(usage, dict) else ()
    succeeded = result.returncode == 0 and not payload.get("is_error") and bool(answered)
    error = None
    if not succeeded:
        error = str(payload.get("result") or payload.get("error") or f"probe exit {result.returncode}; no answering model reported")
    return ModelInfo("claude", model, succeeded, answered_models=answered, warnings=warnings, error=error)


class _AuditPolicy(ModelPolicy):
    """Keep structural validation, but report ALL capability mismatches after discovery.

    The normal loader fails at the first unsupported model or effort. An audit
    must still read those entries to show every mismatch against live truth.
    """

    def reject_codex_model(self, model: str | None, *, fails_when: str) -> str | None:
        return None

    def reject_claude_model(self, model: str | None, *, fails_when: str) -> str | None:
        return None

    def reject_effort(self, effort: str | None, *, engine: str, model: str | None = None) -> str | None:
        return None


def _entries(catalogue: dict[str, TierCatalogue]) -> Iterator[tuple[str, StepModel]]:
    for name, tier in catalogue.items():
        for index, step in enumerate(tier.steps):
            for candidate in step.providers:
                yield f"{name}.steps[{index}].providers.{candidate.provider}", candidate


def _audit(
    catalogue: dict[str, TierCatalogue], codex: list[ModelInfo] | None,
    claude: dict[str, ModelInfo], *, policy: ModelPolicy,
) -> list[str]:
    mismatches = []
    offered = {model.model: model for model in codex} if codex is not None else {}
    if codex is not None:
        for model in codex:
            if model.model not in policy.codex_models:
                mismatches.append(f"policy: Codex model {model.model!r} is offered but missing from the policy")
            else:
                expected = set(policy.efforts_for("codex", model.model))
                actual = set(model.efforts or ())
                for effort in sorted(expected - actual):
                    mismatches.append(f"policy: Codex model {model.model!r} allows effort {effort!r}, not supported by that model")
                for effort in sorted(actual - expected):
                    mismatches.append(f"policy: Codex model {model.model!r} offers effort {effort!r}, missing from the policy")
        for model_name in policy.codex_models:
            if model_name not in offered:
                mismatches.append(f"policy: Codex model {model_name!r} is not offered")
    for where, candidate in _entries(catalogue):
        if candidate.provider == "codex" and codex is not None:
            live = offered.get(candidate.model)
            if live is None:
                mismatches.append(f"catalogue {where}: model {candidate.model!r} is not offered by Codex")
            elif candidate.effort is not None and candidate.effort.strip() not in (live.efforts or ()):
                mismatches.append(f"catalogue {where}: effort {candidate.effort.strip()!r} is not supported by model {candidate.model!r}")
        elif candidate.provider == "claude_code" and candidate.model in claude:
            live = claude[candidate.model]
            if live.available is False:
                mismatches.append(f"catalogue {where}: Claude model {candidate.model!r} is unavailable ({live.error})")
            elif candidate.model.startswith("claude-") and candidate.model not in live.answered_models:
                mismatches.append(f"catalogue {where}: Claude model {candidate.model!r} answered as {', '.join(live.answered_models)}")
        rejection = policy.reject_model(candidate.provider, candidate.model)
        if rejection is not None:
            mismatches.append(f"catalogue {where}: default policy rejects model {candidate.model!r}: {rejection}")
        rejection = policy.reject_effort(candidate.effort, engine=candidate.provider, model=candidate.model)
        if rejection is not None:
            mismatches.append(f"catalogue {where}: default policy rejects effort {candidate.effort!r} for model {candidate.model!r}: {rejection}")
    return mismatches


def inspect_models(*, tiers_path: Path | None = None, probe_claude: bool = False) -> dict[str, Any]:
    catalogue = load_tiers(tiers_path, policy=_AuditPolicy()) if tiers_path is not None else load_default_tiers(policy=_AuditPolicy())
    errors = []
    try:
        codex: list[ModelInfo] | None = asyncio.run(fetch_codex_models())
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        codex = None
        errors.append(f"Codex discovery failed: {exc}")
    rows = list(codex or ())
    known = {row.model for row in rows}
    codex_names = dict.fromkeys([
        *DEFAULT_POLICY.codex_models,
        *(candidate.model for _, candidate in _entries(catalogue) if candidate.provider == "codex"),
    ])
    for model in codex_names:
        if model not in known:
            rows.append(ModelInfo("codex", model, False if codex is not None else None))
    claude_names = dict.fromkeys([
        *(candidate.model for _, candidate in _entries(catalogue) if candidate.provider == "claude_code"),
        "sonnet", "opus",
    ])
    claude: dict[str, ModelInfo] = {}
    if probe_claude:
        with tempfile.TemporaryDirectory(prefix="lazytools-model-probe-") as directory:
            for model in claude_names:
                info = probe_claude_model(model, cwd=Path(directory))
                claude[model] = info
                if info.error is not None:
                    errors.append(f"Claude probe {model!r}: {info.error}")
    rows.extend(claude.get(model, ModelInfo("claude", model, None)) for model in claude_names)
    return {
        "models": [asdict(row) for row in rows],
        "probe_claude": probe_claude,
        "mismatches": _audit(catalogue, codex, claude, policy=DEFAULT_POLICY),
        "errors": errors,
    }


def print_report(report: dict[str, Any]) -> None:
    if report["probe_claude"]:
        print("Claude probes: one turn per model/alias; consumes a small amount of quota.")
    else:
        print("Claude not probed; use --probe-claude (consumes a small amount of quota).")
    print("Claude efforts/defaults: unknown (no model-list endpoint; probes test availability only).")
    table = [["engine", "model", "available", "efforts", "default", "answered model", "warning"]]
    for row in report["models"]:
        efforts = ",".join(row["efforts"]) if row["efforts"] is not None else "unknown"
        if row["default_effort"]:
            efforts += f" (default {row['default_effort']})"
        warning = "; ".join(row["warnings"])
        if row["hidden"]:
            warning = "hidden" + ("; " + warning if warning else "")
        table.append([
            row["engine"], row["model"], "yes" if row["available"] is True else "no" if row["available"] is False else "unknown",
            efforts, "yes" if row["is_default"] is True else "no" if row["is_default"] is False else "unknown",
            ",".join(row["answered_models"]) or "-", warning or "-",
        ])
    widths = [max(len(row[index]) for row in table) for index in range(len(table[0]))]
    for row in table:
        print("  ".join(value.ljust(width) for value, width in zip(row, widths, strict=True)).rstrip())
    for mismatch in report["mismatches"]:
        print(f"mismatch: {mismatch}")
    for error in report["errors"]:
        print(f"error: {error}")
    print(f"Audit: {len(report['mismatches'])} mismatch(es), {len(report['errors'])} error(s).")
