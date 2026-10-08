from __future__ import annotations

import time

import pytest
from lazybridge import Store
from lazybridge.ext.planners import DurableBlackboard

from _projects_parity import original_function
from lazytools.projects import contracts, records, task_claims
from lazytools.projects.keys import JOB_PREFIX


def check(store, project_id, task_index):
    contract = contracts.find_contract_for_task(store, project_id, task_index)
    if contract is not None:
        return f"REJECTED: task {task_index} now has contract {contract.contract_id} -- close it through its verification (accept_verification)."
    return None


def setup(store):
    records.adopt_existing_project(store, project_id="alpha", title="Alpha", objective="files", adoption_reason="existing")
    board = DurableBlackboard(store, "project:alpha")
    board.set_plan("files", ["one"])
    return board


@pytest.mark.parametrize("state", ["todo", "claimed", "running_job", "contract", "blank"])
def test_closure_matches_original(state, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 100.0)
    stores = [Store(), Store()]
    for store in stores:
        board = setup(store)
        if state == "claimed":
            board.claim_next(owner="worker")
        if state == "running_job":
            store.write(f"{JOB_PREFIX}one", {"plan_id": "project:alpha", "task_index": 0, "status": "running"})
        if state == "contract":
            contract = contracts.open_task_contract(store, project_id="alpha", task_index=0, repo="r", observable_result="files", acceptance_criteria=["file exists"], required_checks=["pytest"], risk="low", allowed_effects="files")
            raw = contract.model_dump(mode="json")
            # Give both stores the same contract identity for exact refusal parity.
            store.delete(contracts._contract_key(contract.contract_id))
            store.write(contracts._contract_key("fixed"), {**raw, "contract_id": "fixed"})
    original = original_function("complete_todo_without_verification", time=time, CAS_ATTEMPTS=8,
        _running_job_exists=lambda store, plan, index: task_claims._running_job_exists(store, plan, index, job_prefix=JOB_PREFIX),
        touch_project_progress=records.touch_project_progress, find_contract_for_task=contracts.find_contract_for_task)
    summary = "" if state == "blank" else "verified"
    before = original(stores[0], "alpha", 0, summary)
    after = task_claims.complete_todo_without_verification(stores[1], "alpha", 0, summary, check=check, on_closed=lambda store, project, index: records.touch_project_progress(store, project))
    assert before == after
    assert stores[0].read("blackboard:project:alpha") == stores[1].read("blackboard:project:alpha")
    assert bool(records.get_project(stores[0], "alpha").last_progress_at) == bool(records.get_project(stores[1], "alpha").last_progress_at)


def test_check_repeated_per_cas_and_hook_once(monkeypatch):
    store = Store()
    board = setup(store)
    checks, closed = [], []
    cas = store.compare_and_swap
    calls = []

    def race(key, expected, value):
        calls.append(key)
        if len(calls) == 1:
            store.write(key, {**expected, "concurrent": True})
        return cas(key, expected, value)

    monkeypatch.setattr(store, "compare_and_swap", race)
    kwargs = dict(check=lambda *args: checks.append(args), on_closed=lambda *args: closed.append(args))
    assert "marked done" in task_claims.complete_todo_without_verification(store, "alpha", 0, "verified", **kwargs)
    assert len(checks) == 2 and len(closed) == 1
    assert task_claims.complete_todo_without_verification(store, "alpha", 0, "again", **kwargs).startswith("REJECTED")
    assert len(closed) == 1 and board.snapshot().tasks[0]["result"] == "verified"


def test_competing_close_does_not_call_losing_hook(monkeypatch):
    store = Store()
    setup(store)
    cas = store.compare_and_swap
    winners, losers = [], []

    def race(key, expected, value):
        monkeypatch.setattr(store, "compare_and_swap", cas)
        task_claims.complete_todo_without_verification(store, "alpha", 0, "winner", on_closed=lambda *args: winners.append(args))
        return cas(key, expected, value)

    monkeypatch.setattr(store, "compare_and_swap", race)
    assert task_claims.complete_todo_without_verification(store, "alpha", 0, "loser", on_closed=lambda *args: losers.append(args)).startswith("REJECTED")
    assert len(winners) == 1 and losers == []


def test_new_contract_on_retry_refuses_before_next_cas(monkeypatch):
    store = Store()
    setup(store)
    calls, closed = [], []

    def guard(*args):
        return "REJECTED: new contract" if calls else None

    def lose(key, expected, value):
        calls.append(key)
        return False

    monkeypatch.setattr(store, "compare_and_swap", lose)
    result = task_claims.complete_todo_without_verification(store, "alpha", 0, "verified", check=guard, on_closed=lambda *args: closed.append(args))
    assert result == "REJECTED: new contract" and len(calls) == 1 and closed == []
