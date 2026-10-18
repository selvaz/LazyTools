"""lazytools.projects.task_claims -- releasing a never-started claim, and the
honest "SENZA VERIFICA" shortcut for a task nobody claimed."""

from __future__ import annotations

import pytest
from lazybridge import Store
from lazybridge.ext.planners import DurableBlackboard

from lazytools.projects import contracts, task_claims
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


def _open_contract(store: Store, task_index: int = 0) -> None:
    contracts.open_task_contract(
        store,
        project_id="alpha",
        task_index=task_index,
        repo="r",
        observable_result="files",
        acceptance_criteria=["file exists"],
        required_checks=["pytest"],
        risk="low",
        allowed_effects="files",
    )


def test_complete_todo_without_verification_refuses_when_contract_exists() -> None:
    store = Store()
    board = _board(store, "alpha", ["t0"])
    _open_contract(store)
    # The function looks the contract up itself; the caller passes nothing.
    msg = task_claims.complete_todo_without_verification(store, "alpha", 0, "summary")
    assert msg.startswith("REJECTED") and "acceptance contract" in msg and "accept()" in msg
    assert board.snapshot().tasks[0]["status"] == "todo"


def test_caller_cannot_claim_there_is_no_contract() -> None:
    # The old ``contract_exists`` flag (defaulting to False) is gone: there is no
    # keyword through which a caller can assert "no contract".
    store = Store()
    board = _board(store, "alpha", ["t0"])
    _open_contract(store)
    with pytest.raises(TypeError):
        task_claims.complete_todo_without_verification(store, "alpha", 0, "summary", contract_exists=False)  # type: ignore[call-arg]
    # A permissive caller hook cannot override the built-in gate either.
    msg = task_claims.complete_todo_without_verification(store, "alpha", 0, "summary", check=lambda *_: None)
    assert msg.startswith("REJECTED") and "acceptance contract" in msg
    assert board.snapshot().tasks[0]["status"] == "todo"


def test_contract_opened_between_read_and_cas_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store()
    board = _board(store, "alpha", ["t0"])
    closed: list[tuple] = []
    real_read = store.read
    reads = {"n": 0}

    def read_then_open_contract(key, *args, **kwargs):
        value = real_read(key, *args, **kwargs)
        if key == "blackboard:project:alpha":
            reads["n"] += 1
            if reads["n"] == 1:
                # The board was just read as an untouched todo; a contract appears now.
                _open_contract(store)
        return value

    monkeypatch.setattr(store, "read", read_then_open_contract)
    msg = task_claims.complete_todo_without_verification(store, "alpha", 0, "summary", on_closed=lambda *a: closed.append(a))
    assert msg.startswith("REJECTED") and "acceptance contract" in msg
    assert closed == []
    assert board.snapshot().tasks[0]["status"] == "todo"


def test_contract_opened_while_cas_loses_is_caught_on_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store()
    board = _board(store, "alpha", ["t0"])
    cas = store.compare_and_swap
    attempts = {"n": 0}

    def lose_once_while_contract_opens(key, expected, value):
        if key != "blackboard:project:alpha":
            return cas(key, expected, value)  # e.g. the contract write itself
        attempts["n"] += 1
        if attempts["n"] == 1:
            _open_contract(store)
            return False
        return cas(key, expected, value)

    monkeypatch.setattr(store, "compare_and_swap", lose_once_while_contract_opens)
    msg = task_claims.complete_todo_without_verification(store, "alpha", 0, "summary")
    assert msg.startswith("REJECTED") and "acceptance contract" in msg
    assert attempts["n"] == 1  # refused before a second CAS was even attempted
    assert board.snapshot().tasks[0]["status"] == "todo"


def test_contract_for_another_task_does_not_block_closure() -> None:
    store = Store()
    board = _board(store, "alpha", ["t0", "t1"])
    _open_contract(store, task_index=1)
    assert "marked done" in task_claims.complete_todo_without_verification(store, "alpha", 0, "verified")
    assert board.snapshot().tasks[0]["status"] == "done"
    assert task_claims.complete_todo_without_verification(store, "alpha", 1, "verified").startswith("REJECTED")
    assert board.snapshot().tasks[1]["status"] == "todo"


def test_contract_gate_refusal_helper() -> None:
    store = Store()
    _board(store, "alpha", ["t0"])
    assert task_claims.contract_gate_refusal(store, "alpha", 0) is None
    _open_contract(store)
    refusal = task_claims.contract_gate_refusal(store, "alpha", 0)
    assert refusal is not None and refusal.startswith("REJECTED") and "accept()" in refusal


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
