"""Placement: should a job run on this machine or in the cloud?

``--where auto`` (and ``[cloud] default_where = "auto"``) sends a job to the
cloud when the owner's local machine is the scarce resource:

* the current LOCAL time is inside a production window
  (``[cloud] busy_windows = ["21:00-01:30", ...]``; empty by default, so
  nothing is ever redirected until the owner names a window), or
* no local engine slot is free (every candidate engine is at its
  machine-wide concurrency limit, see ``_concurrency``).

Otherwise the job stays local. The decision always carries the reason, so
``route --where auto`` and ``run --where auto`` can say why.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any

WHERE_AUTO = "auto"
WHERE_CHOICES = ("local", "cloud", "auto")
_WINDOW_RE = re.compile(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*")


def parse_window(text: str) -> tuple[time, time]:
    """``"21:00-01:30"`` -> ``(time(21, 0), time(1, 30))``; a window may wrap midnight."""
    match = _WINDOW_RE.fullmatch(text) if isinstance(text, str) else None
    if not match:
        raise ValueError(f"busy window {text!r} is not 'HH:MM-HH:MM' (24-hour local time, e.g. \"21:00-01:30\")")
    h1, m1, h2, m2 = (int(g) for g in match.groups())
    if not (0 <= h1 <= 23 and 0 <= h2 <= 23 and 0 <= m1 <= 59 and 0 <= m2 <= 59):
        raise ValueError(f"busy window {text!r} has an hour or minute out of range")
    start, end = time(h1, m1), time(h2, m2)
    if start == end:
        raise ValueError(f"busy window {text!r} starts and ends at the same time")
    return start, end


def window_label(text: str) -> str:
    start, end = parse_window(text)
    return f"{start:%H:%M}-{end:%H:%M}"


def inside_window(now: time, windows: tuple[str, ...] | list[str]) -> str | None:
    """The first window containing ``now`` (start inclusive, end exclusive), else ``None``."""
    for text in windows:
        start, end = parse_window(text)
        if (start <= now < end) if start < end else (now >= start or now < end):
            return window_label(text)
    return None


@dataclass(frozen=True)
class Placement:
    where: str  # "local" | "cloud"
    requested: str  # what was asked: local | cloud | auto
    reason: str
    window: str | None = None
    #: engine -> job ids holding its slots, for engines found at their limit
    busy: dict[str, list[str]] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {"where": self.where, "requested": self.requested, "reason": self.reason,
                "window": self.window, "busy_engines": self.busy}

    def line(self) -> str:
        suffix = f" (requested {self.requested})" if self.requested != self.where else ""
        return f"placement: {self.where}{suffix}: {self.reason}"


def local_now() -> datetime:
    """The current local wall-clock time. Patched in tests."""
    return datetime.now()


def decide(requested: str, *, windows: tuple[str, ...], engine: str | None = None,
           now: datetime | None = None) -> Placement:
    """Resolve ``requested`` (local | cloud | auto) to a concrete placement with its reason."""
    if requested not in WHERE_CHOICES:
        raise ValueError(f"--where must be one of {', '.join(WHERE_CHOICES)}, got {requested!r}")
    if requested != WHERE_AUTO:
        return Placement(requested, requested, f"--where {requested} was asked for explicitly")
    from lazytools.code_bridge import _concurrency, _jobs

    moment = now or local_now()
    hit = inside_window(moment.time(), windows)
    if hit is not None:
        return Placement("cloud", requested, f"local time {moment:%H:%M} is inside the busy window {hit}", window=hit)
    slot_config = _concurrency.load_config()
    candidates = [engine] if engine else list(_jobs.ENGINES)
    busy: dict[str, list[str]] = {}
    for name in candidates:
        holders = _concurrency.engine_at_limit(name, slot_config)
        if holders:
            busy[name] = [str(h.get("job_id", "?"))[:8] for h in holders]
    if busy and len(busy) == len(candidates):
        detail = "; ".join(f"{name} slots held by {', '.join(ids)}" for name, ids in busy.items())
        return Placement("cloud", requested, f"no free local engine slot ({detail})", busy=busy)
    where_window = f"; busy windows: {', '.join(window_label(w) for w in windows)}" if windows else "; no busy windows configured"
    free = [name for name in candidates if name not in busy]
    return Placement("local", requested,
                     f"local time {moment:%H:%M} is outside every busy window and a local slot is free "
                     f"({', '.join(free)}){where_window}", busy=busy)


__all__ = ["WHERE_AUTO", "WHERE_CHOICES", "Placement", "decide", "inside_window", "parse_window", "window_label"]
