"""Close a contract-backed project task with evidence, in one call.

A task that carries an acceptance contract cannot take the shortcut
(``task_claims.complete_todo_without_verification`` refuses it). Closing it
otherwise takes six calls in a fixed order: ``verification.claim_verification``
-> ``start_running`` -> ``record_checks`` -> ``record_review`` -> ``accept``,
then ``task_claims.close_accepted_task``. :func:`close_task_with_evidence`
is that sequence, with three guarantees the hand-written version lacks:

* **Validate before claiming.** Every refusal the evidence alone can predict
  (a required check not covered, a check that failed, a missing review the
  contract requires, a narrowed suite the contract does not allow, a task or
  project in the wrong state) is returned BEFORE any verification record is
  written, so a refused call leaves the Store untouched.
* **Never half-claimed.** If something still goes wrong once the verification
  exists (a concurrent writer, an unexpected exception), the record is moved
  to a terminal state: ``request_rework`` when ``accept`` judged the evidence
  insufficient, ``block`` otherwise (a ``pending`` record is first moved to
  ``running``, the only state either decision leaves from). Nothing is left
  ``pending``/``running`` for a reconciler to pick up as live work.
* **Idempotent.** A task that is already ``done`` is a no-op. A verification
  that was accepted for this contract but never consumed by a
  ``closed_accepted`` board event (a crash between ``accept`` and the board
  write) is reused to finish the close, rather than claiming a second one.

The checks are the caller's own evidence -- this module runs no command. A
check's ``command`` must match a contract ``required_check`` exactly (or be one
of the contract's ``allowed_check_exclusions`` narrowing it), the same rule
``verification.accept`` applies.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from lazytools.projects import contracts as _contracts
from lazytools.projects import verification as _verification
from lazytools.projects.keys import BOARD_KEY_PREFIX
from lazytools.projects.records import get_project
from lazytools.projects.task_claims import close_accepted_task

if TYPE_CHECKING:
    from lazybridge import Store

#: One piece of evidence: (command, passed, detail).
CheckEvidence = tuple[str, bool, str]

#: Prefix of every job id this module creates, so manual closes are easy to
#: tell apart from delegated jobs in a verification listing.
MANUAL_JOB_PREFIX = "manual-"


def _board(store: Store, project_id: str) -> dict[str, Any] | None:
    raw = store.read(f"{BOARD_KEY_PREFIX}project:{project_id}")
    return raw if isinstance(raw, dict) else None


def _closing_event(board: dict[str, Any], task_index: int) -> dict[str, Any] | None:
    events = board.get("plan_events", [])
    if not isinstance(events, list):
        return None
    closes = [
        e for e in events if isinstance(e, dict) and e.get("event") == "closed_accepted" and e.get("task_index") == task_index
    ]
    return closes[-1] if closes else None


def _verification_consumed(board: dict[str, Any], verification_id: str) -> bool:
    events = board.get("plan_events", [])
    return isinstance(events, list) and any(
        isinstance(e, dict) and e.get("event") == "closed_accepted" and e.get("verification_id") == verification_id
        for e in events
    )


def _review_performed(review: str) -> bool:
    """False when the findings announce, in the house sentinel wording, that no review ran."""
    findings = review.strip()
    return not findings.startswith((_verification.REVIEW_NOT_PERFORMED, *_verification.LEGACY_REVIEW_FAILURE_PREFIXES))


def _evidence_refusal(contract: _contracts.TaskContract, checks: Sequence[CheckEvidence], review: str) -> str | None:
    """What ``verification.accept`` would refuse on, judged from the evidence alone."""
    if not checks:
        expected = "\n".join(f"  {command}" for command in contract.required_checks)
        return f"REJECTED: no checks given. The contract requires every one of these commands, exactly:\n{expected}"
    blank = [i for i, (command, _passed, _detail) in enumerate(checks) if not str(command).strip()]
    if blank:
        return f"REJECTED: check(s) at position {', '.join(map(str, blank))} have a blank command."
    commands = [str(command) for command, _passed, _detail in checks]
    missing = [
        required
        for required in contract.required_checks
        if not any(
            _contracts.check_satisfies_required(command, required, contract.allowed_check_exclusions) for command in commands
        )
    ]
    if missing:
        listed = ", ".join(repr(command) for command in missing)
        expected = "\n".join(f"  {command}" for command in contract.required_checks)
        return (
            f"REJECTED: missing required check(s): {listed}. A check's command must match the contract's "
            f"required_checks string exactly. Expected commands:\n{expected}\nGiven: "
            + ", ".join(repr(command) for command in commands)
        )
    failed = [command for command, passed, _detail in checks if not passed]
    if failed:
        return (
            "REJECTED: check(s) did not pass: "
            + ", ".join(repr(command) for command in failed)
            + ". A task closes only on passing evidence -- fix the work and rerun them."
        )
    if contract.requires_review and not review.strip():
        return "REJECTED: this contract requires an independent review -- pass its findings as review."
    if contract.requires_review and not _review_performed(review):
        return (
            "REJECTED: the review says it was not performed -- this contract requires an independent "
            "review that actually ran."
        )
    if _contracts.contract_requires_full_suite(contract):
        narrowed = [c for c in commands if _contracts.check_uses_exclusion_flag(c) and c not in contract.allowed_check_exclusions]
        if narrowed:
            return (
                "REJECTED: this contract promises a FULL suite, but these check(s) narrow it with a "
                "--deselect/-k/--ignore flag not listed in allowed_check_exclusions: "
                + ", ".join(repr(c) for c in narrowed)
            )
    return None


def _abandon(store: Store, job_id: str, *, reviewer: str, reason: str, rework: bool) -> str:
    """Move a verification this call claimed to a terminal state. Returns what happened."""
    current = _verification.get_verification(store, job_id)
    if current is None:
        return "no verification record was left behind"
    if current.status in ("accepted", "rework", "blocked"):
        return f"verification {job_id!r} is {current.status!r}"
    if current.status == "pending" and _verification.start_running(store, job_id) is None:
        return f"verification {job_id!r} could not be moved out of 'pending' -- reconcile it by hand"
    decide = _verification.request_rework if rework else _verification.block
    if decide(store, job_id, reviewer=reviewer, reason=reason) is None:
        return f"verification {job_id!r} could not be closed out -- reconcile it by hand"
    return f"verification {job_id!r} recorded as {'rework' if rework else 'blocked'}"


def close_task_with_evidence(
    store: Store,
    project_id: str,
    task_index: int,
    *,
    checks: Sequence[CheckEvidence],
    review: str,
    reason: str,
    reviewer: str,
) -> str:
    """Close one contract-backed project task on the strength of ``checks`` and ``review``.

    ``checks`` is a list of ``(command, passed, detail)``: ``command`` must be a
    contract ``required_check`` string, ``detail`` is a short tail of its output.
    ``review`` is the independent review's findings, ``reason`` the decision
    rationale, ``reviewer`` who is accepting.

    Returns ``task_claims.close_accepted_task``'s ``"task N marked done: ..."``
    on success, ``"task N is already done ..."`` when there is nothing to do,
    and a ``"REJECTED: ..."`` message otherwise. See the module docstring for
    the validate-first / never-half-claimed / idempotent guarantees.
    """
    if isinstance(task_index, bool) or not isinstance(task_index, int):
        return "REJECTED: task_index must be an integer."
    if not reviewer.strip():
        return "REJECTED: reviewer must not be blank."
    if not reason.strip():
        return "REJECTED: reason must not be blank -- say why the evidence is enough to close the task."

    board = _board(store, project_id)
    tasks = board.get("tasks") if board is not None else None
    if board is None or not isinstance(tasks, list) or not tasks:
        return f"REJECTED: project {project_id!r} has no plan on its board."
    if not 0 <= task_index < len(tasks) or not isinstance(tasks[task_index], dict):
        return f"REJECTED: task_index out of range (valid: 0..{len(tasks) - 1})."
    task = tasks[task_index]
    status = task.get("status")
    if status == "done":
        event = _closing_event(board, task_index)
        by = f" (closed by verification {event.get('verification_id')!r})" if event else ""
        return f"task {task_index} is already done{by}; nothing to do."
    if status not in ("todo", "claimed"):
        return f"REJECTED: task {task_index} is {status!r}, not todo or claimed."

    contract = _contracts.find_contract_for_task(store, project_id, task_index)
    if contract is None:
        return (
            f"REJECTED: task {task_index} has no acceptance contract -- close it with "
            "task_claims.complete_todo_without_verification instead."
        )
    if task.get("contract_id") != contract.contract_id:
        return (
            f"REJECTED: task {task_index}'s board contract marker ({task.get('contract_id')!r}) does not match its "
            f"contract {contract.contract_id!r} -- run contract_markers.backfill_contract_markers(..., apply=True) first."
        )
    project = get_project(store, project_id)
    if project is None or project.status != "open":
        found = "missing" if project is None else repr(project.status)
        return f"REJECTED: project {project_id!r} must be open to close a task (it is {found})."

    latest = _verification.get_verification_for_contract(store, contract.contract_id)
    if latest is not None and latest.status == "accepted" and not _verification_consumed(board, latest.verification_id):
        # A previous call (or a delegated job) got as far as accept but never
        # closed the board: finish that close instead of claiming a second one.
        result = close_accepted_task(store, project_id, task_index, latest.verification_id)
        return result if result.startswith("REJECTED") else f"{result} (reused accepted verification {latest.verification_id!r})"
    if latest is not None and latest.status in ("pending", "running"):
        return (
            f"REJECTED: verification {latest.verification_id!r} for this contract is still {latest.status!r} -- "
            "let it finish, or block it, before closing the task by hand."
        )

    refusal = _evidence_refusal(contract, checks, review)
    if refusal is not None:
        return refusal

    job_id = f"{MANUAL_JOB_PREFIX}{project_id}-{task_index}-{uuid.uuid4().hex[:8]}"
    if _verification.claim_verification(store, contract_id=contract.contract_id, job_id=job_id) is None:
        return f"REJECTED: could not claim verification {job_id!r} (it already exists); nothing was written."

    results = [
        _verification.CheckResult(
            command=str(command),
            exit_code=0 if passed else 1,
            output_tail=str(detail),
            outcome="passed" if passed else "check_failed",
        )
        for command, passed, detail in checks
    ]
    try:
        if _verification.start_running(store, job_id) is None:
            raise _Lost("start_running")
        if _verification.record_checks(store, job_id, checks=results, diff_summary=None) is None:
            raise _Lost("record_checks")
        if review.strip() and _verification.record_review(
            store, job_id, reviewer=reviewer, findings=review, performed=_review_performed(review)
        ) is None:
            raise _Lost("record_review")
        try:
            accepted = _verification.accept(store, job_id, reviewer=reviewer, reason=reason)
        except ValueError as exc:
            outcome = _abandon(store, job_id, reviewer=reviewer, reason=f"accept refused: {exc}", rework=True)
            return f"REJECTED: {exc} ({outcome})."
        if accepted is None:
            raise _Lost("accept")
    except _Lost as lost:
        outcome = _abandon(
            store, job_id, reviewer=reviewer, reason=f"manual close aborted: {lost.step} lost a race with another writer", rework=False
        )
        return f"REJECTED: verification {job_id!r} changed underneath this call at {lost.step} ({outcome}); try again."
    except BaseException:
        _abandon(store, job_id, reviewer=reviewer, reason="manual close aborted: unexpected error while closing the task", rework=False)
        raise

    result = close_accepted_task(store, project_id, task_index, accepted.verification_id)
    if result.startswith("REJECTED"):
        # The verification is accepted and cannot be un-accepted; a rerun reuses it.
        return f"{result} Verification {job_id!r} stays accepted; rerun this call to retry the board close."
    return result


class _Lost(Exception):
    def __init__(self, step: str) -> None:
        super().__init__(step)
        self.step = step


__all__ = ["MANUAL_JOB_PREFIX", "CheckEvidence", "close_task_with_evidence"]
