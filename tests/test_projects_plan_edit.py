"""lazytools.projects.plan_edit -- retiring, reopening, scheduling and revising tasks."""

from __future__ import annotations

from lazybridge import Store
from lazybridge.ext.planners import DurableBlackboard

from lazytools.projects import plan_edit


def _board(store: Store, project_id: str, tasks: list[str]) -> DurableBlackboard:
    board = DurableBlackboard(store, plan_id=f"project:{project_id}")
    board.set_plan("r", tasks)
    return board


def test_retire_task_marks_cancelled_with_disposition() -> None:
    store = Store()
    _board(store, "alpha", ["t0", "t1"])
    msg = plan_edit.retire_task(store, "alpha", 0, "t0", "no longer needed", "obsolete")
    assert "retired task 0" in msg
    tasks = DurableBlackboard(store, plan_id="project:alpha").snapshot().tasks
    assert tasks[0]["status"] == "cancelled"
    assert tasks[0]["cancel_disposition"] == "obsolete"


def test_retire_task_rejects_wrong_expected_text() -> None:
    store = Store()
    _board(store, "alpha", ["t0"])
    msg = plan_edit.retire_task(store, "alpha", 0, "wrong text", "reason", "obsolete")
    assert msg.startswith("REJECTED")


def test_retire_task_rejects_finished_work() -> None:
    store = Store()
    board = _board(store, "alpha", ["t0"])
    board.claim_task(0, "t0")
    board.mark_done(0, "finished")
    msg = plan_edit.retire_task(store, "alpha", 0, "t0", "reason", "obsolete")
    assert msg.startswith("REJECTED") and "done task is finished work" in msg


def test_retire_task_superseded_requires_valid_replacement() -> None:
    store = Store()
    _board(store, "alpha", ["t0", "t1"])
    bad = plan_edit.retire_task(store, "alpha", 0, "t0", "replaced", "superseded", superseded_by_task_index=None)
    assert bad.startswith("REJECTED") and "requires superseded_by_task_index" in bad
    ok = plan_edit.retire_task(store, "alpha", 0, "t0", "replaced", "superseded", superseded_by_task_index=1)
    assert "retired task 0" in ok


def test_reopen_task_requires_minimum_reason_length() -> None:
    store = Store()
    _board(store, "alpha", ["t0"])
    plan_edit.retire_task(store, "alpha", 0, "t0", "gone", "obsolete")
    short = plan_edit.reopen_task(store, "alpha", 0, "t0", "too short")
    assert short.startswith("REJECTED") and "minimum" in short


def test_reopen_task_bounded_by_max_autonomous_reopens() -> None:
    store = Store()
    _board(store, "alpha", ["t0"])
    plan_edit.retire_task(store, "alpha", 0, "t0", "gone", "obsolete")
    long_reason = "this time the approach is genuinely different and should work"
    for _ in range(plan_edit.MAX_AUTONOMOUS_REOPENS):
        msg = plan_edit.reopen_task(store, "alpha", 0, "t0", long_reason)
        assert "reopened task 0" in msg
        plan_edit.retire_task(store, "alpha", 0, "t0", "still not it", "obsolete")
    over_limit = plan_edit.reopen_task(store, "alpha", 0, "t0", long_reason)
    assert over_limit.startswith("REJECTED") and "limit" in over_limit


def test_reopen_done_task_for_invalid_closure() -> None:
    store = Store()
    board = _board(store, "alpha", ["t0"])
    board.claim_task(0, "t0", owner="worker-a")
    board.mark_done(0, "finished", owner="worker-a")
    msg = plan_edit.reopen_done_task_for_invalid_closure(
        store, "alpha", 0, "t0", owner="worker-a", reason="closing review was vacuous, confirmed from git"
    )
    assert "reopened from done to claimed" in msg
    task = DurableBlackboard(store, plan_id="project:alpha").snapshot().tasks[0]
    assert task["status"] == "claimed"
    assert task["owner"] == "worker-a"


def test_reopen_done_task_refuses_non_done() -> None:
    store = Store()
    _board(store, "alpha", ["t0"])
    msg = plan_edit.reopen_done_task_for_invalid_closure(store, "alpha", 0, "t0", owner="w", reason="a long enough reason here")
    assert msg.startswith("REJECTED") and "not done" in msg


def test_set_task_schedule_validates_ordering() -> None:
    store = Store()
    _board(store, "alpha", ["t0"])
    bad = plan_edit.set_task_schedule(store, "alpha", 0, "t0", planned_start_at=100.0, due_at=50.0, reason="x", hold=False)
    assert bad.startswith("REJECTED") and "<=" in bad

    ok = plan_edit.set_task_schedule(store, "alpha", 0, "t0", planned_start_at=50.0, due_at=100.0, reason="external event", hold=True)
    assert "scheduled task 0" in ok
    task = DurableBlackboard(store, plan_id="project:alpha").snapshot().tasks[0]
    assert task["due_at"] == 100.0
    assert task["start_hold"] is True


def test_open_indexes_excludes_keep() -> None:
    store = Store()
    _board(store, "alpha", ["t0", "t1", "t2"])
    assert plan_edit.open_indexes(store, "alpha") == [0, 1, 2]
    assert plan_edit.open_indexes(store, "alpha", keep=(1,)) == [0, 2]


def test_revise_plan_retires_and_appends_atomically() -> None:
    store = Store()
    _board(store, "alpha", ["t0", "t1"])
    message, retired, first_new = plan_edit.revise_plan(store, "alpha", "replan", ["t2", "t3"], keep=(0,))
    assert retired == [1]
    assert first_new == 2
    tasks = DurableBlackboard(store, plan_id="project:alpha").snapshot().tasks
    assert tasks[0]["status"] == "todo"  # kept
    assert tasks[1]["status"] == "cancelled"
    assert [t["text"] for t in tasks[2:]] == ["t2", "t3"]
    assert "retired 1 task(s)" in message


def test_revise_plan_rejects_blank_reason() -> None:
    store = Store()
    _board(store, "alpha", ["t0"])
    msg, retired, first_new = plan_edit.revise_plan(store, "alpha", "   ", ["t1"])
    assert msg.startswith("REJECTED")
    assert retired == [] and first_new is None


def test_reopen_budget_left_reads_either_doc_or_events() -> None:
    doc = {"plan_events": [{"event": "reopened", "task_index": 0}]}
    assert plan_edit.reopen_budget_left(doc, 0) == plan_edit.MAX_AUTONOMOUS_REOPENS - 1
    assert plan_edit.reopen_budget_left(doc["plan_events"], 0) == plan_edit.MAX_AUTONOMOUS_REOPENS - 1
    assert plan_edit.reopen_budget_left(doc, 1) == plan_edit.MAX_AUTONOMOUS_REOPENS
