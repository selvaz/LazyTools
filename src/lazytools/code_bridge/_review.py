"""Shared review evidence and rules for the code bridge."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

BLOCKING_SEVERITIES = frozenset({"P0", "P1"})
_KNOWN_SEVERITIES = BLOCKING_SEVERITIES | {"P2", "P3"}
_BADGE_RE = re.compile(r"(?i)^(?:\s|\*\*|<sub>)*(?:!\[|\[)?(P[0-3])\b")

ReviewVerdict = Literal["clean", "findings"]
IndependenceLevel = Literal["unknown", "same-model", "different-model"]


@dataclass(frozen=True)
class Finding:
    severity: str
    message: str
    file: str | None = None
    line: int | None = None


@dataclass(frozen=True)
class Review:
    reviewer_model: str | None = None
    findings: tuple[Finding, ...] = ()
    verdict: ReviewVerdict | None = None
    reviewed_sha: str | None = None
    reviewer_engine: str | None = None
    independence_level: IndependenceLevel = "unknown"


def independence_level(author_model: str, reviewer_model: str | None) -> IndependenceLevel:
    author = author_model.strip().casefold()
    reviewer = (reviewer_model or "").strip().casefold()
    if not author or not reviewer:
        return "unknown"
    return "same-model" if author == reviewer else "different-model"


def is_blocking_severity(severity: str) -> bool:
    severity = severity.upper()
    return severity in BLOCKING_SEVERITIES or severity not in _KNOWN_SEVERITIES


def inline_finding(item: dict[str, Any]) -> Finding:
    body = str(item.get("body") or "").strip()
    badge = _BADGE_RE.match(body)
    severity = badge[1].upper() if badge else "unknown"
    message = body.removeprefix(f"{badge[1]}:").strip() if badge else body
    return Finding(severity, message, file=item.get("path"), line=item.get("line"))
