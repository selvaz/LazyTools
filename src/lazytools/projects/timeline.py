"""What is late and what is stuck, on one screen -- a text Gantt.

Ported from ``lazyceo.project_timeline``. ``blocked_reasons`` is supplied by
the caller (LazyCEO's own daily report computes them through its
``oversight``/``verification.claim_blocker``) rather than re-derived here, so
there is exactly one place that decides why a task cannot be claimed -- this
module just renders whatever it is told.
"""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lazybridge import Store

from lazytools.projects.records import list_projects
from lazytools.projects.schedule import ProjectScheduleStatus, project_schedule_status

_BAR_WIDTH = 12

_STATES: dict[str, tuple[str, bool]] = {
    "behind": ("LATE", True),
    "blocked": ("BLOCKED", True),
    "at_risk": ("AT RISK", True),
    "on_track": ("on track", False),
    "ready_to_close": ("ready to close", True),
    "unscheduled": ("no date", False),
    "paused": ("paused", False),
    "done": ("done", False),
}


def _bar(done: int, total: int) -> str:
    if total <= 0:
        return "-" * _BAR_WIDTH
    filled = round(_BAR_WIDTH * done / total)
    return "#" * filled + "." * (_BAR_WIDTH - filled)


def _due(status: ProjectScheduleStatus, *, now: datetime) -> str:
    target = status.target_completion_at
    if target is None:
        return "no date"
    zone: tzinfo
    try:
        zone = ZoneInfo(status.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        zone = UTC
    local_target = target.astimezone(zone)
    days = (local_target.date() - now.astimezone(zone).date()).days
    when = local_target.strftime("%d/%m")
    if days == 0:
        return "due today" if target >= now else "passed today"
    if days == 1:
        return "due tomorrow"
    if days == -1:
        return "due yesterday"
    if days > 1:
        return f"due {when} ({days}d)"
    return f"due {when} ({-days}d over)"


def render_project_timeline(
    store: Store,
    *,
    now: datetime | None = None,
    blocked_reasons: dict[str, str] | None = None,
    owner: str | None = None,
) -> str:
    """One block per open project: progress, deadline, state, what is stuck.

    ``owner`` filters the same way ``list_projects`` does -- ``None`` shows
    every owner, which is what this function did before the owner model
    existed; callers that want Claude Code's default view (``claude`` +
    ``shared``, with ``ceo`` only on request) apply that filtering choice
    themselves by calling this once per owner group, or by filtering the
    project list and rendering lines directly if a single combined report
    is wanted.
    """
    moment = now or datetime.now(UTC)
    projects = list_projects(store, status="open", owner=owner)
    if not projects:
        return "no open projects."

    lines: list[str] = []
    attention = 0
    for project in projects:
        try:
            status = project_schedule_status(store, project.project_id, moment)
        except Exception as exc:
            lines.append(f"{project.project_id}: schedule unreadable ({type(exc).__name__})")
            attention += 1
            continue

        label, notable = _STATES.get(status.state, (status.state, True))
        reason = (blocked_reasons or {}).get(project.project_id)
        if reason:
            label, notable = "BLOCKED", True
        attention += bool(notable)
        lines.append(
            f"{project.project_id:<20} {_bar(status.done_tasks, status.total_tasks)} "
            f"{status.done_tasks}/{status.total_tasks}  {_due(status, now=moment)}  {label}"
        )

        if reason:
            lines.append(f"    stuck: {reason}")
        for task in status.overdue[:3]:
            lines.append(f"    overdue: [{task.task_index}] {task.text[:60]}")
        if status.next_due is not None and not status.overdue:
            lines.append(f"    next: [{status.next_due.task_index}] {status.next_due.text[:60]}")

    header = f"{len(projects)} open project(s), {attention} needing attention"
    return "\n".join([header, "", *lines])


__all__ = ["render_project_timeline"]
