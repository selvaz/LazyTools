"""MCP tool surface over ``lazytools.projects``.

Follows the house connector pattern (``_is_lazy_tool_provider`` marker,
``as_tools()`` wrapping plain methods with ``Tool.wrap``). Read tools are
always emitted; write tools (bookkeeping only -- same CAS/lease/claim
invariants as the mechanism underneath) are emitted only when constructed
with ``allow_write=True``.

Deliberately NOT exposed here, in every shape: delegation (execution of a
task -- that is ``lazytools-code-bridge``'s job, not this provider's),
Telegram, specialist lifecycle, autonomy-LEVEL changes, and git
merges/releases. See docs/projects.md for the full read/write/excluded
list and why.

Store resolution (``store_db``): explicit path > ``LAZYTOOLS_PROJECTS_STORE_DB``
env var > ``LAZYCEO_CEO_STORE_DB`` env var (the one LazyCEO itself already
uses to hand its store path to child processes). With NONE of those set,
this opens an IN-MEMORY store -- deliberately, same convention as the
``pulse`` connector over the same kind of shared CEO state: constructing a
provider must never, by itself and with no configuration at all, open a
real connection against a live deployment's file (``Store.__init__`` runs
schema DDL immediately, not lazily, so merely instantiating this class
already touches whatever file it is pointed at). ``keys.DEFAULT_CEO_STORE_DB``
documents the path a real deployment sets one of those two env vars (or
``data_source["projects_store_db"]``) TO, to see the live CEO's projects --
it is not a fallback this code chooses on its own. Tests always pass an
explicit path regardless -- see docs/projects.md's concurrency section
before pointing this at a live deployment's file.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lazybridge import Store

from lazytools.projects import admission as _admission
from lazytools.projects import brake as _brake
from lazytools.projects import contracts as _contracts
from lazytools.projects import cost_report as _cost_report
from lazytools.projects import intake as _intake
from lazytools.projects import notes as _notes
from lazytools.projects import owner as _owner
from lazytools.projects import plan_edit as _plan_edit
from lazytools.projects import quota_telemetry as _quota
from lazytools.projects import records as _records
from lazytools.projects import schedule as _schedule
from lazytools.projects import timeline as _timeline
from lazytools.projects import verification as _verification
from lazytools.projects.keys import CEO_STORE_DB_ENV

#: Claude Code's default list/timeline view when no explicit ``owner`` is
#: given: its own projects plus shared ones, never a CEO project by
#: surprise. Pass owner="ceo" (or "all") explicitly for read-only visibility
#: of CEO-owned projects.
_DEFAULT_VIEW_OWNERS = ("claude", "shared")

#: Sentinel meaning "show every owner" -- distinct from the no-argument
#: default above, which narrows to Claude Code's own projects.
_ALL_OWNERS_SENTINEL = "all"

_ENV_STORE_DB = "LAZYTOOLS_PROJECTS_STORE_DB"


def _resolve_store_db(store_db: str | None) -> str | None:
    """The configured path, or None (in-memory) if nothing at all is configured.

    Never falls back to ``keys.DEFAULT_CEO_STORE_DB`` on its own -- see the
    module docstring. A real deployment sets ``LAZYTOOLS_PROJECTS_STORE_DB``
    (or reuses ``LAZYCEO_CEO_STORE_DB``, already set for a LazyCEO specialist
    child process) to that path explicitly.
    """
    return store_db or os.environ.get(_ENV_STORE_DB) or os.environ.get(CEO_STORE_DB_ENV) or None


def _open_store(store_db: str | None) -> Store:
    path = _resolve_store_db(store_db)
    if path is None:
        return Store()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return Store(db=path)


def _owners_filter(owner: str) -> tuple[str, ...] | None:
    """``owner`` as the provider's methods receive it -> a tuple of owners to
    include, or None for every owner. ``"default"`` -> Claude Code's own
    view; ``"all"`` -> no filter; anything else must be a single valid owner."""
    if owner == "default":
        return _DEFAULT_VIEW_OWNERS
    if owner == _ALL_OWNERS_SENTINEL:
        return None
    return (owner,)


def _project_dict(record: _records.ProjectRecord, store: Store) -> dict[str, Any]:
    data = record.model_dump(mode="json")
    data["owner"] = _owner.get_project_owner(store, record.project_id)
    data["brake_enabled"] = _brake.get_project_brake_enabled(store, record.project_id)
    return data


class ProjectsTools:
    """Read/write tools over the shared project registry (``lazytools.projects``)."""

    _is_lazy_tool_provider = True

    def __init__(self, *, store_db: str | None = None, allow_write: bool = False) -> None:
        self._store = _open_store(store_db)
        self._allow_write = allow_write

    # ---- read: projects -------------------------------------------------

    def projects_list(self, status: str | None = None, owner: str = "default") -> list[dict[str, Any]]:
        """List projects, oldest first, optionally filtered by status
        (draft/open/paused/done). ``owner`` is "default" (Claude Code's own
        view: claude + shared), "all", or one of ceo/claude/shared."""
        owners = _owners_filter(owner)
        records = _records.list_projects(self._store, status=status)
        if owners is not None:
            records = [r for r in records if _owner.get_project_owner(self._store, r.project_id) in owners]
        return [_project_dict(r, self._store) for r in records]

    def projects_get(self, project_id: str) -> dict[str, Any] | None:
        """Read one project's full record, including its owner and brake setting."""
        record = _records.get_project(self._store, project_id)
        return _project_dict(record, self._store) if record is not None else None

    def projects_schedule(self, project_id: str) -> dict[str, Any]:
        """Full schedule view for one project: status, tasks, plan reasoning,
        and the schedule-event history -- the Gantt's underlying data."""
        view = _schedule.project_schedule_view(self._store, project_id)
        return view.model_dump(mode="json")

    def projects_timeline(self, owner: str = "default", blocked_reasons: dict[str, str] | None = None) -> str:
        """Text Gantt of every OPEN project: progress bar, deadline, state, what is stuck.

        ``blocked_reasons`` lets a caller that already knows why a specific
        project's next task cannot be claimed (its own oversight read) have
        that reflected as BLOCKED; omit it for a plain schedule-only view.
        """
        owners = _owners_filter(owner)
        if owners is None or len(owners) != 1:
            lines: list[str] = []
            for one_owner in (owners or ("ceo", "claude", "shared")):
                rendered = _timeline.render_project_timeline(
                    self._store, blocked_reasons=blocked_reasons, owner=one_owner
                )
                lines.append(f"--- owner={one_owner} ---\n{rendered}")
            return "\n\n".join(lines)
        return _timeline.render_project_timeline(self._store, blocked_reasons=blocked_reasons, owner=owners[0])

    def projects_notes(self, project_id: str, limit: int = 5) -> list[dict[str, Any]]:
        """The newest notes recorded against a project, most recent first."""
        return _notes.recent_project_notes(self._store, project_id, limit=limit)

    def projects_board_summary(self, project_id: str) -> str:
        """One-line "N todo, M done, ..." summary of a project's task board."""
        return _notes.project_board_summary(self._store, project_id)

    # ---- read: contracts & verification ----------------------------------

    def projects_find_contract(self, project_id: str, task_index: int) -> dict[str, Any] | None:
        """The acceptance contract governing one project task, if any."""
        contract = _contracts.find_contract_for_task(self._store, project_id, task_index)
        return contract.model_dump(mode="json") if contract is not None else None

    def projects_get_contract(self, contract_id: str) -> dict[str, Any] | None:
        """Read one acceptance contract by id."""
        contract = _contracts.get_task_contract(self._store, contract_id)
        return contract.model_dump(mode="json") if contract is not None else None

    def projects_repos_for_project(self, project_id: str) -> list[str]:
        """Every repo any task contract of this project has ever used."""
        return sorted(_contracts.repos_for_project(self._store, project_id))

    def projects_get_verification(self, job_id: str) -> dict[str, Any] | None:
        """Read one verification by job_id (exact or an unambiguous 8+ char prefix)."""
        verification = _verification.get_verification(self._store, job_id)
        return verification.model_dump(mode="json") if verification is not None else None

    def projects_get_verification_for_contract(self, contract_id: str) -> dict[str, Any] | None:
        """The most recent verification for a contract's latest attempt."""
        verification = _verification.get_verification_for_contract(self._store, contract_id)
        return verification.model_dump(mode="json") if verification is not None else None

    # ---- read: quota / brake / cost / jobs --------------------------------

    def projects_quota(self, engine: str) -> dict[str, Any]:
        """Current quota reading for "codex" or "claude_code": per window, used%,
        how far through the window, and the projected end% at this pace."""
        reading = _quota.read_quota_sync(engine)  # type: ignore[arg-type]
        now = datetime.now(UTC)
        return {
            "engine": reading.engine,
            "source": reading.source,
            "observed_at": reading.observed_at.isoformat(),
            "age_seconds": reading.age_seconds(now=now),
            "error": reading.error,
            "windows": [
                {
                    "window_id": w.window_id,
                    "used_percent": w.used_percent,
                    "duration_minutes": w.duration_minutes,
                    "resets_at": w.resets_at.isoformat() if w.resets_at else None,
                    "elapsed_fraction": w.elapsed_fraction(now=reading.observed_at),
                    "projected_end_percent": w.projected_end_percent(now=reading.observed_at),
                }
                for w in reading.windows
            ],
        }

    def projects_brake_status(self, project_id: str, engine: str) -> dict[str, Any]:
        """Whether this project's delegated work on ``engine`` would be admitted
        right now -- read-only: never reserves capacity (unlike actually
        starting a job, which must call the brake for real)."""
        brake_enabled = _brake.get_project_brake_enabled(self._store, project_id)
        reading = _quota.read_quota_sync(engine)  # type: ignore[arg-type]
        budget = _admission.budget_for(engine)  # type: ignore[arg-type]
        in_flight = _admission.in_flight_count(self._store, engine)  # type: ignore[arg-type]
        if not brake_enabled:
            return {
                "project_id": project_id,
                "engine": engine,
                "brake_enabled": False,
                "would_admit": True,
                "detail": "project brake is switched off; engine quota is not consulted for this project",
            }
        decision = _admission.decide(reading, budget, operator_directed=False, in_flight=in_flight)
        return {
            "project_id": project_id,
            "engine": engine,
            "brake_enabled": True,
            "would_admit": decision.allowed,
            "reason": decision.reason,
            "detail": decision.detail,
            "used_percent": decision.used_percent,
            "effective_percent": decision.effective_percent,
            "ceiling_percent": decision.ceiling_percent,
            "autonomous_percent": decision.autonomous_percent,
            "resets_at": decision.resets_at.isoformat() if decision.resets_at else None,
            "shadow": budget.shadow,
        }

    def projects_cost_report(self, project_id: str) -> dict[str, Any]:
        """Today / rolling-7-day spend for this project's delegated jobs."""
        return _cost_report.project_cost_report(self._store, project_id)

    def projects_jobs(self, project_id: str) -> list[dict[str, Any]]:
        """Every delegated-job record attributed to this project, as stored."""
        return _cost_report.project_jobs(self._store, project_id)

    # ---- write: project lifecycle ------------------------------------------

    def projects_create(
        self,
        project_id: str,
        title: str,
        objective: str,
        owner: str = "claude",
        size: str | None = None,
        risk: str | None = None,
        process: str | None = None,
        classification_rationale: str | None = None,
    ) -> str:
        """Create a new project AS A DRAFT, owned by ``owner`` (default "claude").

        A draft does not run: review its plan with projects_review_plan, then
        projects_promote, before any task can be claimed or delegated.
        """
        try:
            record = _records.open_project(
                self._store,
                project_id=project_id,
                title=title,
                objective=objective,
                size=size,
                risk=risk,
                process=process,
                classification_rationale=classification_rationale,
            )
        except ValueError as exc:
            return f"REJECTED: {exc}"
        refusal = _owner.set_project_owner(self._store, project_id, owner)
        if refusal is not None:
            return f"created project {record.project_id!r} AS A DRAFT, but could not set owner: {refusal}"
        return (
            f"created project {record.project_id!r} AS A DRAFT, owner={owner!r}: {record.title} -- {record.objective}. "
            "It will NOT run until projects_review_plan + projects_promote accept a plan with a real deadline "
            "and subtasks carrying acceptance criteria that could fail."
        )

    def projects_review_plan(
        self, project_id: str, observable_result: str, deadline: str, subtasks: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Run the deterministic intake checklist against a plan, and if it
        passes, record that THIS caller reviewed it (by digest).

        ``subtasks`` is ``[{"text": ..., "acceptance_criteria": [...]}, ...]``.
        Judging whether each criterion could actually FAIL is the caller's
        own job before calling this -- the checklist only catches mechanical
        gaps (missing fields, assurance-only criteria). Call projects_promote
        next, with the EXACT same arguments, to actually move the project
        from draft to open.
        """
        project = _records.get_project(self._store, project_id)
        if project is None:
            return {"ok": False, "findings": [f"no project named {project_id!r}"], "digest": None}
        verdict = _intake.check_project_intake(
            objective=project.objective,
            observable_result=observable_result,
            deadline=deadline,
            subtasks=subtasks,
        )
        if not verdict.ok:
            return {"ok": False, "findings": verdict.findings, "digest": None}
        digest = _intake.plan_digest(
            objective=project.objective, observable_result=observable_result, deadline=deadline, subtasks=subtasks
        )
        recorded = _intake.record_project_review(self._store, project_id, reviewed_digest=digest)
        return {"ok": True, "findings": [], "digest": digest, "recorded": recorded}

    def projects_promote(
        self, project_id: str, observable_result: str, deadline: str, subtasks: list[dict[str, Any]]
    ) -> str:
        """Move a project from draft to open, installing ``subtasks`` as its plan.

        Refused unless projects_review_plan already recorded a matching
        digest for THESE EXACT arguments.
        """
        return _intake.promote_project_plan(
            self._store, project_id, observable_result=observable_result, deadline=deadline, subtasks=subtasks
        )

    def projects_pause(self, project_id: str) -> str:
        """Pause an open project. Pure record bookkeeping -- does not stop any
        specialist or running job (that stays LazyCEO's own policy)."""
        if _records.pause_project(self._store, project_id):
            return f"paused project {project_id!r}"
        record = _records.get_project(self._store, project_id)
        if record is None:
            return f"REJECTED: no project named {project_id!r}"
        return f"REJECTED: project {project_id!r} is {record.status!r} -- only an open project can be paused"

    def projects_resume(self, project_id: str) -> str:
        """Resume a paused project, or deliberately reopen a done one."""
        if _records.resume_project(self._store, project_id):
            return f"resumed project {project_id!r}"
        record = _records.get_project(self._store, project_id)
        if record is None:
            return f"REJECTED: no project named {project_id!r}"
        return f"REJECTED: project {project_id!r} is already {record.status!r}"

    def projects_close(self, project_id: str) -> str:
        """Close a project as done. Refused while a task is still open (not
        done/cancelled) -- retire what no longer applies first."""
        from lazybridge.ext.planners import DurableBlackboard

        record = _records.get_project(self._store, project_id)
        if record is None:
            return f"REJECTED: no project named {project_id!r}"
        unfinished = {
            index: task["status"]
            for index, task in enumerate(DurableBlackboard(self._store, plan_id=f"project:{project_id}").snapshot().tasks)
            if task["status"] not in ("done", "cancelled")
        }
        if unfinished:
            listed = ", ".join(f"{index}: {status}" for index, status in unfinished.items())
            return f"REJECTED: project {project_id!r} still has unfinished task(s): {listed}."
        if _records.close_project(self._store, project_id):
            return f"closed project {project_id!r}"
        return f"REJECTED: no project named {project_id!r}"

    def projects_set_owner(self, project_id: str, owner: str) -> str:
        """Reassign a project's owner (ceo/claude/shared). One write, no data copy."""
        refusal = _owner.set_project_owner(self._store, project_id, owner)
        return refusal or f"project {project_id!r} is now owned by {owner!r}"

    def projects_set_brake_enabled(self, project_id: str, enabled: bool) -> str:
        """Turn the per-project quota brake on or off. Does not touch engine-wide ceilings."""
        _brake.set_project_brake_enabled(self._store, project_id, enabled)
        return f"project {project_id!r} quota brake is now {'ON' if enabled else 'OFF'}"

    def projects_set_deadline(self, project_id: str, target_completion_at: str | None, schedule_timezone: str | None = None) -> str:
        """Set or clear a project's deadline. ``target_completion_at`` is an ISO
        timestamp with an explicit offset (e.g. "2026-10-01T18:00+02:00"), or
        None to clear it."""
        parsed = None
        if target_completion_at is not None:
            parsed = _intake.parse_deadline(target_completion_at)
            if parsed is None:
                return f"REJECTED: target_completion_at {target_completion_at!r} names no date"
        try:
            ok = _records.set_project_deadline(
                self._store, project_id, target_completion_at=parsed, schedule_timezone=schedule_timezone
            )
        except ValueError as exc:
            return f"REJECTED: {exc}"
        return f"deadline set for project {project_id!r}" if ok else f"REJECTED: no open/paused/done project named {project_id!r}"

    def projects_add_note(self, project_id: str, text: str, origin: str = "claude") -> str:
        """Record a freeform note against a project."""
        note_id = _notes.add_project_note(self._store, project_id, text, origin=origin)
        return f"note {note_id} recorded for project {project_id!r}"

    # ---- write: plan editing ----------------------------------------------

    def projects_retire_task(
        self, project_id: str, task_index: int, expected_text: str, reason: str, disposition: str, superseded_by_task_index: int | None = None
    ) -> str:
        """Retire one task (no longer part of the work). ``disposition`` is one
        of obsolete/superseded/wrong_plan -- never for FINISHED work."""
        return _plan_edit.retire_task(self._store, project_id, task_index, expected_text, reason, disposition, superseded_by_task_index)

    def projects_reopen_task(self, project_id: str, task_index: int, expected_text: str, reason: str) -> str:
        """Put a failed/retired task back to todo with a fresh attempt budget (bounded)."""
        return _plan_edit.reopen_task(self._store, project_id, task_index, expected_text, reason)

    def projects_reopen_done_task(self, project_id: str, task_index: int, expected_text: str, owner: str, reason: str) -> str:
        """Put a DONE task back to claimed because its closing verification is
        now known to have been vacuous. The one exception to "done stays done"."""
        return _plan_edit.reopen_done_task_for_invalid_closure(self._store, project_id, task_index, expected_text, owner=owner, reason=reason)

    def projects_revise_plan(self, project_id: str, reason: str, new_tasks: list[str], keep: list[int] | None = None) -> str:
        """Retire every retirable task (except those in ``keep``) and append
        ``new_tasks``, in one write."""
        message, _retired, _first_new = _plan_edit.revise_plan(self._store, project_id, reason, new_tasks, keep=tuple(keep or ()))
        return message

    def projects_schedule_task(
        self, project_id: str, task_index: int, expected_text: str, reason: str, planned_start_at: str | None = None, due_at: str | None = None, hold: bool = False
    ) -> str:
        """Set or clear a task's planned-start/due dates (ISO timestamps) and its
        start_hold flag (True = this date waits on something that is not a task)."""
        start_epoch = _iso_to_epoch(planned_start_at)
        due_epoch = _iso_to_epoch(due_at)
        if (planned_start_at is not None and start_epoch is None) or (due_at is not None and due_epoch is None):
            return "REJECTED: planned_start_at/due_at must be ISO timestamps or None"
        return _plan_edit.set_task_schedule(
            self._store, project_id, task_index, expected_text, planned_start_at=start_epoch, due_at=due_epoch, reason=reason, hold=hold
        )

    # ---- write: contracts & verification decisions -------------------------

    def projects_open_contract(
        self,
        project_id: str,
        task_index: int,
        repo: str,
        observable_result: str,
        acceptance_criteria: list[str],
        required_checks: list[str],
        risk: str,
        allowed_effects: str,
        requires_review: bool = True,
        review_waiver: str | None = None,
        base_branch: str | None = None,
    ) -> dict[str, Any] | str:
        """Open a task's durable acceptance contract BEFORE delegating it."""
        try:
            contract = _contracts.open_task_contract(
                self._store,
                project_id=project_id,
                task_index=task_index,
                repo=repo,
                observable_result=observable_result,
                acceptance_criteria=acceptance_criteria,
                required_checks=required_checks,
                risk=risk,
                allowed_effects=allowed_effects,
                requires_review=requires_review,
                review_waiver=review_waiver,
                base_branch=base_branch,
            )
        except ValueError as exc:
            return f"REJECTED: {exc}"
        return contract.model_dump(mode="json")

    def projects_accept_verification(self, job_id: str, reviewer: str, reason: str) -> dict[str, Any] | str:
        """Accept a finished attempt -- only once its checks passed and (if
        required) an independent review is attached and non-empty-scope."""
        try:
            verification = _verification.accept(self._store, job_id, reviewer=reviewer, reason=reason)
        except ValueError as exc:
            return f"REJECTED: {exc}"
        if verification is None:
            return f"REJECTED: no running verification found for job {job_id!r} (or it changed underneath this call)"
        return verification.model_dump(mode="json")

    def projects_request_rework(self, job_id: str, reviewer: str, reason: str) -> dict[str, Any] | str:
        """Send a finished attempt back for rework."""
        verification = _verification.request_rework(self._store, job_id, reviewer=reviewer, reason=reason)
        if verification is None:
            return f"REJECTED: no running verification found for job {job_id!r}"
        return verification.model_dump(mode="json")

    def projects_block_verification(self, job_id: str, reviewer: str, reason: str) -> dict[str, Any] | str:
        """Escalate a finished attempt to a human decision."""
        verification = _verification.block(self._store, job_id, reviewer=reviewer, reason=reason)
        if verification is None:
            return f"REJECTED: no running verification found for job {job_id!r}"
        return verification.model_dump(mode="json")

    def projects_retry_review(self, job_id: str) -> dict[str, Any] | str:
        """Re-run checks and review for an attempt whose review never ran or ran on an empty diff."""
        try:
            verification = _verification.retry_review(self._store, job_id)
        except ValueError as exc:
            return f"REJECTED: {exc}"
        if verification is None:
            return f"REJECTED: no running verification found for job {job_id!r}"
        return verification.model_dump(mode="json")

    def projects_retry_harness(self, job_id: str) -> dict[str, Any] | str:
        """Re-run checks for an attempt blocked by an environment/harness failure."""
        try:
            verification = _verification.retry_harness(self._store, job_id)
        except ValueError as exc:
            return f"REJECTED: {exc}"
        if verification is None:
            return f"REJECTED: no blocked verification found for job {job_id!r}"
        return verification.model_dump(mode="json")

    def projects_reopen_for_empty_review(self, job_id: str) -> dict[str, Any] | str:
        """Send an ACCEPTED verification back to pending because, from git, its
        review is now known to have run against an empty diff."""
        verification = _verification.reopen_for_empty_review(self._store, job_id)
        if verification is None:
            return f"REJECTED: no accepted verification found for job {job_id!r}"
        return verification.model_dump(mode="json")

    # ---- wiring -------------------------------------------------------------

    def as_tools(self) -> list[Any]:
        from lazybridge import Tool

        read_methods = [
            self.projects_list,
            self.projects_get,
            self.projects_schedule,
            self.projects_timeline,
            self.projects_notes,
            self.projects_board_summary,
            self.projects_find_contract,
            self.projects_get_contract,
            self.projects_repos_for_project,
            self.projects_get_verification,
            self.projects_get_verification_for_contract,
            self.projects_quota,
            self.projects_brake_status,
            self.projects_cost_report,
            self.projects_jobs,
        ]
        tools = [Tool.wrap(method, name=method.__name__) for method in read_methods]
        if not self._allow_write:
            return tools

        write_methods = [
            self.projects_create,
            self.projects_review_plan,
            self.projects_promote,
            self.projects_pause,
            self.projects_resume,
            self.projects_close,
            self.projects_set_owner,
            self.projects_set_brake_enabled,
            self.projects_set_deadline,
            self.projects_add_note,
            self.projects_retire_task,
            self.projects_reopen_task,
            self.projects_reopen_done_task,
            self.projects_revise_plan,
            self.projects_schedule_task,
            self.projects_open_contract,
            self.projects_accept_verification,
            self.projects_request_rework,
            self.projects_block_verification,
            self.projects_retry_review,
            self.projects_retry_harness,
            self.projects_reopen_for_empty_review,
        ]
        tools += [Tool.wrap(method, name=method.__name__) for method in write_methods]
        return tools


def _iso_to_epoch(value: str | None) -> float | None:
    if value is None:
        return None
    parsed = _intake.parse_deadline(value)
    return parsed.timestamp() if parsed is not None else None


__all__ = ["ProjectsTools"]
