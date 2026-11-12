"""closure.close_task_with_evidence -- one call that closes a contract-backed task."""

from __future__ import annotations

import pytest
from lazybridge import Store
from lazybridge.ext.planners import DurableBlackboard

from lazytools.projects import closure, contracts, notes, records, task_claims, verification
from lazytools.projects.cli import build_parser, main, parse_check
from lazytools.projects.keys import BOARD_KEY_PREFIX, VERIFICATION_PREFIX

BOARD_KEY = f"{BOARD_KEY_PREFIX}project:alpha"
PYTEST = "python -m pytest tests -q"
RUFF = "ruff check src"


def _setup(store, *, required=(PYTEST, RUFF), requires_review=True):
    records.adopt_existing_project(store, project_id="alpha", title="Alpha", objective="files", adoption_reason="existing")
    DurableBlackboard(store, plan_id="project:alpha").set_plan("files", ["one", "two"])
    return contracts.open_task_contract(
        store, project_id="alpha", task_index=0, repo="r", observable_result="files",
        acceptance_criteria=["file exists"], required_checks=list(required), risk="low", allowed_effects="files",
        requires_review=requires_review, review_waiver=None if requires_review else "docs only",
    )


def _verifications(store):
    return [raw for _key, raw in store.items(prefix=VERIFICATION_PREFIX)]


def _close(store, checks, **kwargs):
    kwargs.setdefault("review", "Read the diff; no issues.")
    kwargs.setdefault("reason", "checks and review pass")
    kwargs.setdefault("reviewer", "operator")
    kwargs.setdefault("review_by", "codex")
    kwargs.setdefault("worker", "writer")
    return closure.close_task_with_evidence(store, "alpha", 0, checks=checks, **kwargs)


PASSING = [(PYTEST, True, "412 passed"), (RUFF, True, "All checks passed!")]


def test_happy_path_closes_the_task_through_accept():
    store = Store()
    contract = _setup(store)
    result = _close(store, PASSING)
    assert result.startswith("task 0 marked done"), result
    task = DurableBlackboard(store, plan_id="project:alpha").snapshot().tasks[0]
    assert task["status"] == "done"
    (raw,) = _verifications(store)
    assert raw["status"] == "accepted" and raw["contract_id"] == contract.contract_id
    assert raw["job_id"].startswith("manual-alpha-0-")
    assert [c["command"] for c in raw["checks"]] == [PYTEST, RUFF]
    assert raw["review"]["findings"] == "Read the diff; no issues."
    # The review is attributed to its author, the acceptance to the acceptor, and
    # the work to the worker -- three distinct identities, all on the record.
    assert raw["review"]["reviewer"] == "codex" and raw["reviewer"] == "operator"
    assert raw["worker"] == "writer"
    assert len({raw["worker"], raw["review"]["reviewer"], raw["reviewer"]}) == 3
    events = store.read(BOARD_KEY)["plan_events"]
    assert events[-1]["event"] == "closed_accepted" and events[-1]["verification_id"] == raw["verification_id"]
    assert records.get_project(store, "alpha").last_progress_at is not None


def test_missing_required_check_refuses_without_claiming():
    store = Store()
    _setup(store)
    before = store.read(BOARD_KEY)
    result = _close(store, [(PYTEST, True, "ok"), ("ruff check .", True, "ok")])
    assert result.startswith("REJECTED: missing required check(s): 'ruff check src'"), result
    # Lists the exact expected command strings.
    assert f"  {PYTEST}\n  {RUFF}" in result
    assert _verifications(store) == []
    assert store.read(BOARD_KEY) == before


def test_no_checks_refuses_listing_expected_commands():
    store = Store()
    _setup(store)
    result = _close(store, [])
    assert result.startswith("REJECTED: no checks given") and RUFF in result
    assert _verifications(store) == []


def test_failed_check_refuses_without_claiming():
    store = Store()
    _setup(store)
    result = _close(store, [(PYTEST, False, "3 failed"), (RUFF, True, "ok")])
    assert result.startswith("REJECTED: check(s) did not pass") and PYTEST in result
    assert _verifications(store) == []
    assert DurableBlackboard(store, plan_id="project:alpha").snapshot().tasks[0]["status"] == "todo"


def test_missing_review_refused_when_contract_requires_one():
    store = Store()
    _setup(store)
    assert _close(store, PASSING, review="  ").startswith("REJECTED: this contract requires an independent review")
    assert _verifications(store) == []


@pytest.mark.parametrize("review", ["REVIEW NOT PERFORMED (reviewer unavailable)", "[codex_code_review] failed: timeout"])
def test_review_not_performed_is_recorded_blocked_and_flagged_not_closed(review):
    store = Store()
    _setup(store)
    result = _close(store, PASSING, review=review, review_by=None)
    assert result.startswith("REJECTED: review not performed") and "NOT closed" in result and "owner" in result, result
    (raw,) = _verifications(store)
    assert raw["status"] == "blocked" and raw["worker"] == "writer"
    assert raw["review"]["performed"] is False and raw["review"]["findings"] == review
    assert raw["decision_reason"].startswith("REVIEW NOT PERFORMED")
    assert DurableBlackboard(store, plan_id="project:alpha").snapshot().tasks[0]["status"] == "todo"
    (note,) = notes.recent_project_notes(store, "alpha")
    assert raw["job_id"] in note["note"] and "REVIEW NOT PERFORMED" in note["note"]


def test_bare_review_not_performed_without_reason_is_refused():
    store = Store()
    _setup(store)
    assert _close(store, PASSING, review="REVIEW NOT PERFORMED").startswith("REJECTED: say why")
    assert _verifications(store) == []


def test_reviewer_equal_to_worker_is_refused():
    store = Store()
    _setup(store)
    result = _close(store, PASSING, reviewer="writer", worker="writer")
    assert result.startswith("REJECTED: reviewer (the acceptor) and the worker are both 'writer'"), result
    assert _verifications(store) == []


def test_reviewer_equal_to_claim_owner_is_refused():
    store = Store()
    _setup(store)
    board = DurableBlackboard(store, plan_id="project:alpha")
    assert board.claim_task(0, "one", owner="writer") == (0, "one")
    # The claim owner is the worker; the caller cannot rename it away.
    assert _close(store, PASSING, reviewer="Writer", worker=None).startswith("REJECTED: reviewer (the acceptor)")
    assert _close(store, PASSING, worker="someone-else").startswith("REJECTED: worker 'someone-else' does not match")
    assert _verifications(store) == []
    result = _close(store, PASSING, worker=None)
    assert result.startswith("task 0 marked done"), result
    (raw,) = _verifications(store)
    assert raw["worker"] == "writer"


def test_review_by_equal_to_worker_is_refused():
    store = Store()
    _setup(store)
    result = _close(store, PASSING, review_by="writer")
    assert result.startswith("REJECTED: review_by and the worker are both 'writer'"), result
    assert _verifications(store) == []


def test_unclaimed_task_without_worker_is_refused():
    store = Store()
    _setup(store)
    assert _close(store, PASSING, worker=None).startswith("REJECTED: task 0 has no claim owner")
    assert _verifications(store) == []


def test_waived_review_records_not_performed_review_honestly():
    store = Store()
    _setup(store, requires_review=False)
    assert _close(store, PASSING, review="REVIEW NOT PERFORMED: waived").startswith("task 0 marked done")
    (raw,) = _verifications(store)
    assert raw["review"]["performed"] is False


@pytest.mark.parametrize("review_by", [None, "  ", "operator", " Operator "])
def test_required_review_needs_an_author_other_than_the_acceptor(review_by):
    store = Store()
    _setup(store)
    result = _close(store, PASSING, review_by=review_by)
    assert result.startswith("REJECTED:") and "review_by" in result, result
    assert _verifications(store) == []


def test_reuse_of_accepted_verification_still_validates_current_evidence():
    store = Store()
    contract = _setup(store)
    job = "accepted-earlier"
    verification.claim_verification(store, contract_id=contract.contract_id, job_id=job)
    verification.start_running(store, job)
    verification.record_checks(
        store, job, checks=[verification.CheckResult(command=c, exit_code=0, output_tail="ok") for c in (PYTEST, RUFF)],
        diff_summary=None,
    )
    verification.record_review(store, job, reviewer="r", findings="fine")
    assert verification.accept(store, job, reviewer="a", reason="ok")
    before = store.read(BOARD_KEY)
    (accepted,) = _verifications(store)
    failing = [(PYTEST, False, "3 failed"), (RUFF, True, "ok")]
    assert _close(store, failing).startswith("REJECTED: check(s) did not pass")
    # A not-performed review is recorded on a FRESH close; on the reuse path it
    # must refuse outright, writing nothing.
    assert _close(store, PASSING, review="REVIEW NOT PERFORMED (timeout)").startswith(
        "REJECTED: the review says it was not performed"
    )
    assert store.read(BOARD_KEY) == before
    assert _verifications(store) == [accepted]
    assert notes.recent_project_notes(store, "alpha") == []
    # The retry with genuine passing evidence then reuses it.
    assert f"reused accepted verification {job!r}" in _close(store, PASSING)


def test_reuse_refuses_a_verification_accepted_by_the_worker():
    store = Store()
    contract = _setup(store)
    job = "self-accepted"
    verification.claim_verification(store, contract_id=contract.contract_id, job_id=job)
    verification.start_running(store, job)
    verification.record_checks(
        store, job, checks=[verification.CheckResult(command=c, exit_code=0, output_tail="ok") for c in (PYTEST, RUFF)],
        diff_summary=None,
    )
    verification.record_review(store, job, reviewer="codex", findings="fine")
    assert verification.accept(store, job, reviewer="writer", reason="ok")
    result = _close(store, PASSING)
    assert result.startswith("REJECTED: accepted verification 'self-accepted'") and "'writer'" in result
    assert DurableBlackboard(store, plan_id="project:alpha").snapshot().tasks[0]["status"] == "todo"


def test_cleanup_retries_a_lost_terminal_transition(monkeypatch):
    store = Store()
    _setup(store)
    monkeypatch.setattr(verification, "record_checks", lambda *a, **k: None)
    real_block = verification.block
    calls = []

    def flaky_block(*args, **kwargs):
        calls.append(1)
        return None if len(calls) == 1 else real_block(*args, **kwargs)

    monkeypatch.setattr(verification, "block", flaky_block)
    result = _close(store, PASSING)
    assert result.startswith("REJECTED:") and "recorded as blocked" in result
    (raw,) = _verifications(store)
    assert raw["status"] == "blocked" and len(calls) == 2


@pytest.mark.parametrize("failure", ["exception", "lost_race"])
def test_contested_cleanup_still_leaves_no_running_verification(monkeypatch, failure):
    """Every cleanup compare-and-swap loses; the record must still end up blocked."""
    store = Store()
    _setup(store)
    real_cas = store.compare_and_swap
    contested = []

    def cas(key, expected, new):
        if contested and key.startswith(VERIFICATION_PREFIX):
            contested.append(key)
            return False
        return real_cas(key, expected, new)

    monkeypatch.setattr(store, "compare_and_swap", cas)

    def fail_checks(*_args, **_kwargs):
        contested.append("on")
        if failure == "exception":
            raise RuntimeError("disk on fire")
        return None

    monkeypatch.setattr(verification, "record_checks", fail_checks)
    if failure == "exception":
        with pytest.raises(RuntimeError, match="disk on fire"):
            _close(store, PASSING)
    else:
        result = _close(store, PASSING)
        assert result.startswith("REJECTED:") and "forced after" in result, result
    (raw,) = _verifications(store)
    assert raw["status"] == "blocked", raw["status"]
    assert "forced" in raw["decision_reason"]
    # Every bounded attempt was tried before the forced write.
    assert len(contested) - 1 >= task_claims.CAS_ATTEMPTS


def test_waived_review_closes_without_review():
    store = Store()
    _setup(store, requires_review=False)
    assert _close(store, PASSING, review="").startswith("task 0 marked done")
    (raw,) = _verifications(store)
    assert raw["review"] is None


def test_second_call_on_a_closed_task_is_a_noop():
    store = Store()
    _setup(store)
    assert _close(store, PASSING).startswith("task 0 marked done")
    board = store.read(BOARD_KEY)
    (raw,) = _verifications(store)
    again = _close(store, PASSING)
    assert again.startswith("task 0 is already done") and raw["verification_id"] in again
    assert store.read(BOARD_KEY) == board
    assert len(_verifications(store)) == 1


def test_task_without_contract_is_refused():
    store = Store()
    records.adopt_existing_project(store, project_id="alpha", title="Alpha", objective="files", adoption_reason="existing")
    DurableBlackboard(store, plan_id="project:alpha").set_plan("files", ["one"])
    result = _close(store, PASSING)
    assert result.startswith("REJECTED: task 0 has no acceptance contract")
    assert "complete_todo_without_verification" in result


def test_running_verification_blocks_a_manual_close():
    store = Store()
    contract = _setup(store)
    assert verification.claim_verification(store, contract_id=contract.contract_id, job_id="delegated")
    assert verification.start_running(store, "delegated")
    result = _close(store, PASSING)
    assert result.startswith("REJECTED: verification 'delegated'") and "'running'" in result
    assert len(_verifications(store)) == 1


def test_accepted_but_unclosed_verification_is_reused():
    store = Store()
    contract = _setup(store)
    job = "crashed-before-close"
    verification.claim_verification(store, contract_id=contract.contract_id, job_id=job)
    verification.start_running(store, job)
    verification.record_checks(
        store, job, checks=[verification.CheckResult(command=c, exit_code=0, output_tail="ok") for c in (PYTEST, RUFF)],
        diff_summary=None,
    )
    verification.record_review(store, job, reviewer="r", findings="fine")
    assert verification.accept(store, job, reviewer="r", reason="ok")
    result = _close(store, PASSING)
    assert result.startswith("task 0 marked done") and f"reused accepted verification {job!r}" in result
    assert len(_verifications(store)) == 1


def test_accept_refusal_after_claim_leaves_no_live_verification(monkeypatch):
    store = Store()
    _setup(store)

    def refuse(*_args, **_kwargs):
        raise ValueError("authorization refused")

    monkeypatch.setattr(verification, "accept", refuse)
    result = _close(store, PASSING)
    assert result.startswith("REJECTED: authorization refused") and "rework" in result
    (raw,) = _verifications(store)
    assert raw["status"] == "rework"
    assert DurableBlackboard(store, plan_id="project:alpha").snapshot().tasks[0]["status"] == "todo"


def test_lost_race_blocks_the_claimed_verification(monkeypatch):
    store = Store()
    _setup(store)
    monkeypatch.setattr(verification, "record_checks", lambda *a, **k: None)
    result = _close(store, PASSING)
    assert result.startswith("REJECTED:") and "record_checks" in result
    (raw,) = _verifications(store)
    assert raw["status"] == "blocked"


def test_unexpected_error_blocks_then_reraises(monkeypatch):
    store = Store()
    _setup(store)

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(verification, "record_review", boom)
    with pytest.raises(RuntimeError):
        _close(store, PASSING)
    (raw,) = _verifications(store)
    assert raw["status"] == "blocked"


def test_shortcut_closer_still_refuses_contract_tasks():
    store = Store()
    _setup(store)
    assert task_claims.complete_todo_without_verification(store, "alpha", 0, "done").startswith("REJECTED")


# ---- CLI -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("pytest=pass", ("pytest", True, "")),
        ("pytest -q=fail:3 failed", ("pytest -q", False, "3 failed")),
        ("ruff check src=pass:a=b: c", ("ruff check src", True, "a=b: c")),
    ],
)
def test_cli_parses_check(value, expected):
    assert parse_check(value) == expected


@pytest.mark.parametrize("value", ["pytest", "pytest=ok", "=pass", "pytest=passed"])
def test_cli_rejects_malformed_check(value):
    with pytest.raises(Exception):  # noqa: B017 -- argparse.ArgumentTypeError
        parse_check(value)


def test_cli_parser_collects_repeated_checks():
    args = build_parser().parse_args(
        ["close-task", "alpha", "0", "--check", f"{PYTEST}=pass:ok", "--check", f"{RUFF}=fail:E501",
         "--review", "fine", "--reason", "r", "--reviewer", "me", "--db", "x.sqlite"]
    )
    assert args.checks == [(PYTEST, True, "ok"), (RUFF, False, "E501")]
    assert (args.project_id, args.task_index, args.db) == ("alpha", 0, "x.sqlite")


def test_cli_closes_task_end_to_end(tmp_path, capsys):
    db = str(tmp_path / "store.sqlite")
    _setup(Store(db=db))
    argv = ["close-task", "alpha", "0", "--check", f"{PYTEST}=pass:ok", "--check", f"{RUFF}=pass",
            "--review", "fine", "--review-by", "codex", "--reason", "evidence", "--reviewer", "me",
            "--worker", "writer", "--db", db]
    assert main(argv) == 0
    assert "task 0 marked done" in capsys.readouterr().out
    assert main(argv) == 0
    assert "already done" in capsys.readouterr().out


def test_cli_refusal_exits_one(tmp_path, capsys):
    db = str(tmp_path / "store.sqlite")
    _setup(Store(db=db))
    argv = ["close-task", "alpha", "0", "--check", f"{PYTEST}=pass", "--review", "fine",
            "--reason", "evidence", "--reviewer", "me", "--worker", "writer", "--db", db]
    assert main(argv) == 1
    assert "missing required check" in capsys.readouterr().err


def test_cli_refuses_without_a_store(monkeypatch, capsys):
    monkeypatch.delenv("LAZYTOOLS_PROJECTS_STORE_DB", raising=False)
    monkeypatch.delenv("LAZYCEO_CEO_STORE_DB", raising=False)
    assert main(["close-task", "alpha", "0", "--reason", "r", "--reviewer", "me"]) == 2
    assert "no store" in capsys.readouterr().err


# ---- MCP -------------------------------------------------------------------


def test_mcp_close_task_is_write_gated_and_closes(tmp_path):
    from lazytools.connectors.projects import ProjectsTools

    db = str(tmp_path / "store.sqlite")
    _setup(Store(db=db))
    assert "projects_close_task" not in {t.name for t in ProjectsTools(store_db=db).as_tools()}
    pt = ProjectsTools(store_db=db, allow_write=True)
    assert "projects_close_task" in {t.name for t in pt.as_tools()}
    assert pt.projects_close_task("alpha", 0, [{"command": PYTEST}], "r", "me").startswith("REJECTED: checks[0]")
    result = pt.projects_close_task(
        "alpha", 0, [{"command": PYTEST, "passed": True, "detail": "ok"}, f"{RUFF}=pass"], "evidence", "me", review="fine",
        review_by="codex", worker="writer",
    )
    assert result.startswith("task 0 marked done"), result


def test_close_task_is_dropped_from_a_read_only_expansion(tmp_path):
    from lazytools.connectors.projects import ProjectsTools
    from lazytools.mcp_server.server import expand_tools

    db = str(tmp_path / "store.sqlite")
    _setup(Store(db=db))
    provider = ProjectsTools(store_db=db, allow_write=True)
    assert "projects_close_task" in {t.name for t in provider.as_tools()}
    assert "projects_close_task" not in expand_tools([provider], read_only=True)
    assert "projects_close_task" in expand_tools([provider], read_only=False)
