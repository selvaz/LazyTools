"""Pure project schedule-health read model.

Ported verbatim (mechanism only, no CEO policy references) from
``lazyceo.project_schedule``. Task timestamps on ``DurableBlackboard`` are
UTC epoch seconds; this turns them into aware datetimes, and keeps project
deadlines independent from task due dates -- neither is inferred from the
other. UTC is the fallback when a project has no configured schedule
timezone.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lazybridge.ext.planners.durable_blackboard import BlackboardSnapshot, DurableBlackboard
from pydantic import BaseModel

from lazytools.projects.records import ProjectRecord, get_project

if TYPE_CHECKING:
    from lazybridge import Store

ScheduleState = Literal[
    "unscheduled",
    "on_track",
    "at_risk",
    "behind",
    "blocked",
    "ready_to_close",
    "done",
    "paused",
]

_OPEN_TASK_STATUSES = ("todo", "claimed")


class ProjectScheduleTask(BaseModel):
    task_index: int
    text: str
    status: str
    planned_start_at: datetime | None
    due_at: datetime | None
    completed_at: datetime | None
    owner: str | None = None
    attempts: int | None = None
    start_hold: bool = False


class ProjectScheduleStatus(BaseModel):
    project_id: str
    state: ScheduleState
    timezone: str
    target_completion_at: datetime | None
    total_tasks: int
    done_tasks: int
    failed_tasks: int
    cancelled_tasks: int
    open_tasks: int
    scheduled_today: list[ProjectScheduleTask]
    completed_today: list[ProjectScheduleTask]
    overdue: list[ProjectScheduleTask]
    next_due: ProjectScheduleTask | None


class ProjectScheduleView(BaseModel):
    """Everything about one project's schedule, from ONE read of each record.

    ``revision`` fingerprints the content the view was built from, so a
    caller paging through ``tasks``/``schedule_events`` across several calls
    can tell the plan changed in between.
    """

    revision: str
    project: ProjectRecord
    schedule_status: ProjectScheduleStatus
    tasks: list[ProjectScheduleTask]
    plan_reasoning: str
    schedule_events: list[dict[str, Any]]


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric):
        return None
    try:
        return datetime.fromtimestamp(numeric, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _project_schedule_tasks(raw_tasks: Iterable[object]) -> list[ProjectScheduleTask]:
    return [
        ProjectScheduleTask(
            task_index=index,
            text=str(task.get("text", "")),
            status=str(task.get("status", "")),
            planned_start_at=_timestamp(task.get("planned_start_at")),
            due_at=_timestamp(task.get("due_at")),
            completed_at=_timestamp(task.get("completed_at")),
            owner=task["owner"] if isinstance(task.get("owner"), str) and task["owner"].strip() else None,
            attempts=task["attempts"]
            if isinstance(task.get("attempts"), int) and not isinstance(task.get("attempts"), bool)
            else None,
            start_hold=task.get("start_hold") is True,
        )
        for index, task in enumerate(raw_tasks)
        if isinstance(task, dict)
    ]


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        return None
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError):
        return None


def _timezone(name: str | None) -> tuple[str, ZoneInfo]:
    if name is not None:
        try:
            return name, ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return "UTC", ZoneInfo("UTC")


def project_schedule_status(store: Store, project_id: str, now: datetime | None = None) -> ProjectScheduleStatus:
    """Compute current schedule health without writing or caching a rollup."""
    project, snapshot = _read_project(store, project_id)
    return _status_of(project, snapshot, now)


_READ_ATTEMPTS = 5


def _read_project(store: Store, project_id: str) -> tuple[ProjectRecord, BlackboardSnapshot]:
    """The one place that touches the store: the project, the board, the project again.

    Reads the project record on BOTH sides of the board read, so a caller
    never sees a project from before an update paired with a board from
    after it (or vice versa).
    """
    for _ in range(_READ_ATTEMPTS):
        before = get_project(store, project_id)
        if before is None:
            raise ValueError(f"project {project_id!r} is not registered")
        if before.project_id != project_id:
            raise ValueError(f"the record stored for {project_id[:48]!r} declares a different project id")
        snapshot = DurableBlackboard(store, plan_id=f"project:{project_id}").snapshot()
        after = get_project(store, project_id)
        if after == before:
            return before, snapshot
    raise RuntimeError(f"project {project_id!r} kept changing while its schedule was being read; try again")


def _status_of(project: ProjectRecord, snapshot: BlackboardSnapshot, now: datetime | None) -> ProjectScheduleStatus:
    project_id = project.project_id
    now = now or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now_utc = now.astimezone(UTC)
    timezone_name, local_timezone = _timezone(project.schedule_timezone)
    today = now_utc.astimezone(local_timezone).date()

    tasks = _project_schedule_tasks(snapshot.tasks)

    scheduled_today = [
        task for task in tasks if task.due_at is not None and task.due_at.astimezone(local_timezone).date() == today
    ]
    completed_today = [
        task
        for task in tasks
        if task.completed_at is not None and task.completed_at.astimezone(local_timezone).date() == today
    ]
    overdue = [
        task
        for task in tasks
        if task.status in _OPEN_TASK_STATUSES and task.due_at is not None and task.due_at < now_utc
    ]
    future_due = [
        task
        for task in tasks
        if task.status in _OPEN_TASK_STATUSES and task.due_at is not None and task.due_at >= now_utc
    ]
    next_due = min(future_due, key=lambda task: task.due_at or now_utc) if future_due else None

    done_tasks = sum(task.status == "done" for task in tasks)
    failed_tasks = sum(task.status == "failed" for task in tasks)
    cancelled_tasks = sum(task.status == "cancelled" for task in tasks)
    open_tasks = sum(task.status in _OPEN_TASK_STATUSES for task in tasks)
    noncancelled = [task for task in tasks if task.status != "cancelled"]
    successfully_complete = bool(tasks) and all(task.status == "done" for task in noncancelled)
    deadline = _aware_utc(project.target_completion_at)
    has_task_schedule = any(task.planned_start_at is not None or task.due_at is not None for task in tasks)

    if project.status == "paused":
        state: ScheduleState = "paused"
    elif project.status == "done":
        state = "done"
    elif failed_tasks:
        state = "blocked"
    elif successfully_complete:
        state = "ready_to_close"
    elif overdue or (deadline is not None and deadline < now_utc):
        state = "behind"
    elif not has_task_schedule and deadline is None:
        state = "unscheduled"
    else:
        risk_horizon = now_utc + timedelta(hours=24)
        near_due = any(
            task.due_at is not None
            and task.due_at >= now_utc
            and (task.due_at <= risk_horizon or task.due_at.astimezone(local_timezone).date() == today)
            for task in tasks
            if task.status in _OPEN_TASK_STATUSES
        )
        state = "at_risk" if near_due else "on_track"

    return ProjectScheduleStatus(
        project_id=project_id,
        state=state,
        timezone=timezone_name,
        target_completion_at=deadline,
        total_tasks=len(tasks),
        done_tasks=done_tasks,
        failed_tasks=failed_tasks,
        cancelled_tasks=cancelled_tasks,
        open_tasks=open_tasks,
        scheduled_today=scheduled_today,
        completed_today=completed_today,
        overdue=overdue,
        next_due=next_due,
    )


def project_schedule_view(store: Store, project_id: str, now: datetime | None = None) -> ProjectScheduleView:
    """Return the complete read-only schedule view for one registered project."""
    project, snapshot = _read_project(store, project_id)
    return ProjectScheduleView(
        revision=_revision(project, snapshot),
        project=project,
        schedule_status=_status_of(project, snapshot, now),
        tasks=_project_schedule_tasks(snapshot.tasks),
        plan_reasoning=snapshot.reasoning,
        schedule_events=[event for event in snapshot.schedule_events if isinstance(event, dict)],
    )


def _revision(project: ProjectRecord, snapshot: BlackboardSnapshot) -> str:
    document = {
        "project": project.model_dump(mode="json"),
        "reasoning": snapshot.reasoning,
        "tasks": snapshot.tasks,
        "events": snapshot.schedule_events,
    }
    canonical = json.dumps(document, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "ProjectScheduleStatus",
    "ProjectScheduleTask",
    "ProjectScheduleView",
    "ScheduleState",
    "project_schedule_status",
    "project_schedule_view",
]
