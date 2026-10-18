"""The contract gate is atomic with the shortcut closure.

A contract used to be a separate store key, so it could commit after the
closure's lookup but before its board CAS, and the task closed anyway
(Codex review of PR #208, P1). ``open_task_contract`` now stamps
``task["contract_id"]`` on the board in a board CAS and the closure refuses on
that marker inside its own board CAS: the two serialize on one key.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from lazybridge import Store
from lazybridge.ext.planners import DurableBlackboard

from lazytools.projects import contract_markers, contracts, task_claims

BOARD_KEY = "blackboard:project:alpha"


def _board(store: Store, tasks: list[str] | None = None) -> DurableBlackboard:
    board = DurableBlackboard(store, plan_id="project:alpha")
    board.set_plan("r", tasks or ["t0", "t1"])
    return board


def _open(store: Store, task_index: int = 0) -> contracts.TaskContract:
    return contracts.open_task_contract(
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


def _write_legacy_contract(store: Store, task_index: int = 0, contract_id: str = "legacy-1", created: str = "2026-10-01T00:00:00+00:00") -> None:
    """A contract exactly as written before the marker existed: key only, board untouched."""
    store.write(
        contracts._contract_key(contract_id),
        {
            "contract_id": contract_id,
            "project_id": "alpha",
            "task_index": task_index,
            "repo": "r",
            "observable_result": "files",
            "acceptance_criteria": ["file exists"],
            "required_checks": ["pytest"],
            "risk": "low",
            "allowed_effects": "files",
            "created_at": created,
        },
    )


def _task(store: Store, index: int = 0) -> dict[str, Any]:
    return DurableBlackboard(store, plan_id="project:alpha").snapshot().tasks[index]


# ---- the P1 interleavings, with the REAL Store CAS ------------------------------


def test_contract_committed_after_lookup_before_real_board_cas_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store()
    _board(store)
    real_cas = store.compare_and_swap
    entered = {"n": 0}

    def contract_wins_then_real_cas(key, expected, value):
        if key == BOARD_KEY:
            entered["n"] += 1
            if entered["n"] == 1:
                # The closure already did its lookup (none) and is entering the
                # board CAS. A contract commits right now, in full...
                monkeypatch.setattr(store, "compare_and_swap", real_cas)
                _open(store)
                monkeypatch.setattr(store, "compare_and_swap", contract_wins_then_real_cas)
        # ...and the closure's CAS is forwarded to the REAL implementation.
        return real_cas(key, expected, value)

    monkeypatch.setattr(store, "compare_and_swap", contract_wins_then_real_cas)
    closed: list[tuple] = []
    msg = task_claims.complete_todo_without_verification(store, "alpha", 0, "verified", on_closed=lambda *a: closed.append(a))

    assert msg.startswith("REJECTED") and "acceptance contract" in msg and "accept()" in msg
    assert closed == []
    assert _task(store)["status"] == "todo"
    assert contracts.find_contract_for_task(store, "alpha", 0) is not None


def test_contract_committed_while_caller_hook_runs_is_refused() -> None:
    # The review's second reproduction: a legitimate LazyCEO-style ``check`` hook
    # that lets a writer commit a contract, then returns None ("allow").
    store = Store()
    _board(store)
    opened: list[contracts.TaskContract] = []

    def hook(_store, _project, _index):
        if not opened:
            writer = threading.Thread(target=lambda: opened.append(_open(store)))
            writer.start()
            writer.join()
        return None

    closed: list[tuple] = []
    msg = task_claims.complete_todo_without_verification(store, "alpha", 0, "verified", check=hook, on_closed=lambda *a: closed.append(a))

    assert len(opened) == 1
    assert msg.startswith("REJECTED") and "acceptance contract" in msg
    assert closed == []
    assert _task(store)["status"] == "todo"


def test_marker_alone_refuses_even_if_contract_key_is_not_there_yet() -> None:
    # open_task_contract stamps the board BEFORE writing the contract key; a
    # closure landing between the two writes must be refused by the marker alone.
    store = Store()
    _board(store)
    contracts.mark_task_has_contract(store, "alpha", 0, "c-pending")
    assert contracts.find_contract_for_task(store, "alpha", 0) is None
    msg = task_claims.complete_todo_without_verification(store, "alpha", 0, "verified")
    assert msg.startswith("REJECTED") and "accept()" in msg
    assert _task(store)["status"] == "todo"


def test_legacy_contract_without_marker_is_still_refused_by_lookup() -> None:
    # Defence in depth: contracts that predate the marker (and were not yet backfilled).
    store = Store()
    _board(store)
    _write_legacy_contract(store)
    assert "contract_id" not in _task(store)
    msg = task_claims.complete_todo_without_verification(store, "alpha", 0, "verified")
    assert msg.startswith("REJECTED") and "accept()" in msg
    assert _task(store)["status"] == "todo"


# ---- concurrent open vs close: exactly one wins ----------------------------------


@pytest.mark.parametrize("round_", range(25))
def test_concurrent_open_and_close_exactly_one_wins(round_: int) -> None:
    store = Store()
    _board(store)
    start = threading.Barrier(2)
    outcome: dict[str, Any] = {}

    def close() -> None:
        start.wait()
        outcome["close"] = task_claims.complete_todo_without_verification(store, "alpha", 0, "verified")

    def open_contract() -> None:
        start.wait()
        try:
            outcome["open"] = _open(store)
        except ValueError as exc:
            outcome["open"] = exc

    threads = [threading.Thread(target=close), threading.Thread(target=open_contract)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    closed_ok = "marked done" in outcome["close"]
    opened_ok = isinstance(outcome["open"], contracts.TaskContract)
    assert closed_ok != opened_ok, outcome  # exactly one winner, never both, never neither
    if closed_ok:
        assert _task(store)["status"] == "done"
        assert contracts.find_contract_for_task(store, "alpha", 0) is None  # no orphan contract on closed work
        assert "already done" in str(outcome["open"])
    else:
        assert outcome["close"].startswith("REJECTED") and "accept()" in outcome["close"]
        assert _task(store)["status"] == "todo"
        assert _task(store)["contract_id"] == outcome["open"].contract_id


# ---- open_task_contract's side of the shared CAS ---------------------------------


def test_open_contract_stamps_marker_and_refuses_done_task() -> None:
    store = Store()
    _board(store)
    contract = _open(store, 0)
    assert _task(store, 0)["contract_id"] == contract.contract_id
    assert "contract_id" not in _task(store, 1)

    assert "marked done" in task_claims.complete_todo_without_verification(store, "alpha", 1, "verified")
    with pytest.raises(ValueError, match="already done"):
        _open(store, 1)
    assert contracts.find_contract_for_task(store, "alpha", 1) is None


def test_open_contract_without_a_board_task_still_works() -> None:
    store = Store()
    contract = _open(store, 7)  # no board at all
    assert contracts.find_contract_for_task(store, "alpha", 7) == contract
    _board(store, ["only"])
    again = _open(store, 7)  # index outside the board
    assert again.contract_id != contract.contract_id


def test_marker_survives_a_claim_and_a_second_contract_replaces_it() -> None:
    store = Store()
    board = _board(store)
    first = _open(store, 0)
    board.claim_task(0, "t0", owner="worker")
    assert _task(store, 0)["contract_id"] == first.contract_id
    second = _open(store, 0)
    assert _task(store, 0)["contract_id"] == second.contract_id


# ---- LazyCEO-style calls with a check hook still work ----------------------------


def test_lazyceo_style_check_hook_still_works() -> None:
    store = Store()
    _board(store)
    seen: list[tuple] = []
    closed: list[tuple] = []

    def lazyceo_check(_store, project_id, task_index):
        seen.append((project_id, task_index))
        contract = contracts.find_contract_for_task(_store, project_id, task_index)
        return f"REJECTED: task {task_index} now has contract {contract.contract_id}" if contract else None

    assert "marked done" in task_claims.complete_todo_without_verification(
        store, "alpha", 0, "verified", check=lazyceo_check, on_closed=lambda *a: closed.append(a)
    )
    assert seen == [("alpha", 0)]
    assert closed == [(store, "alpha", 0)]  # on_closed(store, project_id, task_index), once

    _open(store, 1)
    seen.clear()
    msg = task_claims.complete_todo_without_verification(store, "alpha", 1, "verified", check=lazyceo_check)
    assert msg.startswith("REJECTED") and "accept()" in msg  # the built-in gate refuses first
    assert seen == []  # ...so the caller's hook is never even reached


def test_hook_refusal_still_refuses_without_a_contract() -> None:
    store = Store()
    _board(store)
    msg = task_claims.complete_todo_without_verification(store, "alpha", 0, "verified", check=lambda *_: "REJECTED: caller says no")
    assert msg == "REJECTED: caller says no"
    assert _task(store)["status"] == "todo"


# ---- the marker backfill ----------------------------------------------------------


def _legacy_store() -> Store:
    store = Store()
    _board(store, ["t0", "t1", "t2"])
    _write_legacy_contract(store, 0, "old-0")
    _write_legacy_contract(store, 1, "old-1a", created="2026-10-01T00:00:00+00:00")
    _write_legacy_contract(store, 1, "old-1b", created="2026-10-05T00:00:00+00:00")
    _write_legacy_contract(store, 9, "orphan-9")  # no such board task
    return store


def test_backfill_dry_run_changes_nothing() -> None:
    store = _legacy_store()
    before = store.read(BOARD_KEY)
    report = contract_markers.backfill_contract_markers(store)
    assert store.read(BOARD_KEY) == before
    assert report.applied is False
    assert report.marked == ["alpha:0", "alpha:1"] and report.skipped_no_board_task == ["alpha:9"]


def test_backfill_apply_is_idempotent_and_newest_contract_wins() -> None:
    store = _legacy_store()
    first = contract_markers.backfill_contract_markers(store, apply=True)
    assert first.applied is True and first.marked == ["alpha:0", "alpha:1"]
    assert _task(store, 0)["contract_id"] == "old-0"
    assert _task(store, 1)["contract_id"] == "old-1b"
    assert "contract_id" not in _task(store, 2)

    snapshot = store.read(BOARD_KEY)
    second = contract_markers.backfill_contract_markers(store, apply=True)
    assert second.marked == [] and second.already_marked == ["alpha:0", "alpha:1"]
    assert store.read(BOARD_KEY) == snapshot  # byte-for-byte: no write, not even updated_at


def test_backfill_never_overwrites_an_existing_marker() -> None:
    store = _legacy_store()
    contracts.mark_task_has_contract(store, "alpha", 0, "newer-than-backfill")
    report = contract_markers.backfill_contract_markers(store, apply=True)
    assert "alpha:0" in report.already_marked
    assert _task(store, 0)["contract_id"] == "newer-than-backfill"


def test_backfill_makes_the_closure_gate_atomic_for_legacy_contracts() -> None:
    store = _legacy_store()
    contract_markers.backfill_contract_markers(store, apply=True)
    store.delete(contracts._contract_key("old-0"))  # the marker alone now carries the refusal
    msg = task_claims.complete_todo_without_verification(store, "alpha", 0, "verified")
    assert msg.startswith("REJECTED") and "accept()" in msg


def test_backfill_marks_an_already_done_task_without_failing() -> None:
    store = _legacy_store()
    assert "marked done" in task_claims.complete_todo_without_verification(store, "alpha", 2, "verified")
    _write_legacy_contract(store, 2, "late-2")
    report = contract_markers.backfill_contract_markers(store, apply=True)
    assert "alpha:2" in report.marked
    assert _task(store, 2)["status"] == "done" and _task(store, 2)["contract_id"] == "late-2"


def test_backfill_cli_dry_run_then_apply(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    db = str(tmp_path / "ceo.sqlite")
    store = Store(db=db)
    _board(store, ["t0"])
    _write_legacy_contract(store, 0, "old-0")

    assert contract_markers.main(["--store-db", db]) == 0
    assert "would mark 1" in capsys.readouterr().out
    assert "contract_id" not in _task(Store(db=db), 0)

    assert contract_markers.main(["--store-db", db, "--apply"]) == 0
    assert "marked 1" in capsys.readouterr().out
    assert _task(Store(db=db), 0)["contract_id"] == "old-0"

    assert contract_markers.main(["--store-db", db, "--apply"]) == 0
    assert "marked 0, already marked 1" in capsys.readouterr().out
