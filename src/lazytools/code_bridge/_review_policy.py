"""Fail-closed, tokenized command allow-list for the ``review`` profile."""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping

from lazytools.code_bridge._readonly import sensitive_name

_SEPARATORS = {";", "&&", "||", "|"}
_PUNCTUATION = frozenset(";&|<>(){}")


def _tokens(command: str) -> list[str] | None:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|(){}<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        # A review command can target Windows even when the bridge runs on a
        # POSIX host.  Preserve backslashes so _operand can normalize and
        # inspect Windows paths instead of silently dropping their separators.
        lexer.escape = ""
        result = list(lexer)
    except ValueError:
        return None
    # Expansion and multiline shell programs are intentionally outside the grammar.
    if not result or any(c in command for c in ("$", "`", "\x00", "\n", "\r")):
        return None
    return result


def _operand(word: str) -> bool:
    raw_normalized = word.replace("\\", "/")
    if raw_normalized.startswith(("/", "~/")) or any(part == ".." for part in raw_normalized.split("/")):
        return False
    normalized = raw_normalized.replace(":", "/").rstrip("/")
    parts = [part for part in normalized.split("/") if part]
    lower_parts = [part.lower() for part in parts]
    sensitive = (
        any(sensitive_name(part) for part in parts)
        or any(part.endswith(".env") for part in lower_parts)
        or any(part in {".config", "credentials"} for part in lower_parts)
        or (len(lower_parts) >= 3 and lower_parts[0] == "proc" and lower_parts[-1] == "environ")
    )
    return not sensitive and not any(c in word for c in "*?[]{}")


def _args(words: list[str], flags: set[str], valued: set[str] | None = None) -> bool:
    valued = valued or set()
    i = 0
    while i < len(words):
        word = words[i]
        if not _operand(word):
            return False
        if word == "--":
            return all(_operand(v) for v in words[i + 1 :])
        if word.startswith("-") and word != "-":
            if re.fullmatch(r"-\d+", word) and "-n" in flags:
                i += 1
                continue
            name = word.split("=", 1)[0]
            if name not in flags:
                return False
            if name in valued and "=" not in word:
                i += 1
                if i >= len(words) or not _operand(words[i]):
                    return False
        i += 1
    return True


_GIT = {
    "status": (
        {
            "--short",
            "-s",
            "--porcelain",
            "--branch",
            "-b",
            "--show-stash",
            "--ahead-behind",
            "--untracked-files",
            "-uno",
        },
        set(),
    ),
    "log": (
        {
            "--oneline",
            "--decorate",
            "--stat",
            "--patch",
            "-p",
            "--all",
            "--graph",
            "--no-merges",
            "--name-only",
            "--name-status",
            "--reverse",
            "-n",
            "--max-count",
            "--since",
            "--until",
            "--format",
            "--pretty",
        },
        {"-n", "--max-count", "--since", "--until", "--format", "--pretty"},
    ),
    "diff": (
        {
            "--check",
            "--stat",
            "--name-only",
            "--name-status",
            "--cached",
            "--staged",
            "--color",
            "--no-color",
            "-w",
            "--word-diff",
            "-U",
        },
        {"-U"},
    ),
    "show": (
        {"--stat", "--oneline", "--name-only", "--name-status", "--format", "--pretty", "--color", "--no-color", "-s"},
        {"--format", "--pretty"},
    ),
    "rev-parse": (
        {
            "--verify",
            "--short",
            "--show-toplevel",
            "--show-prefix",
            "--show-cdup",
            "--is-inside-work-tree",
            "--abbrev-ref",
            "--symbolic-full-name",
        },
        {"--short"},
    ),
}


def _git(words: list[str]) -> bool:
    if len(words) < 2 or words[1].startswith("-"):
        return False
    verb, rest = words[1].lower(), words[2:]
    if verb in _GIT:
        return _args(rest, *_GIT[verb])
    if verb == "fetch":
        # Fetch writes refs and objects.  Auto-approve only the plain remote and
        # optional branch form; every mutating/expanding option is held.
        return len(rest) <= 2 and all(not word.startswith("-") and ":" not in word and _operand(word) for word in rest)
    return False


_GH = {
    "view": ({"--comments", "--json", "--jq", "--template", "--web", "-w"}, {"--json", "--jq", "--template"}),
    "diff": ({"--color", "--name-only", "--patch", "--web", "-w"}, {"--color"}),
    "checks": (
        {"--fail-fast", "--interval", "--json", "--jq", "--template", "--watch", "--web", "-w"},
        {"--interval", "--json", "--jq", "--template"},
    ),
}


def _gh(words: list[str]) -> bool:
    if len(words) < 3 or words[1].lower() != "pr" or words[2].lower() not in _GH:
        return False
    flags, valued = _GH[words[2].lower()]
    return _args(words[3:], flags | {"--repo", "-R"}, valued | {"--repo", "-R"})


def _allowed(words: list[str]) -> bool:
    if not words or not all(_operand(w) for w in words):
        return False
    exe = words[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in (".exe", ".cmd"):
        if exe.endswith(suffix):
            exe = exe[: -len(suffix)]
            break
    if exe == "git":
        return _git(words)
    if exe == "gh":
        return _gh(words)
    if exe in {"pytest", "py.test"}:
        return _args(
            words[1:],
            {"-q", "-v", "-x", "-s", "--disable-warnings", "--tb", "-k", "-m", "--maxfail"},
            {"--tb", "-k", "-m", "--maxfail"},
        )
    if exe in {"python", "python3"}:
        return len(words) >= 3 and words[1:3] == ["-m", "pytest"] and _allowed(["pytest", *words[3:]])
    if exe == "npm":
        return words[1:] == ["test"] or words[1:] in (["run", "build"], ["run", "lint"])
    flags = {
        "ls": {"-a", "-A", "-l", "-h", "-la", "-al", "-lh", "-lah", "--all", "--long", "--human-readable"},
        "cat": {"-n", "-b", "-s", "-v", "-E", "-T", "--number"},
        # Recursive grep can traverse unnamed credential files, so it is not
        # part of this narrow allow-list.
        "grep": {"-n", "-i", "-v", "-E", "-F", "-w", "-x", "-l", "-c", "-q", "--line-number", "--ignore-case"},
        "head": {"-n", "-c", "-q", "-v", "--lines", "--bytes"},
        "tail": {"-n", "-c", "-q", "-v", "--lines", "--bytes"},
    }
    valued = {"head": {"-n", "-c", "--lines", "--bytes"}, "tail": {"-n", "-c", "--lines", "--bytes"}}
    return exe in flags and _args(words[1:], flags[exe], valued.get(exe))


def review_denial(
    command: str, cwd: str, execution_cwd: str | None = None, scratch_dir: str | None = None
) -> str | None:
    """Return ``None`` only if every parsed command segment is allow-listed."""
    del execution_cwd, scratch_dir
    if not cwd:
        return "unknown workspace"
    tokens = _tokens(command)
    if not tokens:
        return "unparseable command"
    segments: list[list[str]] = [[]]
    for token in tokens:
        # shlex groups adjacent punctuation (for example ``|&`` and ``&>>``).
        # Anything except the four explicitly supported separators is held.
        if token and all(char in _PUNCTUATION for char in token) and token not in _SEPARATORS:
            return "unsupported shell syntax"
        if token in _SEPARATORS:
            if not segments[-1]:
                return "ambiguous shell syntax"
            segments.append([])
        else:
            segments[-1].append(token)
    if not segments[-1] or any(not _allowed(s) for s in segments):
        return "command is not in the review read-only allow-list"
    return None


def review_tool_denial(name: str, arguments: Mapping[str, object]) -> str | None:
    """Hold native read tools when an explicit operand names secret material."""
    if name not in {"Read", "Glob", "Grep"}:
        return None
    for key, value in arguments.items():
        if key in {"path", "file_path", "pattern", "glob"} and isinstance(value, str) and not _operand(value):
            return "sensitive or unbounded native-tool path"
    return None


__all__ = ["review_denial", "review_tool_denial"]
