"""Accepted verification closes only the board slot still governed by its contract."""

from __future__ import annotations

from datetime import timedelta

import pytest
from lazybridge import Store
from lazybridge.ext.planners import DurableBlackboard

from lazytools.projects import close_accepted_task, contract_markers, contracts, plan_edit, records, verification
from lazytools.projects.keys import BOARD_KEY_PREFIX, PROJECT_PREFIX, TASK_CONTRACT_PREFIX, VERIFICATION_PREFIX
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
    assert ("backfill_contract_markers(..., apply=True)" if marker is None else "does not match") in result
    assert store.read(BOARD_KEY) == raw
    if marker is None:
        # Follow the refusal's advice for a legacy contract, rather than being
        # bounced into the shortcut closer (which correctly refuses contracts).
        assert contract_markers.backfill_contract_markers(store, apply=True).marked == ["alpha:0"]
        assert "marked done" in close_accepted_task(store, "alpha", 0, JOB)


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


@pytest.mark.parametrize("retire_and_reopen", [False, True])
def test_verification_cannot_close_again_after_reopen(retire_and_reopen):
    store = Store()
    _setup(store)
    assert "marked done" in close_accepted_task(store, "alpha", 0, JOB)
    assert "reopened" in plan_edit.reopen_done_task_for_invalid_closure(
        store, "alpha", 0, "one", owner="worker", reason="The original closure was invalid",
    )
    if retire_and_reopen:
        assert "retired" in plan_edit.retire_task(store, "alpha", 0, "one", "needs replanning", "obsolete")
        assert "reopened" in plan_edit.reopen_task(store, "alpha", 0, "one", "New work is needed to repair the invalid closure")
    before = store.read(BOARD_KEY)
    project = records.get_project(store, "alpha")
    closed = []
    result = close_accepted_task(store, "alpha", 0, JOB, on_closed=lambda *args: closed.append(args))
    assert result.startswith("REJECTED:") and "already closed" in result
    assert store.read(BOARD_KEY) == before and closed == []
    assert records.get_project(store, "alpha") == project
    assert sum(e["event"] == "closed_accepted" for e in before["plan_events"]) == 1


def _newer_verification(store, contract_id, status="pending"):
    newer = verification.claim_verification(store, contract_id=contract_id, job_id="newer-job-id")
    previous = verification.get_verification(store, JOB)
    # Make the ordering deterministic even on a clock with low resolution.
    store.write(f"{VERIFICATION_PREFIX}{newer.job_id}", {
        **newer.model_dump(mode="json"), "status": status,
        "created_at": (previous.created_at + timedelta(seconds=1)).isoformat(),
    })


@pytest.mark.parametrize("status", ["pending", "running", "rework", "blocked", "accepted"])
def test_newer_verification_supersedes_old_acceptance(status):
    store = Store()
    contract, _ = _setup(store)
    _newer_verification(store, contract.contract_id, status)
    before = store.read(BOARD_KEY)
    assert "latest accepted verification" in close_accepted_task(store, "alpha", 0, JOB)
    assert store.read(BOARD_KEY) == before
    assert records.get_project(store, "alpha").last_progress_at is None


@pytest.mark.parametrize("status", ["paused", "done", "draft", None])
def test_project_must_exist_and_be_open(status):
    store = Store()
    _setup(store)
    key = f"{PROJECT_PREFIX}alpha"
    if status is None:
        store.delete(key)
    else:
        store.write(key, {**store.read(key), "status": status})
    before, project = store.read(BOARD_KEY), store.read(key)
    closed = []
    assert "must be open" in close_accepted_task(store, "alpha", 0, JOB, on_closed=lambda *args: closed.append(args))
    assert store.read(BOARD_KEY) == before and store.read(key) == project and closed == []


@pytest.mark.parametrize("change", ["reopen_verification", "newer_verification", "pause_project"])
def test_cross_record_change_after_board_read_is_rechecked_before_cas(monkeypatch, change):
    store = Store()
    contract, _ = _setup(store)
    read, cas = store.read, store.compare_and_swap
    board_writes = []

    def change_after_read(key, *args, **kwargs):
        raw = read(key, *args, **kwargs)
        if key == BOARD_KEY:
            if change == "reopen_verification":
                assert verification.reopen_for_empty_review(store, JOB)
            elif change == "newer_verification":
                _newer_verification(store, contract.contract_id)
            else:
                records.pause_project(store, "alpha")
        return raw

    def count_cas(key, expected, value):
        if key == BOARD_KEY:
            board_writes.append(key)
        return cas(key, expected, value)

    monkeypatch.setattr(store, "read", change_after_read)
    monkeypatch.setattr(store, "compare_and_swap", count_cas)
    assert close_accepted_task(store, "alpha", 0, JOB).startswith("REJECTED:")
    assert board_writes == [] and read(BOARD_KEY)["tasks"][0]["status"] == "todo"


def test_competing_closure_event_is_seen_on_cas_retry(monkeypatch):
    store = Store()
    _setup(store)
    cas = store.compare_and_swap
    writes = []

    def consume_before_cas(key, expected, value):
        if key == BOARD_KEY:
            writes.append(key)
            concurrent = {**expected, "plan_events": [{"event": "closed_accepted", "verification_id": JOB}]}
            store.write(key, concurrent)
        return cas(key, expected, value)

    monkeypatch.setattr(store, "compare_and_swap", consume_before_cas)
    assert "already closed" in close_accepted_task(store, "alpha", 0, JOB)
    assert len(writes) == 1 and store.read(BOARD_KEY)["tasks"][0]["status"] == "todo"


@pytest.mark.parametrize("bad_board", [
    "garbage", {}, {"tasks": None}, {"tasks": {"0": {}}}, {"tasks": "garbage"},
    {"tasks": ["garbage"]}, {"tasks": [{}, "garbage"]},
    {"plan_events": None}, {"plan_events": {}}, {"plan_events": ["garbage"]},
])
def test_malformed_board_returns_rejection_without_writing(bad_board):
    store = Store()
    _setup(store)
    raw = store.read(BOARD_KEY)
    # An empty document exercises a missing tasks field; other overrides retain
    # valid contract markers so validation of plan_events is reached as well.
    raw = {**raw, **bad_board} if isinstance(bad_board, dict) and bad_board else bad_board
    store.write(BOARD_KEY, raw)
    assert "REJECTED: malformed board" in close_accepted_task(store, "alpha", 0, JOB)
    assert store.read(BOARD_KEY) == raw
    assert records.get_project(store, "alpha").last_progress_at is None


@pytest.mark.parametrize("index", ["0", None, True, 0.0])
def test_malformed_task_index_is_rejected(index):
    store = Store()
    _setup(store)
    assert "task_index must be an integer" in close_accepted_task(store, "alpha", index, JOB)
    assert store.read(BOARD_KEY)["tasks"][0]["status"] == "todo"


def test_failing_hook_propagates_after_completion_and_progress():
    store = Store()
    _setup(store)
    called = []

    def fail(*args):
        called.append(args)
        raise RuntimeError("hook failed")

    with pytest.raises(RuntimeError, match="hook failed"):
        close_accepted_task(store, "alpha", 0, JOB, on_closed=fail)
    assert len(called) == 1
    assert store.read(BOARD_KEY)["tasks"][0]["status"] == "done"
    assert records.get_project(store, "alpha").last_progress_at is not None
    assert close_accepted_task(store, "alpha", 0, JOB, on_closed=fail).startswith("REJECTED:")
    assert len(called) == 1


def test_unique_verification_prefix_uses_full_id_in_result_and_consumption_event():
    store = Store()
    _setup(store)
    assert "marked done" in close_accepted_task(store, "alpha", 0, JOB[:8])
    doc = store.read(BOARD_KEY)
    assert JOB in doc["tasks"][0]["result"] and doc["plan_events"][0]["verification_id"] == JOB
    plan_edit.reopen_done_task_for_invalid_closure(store, "alpha", 0, "one", owner="worker", reason="invalid closure")
    assert "already closed" in close_accepted_task(store, "alpha", 0, JOB[:8])


def test_ambiguous_verification_prefix_refuses():
    store = Store()
    contract, _ = _setup(store)
    verification.claim_verification(store, contract_id=contract.contract_id, job_id="finished-other-id")
    before = store.read(BOARD_KEY)
    assert "not found (or ambiguous)" in close_accepted_task(store, "alpha", 0, JOB[:8])
    assert store.read(BOARD_KEY) == before
