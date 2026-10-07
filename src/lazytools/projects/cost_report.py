"""Read-only cost and job-status visibility, scoped to ONE project.

Adapted from ``lazyceo.cost_report``'s generic timestamp/cost helpers.
Deliberately narrower in scope than the original: ``build_fleet_cost_report``
aggregates spend across the CEO process AND every registered specialist's
OWN SQLite store, which needs the specialist registry and each specialist's
store path -- specialist lifecycle, explicitly out of scope for this package
(see ``lazyceo.project_work``'s own exclusion). What stays useful without any
of that is reading the job records a project's own delegated work already
left in the SHARED Store, filtered by ``plan_id == f"project:{project_id}"``
-- which is exactly the "job status"/"cost report" granularity the project
layer itself needs. A fleet-wide, cross-store report stays LazyCEO's to
build, over this package's ``project_cost_report``/``project_jobs`` per
project if it wants one.

This module only ever reads; it never writes a job record.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from lazytools.projects.keys import JOB_PREFIX

if TYPE_CHECKING:
    from lazybridge import Store


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _cost(value: Any) -> float:
    """A finite JSON number, or zero for missing/legacy/malformed values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    parsed = float(value)
    return parsed if math.isfinite(parsed) else 0.0


#: Same two reasons LazyCEO's own report distinguishes: a consultant call
#: (ask_codex/ask_claude/ask_designer) whose terminal record's cost_usd is
#: hardcoded to 0.0 despite real spend, or a writer job whose worker raised
#: before ever returning a result to read a real cost from.
_UNMEASURED_COST_KINDS = frozenset({"ask_codex", "ask_claude", "ask_designer"})


def _is_unmeasured(record: dict[str, Any]) -> bool:
    return record.get("kind") in _UNMEASURED_COST_KINDS or record.get("cost_unknown") is True


def _project_job_records(store: Store, project_id: str, *, job_prefix: str) -> list[dict[str, Any]]:
    plan_id = f"project:{project_id}"
    return [
        raw for _key, raw in store.items(prefix=job_prefix) if isinstance(raw, dict) and raw.get("plan_id") == plan_id
    ]


def project_jobs(store: Store, project_id: str, *, job_prefix: str = JOB_PREFIX) -> list[dict[str, Any]]:
    """Every delegated-job record attributed to ``project_id``, as stored.

    Read-only, best-effort: a job record this package never writes, so its
    exact shape (``status``, ``task_index``, ``job_id``, ``kind``, ``cost_usd``,
    ``created_at``, ``finished_at``, ...) is whatever the code-bridge / LazyCEO
    job runner wrote. Not interpreted further here -- callers wanting a
    today/7-day rollup use :func:`project_cost_report`.
    """
    return _project_job_records(store, project_id, job_prefix=job_prefix)


def project_cost_report(
    store: Store, project_id: str, *, job_prefix: str = JOB_PREFIX, now: datetime | None = None
) -> dict[str, Any]:
    """Today / rolling-7-day spend for ONE project's delegated jobs, plus a count of
    real-but-unmeasured records (see ``_is_unmeasured``).

    "Today" starts at UTC midnight. Timestamps are read from ``finished_at``,
    falling back to ``created_at`` -- the same fallback order LazyCEO's own
    fleet report uses, so a job still running (no ``finished_at`` yet) is
    still counted against the window it started in rather than vanishing.
    """
    observed_at = now or datetime.now(UTC)
    observed_at = observed_at.replace(tzinfo=UTC) if observed_at.tzinfo is None else observed_at.astimezone(UTC)
    records = _project_job_records(store, project_id, job_prefix=job_prefix)

    today_start = observed_at.replace(hour=0, minute=0, second=0, microsecond=0)
    seven_day_start = observed_at - timedelta(days=7)
    today_usd = 0.0
    seven_day_usd = 0.0
    unmeasured_today = 0
    unmeasured_seven_days = 0
    by_status: dict[str, int] = {}

    for record in records:
        status = str(record.get("status", "?"))
        by_status[status] = by_status.get(status, 0) + 1
        timestamp = _parse_timestamp(record.get("finished_at")) or _parse_timestamp(record.get("created_at"))
        if timestamp is None or timestamp > observed_at:
            continue
        unmeasured = _is_unmeasured(record)
        if timestamp >= seven_day_start:
            seven_day_usd += _cost(record.get("cost_usd"))
            if unmeasured:
                unmeasured_seven_days += 1
        if timestamp >= today_start:
            today_usd += _cost(record.get("cost_usd"))
            if unmeasured:
                unmeasured_today += 1

    return {
        "project_id": project_id,
        "observed_at": observed_at.isoformat(),
        "today_since": today_start.isoformat(),
        "last_7_days_since": seven_day_start.isoformat(),
        "today_usd": today_usd,
        "last_7_days_usd": seven_day_usd,
        "job_count": len(records),
        "jobs_by_status": by_status,
        "unmeasured_cost_records": {"today": unmeasured_today, "last_7_days": unmeasured_seven_days},
    }


__all__ = ["project_cost_report", "project_jobs"]
