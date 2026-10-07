"""lazytools.projects.schedule / .timeline -- schedule-health read model and text Gantt."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from lazybridge import Store
from lazybridge.ext.planners import DurableBlackboard

from lazytools.projects import owner as owner_mod
from lazytools.projects import records, schedule, timeline


def _open_project_with_plan(store: Store, project_id: str, tasks: list[str]) -> None:
    records.open_project(store, project_id=project_id, title=project_id, objective="x")
    records._apply(store, project_id, {"status": "open"}, allowed_from=("draft",))
    DurableBlackboard(store, plan_id=f"project:{project_id}").set_plan("reasoning", tasks)


def test_unscheduled_project_with_no_deadline() -> None:
    store = Store()
    _open_project_with_plan(store, "alpha", ["t0", "t1"])
    status = schedule.project_schedule_status(store, "alpha")
    assert status.state == "unscheduled"
    assert status.total_tasks == 2
    assert status.open_tasks == 2


def test_behind_when_deadline_passed() -> None:
    store = Store()
    _open_project_with_plan(store, "alpha", ["t0"])
    past = datetime.now(UTC) - timedelta(days=1)
    records.set_project_deadline(store, "alpha", target_completion_at=past)
    status = schedule.project_schedule_status(store, "alpha")
    assert status.state == "behind"


def test_ready_to_close_when_all_tasks_done() -> None:
    store = Store()
    _open_project_with_plan(store, "alpha", ["t0"])
    board = DurableBlackboard(store, plan_id="project:alpha")
    board.claim_task(0, "t0")
    board.mark_done(0, "finished")
    status = schedule.project_schedule_status(store, "alpha")
    assert status.state == "ready_to_close"
    assert status.done_tasks == 1


def test_blocked_when_a_task_failed() -> None:
    store = Store()
    _open_project_with_plan(store, "alpha", ["t0"])
    # max_attempts=1 so a single mark_failed is terminal (no retry-to-todo).
    board = DurableBlackboard(store, plan_id="project:alpha", max_attempts=1)
    board.claim_task(0, "t0")
    board.mark_failed(0, "boom")
    status = schedule.project_schedule_status(store, "alpha")
    assert status.state == "blocked"
    assert status.failed_tasks == 1


def test_paused_and_done_states_are_authoritative() -> None:
    store = Store()
    _open_project_with_plan(store, "alpha", ["t0"])
    records.pause_project(store, "alpha")
    assert schedule.project_schedule_status(store, "alpha").state == "paused"

    records.resume_project(store, "alpha")
    records.close_project(store, "alpha")
    assert schedule.project_schedule_status(store, "alpha").state == "done"


def test_schedule_status_rejects_naive_now() -> None:
    store = Store()
    _open_project_with_plan(store, "alpha", ["t0"])
    import pytest

    with pytest.raises(ValueError, match="timezone-aware"):
        schedule.project_schedule_status(store, "alpha", now=datetime(2026, 1, 1))


def test_project_schedule_view_revision_changes_when_plan_changes() -> None:
    store = Store()
    _open_project_with_plan(store, "alpha", ["t0"])
    view1 = schedule.project_schedule_view(store, "alpha")
    DurableBlackboard(store, plan_id="project:alpha").add_tasks(["t1"])
    view2 = schedule.project_schedule_view(store, "alpha")
    assert view1.revision != view2.revision
    assert len(view2.tasks) == 2


def test_timeline_renders_no_open_projects() -> None:
    store = Store()
    assert timeline.render_project_timeline(store) == "no open projects."


def test_timeline_renders_progress_bar_and_blocked_reason() -> None:
    store = Store()
    _open_project_with_plan(store, "alpha", ["t0", "t1"])
    board = DurableBlackboard(store, plan_id="project:alpha")
    board.claim_task(0, "t0")
    board.mark_done(0, "done")
    rendered = timeline.render_project_timeline(store, blocked_reasons={"alpha": "waiting on review"})
    assert "alpha" in rendered
    assert "1/2" in rendered
    assert "BLOCKED" in rendered
    assert "stuck: waiting on review" in rendered


def test_timeline_owner_filter() -> None:
    store = Store()
    _open_project_with_plan(store, "alpha", ["t0"])
    _open_project_with_plan(store, "beta", ["t0"])
    owner_mod.set_project_owner(store, "alpha", "claude")
    rendered_claude = timeline.render_project_timeline(store, owner="claude")
    assert "alpha" in rendered_claude
    assert "beta" not in rendered_claude
