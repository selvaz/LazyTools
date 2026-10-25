"""Configurable, audited routine-auto tier for BridgeGate."""

from __future__ import annotations

import os
import re
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from lazybridge.engines.coding import ApprovalDecision, ApprovalRequest

from lazytools.code_bridge import _concurrency, _store

_WRAPPER = re.compile(
    r'''^(?:"?C:/WINDOWS/System32/WindowsPowerShell/v1\.0/powershell\.exe"?|powershell(?:\.exe)?|pwsh(?:\.exe)?)
        (?:\s+-(?:NoProfile|NonInteractive|NoLogo))*\s+-(?:Command|c)\s+(.*)$''',
    re.IGNORECASE | re.VERBOSE,
)
_PREFIX = "$env:PYTHONPATH=(Get-Location).Path; "


def _slash(text: str) -> str:
    return re.sub(r"\\+", "/", text)


def normalise(command: str) -> tuple[str, list[str]] | None:
    """Unwrap PowerShell and remove literal quotes without hiding shell syntax."""
    from lazytools.code_bridge._approvals import _tokenize

    if not command or any(c in command for c in "\r\n"):
        return None
    body = _slash(command.strip())
    wrapper = _WRAPPER.fullmatch(body)
    if wrapper:
        body = wrapper[1].strip()
        if body[:1] in ("'", '"') and body[-1:] == body[:1]:
            body = body[1:-1]
    prefixed = body.startswith(_PREFIX)
    if prefixed:
        body = body[len(_PREFIX):]
    if body.startswith("& "):
        body = body[2:]
    # Refuse these even INSIDE quotes. A pipe is accepted only as literal
    # content of a quoted jq value, never as a shell pipeline.
    if any(c in body for c in ";&`$"):
        return None
    words = _tokenize(body)
    if not words:
        return None
    args = [word.text for word in words]
    if prefixed and not (len(args) >= 3 and args[1:3] in (["-m", "pytest"], ["-B", "-m"])):
        return None
    for i, word in enumerate(words):
        if "|" in word.text and not (word.quoted and i > 0 and args[i - 1] in ("--jq", "-q")):
            return None
        if any(c in word.text for c in "<>"):
            return None
    # Preserve argument boundaries: a quoted space is NOT a flag separator.
    # NUL cannot enter through the strict ASCII tokeniser and never reaches
    # a subprocess. Path captures decode it only after the full match.
    return " ".join(arg.replace(" ", "\0") for arg in args), args


def _inside(value: str, root: str, *, child: bool = False) -> bool:
    """Absolute paths only; reject traversal, alternate streams and symlink escapes."""
    win = bool(re.match(r"^[A-Za-z]:/", value))
    cls = PureWindowsPath if win else PurePosixPath
    path, base = cls(value), cls(_slash(root))
    if not path.is_absolute() or not base.is_absolute() or ".." in path.parts:
        return False
    if any(":" in part for part in path.parts[1:]):
        return False
    try:
        relative = path.relative_to(base)
        if child and not relative.parts:
            return False
        # On the matching host, resolve junctions and symlinks as well.
        if win == (os.name == "nt"):
            Path(value).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


@dataclass(frozen=True)
class RoutineRule:
    id: str
    pattern: re.Pattern[str]
    validator: str = ""


@dataclass(frozen=True)
class RoutinePolicy:
    enabled: bool
    scratch_root: str
    bridge_temp_root: str
    rules: tuple[RoutineRule, ...]

    def match(self, command: str) -> str | None:
        normal = normalise(command) if self.enabled else None
        if normal is None:
            return None
        body, args = normal
        for rule in self.rules:
            matched = rule.pattern.fullmatch(body)
            if matched is None:
                continue
            paths = matched.groupdict()
            if "scratch" in paths and not _inside(paths["scratch"].replace("\0", " "), self.scratch_root):
                continue
            if "bridge_temp" in paths and not _inside(paths["bridge_temp"].replace("\0", " "), self.bridge_temp_root, child=True):
                continue
            if rule.validator == "pytest":
                # Exactly one basetemp, as a separate flag and an absolute
                # child of scratch_root. No duplicate/abbreviated override.
                flags = [a for a in args if a.startswith("--base")]
                if flags != ["--basetemp"] or args[-1] == "--basetemp":
                    continue
                index = args.index("--basetemp")
                if "--" in args[:index] or any(a.startswith("@") for a in args):
                    continue
                if not _inside(args[index + 1], self.scratch_root, child=True):
                    continue
                if any(a in ("-o", "--override-ini") or a.startswith("--override-ini=") for a in args):
                    continue
            if rule.validator == "gh-read" and not _gh_read(" ".join(f"'{a}'" if " " in a or "|" in a else a for a in args)):
                continue
            return rule.id
        return None


def _gh_read(command: str) -> bool:
    from lazytools.code_bridge._approvals import _GH_READONLY, _GhShape, is_readonly_gh_query

    # Keep validation local to the routine tier, including the prototype's
    # two extra read subcommands. Reuse the existing strict flag parser.
    return is_readonly_gh_query(command, shapes={
        **_GH_READONLY,
        ("pr", "list"): _GhShape(_GH_READONLY[("run", "list")].value_flags | {"--head", "--base", "--label", "--search", "--assignee", "--author", "--state"}, frozenset({"--draft"}), 0),
        ("run", "watch"): _GhShape(frozenset({"--interval", "-i", "--repo", "-R"}), frozenset({"--exit-status", "--compact"}), 1),
    })


def load_policy(*, enabled: bool | None = None, scratch_root: str | None = None) -> RoutinePolicy:
    path = _concurrency.config_path()
    config = tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    table = config.get("routine_auto", {})
    if not isinstance(table, dict):
        raise ValueError("[routine_auto] must be a table")
    unknown = table.keys() - {"enabled", "scratch_root", "python_executable", "allowlist"}
    if unknown:
        raise ValueError(f"unknown [routine_auto] keys: {sorted(unknown)}")
    active = table.get("enabled", True) if enabled is None else enabled
    if type(active) is not bool:
        raise ValueError("[routine_auto] enabled must be boolean")
    scratch = scratch_root or table.get("scratch_root", "D:/Temp" if os.name == "nt" else tempfile.gettempdir())
    python = table.get("python_executable", sys.executable)
    for name, value in (("scratch_root", scratch), ("python_executable", python)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"[routine_auto] {name} must be a non-empty path")
    scratch = _slash(str(Path(scratch).expanduser()))
    if not (PureWindowsPath(scratch).is_absolute() or PurePosixPath(scratch).is_absolute()):
        raise ValueError("[routine_auto] scratch_root must be absolute")
    sources = [Path(__file__).with_name("routine_allowlist.toml")]
    extra = table.get("allowlist")
    if extra is not None:
        if not isinstance(extra, str) or not extra:
            raise ValueError("[routine_auto] allowlist must be a path")
        extra_path = Path(extra).expanduser()
        sources.append(extra_path if extra_path.is_absolute() else path.parent / extra_path)
    rules: list[RoutineRule] = []
    ids: set[str] = set()
    for source in sources:
        for raw in tomllib.loads(source.read_text(encoding="utf-8")).get("rules", []):
            if not isinstance(raw, dict) or set(raw) - {"id", "pattern", "validator"}:
                raise ValueError(f"{source}: invalid routine rule")
            rule_id, pattern, validator = raw.get("id"), raw.get("pattern"), raw.get("validator", "")
            if not isinstance(rule_id, str) or not rule_id or rule_id in ids:
                raise ValueError(f"{source}: missing or duplicate rule id {rule_id!r}")
            if not isinstance(pattern, str) or validator not in ("", "gh-read", "pytest"):
                raise ValueError(f"{source}: invalid pattern/validator for {rule_id}")
            try:
                compiled = re.compile(pattern.replace("{python}", re.escape(_slash(python).replace(" ", "\0"))))
            except re.error as exc:
                raise ValueError(f"{source}: invalid regex for {rule_id}: {exc}") from exc
            rules.append(RoutineRule(rule_id, compiled, validator))
            ids.add(rule_id)
    return RoutinePolicy(active, scratch, _slash(str(Path(tempfile.gettempdir()) / "lazytools-bridge")), tuple(rules))


@dataclass
class RoutineAuto:
    policy: RoutinePolicy
    store: Any
    queue: Any
    job_id: str

    def approve(self, request: ApprovalRequest, rule_id: str) -> ApprovalDecision:
        # Use the queue's CAS transitions and consume the approval before
        # adding bridge audit fields (queue model round trips omit extras).
        ticket = self.queue.create_ticket(task_id=self.job_id, prompt=f"[routine-auto:{rule_id}] {request.arguments['command']}", ttl=timedelta(minutes=5))
        if not self.queue.approve_ticket(ticket.approval_id, actor="routine-auto", channel="routine-auto"):
            return ApprovalDecision.deny("routine-auto ticket could not be approved")
        if not self.queue.mark_approval_consumed(ticket.approval_id):
            return ApprovalDecision.deny("routine-auto ticket could not be consumed")
        key = f"{_store.APPROVAL_PREFIX}{ticket.approval_id}"
        raw = self.store.read(key)
        self.store.write(key, {**raw, "approved_by": "routine-auto", "matched_rule_id": rule_id,
                               "command": request.arguments["command"], "cwd": request.cwd})
        return ApprovalDecision.allow()
