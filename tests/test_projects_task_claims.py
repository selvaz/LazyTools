"""lazytools.projects.task_claims -- releasing a never-started claim, and the
honest "SENZA VERIFICA" shortcut for a task nobody claimed."""

from __future__ import annotations

from lazybridge import Store
from lazybridge.ext.planners import DurableBlackboard

from lazytools.projects import task_claims
from lazytools.projects.keys import JOB_PREFIX


def _board(store: Store, project_id: str, tasks: list[str]) -> DurableBlackboard:
    board = DurableBlackboard(store, plan_id=f"project:{project_id}")
    board.set_plan("r", tasks)
    return board


def test_release_claim_returns_claimed_task_to_todo_without_counting_attempt() -> None:
    store = Store()
    board = _board(store, "alpha", ["t0"])
    board.claim_task(0, "t0", owner="worker-a")
    ok = task_claims.release_claim(store, "project:alpha", 0, owner="worker-a", restore_attempts=0, note="setup failed before work started")
    assert ok is True
    task = board.snapshot().tasks[0]
    assert task["status"] == "todo"
    assert task["attempts"] == 0
    assert task["error"].startswith("INFRA:")


def test_release_claim_refuses_when_owner_mismatch() -> None:
    store = Store()
    board = _board(store, "alpha", ["t0"])
    board.claim_task(0, "t0", owner="worker-a")
    ok = task_claims.release_claim(store, "project:alpha", 0, owner="worker-b", restore_attempts=None, note="x")
    assert ok is False
    assert board.snapshot().tasks[0]["status"] == "claimed"


def test_release_claim_refuses_when_newer_job_exists() -> None:
    store = Store()
    board = _board(store, "alpha", ["t0"])
    board.claim_task(0, "t0", owner="worker-a")
    store.write(
        f"{JOB_PREFIX}newer-job",
        {"job_id": "newer-job", "plan_id": "project:alpha", "task_index": 0, "created_at": "2026-10-07T12:00:00+00:00"},
    )
    ok = task_claims.release_claim(
        store, "project:alpha", 0, owner="worker-a", restore_attempts=None, note="x", expected_job_id="older-job"
    )
    assert ok is False


def test_complete_todo_without_verification_closes_untouched_task() -> None:
    store = Store()
    _board(store, "alpha", ["t0"])
    msg = task_claims.complete_todo_without_verification(store, "alpha", 0, "verified in person, works")
    assert "marked done" in msg
    board = DurableBlackboard(store, plan_id="project:alpha")
    assert board.snapshot().tasks[0]["status"] == "done"


def test_complete_todo_without_verification_refuses_claimed_task() -> None:
    store = Store()
    board = _board(store, "alpha", ["t0"])
    board.claim_task(0, "t0", owner="worker-a")
    msg = task_claims.complete_todo_without_verification(store, "alpha", 0, "summary")
    assert msg.startswith("REJECTED")


def test_complete_todo_without_verification_refuses_when_contract_exists() -> None:
    store = Store()
    _board(store, "alpha", ["t0"])
    msg = task_claims.complete_todo_without_verification(store, "alpha", 0, "summary", contract_exists=True)
    assert msg.startswith("REJECTED") and "acceptance contract" in msg


def test_complete_todo_without_verification_refuses_blank_summary() -> None:
    store = Store()
    _board(store, "alpha", ["t0"])
    msg = task_claims.complete_todo_without_verification(store, "alpha", 0, "   ")
    assert msg.startswith("REJECTED")


def test_complete_todo_without_verification_refuses_running_job() -> None:
    store = Store()
    _board(store, "alpha", ["t0"])
    store.write(
        f"{JOB_PREFIX}live-job",
        {"job_id": "live-job", "plan_id": "project:alpha", "task_index": 0, "status": "running"},
    )
    msg = task_claims.complete_todo_without_verification(store, "alpha", 0, "summary")
    assert msg.startswith("REJECTED") and "running or awaiting approval" in msg
