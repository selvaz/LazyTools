"""``cloud-envs``: which cloud environments exist and which repository each serves.

Codex Cloud: the environments the account's tasks ran in, read from
``codex cloud list --json`` (``environment_id`` / ``environment_label`` of each
task), joined with the owner's ``[cloud.codex.envs]`` map (repo -> env id).
Claude cloud: the account's default environment (chosen at claude.ai/code, not
by the CLI) and any ``[cloud.claude.routines]`` entries.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from lazytools.code_bridge import _cloud, _cloud_claude
from lazytools.code_bridge._cloud import CloudConfig, CloudError

_SLUG_RE = re.compile(r"[^/\s]+/[^/\s]+")


def parse_codex_list(text: str) -> dict[str, str]:
    """``{environment_id: environment_label}`` from ``codex cloud list --json`` (tolerant of the wrapper shape)."""
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise CloudError(f"codex cloud list --json printed no JSON: {_cloud.redact(text.strip())[:300]}") from exc
    items = payload
    if isinstance(payload, dict):
        items = next((payload[k] for k in ("tasks", "items", "data", "results") if isinstance(payload.get(k), list)), [])
    envs: dict[str, str] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        env_id = item.get("environment_id") or item.get("env_id")
        if env_id:
            label = str(item.get("environment_label") or item.get("env_label") or "")
            if str(env_id) not in envs or (label and not envs[str(env_id)]):
                envs[str(env_id)] = label
    return envs


def list_envs(config: CloudConfig, *, current_repo: str | None = None, probe_codex: bool = True) -> dict[str, Any]:
    """Rows for every known environment plus notes about what could not be read."""
    notes: list[str] = []
    seen: dict[str, str] = {}
    if probe_codex:
        try:
            seen = parse_codex_list(_cloud.CodexCloud()._run("list", "--json")[1])
        except CloudError as exc:
            notes.append(f"could not read Codex Cloud environments (showing the config only): {exc}")
    mapped: dict[str, list[str]] = {}
    for repo, env_id in config.envs.get("codex", {}).items():
        mapped.setdefault(env_id, []).append(repo)
    rows: list[dict[str, Any]] = []
    for env_id in sorted(set(seen) | set(mapped)):
        label = seen.get(env_id, "")
        repos = sorted(mapped.get(env_id, []))
        hint = label if label and _SLUG_RE.fullmatch(label) else None
        rows.append({
            "provider": "codex", "env_id": env_id, "label": label, "repos": repos,
            "repo": repos[0] if repos else hint,
            "configured": bool(repos), "seen_in_codex": env_id in seen,
            "note": None if repos else "not mapped: add it under [cloud.codex.envs] to use it from this bridge",
        })
    rows.append({
        "provider": "claude", "env_id": _cloud_claude.ENV_LABEL, "label": "account default environment (claude.ai/code)",
        "repos": [], "repo": None, "configured": True, "seen_in_codex": False, "note": _cloud_claude.ENV_NOTE,
    })
    for name, entry in sorted(config.routines.items()):
        rows.append({
            "provider": "claude", "env_id": f"routine:{name}", "label": f"routine {entry['id']}", "repos": [],
            "repo": None, "configured": True, "seen_in_codex": False,
            "token_env": entry["token_env"], "token_set": bool(os.environ.get(entry["token_env"], "").strip()),
            "note": "the routine's own repository, branch and environment apply",
        })
    result: dict[str, Any] = {"environments": rows, "notes": notes}
    if current_repo:
        env = config.envs.get("codex", {}).get(current_repo.lower())
        result["current_repo"] = current_repo
        result["current_codex_env"] = env
        if not env:
            notes.append(f"{current_repo} has no Codex Cloud environment: {_cloud.SETUP_DOC}")
    return result


def format_envs(report: dict[str, Any]) -> str:
    lines = []
    for row in report["environments"]:
        repo = row["repo"] or "-"
        flags = "mapped" if row["configured"] else "unmapped"
        extra = f"  {row['label']}" if row["label"] else ""
        lines.append(f"{row['provider']:<6}  {row['env_id']:<28}  repo={repo:<28}  {flags}{extra}")
        if row.get("note") and not row["configured"]:
            lines.append(f"        {row['note']}")
    if report.get("current_repo"):
        lines.append(f"this repo: {report['current_repo']} -> codex env {report.get('current_codex_env') or '(none)'}")
    lines += [f"note: {n}" for n in report["notes"]]
    return "\n".join(lines)


__all__ = ["format_envs", "list_envs", "parse_codex_list"]


def check_claude_setup(config: CloudConfig) -> dict[str, Any]:
    """Local-only check of the Claude cloud routine setup; never calls the API.

    Reports whether the config exists, whether any routine is configured, and
    for each routine whether its ``token_env`` variable is set (the value is
    never read into the report) and whether it declares ``model``. The
    repository and branch are required by the config loader, so a routine
    that loaded has both. ``problems`` is empty when ``run --where cloud
    --engine claude`` can pick a routine by ``--model``.
    """
    problems: list[str] = []
    if config.path is None or not config.path.is_file():
        problems.append(
            f"no bridge config file found: create code-bridge.toml with [cloud.claude.routines.<name>] "
            f"(see {_cloud.SETUP_DOC})"
        )
    if not config.routines:
        problems.append(
            "no routine configured: add [cloud.claude.routines.<name>] with id, token_env, repo, branch and "
            f"model, one routine per model (see {_cloud.SETUP_DOC})"
        )
    rows: list[dict[str, Any]] = []
    seen: dict[str, list[str]] = {}
    for name, entry in sorted(config.routines.items()):
        token_set = bool(os.environ.get(entry["token_env"], "").strip())
        model = entry.get("model")
        rows.append({
            "routine": name, "repo": entry["repo"], "branch": entry["branch"], "model": model,
            "token_env": entry["token_env"], "token_set": token_set, "default": name == config.default_routine,
        })
        if not token_set:
            problems.append(f"routine {name!r}: environment variable {entry['token_env']} is not set")
        if not model:
            problems.append(f"routine {name!r}: no model declared (add `model = \"...\"` so --model can select it)")
        else:
            seen.setdefault(model, []).append(name)
    for model, names in sorted(seen.items()):
        if len(names) > 1:
            problems.append(f"model {model!r} is declared by several routines ({', '.join(names)}): "
                            "--model would refuse; pass --routine to choose")
    return {"config": str(config.path) if config.path else None, "routines": rows, "problems": problems,
            "ok": not problems}


def format_claude_setup(report: dict[str, Any]) -> str:
    lines = [f"config: {report['config'] or '(none)'}"]
    for row in report["routines"]:
        lines.append(
            f"routine {row['routine']:<16} repo={row['repo']}  branch={row['branch']}  "
            f"model={row['model'] or '-'}  token {row['token_env']}: {'set' if row['token_set'] else 'MISSING'}"
            + ("  (default)" if row["default"] else "")
        )
    lines += [f"problem: {p}" for p in report["problems"]]
    lines.append("ok" if report["ok"] else f"{len(report['problems'])} problem(s)")
    return "\n".join(lines)
