"""Release unused claims and close tasks with accepted or manual verification.

Adapted from ``lazyceo.task_claims``, mechanism only. Acceptance policy belongs
in ``verification.accept``'s injectable ``authorization_check``; closing an
accepted task here only records that decision on its matching board slot.

``_newer_job_exists`` here reads job records under the configurable
``job_prefix`` directly (read-only) rather than importing LazyCEO's job
registry module -- this package depends on nothing in LazyCEO.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from lazytools.projects.contracts import find_contract_for_task, get_task_contract
from lazytools.projects.keys import BOARD_KEY_PREFIX, JOB_PREFIX, TASK_CONTRACT_PREFIX, VERIFICATION_PREFIX
from lazytools.projects.records import touch_project_progress
from lazytools.projects.verification import get_verification

if TYPE_CHECKING:
    from lazybridge import Store

CAS_ATTEMPTS = 8
ClosureCheck = Callable[["Store", str, int], str | None]
ClosedHook = Callable[["Store", str, int], None]


def _newer_job_exists(store: Store, plan_id: str, task_index: int, job_id: str, *, job_prefix: str) -> bool:
    """Whether some OTHER job registered for this exact board slot is more recent than ``job_id``."""
    matches = [
        raw
        for _key, raw in store.items(prefix=job_prefix)
        if isinstance(raw, dict) and raw.get("plan_id") == plan_id and raw.get("task_index") == task_index and isinstance(raw.get("created_at"), str)
    ]
    if not matches:
        return False
    newest = max(matches, key=lambda raw: str(raw["created_at"]))
    return newest.get("job_id") != job_id


def _running_job_exists(store: Store, plan_id: str, task_index: int, *, job_prefix: str) -> bool:
    for _key, raw in store.items(prefix=job_prefix):
        if isinstance(raw, dict) and raw.get("plan_id") == plan_id and raw.get("task_index") == task_index and raw.get("status") in ("running", "awaiting_approval"):
            return True
    return False


_CONTRACT_REFUSAL = "REJECTED: task {task_index} has an acceptance contract -- close it through accept() instead."


def contract_gate_refusal(store: Store, project_id: str, task_index: int, *, prefix: str = TASK_CONTRACT_PREFIX) -> str | None:
    """The REJECTED message if ``(project_id, task_index)`` has an acceptance
    contract, else None.

    This is the lookup half only (defence in depth: it also catches contracts
    written by older code that never stamped the board). It is NOT atomic with
    a board write on its own: a closer must also refuse on the board marker
    ``task["contract_id"]`` inside its own board CAS, as
    ``complete_todo_without_verification`` does. Never trust a flag from the caller."""
    if find_contract_for_task(store, project_id, task_index, prefix=prefix) is not None:
        return _CONTRACT_REFUSAL.format(task_index=task_index)
    return None


def complete_todo_without_verification(
    store: Store,
    project_id: str,
    task_index: int,
    summary: str,
    *,
    board_prefix: str = BOARD_KEY_PREFIX,
    job_prefix: str = JOB_PREFIX,
    contract_prefix: str = TASK_CONTRACT_PREFIX,
    check: ClosureCheck | None = None,
    on_closed: ClosedHook | None = None,
) -> str:
    """Close a ``todo`` project task directly, without a claim: the honest-shortcut
    path for a caller who never claimed the task but has verified in person that
    the work is genuinely done.

    Only closes a task that is EXACTLY ``todo`` with no ``owner``/``claimed_at``.
    Refused while a job is still ``running``/``awaiting_approval`` against it
    (an inconsistent state for a todo task).

    This function enforces the contract gate itself, in two layers. (1) Atomic:
    it refuses when the board task carries ``contract_id`` (stamped by
    ``contracts.open_task_contract`` in a board CAS), judged on the same board
    snapshot its own CAS compares against -- so a contract created at any
    moment either lands before that snapshot (refused) or changes the board
    and makes this CAS fail (the retry refuses). (2) Defence in depth: it also
    runs ``contracts.find_contract_for_task``, which catches a contract written
    without a marker (before the marker existed; see
    ``contract_markers.backfill_contract_markers``). A task with an acceptance
    contract must close through ``verification.accept``. There is no
    caller-supplied flag to skip or assert either layer.

    ``check(store, project_id, task_index)`` is an EXTRA caller refusal that
    runs after the built-in gate, immediately before every board CAS. None
    allows closure; a string refuses. It can only add refusals, never bypass
    the contract gate. ``on_closed`` runs once after the winning CAS. These
    hooks are synchronous and their exceptions propagate; a hook failure
    after CAS does not undo the close or cause a second invocation.
    """
    if not summary.strip():
        return "REJECTED: a 1-3 sentence summary is required."

    plan_id = f"project:{project_id}"
    if _running_job_exists(store, plan_id, task_index, job_prefix=job_prefix):
        return (
            f"REJECTED: task {task_index} has a job still running or awaiting approval against it -- "
            "that is an inconsistent state for a todo task; let the job finish or fail first."
        )

    key = f"{board_prefix}{plan_id}"
    for _ in range(CAS_ATTEMPTS):
        raw = store.read(key)
        if not isinstance(raw, dict) or not raw.get("tasks"):
            return "REJECTED: no plan set for this project."
        tasks = raw["tasks"]
        if not 0 <= task_index < len(tasks):
            return f"REJECTED: task_index out of range (valid: 0..{len(tasks) - 1})."
        task = tasks[task_index]
        if task.get("status") != "todo" or task.get("owner") is not None or task.get("claimed_at") is not None:
            return (
                f"REJECTED: task {task_index} is {task.get('status')!r} (owner={task.get('owner')!r}), "
                "not an unclaimed todo task -- this path is only for a task nobody has touched yet."
            )
        # The atomic part of the gate: the marker is read from the SAME board
        # snapshot (``raw``) the CAS below compares against, so a contract that
        # stamps the board after this read makes that CAS fail and the retry
        # lands here. ``open_task_contract`` stamps the board before it writes
        # the contract key, so there is no window with a contract but no marker.
        if task.get("contract_id"):
            return _CONTRACT_REFUSAL.format(task_index=task_index)
        new_tasks = [dict(t) for t in tasks]
        updated = dict(new_tasks[task_index])
        updated.update(status="done", result=summary, error="", cancel_reason="", owner=None, claimed_at=None, completed_at=time.time())
        new_tasks[task_index] = updated
        new_doc = {**raw, "tasks": new_tasks, "updated_at": time.time()}
        gate = contract_gate_refusal(store, project_id, task_index, prefix=contract_prefix)
        if gate is not None:
            return gate
        if check is not None:
            refusal = check(store, project_id, task_index)
            if refusal is not None:
                return refusal
        if store.compare_and_swap(key, raw, new_doc):
            if on_closed is not None:
                on_closed(store, project_id, task_index)
            return f"task {task_index} marked done (SENZA VERIFICA, no claim): {summary}"

    return f"REJECTED: task {task_index} could not be closed safely -- another writer kept changing the board across {CAS_ATTEMPTS} retries; try again."


def close_accepted_task(
    store: Store,
    project_id: str,
    task_index: int,
    verification_id: str,
    *,
    board_prefix: str = BOARD_KEY_PREFIX,
    contract_prefix: str = TASK_CONTRACT_PREFIX,
    verification_prefix: str = VERIFICATION_PREFIX,
    on_closed: ClosedHook | None = None,
) -> str:
    """Close a todo/claimed task whose current contract has been accepted.

    The task update and plan event share one winning board CAS. Every retry
    re-reads the verification, contract and board marker, so a changed marker
    cannot close under an earlier contract's acceptance. Separate verification
    and contract keys are not part of a multi-key transaction.

    After the winning CAS, touch project progress (best effort), then call
    ``on_closed`` once. As with the shortcut closer, hook exceptions propagate
    and do not undo the close or cause a second invocation.
    """
    key = f"{board_prefix}project:{project_id}"
    for _ in range(CAS_ATTEMPTS):
        try:
            verification = get_verification(store, verification_id, prefix=verification_prefix)
            if verification is None:
                return f"REJECTED: verification {verification_id!r} not found (or ambiguous)."
            if verification.status != "accepted":
                return f"REJECTED: verification {verification.verification_id!r} is {verification.status!r}, not accepted."
            contract = get_task_contract(store, verification.contract_id, prefix=contract_prefix)
        except ValueError as exc:
            return f"REJECTED: invalid verification or contract record: {exc}"
        if contract is None:
            return f"REJECTED: contract {verification.contract_id!r} not found."
        if contract.contract_id != verification.contract_id or contract.project_id != project_id or contract.task_index != task_index:
            return f"REJECTED: verification's contract does not match project {project_id!r}, task {task_index}."

        raw = store.read(key)
        if not isinstance(raw, dict) or not raw.get("tasks"):
            return "REJECTED: no plan set for this project."
        tasks = raw["tasks"]
        if not 0 <= task_index < len(tasks):
            return f"REJECTED: task_index out of range (valid: 0..{len(tasks) - 1})."
        task = tasks[task_index]
        if task.get("status") not in ("todo", "claimed"):
            return f"REJECTED: task {task_index} is {task.get('status')!r}, not todo or claimed."
        if not task.get("contract_id"):
            return f"REJECTED: task {task_index} has no contract -- use complete_todo_without_verification instead."
        if task["contract_id"] != verification.contract_id:
            return f"REJECTED: task {task_index} contract_id does not match verification {verification.verification_id!r}."

        summary = (
            f"Accepted verification {verification.verification_id}; "
            f"reviewer: {verification.reviewer}; decision: {verification.decision_reason}"
        )
        now = time.time()
        new_tasks = [dict(t) for t in tasks]
        new_tasks[task_index].update(
            status="done", result=summary, error="", cancel_reason="", owner=None, claimed_at=None, completed_at=now
        )
        event = {
            "at": now,
            "event": "closed_accepted",
            "task_index": task_index,
            "was": task["status"],
            "contract_id": contract.contract_id,
            "verification_id": verification.verification_id,
            "reviewer": verification.reviewer,
            "reason": verification.decision_reason,
        }
        new_doc = {**raw, "tasks": new_tasks, "updated_at": now, "plan_events": [*raw.get("plan_events", []), event]}
        if store.compare_and_swap(key, raw, new_doc):
            touch_project_progress(store, project_id)
            if on_closed is not None:
                on_closed(store, project_id, task_index)
            return f"task {task_index} marked done: {summary}"

    return f"REJECTED: task {task_index} could not be closed safely -- another writer kept changing the board across {CAS_ATTEMPTS} retries; try again."


def release_claim(
    store: Store,
    plan_id: str,
    task_index: int,
    *,
    owner: str | None,
    restore_attempts: int | None,
    note: str,
    expected_job_id: str | None = None,
    board_prefix: str = BOARD_KEY_PREFIX,
    job_prefix: str = JOB_PREFIX,
) -> bool:
    """``claimed`` -> ``todo`` without counting it as a failed attempt: for a task
    whose setup failed BEFORE any real work started.

    Deliberately best-effort: never raises, returns ``False`` for every
    reason it did not release.
    """
    log = logging.getLogger(__name__)
    try:
        if expected_job_id is not None and _newer_job_exists(store, plan_id, task_index, expected_job_id, job_prefix=job_prefix):
            log.info("release_claim: not releasing %s#%d -- a job newer than %s already exists for this task", plan_id, task_index, expected_job_id)
            return False

        key = f"{board_prefix}{plan_id}"
        for _ in range(CAS_ATTEMPTS):
            raw = store.read(key)
            if not isinstance(raw, dict) or not raw.get("tasks"):
                return False
            tasks = raw["tasks"]
            if not 0 <= task_index < len(tasks):
                return False
            task = tasks[task_index]
            if task.get("status") != "claimed":
                return False
            if owner is not None and task.get("owner") != owner:
                return False
            new_tasks = [dict(t) for t in tasks]
            updated = dict(new_tasks[task_index])
            updated["status"] = "todo"
            updated["owner"] = None
            updated["claimed_at"] = None
            updated["error"] = f"INFRA: {note}"
            if restore_attempts is not None:
                updated["attempts"] = restore_attempts
            new_tasks[task_index] = updated
            new_doc = {**raw, "tasks": new_tasks, "updated_at": time.time()}
            if store.compare_and_swap(key, raw, new_doc):
                return True
        log.warning("release_claim: gave up on %s#%d after %d CAS retries -- another worker is writing continuously", plan_id, task_index, CAS_ATTEMPTS)
        return False
    except Exception:
        log.exception("release_claim failed for %s#%d", plan_id, task_index)
        return False


__all__ = ["ClosedHook", "ClosureCheck", "close_accepted_task", "complete_todo_without_verification", "contract_gate_refusal", "release_claim"]
