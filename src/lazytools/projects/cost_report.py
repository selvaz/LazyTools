"""Read-only project and fleet cost visibility, adapted from LazyCEO.

Fleet callers supply specialist stores/paths and key prefixes. Registry,
lifecycle, budget and enforcement policy stay outside this package.
This module never writes a job record or opens an implicit database path.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
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


def cost_totals(
    records: Iterable[dict[str, Any]], *, now: datetime,
    timestamp_fields: tuple[str, ...] = ("finished_at", "created_at"),
) -> dict[str, float]:
    """UTC today and rolling seven days, using the first parseable timestamp."""
    now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    seven_day_start = now - timedelta(days=7)
    today = seven_days = 0.0
    for record in records:
        timestamp = next((parsed for field in timestamp_fields if (parsed := _parse_timestamp(record.get(field))) is not None), None)
        if timestamp is None or timestamp > now:
            continue
        cost = _cost(record.get("cost_usd"))
        if timestamp >= seven_day_start:
            seven_days += cost
        if timestamp >= today_start:
            today += cost
    return {"today_usd": today, "last_7_days_usd": seven_days}


def unmeasured_cost_counts(
    records: Iterable[dict[str, Any]], *, now: datetime,
    timestamp_fields: tuple[str, ...] = ("finished_at", "created_at"),
) -> dict[str, int]:
    """Count placeholder costs without estimating the missing spend."""
    now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    seven_day_start = now - timedelta(days=7)
    today = seven_days = 0
    for record in records:
        if not _is_unmeasured(record):
            continue
        timestamp = next((parsed for field in timestamp_fields if (parsed := _parse_timestamp(record.get(field))) is not None), None)
        if timestamp is None or timestamp > now:
            continue
        if timestamp >= seven_day_start:
            seven_days += 1
        if timestamp >= today_start:
            today += 1
    return {"today": today, "last_7_days": seven_days}


def read_store_records_read_only(path: Path, *, prefixes: tuple[str, ...]) -> dict[str, list[dict[str, Any]]]:
    """Read all requested families in one SQLite snapshot; never create a DB.

    Any schema, JSON or read error rejects the complete store. Caller paths
    are explicit; this module never discovers a registry or production path.
    """
    resolved = path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    grouped: dict[str, list[dict[str, Any]]] = {prefix: [] for prefix in prefixes}
    with sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True, timeout=1.0) as connection:
        connection.execute("BEGIN")
        for prefix in prefixes:
            rows = connection.execute("SELECT value FROM store WHERE substr(key, 1, length(?)) = ?", (prefix, prefix)).fetchall()
            for (raw,) in rows:
                value = json.loads(raw)
                if isinstance(value, dict):
                    grouped[prefix].append(value)
    return grouped


def build_fleet_cost_report(
    store: Store, *, specialist_stores: Mapping[str, Store | Path],
    job_prefix: str = JOB_PREFIX, task_prefix: str | None = None,
    primary_name: str = "CEO itself", now: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate every job across explicit stores, without project filtering.

    ``task_prefix`` optionally includes each agent's own turns (timestamp
    ``completed_at``). Specialist paths are opened read-only; callers own
    specialist enumeration and fresh Store connections. Unavailable stores
    are reported, never silently presented as complete zero-cost totals.
    """
    if task_prefix == job_prefix:
        raise ValueError("task_prefix and job_prefix must differ to avoid double counting")
    if primary_name in specialist_stores:
        raise ValueError("primary_name must differ from specialist names")
    observed_at = now or datetime.now(UTC)
    observed_at = observed_at.replace(tzinfo=UTC) if observed_at.tzinfo is None else observed_at.astimezone(UTC)
    prefixes = (job_prefix,) if task_prefix is None else (job_prefix, task_prefix)
    lines: list[dict[str, Any]] = []
    unavailable: list[dict[str, str]] = []
    unmeasured = {"today": 0, "last_7_days": 0}
    for name, source in [(primary_name, store), *specialist_stores.items()]:
        try:
            if isinstance(source, Path):
                grouped = read_store_records_read_only(source, prefixes=prefixes)
            else:
                grouped = {prefix: [raw for _key, raw in source.items(prefix=prefix) if isinstance(raw, dict)] for prefix in prefixes}
            jobs = grouped[job_prefix]
            totals = cost_totals(jobs, now=observed_at)
            if task_prefix is not None:
                turn_totals = cost_totals(grouped[task_prefix], now=observed_at, timestamp_fields=("completed_at",))
                totals = {key: value + turn_totals[key] for key, value in totals.items()}
            counts = unmeasured_cost_counts(jobs, now=observed_at)
            unmeasured = {key: value + counts[key] for key, value in unmeasured.items()}
            lines.append({"name": name, "available": True, **totals})
        except (OSError, sqlite3.Error, ValueError) as exc:
            reason = f"{type(exc).__name__}: {exc}"
            lines.append({"name": name, "available": False, "today_usd": 0.0, "last_7_days_usd": 0.0, "unavailable_reason": reason})
            unavailable.append({"name": name, "reason": reason})
    lines.sort(key=lambda line: (line["available"], line["last_7_days_usd"], line["today_usd"], line["name"]), reverse=True)
    return {
        "observed_at": observed_at.isoformat(),
        "today_since": observed_at.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
        "last_7_days_since": (observed_at - timedelta(days=7)).isoformat(),
        "today_usd": sum(line["today_usd"] for line in lines if line["available"]),
        "last_7_days_usd": sum(line["last_7_days_usd"] for line in lines if line["available"]),
        "breakdown": lines, "unavailable": unavailable, "unmeasured_cost_records": unmeasured,
    }


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


__all__ = ["build_fleet_cost_report", "cost_totals", "project_cost_report", "project_jobs", "read_store_records_read_only", "unmeasured_cost_counts"]
