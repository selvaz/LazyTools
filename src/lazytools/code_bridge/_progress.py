"""Per-turn git evidence. Remote reads never fetch or change the workspace."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=10,
    ).stdout.strip()


def snapshot(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = {"head": None, "pushed_refs": None}
    try:
        _git(path, "rev-parse", "--is-inside-work-tree")
        # An unborn HEAD is a valid baseline, unlike a failed git probe.
        data["head"] = _git(path, "rev-parse", "--verify", "--quiet", "HEAD") or None
    except subprocess.CalledProcessError as exc:
        if exc.cmd[-1] != "HEAD" or exc.returncode != 1:
            data["head_error"] = str(exc)
    except (OSError, subprocess.TimeoutExpired) as exc:
        data["head_error"] = str(exc)
    try:
        remotes = _git(path, "remote").splitlines()
        # The routine policy writes only to origin. Read the actual remote,
        # rather than treating a fetch's remote-tracking update as a push.
        data["pushed_refs"] = dict(
            (ref, sha) for sha, ref in (line.split() for line in _git(path, "ls-remote", "--heads", "origin").splitlines())
        ) if "origin" in remotes else {}
    except (OSError, subprocess.SubprocessError) as exc:
        data["remote_error"] = str(exc)
    return data


def changes(path: Path, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    commits: list[str] = []
    error = before.get("head_error") or after.get("head_error")
    if not error and after["head"] and after["head"] != before["head"]:
        try:
            commits = _git(path, "rev-list", "--reverse", after["head"], *([f"^{before['head']}"] if before["head"] else [])).splitlines()
        except (OSError, subprocess.SubprocessError) as exc:
            error = str(exc)
    refs = []
    if before["pushed_refs"] is not None and after["pushed_refs"] is not None:
        refs = [
            {"ref": ref, "before": before["pushed_refs"].get(ref), "after": sha}
            for ref, sha in sorted(after["pushed_refs"].items())
            if sha != before["pushed_refs"].get(ref)
        ]
    return {
        "head_before": before["head"], "head_after": after["head"],
        "commits": commits, "pushed_refs": refs, "progress": bool(commits or refs),
        **({"evidence_error": error} if error else {}),
        **({"remote_error": before.get("remote_error") or after.get("remote_error")} if before.get("remote_error") or after.get("remote_error") else {}),
    }


def history_text(turns: list[dict[str, Any]]) -> str:
    lines = []
    for turn in turns:
        commits = ", ".join(turn["commits"]) or "none"
        refs = ", ".join(f"{r['ref']}={r['after']}" for r in turn["pushed_refs"]) or "none"
        lines.append(f"turn {turn['turn']} ({'initial' if turn['turn'] == 0 else 'continuation'}): {turn['outcome']}; commits: {commits}; pushed refs: {refs}")
    return "\n".join(lines)
