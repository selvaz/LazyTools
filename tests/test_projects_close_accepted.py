"""Accepted verification closes only the board slot still governed by its contract."""

from __future__ import annotations

import pytest
from lazybridge import Store
from lazybridge.ext.planners import DurableBlackboard

from lazytools.projects import close_accepted_task, contracts, records, verification
from lazytools.projects.keys import BOARD_KEY_PREFIX, TASK_CONTRACT_PREFIX, VERIFICATION_PREFIX
from lazytools.projects.task_claims import CAS_ATTEMPTS

BOARD_KEY = f"{BOARD_KEY_PREFIX}project:alpha"
JOB = "finished-job-id"


def _setup(store):
    records.adopt_existing_project(store, project_id="alpha", title="Alpha", objective="files", adoption_reason="existing")
    board = DurableBlackboard(store, plan_id="project:alpha")
    board.set_plan("files", ["one", "two"])
    contract = contracts.open_task_contract(
        store, project_id="alpha", task_index=0, repo="r", observable_result="files",
        acceptance_criteria=["file exists"], required_checks=["pytest"], risk="low", allowed_effects="files",
    )
    assert verification.claim_verification(store, contract_id=contract.contract_id, job_id=JOB)
    assert verification.start_running(store, JOB)
    assert verification.record_checks(store, JOB, checks=[verification.CheckResult(
        command="pytest", exit_code=0, output_tail="passed",
    )], diff_summary="one file changed")
    assert verification.record_review(store, JOB, reviewer="independent-reviewer", findings="Checked diff; no issues")
    assert verification.accept(store, JOB, reviewer="operator", reason="Checks and independent review passed")
    return contract, board


@pytest.mark.parametrize("claimed", [False, True])
def test_matching_acceptance_closes_in_one_board_cas_with_audit_and_progress(monkeypatch, claimed):
    store = Store()
    contract, board = _setup(store)
    if claimed:
        assert board.claim_task(0, "one", owner="worker")
    raw = store.read(BOARD_KEY)
    raw["plan_events"] = [{"event": "existing"}]
    raw["tasks"][0].update(error="old error", cancel_reason="old reason", custom="preserved")
    store.write(BOARD_KEY, raw)
    cas = store.compare_and_swap
    writes, closed = [], []

    def count(key, expected, value):
        if key == BOARD_KEY:
            writes.append(key)
        return cas(key, expected, value)

    monkeypatch.setattr(store, "compare_and_swap", count)
    message = close_accepted_task(store, "alpha", 0, JOB, on_closed=lambda *args: closed.append(args))
    assert "marked done" in message
    assert len(writes) == 1 and closed == [(store, "alpha", 0)]
    doc = store.read(BOARD_KEY)
    task = doc["tasks"][0]
    assert task["status"] == "done" and task["completed_at"] > 0
    assert task["owner"] is None and task["claimed_at"] is None
    assert task["error"] == task["cancel_reason"] == ""
    assert task["custom"] == "preserved" and task["contract_id"] == contract.contract_id
    assert all(part in task["result"] for part in (JOB, "operator", "Checks and independent review passed"))
    assert doc["tasks"][1] == raw["tasks"][1]
    assert doc["plan_events"][0] == {"event": "existing"}
    event = doc["plan_events"][1]
    assert event == {
        "event": "closed_accepted", "at": task["completed_at"], "task_index": 0,
        "was": "claimed" if claimed else "todo", "contract_id": contract.contract_id,
        "verification_id": JOB, "reviewer": "operator", "reason": "Checks and independent review passed",
    }
    assert doc["updated_at"] == task["completed_at"]
    assert records.get_project(store, "alpha").last_progress_at is not None
    assert close_accepted_task(store, "alpha", 0, JOB).startswith("REJECTED:")
    assert len(closed) == 1


@pytest.mark.parametrize("status", ["pending", "running", "rework", "blocked"])
def test_unaccepted_refuses_without_board_changes(status):
    store = Store()
    _setup(store)
    key = f"{VERIFICATION_PREFIX}{JOB}"
    store.write(key, {**store.read(key), "status": status})
    before = store.read(BOARD_KEY)
    assert "not accepted" in close_accepted_task(store, "alpha", 0, JOB)
    assert store.read(BOARD_KEY) == before
    assert records.get_project(store, "alpha").last_progress_at is None


@pytest.mark.parametrize("marker", [None, "different-contract"])
def test_missing_or_mismatching_board_marker_refuses(marker):
    store = Store()
    _setup(store)
    raw = store.read(BOARD_KEY)
    raw["tasks"][0]["contract_id"] = marker
    store.write(BOARD_KEY, raw)
    result = close_accepted_task(store, "alpha", 0, JOB)
    assert result.startswith("REJECTED:")
    assert ("complete_todo_without_verification" if marker is None else "does not match") in result
    assert store.read(BOARD_KEY) == raw


@pytest.mark.parametrize("status", ["done", "cancelled", "retired", "failed"])
def test_nonopen_task_refuses(status):
    store = Store()
    _setup(store)
    raw = store.read(BOARD_KEY)
    raw["tasks"][0]["status"] = status
    store.write(BOARD_KEY, raw)
    assert "not todo or claimed" in close_accepted_task(store, "alpha", 0, JOB)
    assert store.read(BOARD_KEY) == raw


@pytest.mark.parametrize("change", [{"project_id": "other"}, {"task_index": 1}, {"contract_id": "other"}, None])
def test_contract_must_exist_and_identify_this_slot(change):
    store = Store()
    contract, _ = _setup(store)
    key = f"{TASK_CONTRACT_PREFIX}{contract.contract_id}"
    if change is None:
        store.delete(key)
    else:
        store.write(key, {**store.read(key), **change})
    before = store.read(BOARD_KEY)
    assert close_accepted_task(store, "alpha", 0, JOB).startswith("REJECTED:")
    assert store.read(BOARD_KEY) == before


@pytest.mark.parametrize("marker_change", [False, True])
def test_real_cas_conflict_retries_and_rechecks_marker(monkeypatch, marker_change):
    store = Store()
    _setup(store)
    cas = store.compare_and_swap
    writes, closed = [], []

    def race(key, expected, value):
        if key == BOARD_KEY:
            writes.append(key)
            if len(writes) == 1:
                concurrent = store.read(key)
                concurrent["concurrent"] = "preserve this"
                if marker_change:
                    concurrent["tasks"][0]["contract_id"] = "new-contract"
                store.write(key, concurrent)
        return cas(key, expected, value)

    monkeypatch.setattr(store, "compare_and_swap", race)
    result = close_accepted_task(store, "alpha", 0, JOB, on_closed=lambda *args: closed.append(args))
    doc = store.read(BOARD_KEY)
    assert doc["concurrent"] == "preserve this"
    if marker_change:
        assert result.startswith("REJECTED:") and "does not match" in result
        assert len(writes) == 1 and closed == []
        assert doc["tasks"][0]["status"] == "todo"
        assert doc["tasks"][0]["contract_id"] == "new-contract"
        assert not doc.get("plan_events")
        assert records.get_project(store, "alpha").last_progress_at is None
    else:
        assert "marked done" in result and len(writes) == 2 and len(closed) == 1
        assert len(doc["plan_events"]) == 1


def test_retry_budget_exhaustion_and_missing_inputs_refuse(monkeypatch):
    store = Store()
    _setup(store)
    assert close_accepted_task(store, "alpha", 0, "missing").startswith("REJECTED:")
    assert close_accepted_task(store, "alpha", -1, JOB).startswith("REJECTED:")
    attempts = []

    def lose(*args):
        attempts.append(args)
        return False

    monkeypatch.setattr(store, "compare_and_swap", lose)
    assert "retries" in close_accepted_task(store, "alpha", 0, JOB)
    assert len(attempts) == CAS_ATTEMPTS
    assert store.read(BOARD_KEY)["tasks"][0]["status"] == "todo"


def test_custom_prefixes():
    store = Store()
    contract, _ = _setup(store)
    for prefix, suffix in [(BOARD_KEY_PREFIX, "project:alpha"), (TASK_CONTRACT_PREFIX, contract.contract_id), (VERIFICATION_PREFIX, JOB)]:
        store.write(f"custom:{prefix}{suffix}", store.read(f"{prefix}{suffix}"))
        store.delete(f"{prefix}{suffix}")
    assert "marked done" in close_accepted_task(
        store, "alpha", 0, JOB, board_prefix=f"custom:{BOARD_KEY_PREFIX}",
        contract_prefix=f"custom:{TASK_CONTRACT_PREFIX}", verification_prefix=f"custom:{VERIFICATION_PREFIX}",
    )
    assert store.read(f"custom:{BOARD_KEY}")["tasks"][0]["status"] == "done"
