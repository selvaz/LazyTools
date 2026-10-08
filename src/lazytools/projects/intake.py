"""A project does not start until it has been defined.

Ported from ``lazyceo.project_intake`` (the deterministic checklist,
``plan_digest``, ``promotion_refusal``), plus a thin ``promote_project_plan``
wrapper over ``records``/``plan_edit`` mechanism that actually moves a
project from draft to open.

Left behind, as CEO policy: LazyCEO's own ``promote_project`` tool (in
``simple/agent.py``) additionally runs an LLM *reviewer* over the plan's
falsifiability (``lazyceo.contract_review.review_contract_falsifiability``)
before accepting ``record_project_review`` below. That reviewer call -- its
prompt, its model choice -- is CEO policy. For a Claude Code session calling
these tools directly, the calling agent itself plays that role: it reads the
plan, judges whether the criteria are falsifiable, and only then calls
``record_project_review``. The deterministic checklist here is the one gate
that runs regardless of who is judging.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from lazytools.projects.records import _apply, get_project

if TYPE_CHECKING:
    from lazybridge import Store

MINIMUM_SUBTASKS = 2

_EMPTY_ASSURANCES = (
    "works correctly",
    "works as expected",
    "is correct",
    "is complete",
    "done properly",
    "no regressions",
    "everything works",
)


@dataclass(frozen=True)
class IntakeVerdict:
    ok: bool
    findings: list[str] = field(default_factory=list)

    def rejection_text(self) -> str:
        lines = "\n".join(f"  - {finding}" for finding in self.findings)
        return (
            "REJECTED: this project is not defined well enough to start.\n"
            f"{lines}\n"
            "Fix these and submit it again -- nothing was started, so nothing was spent."
        )


def _blank(value: Any) -> bool:
    return not isinstance(value, str) or not value.strip()


def parse_deadline(value: Any) -> datetime | None:
    """The date a deadline names, or None if it names no date."""
    if _blank(value):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def check_project_intake(
    *,
    objective: Any,
    observable_result: Any,
    deadline: Any,
    subtasks: Any,
    minimum_subtasks: int = MINIMUM_SUBTASKS,
) -> IntakeVerdict:
    """The mechanical half of the intake gate. Every finding names what is missing and where."""
    findings: list[str] = []

    if _blank(objective):
        findings.append("objective is empty: what is this project for?")
    if _blank(observable_result):
        findings.append(
            "observable_result is empty: name something a person could look at to see the project is finished"
        )
    if _blank(deadline):
        findings.append("deadline is empty: a project with no date cannot be behind")
    elif parse_deadline(deadline) is None:
        findings.append(
            f"deadline says {str(deadline).strip()!r}, which names no date: give an ISO "
            "date or timestamp (2026-10-01, or 2026-10-01T18:00+02:00)"
        )

    if not isinstance(subtasks, (list, tuple)) or not subtasks:
        findings.append("subtasks is empty: a project that is one step is a task, not a project")
        return IntakeVerdict(ok=False, findings=findings)

    if len(subtasks) < minimum_subtasks:
        findings.append(
            f"only {len(subtasks)} subtask(s); at least {minimum_subtasks} are needed before "
            "a project can start, or it has not been broken down"
        )

    for index, subtask in enumerate(subtasks):
        label = f"subtask {index}"
        if not isinstance(subtask, dict):
            findings.append(f"{label} is not an object with a text and criteria")
            continue
        if _blank(subtask.get("text")):
            findings.append(f"{label} has no text")
        criteria = subtask.get("acceptance_criteria")
        if not isinstance(criteria, (list, tuple)) or not criteria:
            findings.append(f"{label} has no acceptance criteria")
            continue
        for position, criterion in enumerate(criteria):
            where = f"{label} criterion {position}"
            if _blank(criterion):
                findings.append(f"{where} is empty")
                continue
            lowered = criterion.strip().lower()
            if any(phrase in lowered for phrase in _EMPTY_ASSURANCES):
                findings.append(
                    f"{where} says {criterion.strip()!r}, which is an assurance rather than "
                    "something that could be shown to have failed"
                )

    return IntakeVerdict(ok=not findings, findings=findings)


def plan_digest(*, objective: str, observable_result: str, deadline: str, subtasks: list[dict[str, Any]]) -> str:
    """A stable fingerprint of everything a reviewer was shown."""
    payload = {
        "objective": objective.strip(),
        "observable_result": observable_result.strip(),
        "deadline": deadline.strip(),
        "subtasks": [
            {
                "text": str(subtask.get("text", "")).strip(),
                "acceptance_criteria": [str(c).strip() for c in subtask.get("acceptance_criteria", [])],
            }
            for subtask in subtasks
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def promotion_refusal(*, reviewed_digest: str | None, current_digest: str) -> str | None:
    """None if this plan may leave draft, otherwise why not."""
    if reviewed_digest is None:
        return (
            "REJECTED: this project has not been reviewed. The checklist and an independent "
            "review must both pass before it can leave draft."
        )
    if reviewed_digest != current_digest:
        return (
            "REJECTED: the plan changed after it was reviewed "
            f"(reviewed {reviewed_digest[:12]}, now {current_digest[:12]}). "
            "The verdict applied to the earlier version, so it does not carry over -- "
            "review the current one."
        )
    return None


def record_project_review(
    store: Store, project_id: str, *, reviewed_digest: str
) -> bool:
    """Remember which revision of the plan a reviewer (whoever played that role) accepted.

    Only meaningful on a draft. Mirrors ``lazyceo.projects.record_intake_review``.
    """
    return _apply(store, project_id, {"reviewed_digest": reviewed_digest}, allowed_from=("draft",))


def _apply_intake_fields(
    store: Store, project_id: str, *, observable_result: str, deadline: str, acceptance_criteria: list[str] | None = None
) -> bool:
    updates: dict[str, object] = {"observable_result": observable_result.strip()}
    if acceptance_criteria is not None:
        updates["acceptance_criteria"] = [str(c).strip() for c in acceptance_criteria]
    parsed = parse_deadline(deadline)
    if parsed is not None:
        updates["target_completion_at"] = parsed
    return _apply(store, project_id, updates, allowed_from=("draft", "open"))


def install_project_plan(store: Store, project_id: str, *, objective: str, tasks: list[str]) -> str | None:
    """Install or replace an untouched plan with whole-board CAS; None means success.

    An identical plan is a no-op, preserving claims, results and schedules.
    Started tasks include previous attempts returned to todo or cancelled.
    """
    from lazybridge.ext.planners.durable_blackboard import BLACKBOARD_VERSION

    from lazytools.projects.keys import BOARD_KEY_PREFIX
    from lazytools.projects.plan_edit import _fresh_task

    key = f"{BOARD_KEY_PREFIX}project:{project_id}"
    for _ in range(8):
        raw = store.read(key)
        current = raw if isinstance(raw, dict) else {}
        old_tasks = current.get("tasks", [])
        if current.get("reasoning") == objective.strip() and [t.get("text") for t in old_tasks] == tasks:
            return None
        started = [
            f"{i}: {t.get('text')!r}" for i, t in enumerate(old_tasks)
            if t.get("status") in ("claimed", "done", "failed") or t.get("attempts", 0)
            or t.get("owner") is not None or t.get("claimed_at") is not None or t.get("result") or t.get("error")
            or (t.get("completed_at") is not None and t.get("status") != "cancelled")
        ]
        if started:
            return "REJECTED: cannot replace a different plan; tasks already started: " + ", ".join(started)
        moment = time.time()
        updated = {
            **current, "version": BLACKBOARD_VERSION, "plan_id": f"project:{project_id}",
            "reasoning": objective.strip(), "created_at": moment, "updated_at": moment,
            "tasks": [_fresh_task(text) for text in tasks], "schedule_events": [],
        }
        if store.compare_and_swap(key, raw, updated):
            return None
    return "REJECTED: the board changed while installing the plan -- read it again and retry"


def promote_project_plan(
    store: Store,
    project_id: str,
    *,
    observable_result: str,
    deadline: str,
    subtasks: list[dict[str, Any]],
) -> str:
    """Take a project from draft to open, once the deterministic checklist passes and
    the caller has already recorded a review (:func:`record_project_review`) of the
    EXACT same plan.

    ``subtasks`` is ``[{"text": ..., "acceptance_criteria": [...]}, ...]``. Installs the
    plan onto the project's board (``DurableBlackboard.set_plan``) only once promotion
    actually succeeds, so a project cannot end up "open" with an empty board.
    """
    record = get_project(store, project_id)
    if record is None:
        return f"REJECTED: no project named {project_id!r} -- call open_project first"

    verdict = check_project_intake(
        objective=record.objective, observable_result=observable_result, deadline=deadline, subtasks=subtasks
    )
    if not verdict.ok:
        return verdict.rejection_text()

    digest = plan_digest(
        objective=record.objective, observable_result=observable_result, deadline=deadline, subtasks=subtasks
    )
    refusal = promotion_refusal(reviewed_digest=record.reviewed_digest, current_digest=digest)
    if refusal is not None:
        return refusal

    criteria = [str(c) for s in subtasks for c in s.get("acceptance_criteria", [])]
    if not _apply_intake_fields(store, project_id, observable_result=observable_result, deadline=deadline, acceptance_criteria=criteria):
        return "REJECTED: the project changed while it was being promoted -- read it again and retry"

    refusal = install_project_plan(store, project_id, objective=record.objective, tasks=[str(s["text"]) for s in subtasks])
    if refusal is not None:
        return refusal

    if not _apply(store, project_id, {"status": "open"}, allowed_from=("draft",)):
        return "REJECTED: the project changed while it was being promoted -- read it again and retry"
    return (
        f"promoted project {project_id!r} to open with the reviewed plan installed: "
        f"{len(subtasks)} subtask(s), each with acceptance criteria"
    )


__all__ = [
    "IntakeVerdict",
    "MINIMUM_SUBTASKS",
    "check_project_intake",
    "install_project_plan",
    "parse_deadline",
    "plan_digest",
    "promote_project_plan",
    "promotion_refusal",
    "record_project_review",
]
