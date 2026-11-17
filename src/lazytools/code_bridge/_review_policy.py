"""Fail-closed exceptions for the local ``review`` approval profile."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from lazytools.code_bridge._readonly import sensitive_name

_GIT_DENY = {
    "push", "commit", "merge", "rebase", "cherry-pick", "revert", "pull",
    "checkout", "switch", "worktree", "replace", "filter-branch", "update-ref",
}
_PUBLISH = {("npm", "publish"), ("pnpm", "publish"), ("yarn", "publish"),
            ("cargo", "publish"), ("dotnet", "nuget"), ("twine", "upload"),
            ("gem", "push"), ("docker", "push"), ("podman", "push")}
_SCHEDULE = {"crontab", "at", "schtasks", "launchctl"}
_SERVICE = {"systemctl", "service", "sc", "net"}
_WRITE_COMMANDS = {"touch", "mkdir", "install", "cp", "mv", "tee", "truncate"}
_SHELLS = {"sh", "bash", "zsh", "dash", "cmd", "cmd.exe"}


def _words(text: str) -> list[str] | None:
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        return None


def _path_denied(value: str, cwd: Path, *, removal: bool = False) -> bool:
    if value.startswith("-") or value in {".", "./"}:
        return False
    raw = Path(value).expanduser()
    try:
        target = (cwd / raw).resolve() if not raw.is_absolute() else raw.resolve()
        target.relative_to(cwd.resolve())
    except (OSError, RuntimeError, ValueError):
        return True
    return any(sensitive_name(part) for part in target.parts) or (removal and target == cwd.resolve())


def _git_subcommand(words: list[str], start: int) -> tuple[str | None, list[str]]:
    """Skip git config overrides/global options without letting them hide a verb."""
    i = start + 1
    value_options = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}
    while i < len(words):
        item = words[i]
        flag = item.split("=", 1)[0]
        if flag in value_options:
            i += 1 if "=" in item else 2
        elif item.startswith("-"):
            i += 1
        else:
            return item.lower(), words[i + 1:]
    return None, []


def _exact_review_comment(words: list[str]) -> bool:
    return (
        len(words) == 8 and [word.lower() for word in words[:3]] == ["gh", "pr", "comment"]
        and words[3].isdigit() and words[4] == "--repo"
        and bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", words[5]))
        and words[6] == "--body-file" and bool(words[7])
    )


def review_denial(command: str, cwd: str) -> str | None:
    """Return the held capability, or ``None`` when review may auto-approve."""
    words = _words(command)
    if not words:
        return "unparseable command"
    root = Path(cwd)
    lower = [word.lower() for word in words]

    # Leading operators and nested command interpreters must not hide a denied verb.
    if re.match(r"\s*(?:[;&|]|&&|\|\|)", command):
        return "leading shell separator"
    for i, word in enumerate(lower):
        if word in _SHELLS and i + 2 < len(words) and lower[i + 1] in {"-c", "/c"}:
            nested = review_denial(words[i + 2], cwd)
            if nested:
                return nested
        if word in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
            try:
                index = next(j for j in range(i + 1, len(words)) if lower[j] in {"-command", "-c"})
            except StopIteration:
                continue
            nested = review_denial(" ".join(words[index + 1:]), cwd)
            if nested:
                return nested

    for i, word in enumerate(lower):
        if word == "git":
            verb, args = _git_subcommand(words, i)
            if verb in _GIT_DENY:
                return f"git {verb}"
            if verb == "reset" and "--hard" in args:
                return "git reset --hard"
            if verb == "branch" and any(a in {"-d", "-D", "--delete"} for a in args):
                return "git branch deletion"
        if word == "gh" and i + 1 < len(words):
            tail, low_tail = words[i:], lower[i:]
            if low_tail[:3] == ["gh", "pr", "comment"] and _exact_review_comment(tail):
                continue
            if low_tail[:3] == ["gh", "pr", "comment"]:
                return "GitHub PR comment outside the one-comment exception"
            if len(low_tail) > 2 and low_tail[1] == "pr" and low_tail[2] in {
                "merge", "close", "edit", "review", "ready", "reopen"
            }:
                return "GitHub PR state change"
            if len(low_tail) > 1 and low_tail[1] == "api":
                api_args = low_tail[2:]
                for j, arg in enumerate(api_args):
                    if arg in {"-f", "--field", "--raw-field", "--input"} or arg.startswith(("--field=", "--raw-field=", "--input=")):
                        return "gh api write input"
                    if arg in {"-x", "--method"} and (j + 1 == len(api_args) or api_args[j + 1] != "get"):
                        return "gh api non-GET method"
                    if arg.startswith("--method=") and arg.split("=", 1)[1] != "get":
                        return "gh api non-GET method"
            if len(low_tail) > 2 and low_tail[1] == "release" and low_tail[2] != "view":
                return "publishing to a remote"
        if tuple(lower[i:i + 2]) in _PUBLISH:
            return "package publishing"
        if word in _SCHEDULE:
            return "scheduled task control"
        if word in _SERVICE and not (word == "net" and lower[i:i + 2] not in (["net", "start"], ["net", "stop"])):
            return "service control"
        if word in {"sudo", "doas"}:
            return "sandbox escalation"
        if word in {"rm", "rmdir", "del", "erase", "remove-item"}:
            for operand in words[i + 1:]:
                if _path_denied(operand, root, removal=True):
                    return "removal outside workspace or credential path"
        if word in _WRITE_COMMANDS:
            for operand in words[i + 1:]:
                if _path_denied(operand, root):
                    return "write outside workspace or credential path"

    # Shell output redirects are writes too.
    for target in re.findall(r"(?<![0-9])>{1,2}\s*([^\s;&|]+)", command):
        if target != "/dev/null" and _path_denied(target.strip("'\""), root):
            return "redirect outside workspace or credential path"
    return None


__all__ = ["review_denial"]
