"""Durable task-verification loop: checks run, a review is attached, and a
project task is not marked done until a verification is explicitly accepted.

Ported from ``lazyceo.verification`` (the ``Verification`` state machine;
``TaskContract`` itself is ``contracts.py``), mechanism only.

Left behind, as CEO policy, replaced with injectable hooks:

- ``accept``'s ``authorization_check`` -- LazyCEO's own
  ``_refuse_if_not_the_ceos_to_accept`` reads its per-project autonomy level
  (``lazyceo.project_autonomy``/``lazyceo.boost``) to decide whether the
  CALLER (as opposed to the operator) may accept a verification at all. That
  is CEO policy end to end; this module takes a plain
  ``Callable[[Store, TaskContract], str | None]`` instead -- ``None`` means
  "allowed", a string is the refusal text raised as ``ValueError``. The
  default allows everything, i.e. no autonomy gate, which is the right
  default for a Claude Code session accepting its OWN project's work.
- ``claim_blocker``'s high-risk-contract-inadequacy check -- LazyCEO's
  ``_contract_inadequate_for`` (``lazyceo.simple.agent``) encodes its own
  per-project-risk contract-completeness rules. Replaced with an injectable
  ``contract_adequate: Callable[[TaskContract, str], str | None]``, same
  shape (``None`` = adequate).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from lazytools.projects.contracts import (
    TaskContract,
    check_satisfies_required,
    check_uses_exclusion_flag,
    contract_requires_full_suite,
    find_contract_for_task,
    get_task_contract,
)
from lazytools.projects.keys import VERIFICATION_PREFIX

VerificationStatus = Literal["pending", "running", "accepted", "rework", "blocked"]


class CheckResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    command: str
    exit_code: int
    output_tail: str
    #: "passed" (exit 0), "check_failed" (ran, exit != 0), or "harness_error"
    #: (could not even run -- says nothing about the work).
    outcome: Literal["passed", "check_failed", "harness_error"] = "passed"

    @model_validator(mode="before")
    @classmethod
    def _infer_outcome_for_legacy_records(cls, data: Any) -> Any:
        if isinstance(data, dict) and "outcome" not in data:
            exit_code = data.get("exit_code")
            data = {**data, "outcome": "passed" if exit_code == 0 else "check_failed"}
        return data


#: How a review that could not run announces itself in its own findings.
REVIEW_NOT_PERFORMED = "REVIEW NOT PERFORMED"

#: LazyTools' own literal failure-banner prefixes (``codex_code_review``/``claude_code_review``
#: returning their own failure as ordinary findings text, on an un-upgraded install).
LEGACY_REVIEW_FAILURE_PREFIXES = ("[codex_code_review] failed", "[claude_code_review] failed")


class ReviewRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    reviewer: str
    findings: str
    recorded_at: datetime
    #: False when the reviewer could not be reached at all.
    performed: bool = True
    #: True when the base..head range this review ran against showed NO
    #: change at all -- computed by ``diff_is_empty`` from git, never from
    #: the reviewer's own prose.
    empty_scope: bool = False

    @model_validator(mode="before")
    @classmethod
    def _infer_performed_for_legacy_records(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        findings = str(data.get("findings", ""))
        if "performed" not in data:
            failed = findings.startswith(REVIEW_NOT_PERFORMED) or findings.startswith(LEGACY_REVIEW_FAILURE_PREFIXES)
            return {**data, "performed": not failed}
        if data.get("performed") and findings.startswith(LEGACY_REVIEW_FAILURE_PREFIXES):
            return {**data, "performed": False}
        return data


class Verification(BaseModel):
    model_config = ConfigDict(extra="allow")

    verification_id: str
    contract_id: str
    job_id: str
    status: VerificationStatus
    checks: list[CheckResult] = []
    #: Attached by the reconciler, never by the agent whose work is being judged.
    review: ReviewRecord | None = None
    #: Reviews this record has SUPERSEDED -- audit only, never read by any gate.
    superseded_reviews: list[ReviewRecord] = []
    diff_summary: str | None = None
    reviewer: str | None = None
    decision_reason: str | None = None
    created_at: datetime
    decided_at: datetime | None = None
    #: Stamped by ``reopen_for_empty_review`` -- ``accept``'s gate refuses any
    #: ``review`` recorded strictly before this timestamp.
    reopened_at: datetime | None = None


def _verification_key(job_id: str, *, prefix: str = VERIFICATION_PREFIX) -> str:
    return f"{prefix}{job_id}"


def claim_verification(
    store: Any, *, contract_id: str, job_id: str, prefix: str = VERIFICATION_PREFIX
) -> Verification | None:
    """Create a Verification for this finished job, but only if nothing already has."""
    verification = Verification(
        verification_id=job_id,
        contract_id=contract_id,
        job_id=job_id,
        status="pending",
        created_at=datetime.now(UTC),
    )
    if not store.compare_and_swap(_verification_key(job_id, prefix=prefix), None, verification.model_dump(mode="json")):
        return None
    return verification


#: A job listing prints exactly an 8-char job_id prefix; anything shorter
#: (including "") is malformed input, not a real short id.
_MIN_JOB_ID_PREFIX = 8


def get_verification(store: Any, job_id: str, *, prefix: str = VERIFICATION_PREFIX) -> Verification | None:
    """The verification for ``job_id`` -- an exact match, or an unambiguous prefix match.

    An undersized or ambiguous PREFIX returns None rather than guessing: this
    backs decision tools that MUTATE whatever record it returns.
    """
    if not job_id:
        return None
    exact = store.read(_verification_key(job_id, prefix=prefix))
    if isinstance(exact, dict):
        return Verification.model_validate(exact)
    if len(job_id) < _MIN_JOB_ID_PREFIX:
        return None
    matches = [
        raw
        for _key, raw in store.items(prefix=prefix)
        if isinstance(raw, dict) and str(raw.get("job_id", "")).startswith(job_id)
    ]
    if len(matches) != 1:
        return None
    return Verification.model_validate(matches[0])


def get_verification_for_contract(
    store: Any, contract_id: str, *, prefix: str = VERIFICATION_PREFIX
) -> Verification | None:
    """The most recent verification for a contract's latest attempt."""
    candidates = [
        Verification.model_validate(raw)
        for _key, raw in store.items(prefix=prefix)
        if isinstance(raw, dict) and raw.get("contract_id") == contract_id
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda v: v.created_at)


def _transition(
    store: Any,
    job_id: str,
    *,
    from_status: tuple[str, ...],
    updates: dict[str, object],
    expected: Verification | None = None,
    prefix: str = VERIFICATION_PREFIX,
) -> Verification | None:
    """Compare-and-swap one field change, guarded by the current status."""
    raw = store.read(_verification_key(job_id, prefix=prefix))
    if not isinstance(raw, dict):
        return None
    verification = Verification.model_validate(raw)
    if verification.status not in from_status:
        return None
    if expected is not None and verification != expected:
        return None
    updated = verification.model_copy(update=updates)
    if not store.compare_and_swap(_verification_key(job_id, prefix=prefix), raw, updated.model_dump(mode="json")):
        return None
    return updated


def start_running(store: Any, job_id: str, *, prefix: str = VERIFICATION_PREFIX) -> Verification | None:
    """pending -> running, discarding the evidence of any EARLIER run (kept in superseded_reviews)."""
    raw = store.read(_verification_key(job_id, prefix=prefix))
    if not isinstance(raw, dict):
        return None
    current = Verification.model_validate(raw)
    superseded = [*current.superseded_reviews, current.review] if current.review is not None else None
    updates: dict[str, Any] = {"status": "running", "review": None, "checks": []}
    if superseded is not None:
        updates["superseded_reviews"] = superseded
    return _transition(store, job_id, from_status=("pending",), updates=updates, expected=current, prefix=prefix)


def record_checks(
    store: Any, job_id: str, *, checks: list[CheckResult], diff_summary: str | None, prefix: str = VERIFICATION_PREFIX
) -> Verification | None:
    """Attach the automatic-check results and diff. Does NOT change status."""
    return _transition(
        store,
        job_id,
        from_status=("running",),
        updates={"checks": checks, "diff_summary": diff_summary},
        prefix=prefix,
    )


def record_review(
    store: Any,
    job_id: str,
    *,
    reviewer: str,
    findings: str,
    performed: bool = True,
    empty_scope: bool = False,
    prefix: str = VERIFICATION_PREFIX,
) -> Verification | None:
    """Attach an independent review's own words to this attempt. Does NOT change status."""
    return _transition(
        store,
        job_id,
        from_status=("running",),
        updates={
            "review": ReviewRecord(
                reviewer=reviewer,
                findings=findings,
                recorded_at=datetime.now(UTC),
                performed=performed,
                empty_scope=empty_scope,
            )
        },
        prefix=prefix,
    )


def retry_review(
    store: Any, job_id: str, *, expected: Verification | None = None, prefix: str = VERIFICATION_PREFIX
) -> Verification | None:
    """Send an attempt back for its checks and review to be run again.

    Only when the recorded review genuinely did not run or ran against an
    empty scope -- not a way to re-roll a review that genuinely looked at
    the work and said something.
    """
    raw = store.read(_verification_key(job_id, prefix=prefix))
    if not isinstance(raw, dict):
        return None
    current = Verification.model_validate(raw)
    if current.review is None or (current.review.performed and not current.review.empty_scope):
        raise ValueError(
            "retry_review only applies to an attempt whose review either could not be run at all, or ran "
            "against an empty diff. This one has a real, non-empty review recorded -- disagreeing with its "
            "findings is a decision (accept, request_rework or block), not a retry."
        )
    return _transition(
        store,
        job_id,
        from_status=("running",),
        updates={"status": "pending", "review": None},
        expected=expected,
        prefix=prefix,
    )


def retry_harness(
    store: Any, job_id: str, *, expected: Verification | None = None, prefix: str = VERIFICATION_PREFIX
) -> Verification | None:
    """Send a HARNESS-blocked attempt's evidence back to be re-checked.

    Only applies to a record blocked for an environment reason
    (``decision_reason`` starting with ``"HARNESS:"``), never to re-roll a
    human's deliberate ``block``.
    """
    raw = store.read(_verification_key(job_id, prefix=prefix))
    if not isinstance(raw, dict):
        return None
    current = Verification.model_validate(raw)
    if current.status != "blocked" or not (current.decision_reason or "").startswith("HARNESS:"):
        raise ValueError(
            "retry_harness only applies to an attempt blocked by a harness/environment failure (its "
            "decision_reason starts with 'HARNESS:'). This one is not -- that decision is not a retry."
        )
    return _transition(
        store,
        job_id,
        from_status=("blocked",),
        updates={"status": "pending", "reviewer": None, "decision_reason": None, "decided_at": None},
        expected=expected,
        prefix=prefix,
    )


def reopen_for_empty_review(
    store: Any, job_id: str, *, expected: Verification | None = None, prefix: str = VERIFICATION_PREFIX
) -> Verification | None:
    """Send an ACCEPTED verification back to 'pending' because, from git, its
    work was confirmed to have been reviewed against an empty diff.

    Only transitions FROM "accepted". INVALIDATES ``review`` outright
    (moved to ``superseded_reviews``), rather than merely relabeling it, so
    the very next reconciler tick gets a genuinely fresh review.
    """
    raw = store.read(_verification_key(job_id, prefix=prefix))
    if not isinstance(raw, dict):
        return None
    current = Verification.model_validate(raw)
    if current.status != "accepted":
        return None
    if expected is not None and current != expected:
        return None
    superseded = [*current.superseded_reviews]
    if current.review is not None:
        superseded.append(current.review)
    updated = current.model_copy(
        update={
            "status": "pending",
            "reviewer": None,
            "decision_reason": None,
            "decided_at": None,
            "review": None,
            "superseded_reviews": superseded,
            "reopened_at": datetime.now(UTC),
        }
    )
    if not store.compare_and_swap(_verification_key(job_id, prefix=prefix), raw, updated.model_dump(mode="json")):
        return None
    return updated


#: accept()'s authorization hook. None means "allowed" (no autonomy gate) --
#: the right default here, since LazyCEO's own per-project autonomy levels
#: are policy this package does not know. See the module docstring.
AuthorizationCheck = Callable[[Any, TaskContract], "str | None"]


def accept(
    store: Any,
    job_id: str,
    *,
    reviewer: str,
    reason: str,
    expected: Verification | None = None,
    authorization_check: AuthorizationCheck | None = None,
    prefix: str = VERIFICATION_PREFIX,
) -> Verification | None:
    """Accept an attempt -- only once its checks have actually run and all passed.

    Raises ``ValueError`` (distinct from the ``None`` every other outcome
    here uses for "lost a race / wrong state") when the evidence does not
    support acceptance.
    """
    raw = store.read(_verification_key(job_id, prefix=prefix))
    if authorization_check is not None and isinstance(raw, dict):
        contract_id = str(raw.get("contract_id", ""))
        contract_for_auth = get_task_contract(store, contract_id)
        if contract_for_auth is not None:
            refusal = authorization_check(store, contract_for_auth)
            if refusal is not None:
                raise ValueError(refusal)
    validated: Verification | None = None
    if isinstance(raw, dict) and raw.get("status") == "running":
        current = Verification.model_validate(raw)
        validated = current
        contract = get_task_contract(store, current.contract_id)
        if not current.checks:
            missing_text = "" if contract is None else " Missing required checks: " + ", ".join(repr(command) for command in contract.required_checks)
            raise ValueError(
                "no checks have been recorded for this attempt yet -- accepting now would close the task "
                "with no evidence at all. Wait for the required_checks to run." + missing_text
            )
        if contract is not None and contract.requires_review:
            if current.review is None:
                raise ValueError("this contract requires an independent review and none has been recorded yet.")
            if current.reopened_at is not None and current.review.recorded_at < current.reopened_at:
                raise ValueError(
                    "the recorded review predates this verification's most recent reopen -- it says nothing "
                    "about the state being judged now. Wait for a fresh review, or retry_review."
                )
            if not current.review.performed:
                raise ValueError(
                    "the independent review did not actually run -- the recorded findings only say so. "
                    "Fix the reviewer and retry_review, or block to bring in a human."
                )
            if current.review.empty_scope:
                raise ValueError(
                    "the independent review's base and head produced an EMPTY diff (confirmed from git) -- "
                    "nothing was actually reviewed, regardless of what its findings say."
                )

        failed = [c for c in current.checks if c.outcome != "passed"]
        if failed:
            commands = ", ".join(f"{c.command!r} (exit {c.exit_code})" for c in failed)
            raise ValueError(
                f"{len(failed)} required check(s) did not pass: {commands}. "
                "Use request_rework to send it back, or block to escalate."
            )
        if contract is not None:
            missing = [
                command for command in contract.required_checks
                if not any(check_satisfies_required(c.command, command, contract.allowed_check_exclusions) for c in current.checks)
            ]
            if missing:
                raise ValueError("missing required checks: " + ", ".join(repr(command) for command in missing))
        if contract is not None and contract_requires_full_suite(contract):
            narrowed = [c.command for c in current.checks if check_uses_exclusion_flag(c.command)]
            unexplained = [cmd for cmd in narrowed if cmd not in contract.allowed_check_exclusions]
            if unexplained:
                commands = ", ".join(repr(cmd) for cmd in unexplained)
                raise ValueError(
                    f"this contract promises a FULL suite, but {len(unexplained)} required check(s) used a "
                    f"--deselect/-k/--ignore flag that was never listed in allowed_check_exclusions: {commands}."
                )
    # Fence the write against exactly the snapshot that was just judged: with no
    # ``expected`` of its own, a caller otherwise accepts whatever the record
    # happens to be AT THE TRANSITION, not what it validated above -- a concurrent
    # writer could swap a passing check for a failing one (or clear the review) in
    # the gap between this function's own read and _transition's, and the CAS would
    # still succeed because it only checks status, never content. Found by Codex
    # review before this ever shipped.
    return _transition(
        store,
        job_id,
        from_status=("running",),
        updates={
            "status": "accepted",
            "reviewer": reviewer,
            "decision_reason": reason,
            "decided_at": datetime.now(UTC),
        },
        expected=expected if expected is not None else validated,
        prefix=prefix,
    )


def request_rework(
    store: Any,
    job_id: str,
    *,
    reviewer: str,
    reason: str,
    expected: Verification | None = None,
    prefix: str = VERIFICATION_PREFIX,
) -> Verification | None:
    return _transition(
        store,
        job_id,
        from_status=("running",),
        updates={"status": "rework", "reviewer": reviewer, "decision_reason": reason, "decided_at": datetime.now(UTC)},
        expected=expected,
        prefix=prefix,
    )


def block(
    store: Any,
    job_id: str,
    *,
    reviewer: str,
    reason: str,
    expected: Verification | None = None,
    prefix: str = VERIFICATION_PREFIX,
) -> Verification | None:
    return _transition(
        store,
        job_id,
        from_status=("running",),
        updates={"status": "blocked", "reviewer": reviewer, "decision_reason": reason, "decided_at": datetime.now(UTC)},
        expected=expected,
        prefix=prefix,
    )


def reclaim_interrupted_verifications(store: Any, *, prefix: str = VERIFICATION_PREFIX) -> list[str]:
    """Reset every "running" verification to "pending" -- a restart orphans the coroutine
    that would have finished it, but rerunning the same checks/review is idempotent."""
    reclaimed = []
    for key, raw in store.items(prefix=prefix):
        if (
            isinstance(raw, dict)
            and raw.get("status") == "running"
            and store.compare_and_swap(key, raw, {**raw, "status": "pending"})
        ):
            reclaimed.append(raw.get("verification_id", key))
    return reclaimed


#: claim_blocker()'s contract-adequacy hook, for a high-risk project. None
#: means "adequate" (no extra requirement) -- see the module docstring.
ContractAdequacyCheck = Callable[[TaskContract, str], "str | None"]


def claim_blocker(
    store: Any,
    *,
    project: Any,
    tasks: list[dict],
    lease_seconds: float,
    max_attempts: int,
    now: float,
    contract_adequate: ContractAdequacyCheck | None = None,
) -> tuple[int | None, str | None]:
    """The task a claim would take, and why it cannot be taken.

    Returns ``(index, reason)``: ``reason`` is None when the claim would
    succeed; ``index`` is None when there is nothing to claim at all.
    """
    eligible = [
        (index, task)
        for index, task in enumerate(tasks)
        if (
            task["status"] == "todo"
            or (
                task["status"] == "claimed"
                and task.get("claimed_at") is not None
                and now - float(task["claimed_at"]) > lease_seconds
            )
        )
        and task.get("attempts", 0) < max_attempts
    ]
    if not eligible:
        return None, None
    index, _task = eligible[0]
    if getattr(project, "risk", None) != "high" or contract_adequate is None:
        return index, None
    existing = find_contract_for_task(store, project.project_id, index)
    if existing is None:
        return index, f"task {index} requires an acceptance contract that does not exist"
    inadequate = contract_adequate(existing, project.risk)
    if inadequate is not None:
        return index, f"task {index}'s contract is not enough for a high-risk project: {inadequate}"
    return index, None


__all__ = [
    "AuthorizationCheck",
    "CheckResult",
    "ContractAdequacyCheck",
    "LEGACY_REVIEW_FAILURE_PREFIXES",
    "REVIEW_NOT_PERFORMED",
    "ReviewRecord",
    "Verification",
    "VerificationStatus",
    "accept",
    "block",
    "claim_blocker",
    "claim_verification",
    "get_verification",
    "get_verification_for_contract",
    "reclaim_interrupted_verifications",
    "record_checks",
    "record_review",
    "reopen_for_empty_review",
    "request_rework",
    "retry_harness",
    "retry_review",
    "start_running",
]
