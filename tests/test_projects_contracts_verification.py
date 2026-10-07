"""lazytools.projects.contracts / .verification -- acceptance contracts and the
verification state machine, including the injectable policy hooks that
replace LazyCEO's own autonomy/contract-adequacy checks."""

from __future__ import annotations

import pytest
from lazybridge import Store

from lazytools.projects import contracts, verification


def _open_contract(store: Store, **overrides) -> contracts.TaskContract:
    kwargs = dict(
        project_id="alpha",
        task_index=0,
        repo="some-repo",
        observable_result="a thing happens",
        acceptance_criteria=["pytest tests/test_x.py passes"],
        required_checks=["pytest -q"],
        risk="low",
        allowed_effects="write files in some-repo",
    )
    kwargs.update(overrides)
    return contracts.open_task_contract(store, **kwargs)


def test_open_task_contract_requires_nonblank_fields() -> None:
    store = Store()
    with pytest.raises(ValueError, match="repo"):
        _open_contract(store, repo="  ")
    with pytest.raises(ValueError, match="acceptance_criteria"):
        _open_contract(store, acceptance_criteria=[])


def test_open_task_contract_cannot_waive_review_on_high_risk() -> None:
    store = Store()
    with pytest.raises(ValueError, match="cannot be waived"):
        _open_contract(store, risk="high", requires_review=False, review_waiver="trust me")


def test_open_task_contract_waiver_requires_reason() -> None:
    store = Store()
    with pytest.raises(ValueError, match="review_waiver"):
        _open_contract(store, requires_review=False)
    contract = _open_contract(store, requires_review=False, review_waiver="trivial doc fix")
    assert contract.requires_review is False
    assert contract.review_waiver == "trivial doc fix"


def test_find_contract_for_task_picks_most_recent() -> None:
    store = Store()
    first = _open_contract(store)
    second = _open_contract(store, observable_result="a different thing")
    found = contracts.find_contract_for_task(store, "alpha", 0)
    assert found is not None
    assert found.contract_id in (first.contract_id, second.contract_id)
    assert found.created_at >= first.created_at


def test_repos_for_project() -> None:
    store = Store()
    _open_contract(store, repo="repo-a")
    _open_contract(store, task_index=1, repo="repo-b")
    assert contracts.repos_for_project(store, "alpha") == {"repo-a", "repo-b"}


def test_contract_requires_full_suite_and_exclusion_flag_detection() -> None:
    contract = contracts.TaskContract(
        contract_id="c1", project_id="p", task_index=0, repo="r",
        observable_result="run the full suite", acceptance_criteria=["x"], required_checks=["pytest -q"],
        risk="low", allowed_effects="x", created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    assert contracts.contract_requires_full_suite(contract) is True
    assert contracts.check_uses_exclusion_flag("pytest -q --deselect tests/test_x.py") is True
    assert contracts.check_uses_exclusion_flag("pytest -q") is False


def _verification_flow(store: Store, contract: contracts.TaskContract, job_id: str = "job12345678") -> None:
    assert verification.claim_verification(store, contract_id=contract.contract_id, job_id=job_id) is not None
    assert verification.start_running(store, job_id).status == "running"
    verification.record_checks(
        store, job_id, checks=[verification.CheckResult(command="pytest -q", exit_code=0, output_tail="ok")], diff_summary="+1"
    )
    verification.record_review(store, job_id, reviewer="claude", findings="looks fine")


def test_accept_requires_checks_present() -> None:
    store = Store()
    contract = _open_contract(store)
    verification.claim_verification(store, contract_id=contract.contract_id, job_id="job12345678")
    verification.start_running(store, "job12345678")
    with pytest.raises(ValueError, match="no checks"):
        verification.accept(store, "job12345678", reviewer="claude", reason="x")


def test_accept_requires_review_when_contract_requires_it() -> None:
    store = Store()
    contract = _open_contract(store)
    verification.claim_verification(store, contract_id=contract.contract_id, job_id="job12345678")
    verification.start_running(store, "job12345678")
    verification.record_checks(store, "job12345678", checks=[verification.CheckResult(command="pytest -q", exit_code=0, output_tail="ok")], diff_summary=None)
    with pytest.raises(ValueError, match="independent review"):
        verification.accept(store, "job12345678", reviewer="claude", reason="x")


def test_accept_fences_against_evidence_changed_after_validation() -> None:
    """A concurrent writer that swaps a passing check for a failing one between
    accept()'s own validation read and its final CAS must lose the race, even
    when the caller passed no ``expected`` of its own -- accept() must fence its
    write against the exact snapshot it validated, not whatever ``_transition``
    happens to re-read later. Found by Codex review before this ever shipped."""
    store = Store()
    contract = _open_contract(store)
    _verification_flow(store, contract)

    key = verification._verification_key("job12345678")
    original_read = store.read
    calls = {"n": 0}

    def racing_read(k, default=None):
        value = original_read(k, default)
        if k == key:
            calls["n"] += 1
            if calls["n"] == 1:
                # accept()'s OWN validation read just happened (it saw a passing
                # check). Simulate another writer landing a failing check in the
                # gap before _transition's separate, later read of the same key.
                verification.record_checks(
                    store, "job12345678",
                    checks=[verification.CheckResult(command="pytest -q", exit_code=1, output_tail="FAILED")],
                    diff_summary=None,
                )
        return value

    store.read = racing_read  # type: ignore[method-assign]
    result = verification.accept(store, "job12345678", reviewer="claude", reason="all good")
    assert result is None  # fenced -- must NOT have accepted the stale, already-passing snapshot

    store.read = original_read  # type: ignore[method-assign]
    # the record now genuinely has a failing check: a fresh accept() refuses normally
    with pytest.raises(ValueError, match="did not pass"):
        verification.accept(store, "job12345678", reviewer="claude", reason="x")


def test_accept_refuses_failed_check() -> None:
    store = Store()
    contract = _open_contract(store)
    verification.claim_verification(store, contract_id=contract.contract_id, job_id="job12345678")
    verification.start_running(store, "job12345678")
    verification.record_checks(store, "job12345678", checks=[verification.CheckResult(command="pytest -q", exit_code=1, output_tail="FAILED")], diff_summary=None)
    verification.record_review(store, "job12345678", reviewer="claude", findings="fine")
    with pytest.raises(ValueError, match="did not pass"):
        verification.accept(store, "job12345678", reviewer="claude", reason="x")


def test_accept_succeeds_on_full_evidence() -> None:
    store = Store()
    contract = _open_contract(store)
    _verification_flow(store, contract)
    result = verification.accept(store, "job12345678", reviewer="claude", reason="all good")
    assert result is not None
    assert result.status == "accepted"


def test_accept_honours_authorization_check_hook() -> None:
    store = Store()
    contract = _open_contract(store)
    _verification_flow(store, contract)

    def deny(_store: Store, _contract: contracts.TaskContract) -> str | None:
        return "REJECTED: this project is at a supervised autonomy level"

    with pytest.raises(ValueError, match="supervised autonomy"):
        verification.accept(store, "job12345678", reviewer="claude", reason="x", authorization_check=deny)

    # the default (no hook) still works on the same record
    result = verification.accept(store, "job12345678", reviewer="claude", reason="x")
    assert result is not None and result.status == "accepted"


def test_empty_scope_review_blocks_accept_and_retry_review_allows_rerun() -> None:
    store = Store()
    contract = _open_contract(store)
    verification.claim_verification(store, contract_id=contract.contract_id, job_id="job12345678")
    verification.start_running(store, "job12345678")
    verification.record_checks(store, "job12345678", checks=[verification.CheckResult(command="pytest -q", exit_code=0, output_tail="ok")], diff_summary=None)
    verification.record_review(store, "job12345678", reviewer="claude", findings="nothing to review", empty_scope=True)
    with pytest.raises(ValueError, match="EMPTY diff"):
        verification.accept(store, "job12345678", reviewer="claude", reason="x")

    retried = verification.retry_review(store, "job12345678")
    assert retried is not None and retried.status == "pending" and retried.review is None


def test_reopen_for_empty_review_invalidates_accepted_review() -> None:
    store = Store()
    contract = _open_contract(store)
    _verification_flow(store, contract)
    verification.accept(store, "job12345678", reviewer="claude", reason="x")
    reopened = verification.reopen_for_empty_review(store, "job12345678")
    assert reopened is not None
    assert reopened.status == "pending"
    assert reopened.review is None
    assert len(reopened.superseded_reviews) == 1


def test_request_rework_and_block() -> None:
    store = Store()
    contract = _open_contract(store)
    _verification_flow(store, contract)
    reworked = verification.request_rework(store, "job12345678", reviewer="claude", reason="not quite")
    assert reworked.status == "rework"

    store2 = Store()
    contract2 = _open_contract(store2)
    _verification_flow(store2, contract2)
    blocked = verification.block(store2, "job12345678", reviewer="claude", reason="needs a human")
    assert blocked.status == "blocked"


def test_retry_harness_only_applies_to_harness_blocked() -> None:
    store = Store()
    contract = _open_contract(store)
    _verification_flow(store, contract)
    verification.block(store, "job12345678", reviewer="claude", reason="a human decision")
    with pytest.raises(ValueError, match="not a retry"):
        verification.retry_harness(store, "job12345678")


def test_get_verification_prefix_lookup_requires_unambiguous_match() -> None:
    store = Store()
    contract = _open_contract(store)
    verification.claim_verification(store, contract_id=contract.contract_id, job_id="job1234567890")
    assert verification.get_verification(store, "job12345678") is not None
    assert verification.get_verification(store, "short") is None  # too short
    assert verification.get_verification(store, "") is None


def test_reclaim_interrupted_verifications_resets_running_to_pending() -> None:
    store = Store()
    contract = _open_contract(store)
    verification.claim_verification(store, contract_id=contract.contract_id, job_id="job12345678")
    verification.start_running(store, "job12345678")
    reclaimed = verification.reclaim_interrupted_verifications(store)
    assert "job12345678" in reclaimed
    assert verification.get_verification(store, "job12345678").status == "pending"


def test_claim_blocker_contract_adequacy_hook() -> None:
    store = Store()
    contract = _open_contract(store, risk="high")

    class FakeProject:
        project_id = "alpha"
        risk = "high"

    tasks = [{"status": "todo", "attempts": 0}]
    index, reason = verification.claim_blocker(
        store, project=FakeProject(), tasks=tasks, lease_seconds=900.0, max_attempts=3, now=0.0,
        contract_adequate=lambda _c, _r: "no required_checks listed",
    )
    assert index == 0
    assert reason is not None and "not enough" in reason

    index2, reason2 = verification.claim_blocker(
        store, project=FakeProject(), tasks=tasks, lease_seconds=900.0, max_attempts=3, now=0.0,
        contract_adequate=lambda _c, _r: None,
    )
    assert index2 == 0 and reason2 is None
    assert contract.risk == "high"


def test_claim_blocker_no_eligible_tasks() -> None:
    store = Store()

    class FakeProject:
        project_id = "alpha"
        risk = "low"

    tasks = [{"status": "done"}]
    index, reason = verification.claim_blocker(store, project=FakeProject(), tasks=tasks, lease_seconds=900.0, max_attempts=3, now=0.0)
    assert index is None and reason is None
