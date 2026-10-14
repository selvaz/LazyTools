"""Editing a project's plan after it exists: retire a task, reopen a stuck
one, replace what is left, set a task's schedule.

Ported from ``lazyceo.plan_edit``, mechanism only. Operates directly on the
``DurableBlackboard`` document (``f"{BOARD_KEY_PREFIX}project:{project_id}"``),
the same whole-document compare-and-swap the board itself uses, so these
edits compose with ``DurableBlackboard``'s own ``claim_next``/``mark_done``/
``mark_failed`` rather than needing a parallel task representation.

**Task indexes never move.** Retiring marks a task ``cancelled`` with a
reason and a ``disposition``; it stays in the record and on the Gantt,
muted. Finished work (``done``) is never touched by anything here except
``reopen_done_task_for_invalid_closure``, the one deliberate exception (a
closure later confirmed, from git, to have been vacuous).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any, Literal

from lazybridge import Store

from lazytools.projects.keys import BOARD_KEY_PREFIX

CAS_ATTEMPTS = 8

RetireDisposition = Literal["obsolete", "superseded", "wrong_plan"]
_RETIRE_DISPOSITIONS: tuple[str, ...] = ("obsolete", "superseded", "wrong_plan")

#: Stamped only by ``revise_plan``, never accepted from ``retire_task`` directly.
_PLAN_REVISED_DISPOSITION = "plan_revised"

RETIRABLE = ("todo", "claimed", "failed")
REOPENABLE = ("failed", "cancelled")

#: How many times a caller may reopen the SAME task on its own say-so before a
#: human has to weigh in. See ``reopen_task``.
MAX_AUTONOMOUS_REOPENS = 2

MIN_REOPEN_REASON_CHARS = 30


def _board_key(project_id: str, *, prefix: str = BOARD_KEY_PREFIX) -> str:
    return f"{prefix}project:{project_id}"


def _reopen_count(events: list[dict[str, Any]], task_index: int) -> int:
    return sum(
        1 for e in events if isinstance(e, dict) and e.get("event") == "reopened" and e.get("task_index") == task_index
    )


def reopen_budget_left(doc_or_events: dict[str, Any] | list[dict[str, Any]], task_index: int) -> int:
    """How many more times ``task_index`` may be reopened autonomously."""
    events = doc_or_events.get("plan_events", []) if isinstance(doc_or_events, dict) else list(doc_or_events)
    return max(0, MAX_AUTONOMOUS_REOPENS - _reopen_count(events, task_index))


def _fresh_task(text: str) -> dict[str, Any]:
    return {
        "text": str(text),
        "status": "todo",
        "result": "",
        "error": "",
        "cancel_reason": "",
        "attempts": 0,
        "owner": None,
        "claimed_at": None,
        "planned_start_at": None,
        "due_at": None,
        "completed_at": None,
    }


def _mutate(
    store: Store,
    project_id: str,
    apply: Callable[[dict[str, Any]], tuple[dict[str, Any] | None, str]],
    *,
    prefix: str = BOARD_KEY_PREFIX,
) -> str:
    key = _board_key(project_id, prefix=prefix)
    for _ in range(CAS_ATTEMPTS):
        raw = store.read(key)
        if not isinstance(raw, dict) or not raw.get("tasks"):
            return f"REJECTED: project {project_id!r} has no plan yet -- set a plan first."
        new_doc, message = apply(raw)
        if new_doc is None:
            return message
        new_doc["updated_at"] = time.time()
        if store.compare_and_swap(key, raw, new_doc):
            return message
    return "REJECTED: the plan changed while this was being written and kept changing -- read it again and retry"


def _event(doc: dict[str, Any], **fields: Any) -> list[dict[str, Any]]:
    return [*doc.get("plan_events", []), {"at": time.time(), **fields}]


def _check(tasks: list[dict[str, Any]], index: int, expected_text: str) -> str | None:
    if not 0 <= index < len(tasks):
        return f"REJECTED: task_index out of range (valid: 0..{len(tasks) - 1})."
    if tasks[index]["text"] != expected_text:
        return (
            f"REJECTED: task {index}'s current text does not match expected_text -- "
            "read the current plan first."
        )
    return None


def _retired(
    task: dict[str, Any],
    reason: str,
    now: float,
    *,
    disposition: str,
    superseded_by_task_index: int | None = None,
) -> dict[str, Any]:
    return {
        **task,
        "status": "cancelled",
        "cancel_reason": reason,
        "cancel_disposition": disposition,
        "superseded_by_task_index": superseded_by_task_index,
        "completed_at": now,
        "owner": None,
        "claimed_at": None,
    }


def _disposition_problem(
    tasks: list[dict[str, Any]], task_index: int, disposition: str, superseded_by_task_index: int | None
) -> str | None:
    if disposition not in _RETIRE_DISPOSITIONS:
        return (
            f"REJECTED: disposition {disposition!r} is not one of {_RETIRE_DISPOSITIONS}. Retirement records "
            "abandoned planned work, never completed work."
        )
    if disposition != "superseded":
        if superseded_by_task_index is not None:
            return (
                f"REJECTED: superseded_by_task_index is only accepted with disposition 'superseded' "
                f"(got disposition {disposition!r})."
            )
        return None
    if superseded_by_task_index is None:
        return "REJECTED: disposition 'superseded' requires superseded_by_task_index naming the replacement task."
    if not 0 <= superseded_by_task_index < len(tasks):
        return (
            f"REJECTED: superseded_by_task_index {superseded_by_task_index} does not exist "
            f"(valid: 0..{len(tasks) - 1})."
        )
    if superseded_by_task_index == task_index:
        return "REJECTED: superseded_by_task_index cannot be the task being retired itself."
    if tasks[superseded_by_task_index].get("status") == "cancelled":
        return f"REJECTED: task {superseded_by_task_index} is cancelled and cannot be the replacement task."
    return None


def retire_task(
    store: Store,
    project_id: str,
    task_index: int,
    expected_text: str,
    reason: str,
    disposition: str,
    superseded_by_task_index: int | None = None,
) -> str:
    """Retire one task: no longer part of the work. Never for FINISHED work."""
    if not reason.strip():
        return "REJECTED: a reason is required."

    def apply(doc: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        tasks = [dict(t) for t in doc["tasks"]]
        problem = _check(tasks, task_index, expected_text)
        if problem is not None:
            return None, problem
        status = tasks[task_index].get("status")
        if status not in RETIRABLE:
            return None, (
                f"REJECTED: task {task_index} is {status}; only a task that is todo, claimed or failed can be "
                "retired (a done task is finished work, and a cancelled one is already retired)."
            )
        disposition_problem = _disposition_problem(tasks, task_index, disposition, superseded_by_task_index)
        if disposition_problem is not None:
            return None, disposition_problem
        now = time.time()
        tasks[task_index] = _retired(
            tasks[task_index],
            reason.strip(),
            now,
            disposition=disposition,
            superseded_by_task_index=superseded_by_task_index,
        )
        events = _event(
            doc,
            event="retired",
            task_index=task_index,
            was=status,
            reason=reason.strip(),
            disposition=disposition,
            superseded_by_task_index=superseded_by_task_index,
        )
        return {**doc, "tasks": tasks, "plan_events": events}, (
            f"retired task {task_index} (was {status}, {disposition}): {reason.strip()}"
        )

    return _mutate(store, project_id, apply)


def set_task_schedule(
    store: Store,
    project_id: str,
    task_index: int,
    expected_text: str,
    *,
    planned_start_at: float | None,
    due_at: float | None,
    reason: str,
    hold: bool,
) -> str:
    """Set or clear a task's planned start/due dates AND its ``start_hold`` flag.

    ``start_hold`` is an ecosystem-level scheduling flag (whether a task's
    date waits on something that is not a task -- an external event, a
    scheduled job, a data release, the operator): leave it False for an
    ordinary ordering choice, True when a driver loop (LazyCEO's or any
    other) must never advance this date on its own.
    """
    for name, value in (("planned_start_at", planned_start_at), ("due_at", due_at)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            return f"REJECTED: {name} must be a finite number or None, got {value!r}"
        if value is not None and not math.isfinite(value):
            return f"REJECTED: {name} must be finite, got {value!r}"
    if planned_start_at is not None and due_at is not None and planned_start_at > due_at:
        return "REJECTED: planned_start_at must be <= due_at"
    if not reason.strip():
        return "REJECTED: a reason is required (it is recorded in the schedule history)"

    def apply(doc: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        tasks = [dict(t) for t in doc["tasks"]]
        problem = _check(tasks, task_index, expected_text)
        if problem is not None:
            return None, problem
        status = tasks[task_index].get("status")
        if status not in ("todo", "claimed"):
            return None, (
                f"REJECTED: task {task_index} is {status}, not open -- only a todo or claimed task can be scheduled."
            )
        now = time.time()
        tasks[task_index] = {
            **tasks[task_index],
            "planned_start_at": planned_start_at,
            "due_at": due_at,
            "start_hold": bool(hold),
        }
        event = {
            "task_index": task_index,
            "planned_start_at": planned_start_at,
            "due_at": due_at,
            "hold": bool(hold),
            "reason": reason.strip(),
            "at": now,
        }
        new_doc = {**doc, "tasks": tasks, "schedule_events": [*doc.get("schedule_events", []), event]}
        return new_doc, (
            f"scheduled task {task_index}: planned_start={planned_start_at}, due={due_at}, hold={bool(hold)}"
        )

    return _mutate(store, project_id, apply)


def reopen_task(store: Store, project_id: str, task_index: int, expected_text: str, reason: str) -> str:
    """Put a failed or retired task back to ``todo`` with a fresh attempt budget.

    Bounded: ``reason`` under ``MIN_REOPEN_REASON_CHARS`` is rejected, and the
    SAME task_index may be reopened at most ``MAX_AUTONOMOUS_REOPENS`` times.
    """
    if not reason.strip():
        return "REJECTED: a reason is required."
    if len(reason.strip()) < MIN_REOPEN_REASON_CHARS:
        return (
            f"REJECTED: reason is only {len(reason.strip())} character(s) (minimum {MIN_REOPEN_REASON_CHARS}) -- "
            "say what will be different this time, not just that it should be retried."
        )

    def apply(doc: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        tasks = [dict(t) for t in doc["tasks"]]
        problem = _check(tasks, task_index, expected_text)
        if problem is not None:
            return None, problem
        task = tasks[task_index]
        status = task.get("status")
        if status not in REOPENABLE:
            return None, f"REJECTED: task {task_index} is {status}; only a failed or retired task can be reopened."
        already = _reopen_count(doc.get("plan_events", []), task_index)
        if already >= MAX_AUTONOMOUS_REOPENS:
            return None, (
                f"REJECTED: task {task_index} already reopened {already} times (limit {MAX_AUTONOMOUS_REOPENS}). "
                "Retire it, revise the plan, or ask for a specific decision."
            )
        attempts = task.get("attempts", 0)
        tasks[task_index] = {
            **task,
            "status": "todo",
            "attempts": 0,
            "error": "",
            "cancel_reason": "",
            "cancel_disposition": None,
            "superseded_by_task_index": None,
            "owner": None,
            "claimed_at": None,
            "completed_at": None,
        }
        events = _event(
            doc, event="reopened", task_index=task_index, was=status, attempts_before=attempts, reason=reason.strip()
        )
        return {**doc, "tasks": tasks, "plan_events": events}, (
            f"reopened task {task_index} (was {status} after {attempts} attempt(s)): {reason.strip()}"
        )

    return _mutate(store, project_id, apply)


def reopen_done_task_for_invalid_closure(
    store: Store, project_id: str, task_index: int, expected_text: str, *, owner: str, reason: str
) -> str:
    """Put a ``done`` task back to ``claimed`` because the verification that
    closed it is now known to have been vacuous.

    The one deliberate exception to "finished work is never touched". No
    reopen-count limit: this repairs a bookkeeping error, not a normal retry.
    """
    if not reason.strip():
        return "REJECTED: a reason is required."
    if not owner.strip():
        return "REJECTED: an owner is required to hold the reopened claim."

    def apply(doc: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        tasks = [dict(t) for t in doc["tasks"]]
        problem = _check(tasks, task_index, expected_text)
        if problem is not None:
            return None, problem
        task = tasks[task_index]
        status = task.get("status")
        if status != "done":
            return None, (
                f"REJECTED: task {task_index} is {status}, not done -- this verb only undoes an "
                "invalid 'done' closure; use reopen_task for a failed/cancelled task instead."
            )
        tasks[task_index] = {
            **task,
            "status": "claimed",
            "owner": owner,
            "claimed_at": time.time(),
            "completed_at": None,
        }
        events = _event(
            doc,
            event="reopened_done_invalid_closure",
            task_index=task_index,
            owner=owner,
            reason=reason.strip(),
        )
        return {**doc, "tasks": tasks, "plan_events": events}, (
            f"task {task_index} reopened from done to claimed (invalid closure, owner {owner!r}): {reason.strip()}"
        )

    return _mutate(store, project_id, apply)


def open_indexes(store: Store, project_id: str, *, keep: tuple[int, ...] = (), prefix: str = BOARD_KEY_PREFIX) -> list[int]:
    """The tasks a plan revision would retire: every retirable one, except those in ``keep``."""
    raw = store.read(_board_key(project_id, prefix=prefix))
    if not isinstance(raw, dict):
        return []
    return [i for i, t in enumerate(raw.get("tasks", [])) if t.get("status") in RETIRABLE and i not in keep]


def revise_plan(
    store: Store, project_id: str, reason: str, new_tasks: list[str], *, keep: tuple[int, ...] = ()
) -> tuple[str, list[int], int | None]:
    """Retire every retirable task except those in ``keep`` AND append ``new_tasks``, in ONE write.

    All or nothing. Returns the message, the indexes retired, and the index
    the new tasks start at (None if there are none).
    """
    if not reason.strip():
        return "REJECTED: a reason is required.", [], None
    clean = [str(t).strip() for t in new_tasks if str(t).strip()]
    retired: list[int] = []
    first_new: list[int] = []

    def apply(doc: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        retired.clear()
        first_new.clear()
        tasks = [dict(t) for t in doc["tasks"]]
        bad = [i for i in keep if not 0 <= i < len(tasks)]
        if bad:
            return None, f"REJECTED: keep names task(s) {bad} that do not exist (valid: 0..{len(tasks) - 1})."
        now = time.time()
        events = list(doc.get("plan_events", []))
        for i, task in enumerate(tasks):
            if task.get("status") in RETIRABLE and i not in keep:
                events.append(
                    {
                        "at": now,
                        "event": "retired",
                        "task_index": i,
                        "was": task["status"],
                        "reason": reason.strip(),
                        "disposition": _PLAN_REVISED_DISPOSITION,
                        "superseded_by_task_index": None,
                    }
                )
                tasks[i] = _retired(task, reason.strip(), now, disposition=_PLAN_REVISED_DISPOSITION)
                retired.append(i)
        if not retired and not clean:
            return None, "nothing to retire: no task is todo, claimed or failed outside `keep`."
        if clean:
            first_new.append(len(tasks))
            tasks.extend(_fresh_task(text) for text in clean)
            events.append(
                {
                    "at": now,
                    "event": "appended",
                    "from_index": first_new[0],
                    "count": len(clean),
                    "reason": reason.strip(),
                }
            )
        parts = [f"retired {len(retired)} task(s): {list(retired)}" if retired else "nothing to retire"]
        if clean:
            parts.append(f"added {len(clean)} new task(s) from index {first_new[0]}")
        return {**doc, "tasks": tasks, "plan_events": events}, "; ".join(parts)

    message = _mutate(store, project_id, apply)
    if message.startswith("REJECTED") or message.startswith("nothing to retire: no task"):
        return message, [], None
    return message, list(retired), (first_new[0] if first_new else None)


__all__ = [
    "MAX_AUTONOMOUS_REOPENS",
    "MIN_REOPEN_REASON_CHARS",
    "REOPENABLE",
    "RETIRABLE",
    "RetireDisposition",
    "open_indexes",
    "reopen_budget_left",
    "reopen_done_task_for_invalid_closure",
    "reopen_task",
    "retire_task",
    "revise_plan",
    "set_task_schedule",
]
