"""Releasing a claim that never got real work started against it, and
closing a ``todo`` task that was verified by hand rather than through a
contract.

Adapted from ``lazyceo.task_claims``. Left behind, as CEO/delegation policy:
``close_accepted_task`` (closing a project task automatically once a
verification is accepted) reaches into LazyCEO's own boost/autonomy
(``effective_capability``) to decide whether the CALLER may finalize at
all -- that gate belongs with ``verification.accept``'s injectable
``authorization_check`` (see that module), and the "close the board slot
once a verification is accepted" bookkeeping itself is thin enough that a
caller can do it directly with
``records.touch_project_progress``/``DurableBlackboard.mark_done`` once its
own policy has decided the verification is accepted. Porting a second,
parallel version of that bookkeeping here would just be a second place for
it to drift from the first.

``_newer_job_exists`` here reads job records under the configurable
``job_prefix`` directly (read-only) rather than importing LazyCEO's job
registry module -- this package depends on nothing in LazyCEO.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from lazytools.projects.keys import BOARD_KEY_PREFIX, JOB_PREFIX

if TYPE_CHECKING:
    from lazybridge import Store

CAS_ATTEMPTS = 8


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


def complete_todo_without_verification(
    store: Store,
    project_id: str,
    task_index: int,
    summary: str,
    *,
    board_prefix: str = BOARD_KEY_PREFIX,
    job_prefix: str = JOB_PREFIX,
    contract_exists: bool = False,
) -> str:
    """Close a ``todo`` project task directly, without a claim: the honest-shortcut
    path for a caller who never claimed the task but has verified in person that
    the work is genuinely done.

    Only closes a task that is EXACTLY ``todo`` with no ``owner``/``claimed_at``.
    Refused while a job is still ``running``/``awaiting_approval`` against it
    (an inconsistent state for a todo task), and refused when ``contract_exists``
    is True -- the caller is expected to have already checked
    ``contracts.find_contract_for_task`` for this ``(project_id, task_index)``
    and pass that result in, since a contract means the task should close
    through ``verification.accept`` instead.
    """
    if not summary.strip():
        return "REJECTED: a 1-3 sentence summary is required."
    if contract_exists:
        return f"REJECTED: task {task_index} has an acceptance contract -- close it through accept() instead."

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
        new_tasks = [dict(t) for t in tasks]
        updated = dict(new_tasks[task_index])
        updated.update(status="done", result=summary, error="", cancel_reason="", owner=None, claimed_at=None, completed_at=time.time())
        new_tasks[task_index] = updated
        new_doc = {**raw, "tasks": new_tasks, "updated_at": time.time()}
        if store.compare_and_swap(key, raw, new_doc):
            return f"task {task_index} marked done (SENZA VERIFICA, no claim): {summary}"

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


__all__ = ["complete_todo_without_verification", "release_claim"]
