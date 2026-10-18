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
from lazytools.projects.intake import install_project_plan

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


def _contract_keys(store: Store) -> list[str]:
    return [key for key, _raw in store.items(prefix=contracts.TASK_CONTRACT_PREFIX)]


# ---- P1a: a contract for a task that is not on the board is refused ----------------


@pytest.mark.parametrize("shape", ["no_board", "empty_board", "index_out_of_range"])
def test_open_contract_refuses_when_the_marker_cannot_be_written(shape: str) -> None:
    store = Store()
    if shape == "empty_board":
        store.write(BOARD_KEY, {"version": 1, "plan_id": "project:alpha", "reasoning": "r", "tasks": []})
    elif shape == "index_out_of_range":
        _board(store, ["only"])
    with pytest.raises(ValueError, match="not on the board of project 'alpha'"):
        _open(store, 1)
    assert _contract_keys(store) == []  # no key written, so nothing can escape the gate


def test_no_ghost_contract_lets_a_later_installed_task_close_by_shortcut() -> None:
    # The review's P1a: contract for a task that does not exist yet, then the plan
    # is installed and the task closes with a contract that was never marked.
    store = Store()
    _board(store, ["only"])
    with pytest.raises(ValueError):
        _open(store, 1)
    assert task_claims.complete_todo_without_verification(store, "alpha", 0, "verified").startswith("task 0 marked done")
    assert _contract_keys(store) == []
    assert contracts.find_contract_for_task(store, "alpha", 1) is None


def test_mcp_tool_reports_rejected_for_a_task_not_on_the_board(tmp_path) -> None:
    from lazytools.connectors.projects import ProjectsTools

    pt = ProjectsTools(store_db=str(tmp_path / "s.sqlite"), allow_write=True)
    pt.projects_create("alpha", "Alpha", "x")
    result = pt.projects_open_contract("alpha", 0, "repo", "obs", ["c0 passes"], ["pytest"], risk="low", allowed_effects="files")
    assert isinstance(result, str) and result.startswith("REJECTED") and "not on the board of project" in result
    assert pt.projects_find_contract("alpha", 0) is None


# ---- P1b: plan installation must not erase (or replace around) the marker ----------


def _install(store: Store, tasks: list[str], objective: str = "obj") -> str | None:
    return install_project_plan(store, "alpha", objective=objective, tasks=tasks)


def test_install_plan_keeps_the_marker_on_a_kept_task_so_the_gate_still_holds() -> None:
    store = Store()
    _board(store, ["t0", "t1"])
    contract = _open(store, 0)
    # Same index + same text, different objective: tasks are rebuilt, marker carried over.
    assert _install(store, ["t0", "t1"], objective="a changed objective") is None
    assert _task(store, 0)["contract_id"] == contract.contract_id
    assert "contract_id" not in _task(store, 1)
    msg = task_claims.complete_todo_without_verification(store, "alpha", 0, "verified")
    assert msg.startswith("REJECTED") and "accept()" in msg
    # The marker alone carries it (the contract key is not consulted for this check).
    store.delete(contracts._contract_key(contract.contract_id))
    assert task_claims.complete_todo_without_verification(store, "alpha", 0, "verified").startswith("REJECTED")


def test_install_plan_between_stamp_and_contract_key_does_not_open_the_shortcut(monkeypatch: pytest.MonkeyPatch) -> None:
    # The review's P1b interleaving, with real CASes: the opener has stamped the board
    # and is about to persist the contract key; a plan reinstall (same text, new
    # objective) lands in that window; then the closure runs.
    store = Store()
    _board(store, ["t0", "t1"])
    real_cas = store.compare_and_swap
    reinstalled: list[str | None] = []
    mid_window: list[str] = []

    def reinstall_before_key_write(key, expected, value):
        if key.startswith(contracts.TASK_CONTRACT_PREFIX) and not reinstalled:
            reinstalled.append(_install(store, ["t0", "t1"], objective="changed under the opener"))
            # The contract KEY does not exist yet, so only the board marker can refuse.
            assert contracts.find_contract_for_task(store, "alpha", 0) is None
            mid_window.append(task_claims.complete_todo_without_verification(store, "alpha", 0, "verified"))
        return real_cas(key, expected, value)

    monkeypatch.setattr(store, "compare_and_swap", reinstall_before_key_write)
    contract = _open(store, 0)
    monkeypatch.setattr(store, "compare_and_swap", real_cas)

    assert reinstalled == [None]  # the reinstall really happened and succeeded
    assert len(mid_window) == 1 and mid_window[0].startswith("REJECTED") and "accept()" in mid_window[0]
    assert _task(store, 0)["contract_id"] == contract.contract_id
    msg = task_claims.complete_todo_without_verification(store, "alpha", 0, "verified")
    assert msg.startswith("REJECTED") and "accept()" in msg
    assert _task(store, 0)["status"] == "todo"


def test_install_plan_refuses_to_replace_or_drop_a_task_with_a_marker() -> None:
    store = Store()
    _board(store, ["t0", "t1"])
    contract = _open(store, 1)
    before = store.read(BOARD_KEY)

    changed = _install(store, ["t0", "different"])
    assert changed is not None and changed.startswith("REJECTED") and "acceptance contract" in changed and "1: 't1'" in changed
    shifted = _install(store, ["new first", "t0", "t1"])  # t1 would move to index 2
    assert shifted is not None and shifted.startswith("REJECTED") and "acceptance contract" in shifted
    dropped = _install(store, ["t0"])
    assert dropped is not None and dropped.startswith("REJECTED") and "acceptance contract" in dropped
    assert store.read(BOARD_KEY) == before  # nothing was written
    assert _task(store, 1)["contract_id"] == contract.contract_id


def test_install_plan_still_replaces_an_untouched_unmarked_plan() -> None:
    store = Store()
    _board(store, ["t0", "t1"])
    _open(store, 0)
    assert _install(store, ["t0", "t1", "t2 appended"]) is None  # extending is fine: marked task kept
    assert [t["text"] for t in DurableBlackboard(store, plan_id="project:alpha").snapshot().tasks] == ["t0", "t1", "t2 appended"]
    assert "contract_id" in _task(store, 0)

    other = Store()
    _board(other, ["a", "b"])
    assert _install(other, ["x", "y", "z"]) is None  # nothing marked: replaced as before
    assert "contract_id" not in _task(other, 0)


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


def test_backfill_reports_a_task_that_vanishes_after_the_scan_as_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    # The review's P2: a live plan replacement removes the task after the backfill's
    # first read; the helper then returns False and nothing may be reported as marked.
    store = _legacy_store()
    real_mark = contract_markers.mark_task_has_contract

    def plan_shrinks_then_mark(st, project_id, task_index, contract_id, **kwargs):
        if task_index == 1:
            board = st.read(BOARD_KEY)
            st.write(BOARD_KEY, {**board, "tasks": board["tasks"][:1]})
        return real_mark(st, project_id, task_index, contract_id, **kwargs)

    monkeypatch.setattr(contract_markers, "mark_task_has_contract", plan_shrinks_then_mark)
    report = contract_markers.backfill_contract_markers(store, apply=True)
    assert report.marked == ["alpha:0"]
    assert report.skipped_no_board_task == ["alpha:1", "alpha:9"]
    assert "marked 1" in report.summary() and "skipped (no board task) 2" in report.summary()
    assert len(DurableBlackboard(store, plan_id="project:alpha").snapshot().tasks) == 1


def test_backfill_reports_a_marker_stamped_by_someone_else_as_already_marked(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _legacy_store()
    real_mark = contract_markers.mark_task_has_contract

    def someone_stamps_first(st, project_id, task_index, contract_id, **kwargs):
        if task_index == 0:
            real_mark(st, project_id, 0, "stamped-by-a-new-contract")
        return real_mark(st, project_id, task_index, contract_id, **kwargs)

    monkeypatch.setattr(contract_markers, "mark_task_has_contract", someone_stamps_first)
    report = contract_markers.backfill_contract_markers(store, apply=True)
    assert "alpha:0" in report.already_marked and "alpha:0" not in report.marked
    assert _task(store, 0)["contract_id"] == "stamped-by-a-new-contract"


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
