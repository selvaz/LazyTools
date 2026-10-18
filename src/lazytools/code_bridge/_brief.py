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
# 'C:\dir\file' or 'C:/dir/file'; '<', '>' and '*' stay inside the match so a
# placeholder is seen as one and dropped, not truncated into a wrong path.
_WINDOWS_PATH = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/][^\s\"'`|?,;()\[\]{}]*")
# '/a/b' with at least two segments, not glued to a word ('and/or', 'origin/main').
_POSIX_PATH = re.compile(r"(?<![\w.:/\\~\-])/(?:[\w.\-@+]+/)+[\w.\-@+]*")
_PLACEHOLDER = re.compile(r"\.\.\.|\u2026|[<>*$%]")
_TRAILING = ".:!"


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


def _absolute_paths(text: str) -> list[str]:
    text = _URL.sub(" ", text)
    seen: dict[str, None] = {}
    for pattern in (_WINDOWS_PATH, _POSIX_PATH):
        for match in pattern.finditer(text):
            raw = match.group(0).rstrip(_TRAILING)
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
    for raw in _absolute_paths(text):
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
