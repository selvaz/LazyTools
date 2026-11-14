"""Autonomy reporting for completed code-bridge jobs."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import Any

from lazytools.code_bridge import _jobs

TERMINAL = {"done", "partial", "failed", "interrupted"}


def week_bounds(value: str) -> tuple[date, date]:
    try:
        year, week = value.split("-W", 1)
        start = date.fromisocalendar(int(year), int(week), 1)
    except (ValueError, TypeError) as exc:
        raise ValueError("--week must be an ISO week such as 2026-W46") from exc
    return start, start + timedelta(days=7)


def report(store: Any, *, since: date, until: date) -> dict[str, Any]:
    """Return finished-job autonomy for the half-open UTC date range."""
    start = datetime.combine(since, datetime.min.time(), tzinfo=UTC).timestamp()
    end = datetime.combine(until, datetime.min.time(), tzinfo=UTC).timestamp()
    finished = []
    for job in _jobs.list_jobs(store, all_jobs=True):
        finished_at = job.get("completed_at") or job.get("finished_at") or job.get("updated_at") or job.get("created_at")
        if job.get("status") in TERMINAL and isinstance(finished_at, (int, float)) and start <= finished_at < end:
            finished.append(job)
    causes = Counter(
        str(item.get("kind") or "unknown")
        for job in finished
        for item in (job.get("manual_interventions") or [])
        if isinstance(item, dict)
    )
    autonomous = sum(not job.get("manual_interventions") for job in finished)
    total = len(finished)
    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "finished_jobs": total,
        "autonomous_jobs": autonomous,
        "autonomy_percent": (100.0 * autonomous / total) if total else None,
        "target_percent": 90.0,
        "target_met": (100.0 * autonomous / total >= 90.0) if total else None,
        "intervention_causes": dict(sorted(causes.items())),
    }
