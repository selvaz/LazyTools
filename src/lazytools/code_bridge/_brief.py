"""Pre-launch check of a delegation brief.

A pure function, :func:`check_brief`, that looks at the task text of a
``run`` and reports the shapes known to cause rework: a missing section, a
path the engine cannot read, a quoting pattern that raises an approval ticket,
a persona line. It reads no file and touches no filesystem: paths are compared
lexically, and a path need not exist to be judged.

The rules are the bridge-relevant part of the ecosystem's delegation-brief
reference; ``docs/code-bridge.md`` ("Brief check") lists every code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Severity = Literal["error", "warn"]

#: A brief shorter than this cannot carry the context an engine needs.
MIN_BRIEF_CHARS = 200

#: Interpreter installs that every job may legitimately name.
_IGNORED_PATH_PREFIXES = ("c:/programdata/spyder-6",)


#: Every code :func:`check_brief` can emit; ``docs/code-bridge.md`` must list each.
CODES = (
    "missing-goal",
    "missing-done-when",
    "missing-context",
    "missing-constraints",
    "missing-report",
    "path-outside-cwd",
    "quoting-jq",
    "quoting-commit-message",
    "persona-line",
    "step-by-step",
    "brief-too-short",
)


@dataclass(frozen=True)
class Finding:
    code: str
    severity: Severity
    message: str

    def format(self) -> str:
        return f"{self.severity} {self.code}: {self.message}"


# --------------------------------------------------------------------------- #
# sections
# --------------------------------------------------------------------------- #

#: (code, label, severity, heading alternatives -- English first, then Italian)
_SECTIONS: tuple[tuple[str, str, Severity, str], ...] = (
    ("missing-goal", "Goal", "error", r"goal|obiettivo"),
    ("missing-context", "Context", "warn", r"context|contesto"),
    ("missing-constraints", "Constraints", "warn", r"constraints|vincoli"),
    ("missing-done-when", "Done when", "error", r"done\s+when|fatto\s+quando"),
    ("missing-report", "Report", "warn", r"report|rapporto"),
)

_FENCE = re.compile(r"^\s{0,3}(```|~~~)")


def _prose_lines(text: str) -> list[str]:
    """Lines outside fenced code blocks (a template quoted in a fence is not a heading)."""
    lines: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        marker = _FENCE.match(line)
        if marker:
            if fence is None:
                fence = marker.group(1)
            elif marker.group(1) == fence:
                fence = None
            continue
        if fence is None:
            lines.append(line)
    return lines


def _heading_pattern(names: str) -> re.Pattern[str]:
    # '## Done when', '## Done when:', '**Done when**', 'Done when:' -- the
    # whole line is the section name, optionally marked up and followed by a colon.
    return re.compile(rf"^\s{{0,3}}(?:#{{1,6}}\s*)?(?:\*\*|__)?(?:{names})\s*:?\s*(?:\*\*|__)?\s*:?\s*$", re.IGNORECASE)


_HEADINGS = {code: _heading_pattern(names) for code, _label, _sev, names in _SECTIONS}


def _check_sections(text: str) -> list[Finding]:
    lines = _prose_lines(text)
    found: list[Finding] = []
    for code, label, severity, _names in _SECTIONS:
        if not any(_HEADINGS[code].match(line) for line in lines):
            found.append(Finding(code, severity, f"no '{label}' section heading"))
    return found


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #

_URL = re.compile(r"\b[a-z][a-z0-9+.\-]*://\S+", re.IGNORECASE)
# Where an absolute path starts: 'C:\' or 'C:/', or '/a/b' with at least two
# segments that is not glued to a word ('and/or', 'origin/main').
_PATH_START = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]|(?<![\w.:/\\~\-])/(?=[\w.\-@+]+/)")
# An unquoted path token stops at whitespace and at these characters.
_TOKEN = re.compile(r"[^\s\"'`|?,;()\[\]{}]*")
# One more word that continues a path across a space: 'repo copy/file.py'. It
# must carry a separator, so plain prose after a path never joins it.
_CONTINUATION = re.compile(r" (?![-\\/]|[A-Za-z]:[\\/])[^\s\"'`|?,;()\[\]{}\\/]+[\\/][^\s\"'`|?,;()\[\]{}]*")
_QUOTES = "\"'`"
_PLACEHOLDER = re.compile(r"\.\.\.|\u2026|[<>*$%]")
_TRAILING = ".:!"
_MAX_CONTINUATIONS = 5
# What may follow the cwd for the cwd to be a whole path (not 'repo-copy' or 'repo.bak').
_BOUNDARY = r"(?=[\\/]|\s|$|[`'\")\],;:!?]|\.(?:\s|$))"


def _split(path: str) -> tuple[str, ...]:
    """Lexical, case-folded path parts; ``C:\\a\\b`` and ``/c/a/b`` agree. Never touches the disk."""
    p = path.replace("\\", "/")
    drive = ""
    if re.match(r"^[A-Za-z]:(/|$)", p):
        drive, p = p[:2].casefold(), p[2:]
    elif re.match(r"^/[A-Za-z](/|$)", p):  # Git Bash / WSL-style '/c/Users/...'
        drive, p = p[1].casefold() + ":", p[2:]
    parts: list[str] = []
    for part in p.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part.casefold() if drive else part)
    return (drive or "/", *parts)


def _inside(path: tuple[str, ...], base: tuple[str, ...]) -> bool:
    return path[: len(base)] == base


def _cwd_pattern(cwd: str | Path) -> re.Pattern[str] | None:
    """The cwd as it would be written in a brief, spaces and either slash included."""
    text = str(cwd).rstrip("\\/")
    parts = [re.escape(part) for part in re.split(r"[\\/]+", text) if part]
    if not parts:
        return None
    lead = r"[\\/]+" if text.startswith(("/", "\\")) else ""
    flags = re.IGNORECASE if re.match(r"[A-Za-z]:", text) else 0  # drive paths are case-insensitive
    return re.compile(lead + r"[\\/]+".join(parts) + _BOUNDARY, flags)


def _extent(line: str, start: int, cwd_re: re.Pattern[str] | None) -> str:
    """The path that begins at ``line[start]``.

    A path is cut in three ways, most exact first:

    * quoted (``"..."``, ``'...'``, backticks): everything up to the closing quote;
    * it begins with ``--cwd`` itself: the cwd text is taken whole, so spaces in
      the cwd cannot truncate it, and a following word that carries a separator
      (``repo copy/file.py``) is a sibling directory, not the repo;
    * otherwise a token, joined across a space only to a next word that carries a
      separator (``C:/Program Files/x``).

    Anything else with a space must be quoted. Cutting a path short never turns an
    outside path into an inside one, except a sibling of the cwd whose own name has
    several words.
    """
    if start > 0 and line[start - 1] in _QUOTES:
        closing = line.find(line[start - 1], start)
        if closing > start:
            return line[start:closing]
    end = start
    cwd_match = cwd_re.match(line, start) if cwd_re else None
    if cwd_match:
        end = cwd_match.end()
        if line[end : end + 1] not in ("\\", "/"):
            sibling = _CONTINUATION.match(line, end)
            if sibling is None:
                return line[start:end]
    token = _TOKEN.match(line, end)
    if token is not None:
        end = token.end()
    for _ in range(_MAX_CONTINUATIONS):
        if line[start:end].endswith(("/", "\\", *_TRAILING)):
            break
        more = _CONTINUATION.match(line, end)
        if more is None:
            break
        end = more.end()
    return line[start:end]


def _absolute_paths(text: str, cwd_re: re.Pattern[str] | None = None) -> list[str]:
    text = _URL.sub(" ", text)
    seen: dict[str, None] = {}
    for line in text.splitlines():
        for match in _PATH_START.finditer(line):
            raw = _extent(line, match.start(), cwd_re).rstrip(_TRAILING)
            if _PLACEHOLDER.search(raw):
                continue
            seen.setdefault(raw)
    return list(seen)


def _check_paths(text: str, *, engine: str, cwd: str | Path) -> list[Finding]:
    # Claude jobs read only their own repo. Codex jobs read the whole root, so
    # there is nothing to say about a path in a Codex brief.
    if engine != "claude":
        return []
    base = _split(str(cwd))
    found: list[Finding] = []
    for raw in _absolute_paths(text, _cwd_pattern(cwd)):
        parts = _split(raw)
        joined = "/".join(parts)
        if any(joined.startswith(prefix) for prefix in _IGNORED_PATH_PREFIXES):
            continue
        if not _inside(parts, base):
            found.append(
                Finding(
                    "path-outside-cwd",
                    "error",
                    f"{raw} is outside --cwd {cwd}; a Claude job can read only its own repo "
                    "(copy what it needs into the repo or into the brief)",
                )
            )
    return found


# --------------------------------------------------------------------------- #
# quoting
# --------------------------------------------------------------------------- #

_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_COMMAND_LINE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)?(?:[$>]\s*)?(?:gh|git)\s", re.IGNORECASE)
_JQ_QUOTED = re.compile(r"\bgh\b.*--jq\s+[\"']")
_MESSAGE_QUOTED = re.compile(r"\bgit\b.*\s-m\s+[\"']")


def _command_regions(text: str) -> list[tuple[int, str]]:
    """(line number, text) of every place the brief shows a command to run."""
    regions: list[tuple[int, str]] = []
    fence: str | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        marker = _FENCE.match(line)
        if marker:
            fence = marker.group(1) if fence is None else (None if marker.group(1) == fence else fence)
            continue
        if fence is not None or _COMMAND_LINE.match(line):
            regions.append((number, line))
            continue
        # A code span counts only when it is itself a command ('gh pr list --jq ...'),
        # not a bare flag quoted in prose ("without `--jq '<filter>'`").
        regions.extend((number, span.group(1)) for span in _CODE_SPAN.finditer(line))
    return regions


def _check_quoting(text: str) -> list[Finding]:
    found: list[Finding] = []
    reported: set[str] = set()
    for number, region in _command_regions(text):
        if "quoting-jq" not in reported and _JQ_QUOTED.search(region):
            reported.add("quoting-jq")
            found.append(
                Finding(
                    "quoting-jq",
                    "warn",
                    f"line {number} runs `gh ... --jq '<filter>'`: a quoted argument raises an approval "
                    "ticket; ask for `gh ... --json <fields>` and filter the output instead",
                )
            )
        if "quoting-commit-message" not in reported and _MESSAGE_QUOTED.search(region):
            reported.add("quoting-commit-message")
            found.append(
                Finding(
                    "quoting-commit-message",
                    "warn",
                    f'line {number} runs `git ... -m "<text>"`: a quoted argument raises an approval '
                    "ticket; write the message to a file and use `git commit -F <file>`",
                )
            )
    return found


# --------------------------------------------------------------------------- #
# style
# --------------------------------------------------------------------------- #

_PERSONA = re.compile(r"^\s*you are (?:a|an) ", re.IGNORECASE)
_STEP_BY_STEP = re.compile(r"think\s+(?:through\s+)?step[\s-]by[\s-]step", re.IGNORECASE)


def _check_style(text: str) -> list[Finding]:
    found: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if _PERSONA.match(line):
            found.append(Finding("persona-line", "warn", f"line {number} sets a persona; state the task, not a role"))
            break
    if _STEP_BY_STEP.search(text):
        found.append(
            Finding("step-by-step", "warn", "'think step by step' adds nothing for a reasoning model; remove it")
        )
    return found


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def check_brief(text: str, *, engine: str, cwd: str | Path, root: str | Path | None = None) -> list[Finding]:
    """Findings for ``text``, a brief about to be sent to ``engine`` working in ``cwd``.

    ``root`` is the confinement root. No current check needs it (a Codex job
    reads the whole root, so a path in a Codex brief is never a finding); it is
    part of the signature so callers pass the full launch context.
    """
    findings: list[Finding] = []
    if len(text.strip()) < MIN_BRIEF_CHARS:
        findings.append(Finding("brief-too-short", "warn", "brief too short to carry context"))
    findings += _check_sections(text)
    findings += _check_paths(text, engine=engine, cwd=cwd)
    findings += _check_quoting(text)
    findings += _check_style(text)
    return findings


__all__ = ["CODES", "MIN_BRIEF_CHARS", "Finding", "check_brief"]
