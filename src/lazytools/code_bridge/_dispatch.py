"""Restart-safe dispatch of a projects task graph through bridge jobs."""
from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from lazybridge import Store
from lazybridge.ext.knowledge import DEFAULT_PREFIX, DurableKnowledgeBase, slugify

from lazytools.code_bridge import (
    _cloud,
    _cloud_land,
    _cloud_place,
    _concurrency,
    _dispatch_launch,
    _dispatch_workspace,
    _jobs,
    _routing,
    _store,
)
from lazytools.code_bridge._cloud import CloudError
from lazytools.code_bridge._lockfile import _pid_alive
from lazytools.connectors.projects.tools import _resolve_store_db
from lazytools.projects.plan_edit import _board_key
from lazytools.projects.task_fields import task_id as board_task_id
from lazytools.projects.task_service import (
    AttemptContinuation,
    CompletionEvidence,
    PlacementEvidence,
    ProjectTaskService,
    TaskPrincipal,
)
from lazytools.projects.task_store import TaskServiceError, transaction

TERMINAL = frozenset({"done", "partial", "failed", "interrupted", "cancelled"})
DEFAULT_STALE_MINUTES = 10
DEFAULT_TOOL_STALE_MINUTES = 60  # one tool call running longer than this is stale
DEFAULT_MAX_CHECK_RETRIES = 2
STALE_HEARTBEAT = "engine heartbeat went stale"
# Engine crashes (not task failures): retried once with a fresh attempt.
RETRYABLE_FAILURES = ("App Server exited", "outcome is unknown", "3221226505", "0xC0000409",
                      "no new commit since the task started", STALE_HEARTBEAT, "database is locked")


def _tool_call_in_progress(job: dict[str, Any], now: float, bound: float) -> bool:
    """A tool call is running in a live job process and within its own bound.

    The engine emits no events while a tool (a long shell command) runs, so
    the event heartbeat alone would cancel healthy jobs. The engine is driven by
    the job's worker process (``pid``): when the engine exits, the worker ends
    the turn and clears its tool calls; when the worker dies, nothing is left
    to finish them.
    """
    calls = job.get("tool_calls")
    pid = job.get("pid")
    if not isinstance(calls, dict) or not calls or not isinstance(pid, int) or not _pid_alive(pid):
        return False
    starts = [call.get("started_at") if isinstance(call, dict) else None for call in calls.values()]
    return all(isinstance(start, (int, float)) and now - start < bound for start in starts)


def _has_stale_heartbeat(job: dict[str, Any], cutoff: float, *, now: float | None = None,
                         tool_bound: float = DEFAULT_TOOL_STALE_MINUTES * 60) -> bool:
    heartbeat = job.get("heartbeat_at")
    if job.get("status") != "running" or not isinstance(heartbeat, (int, float)) or heartbeat >= cutoff:
        return False
    return not _tool_call_in_progress(job, time.time() if now is None else now, tool_bound)


@dataclass(frozen=True)
class Task:
    task_id: str
    revision: str
    status: str
    data: dict[str, Any]
    dispatch: dict[str, Any] | None
    hold_reason: str | None = None  # reason recorded with the task's current start_hold


def _tasks(service: ProjectTaskService, project_id: str) -> list[Task]:
    with transaction(service.path) as store:
        board = store.read(_board_key(project_id), {"tasks": []})
        holds: dict[int, str] = {}
        for event in board.get("schedule_events", []):
            if event.get("hold") and event.get("reason") and "task_index" in event:
                holds[event["task_index"]] = str(event["reason"])  # later events win
        return [Task(f"{project_id}/{board_task_id(data, index)}", data.get("revision", "0"),
                     data.get("status", "todo"), data,
                     data.get("dispatches", [None])[-1] if data.get("dispatches") else None,
                     hold_reason=holds.get(index))
                for index, data in enumerate(board["tasks"])]


class Dispatcher:
    def __init__(self, service: ProjectTaskService, principal: TaskPrincipal, launcher: _dispatch_launch.Launcher,
                 project_id: str, *, parallel: int = 1, emit: Callable[[str], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 task_reader: Callable[[ProjectTaskService, str], list[Task]] = _tasks,
                 clock: Callable[[], float] = time.time, orphan_grace: float = 30.0,
                 router: Callable[..., str | None] | None = None, max_retries: int = 1,
                 json_events: bool = False, knowledge_store: Store | None = None,
                 stale_minutes: int = DEFAULT_STALE_MINUTES,
                 capacity: Callable[[str], int | None] | None = None, router_with_engine: bool = False,
                 cloud_placer: Callable[[dict[str, Any], str], tuple[str, str]] | None = None,
                 land: bool = False):
        if not 1 <= parallel <= 100:
            raise ValueError("--parallel must be between 1 and 100")
        if stale_minutes < 1:
            raise ValueError("--stale-minutes must be at least 1")
        self.service, self.principal, self.launcher = service, principal, launcher
        self.project_id, self.parallel, self.sleep = project_id, parallel, sleep
        # Flush each event: the dispatcher is usually piped into a log or a watcher.
        self.emit: Callable[[str], None] = emit or (lambda line: print(line, flush=True))
        self.task_reader = task_reader
        self.clock, self.orphan_grace = clock, orphan_grace
        self.stale_seconds = stale_minutes * 60
        self.router = router
        self.capacity = capacity  # engine family -> local slot limit (None = unlimited)
        self.router_with_engine = router_with_engine  # router accepts (tier, engine_family)
        # (task data, tier) -> (engine family, cloud environment id); raises when cloud is not possible
        self.cloud_placer = cloud_placer
        self.land = land  # push each published branch to origin and open its PR
        # Accepted, published tasks awaiting landing; drained after every claimed task is settled.
        self.landings: list[tuple[Task, Path, str, str, list[str]]] = []
        self.reported_blocked: set[str] = set()
        self.unsupported: set[str] = set()
        self.waiting: set[str] = set()
        self.retries: dict[str, int] = {}
        self.seen_tickets: set[str] = set()
        self.cancelled: set[str] = set()
        self.max_retries = max_retries
        self.json_events = json_events
        self.knowledge_store = knowledge_store
        self.knowledge = DurableKnowledgeBase(knowledge_store) if knowledge_store is not None else None

    def _event(self, kind: str, task_id: str, detail: str = "", *, job_id: str | None = None) -> None:
        """Emit one event: a text line, or with --json the same keys wait-any events carry."""
        if self.json_events:
            self.emit(json.dumps({"kind": kind, "job_id": job_id, "task_id": task_id, "detail": detail}))
        elif kind == "launched":
            self.emit(f"launched {job_id}")
        else:
            self.emit(f"{kind} {task_id} {detail}" if detail else f"{kind} {task_id}")

    def _with_lessons(self, task: dict[str, Any]) -> dict[str, Any]:
        if self.knowledge is None:
            return task
        try:
            text = str(task["text"])
            matches = self.knowledge.find_lessons(text, limit=3)
            if matches:
                lines = []
                for lesson in matches:
                    topic = " ".join(str(lesson.get("topic", "")).split())[:500]
                    line = f"- {topic}"
                    for field, label in (("what_worked", "What worked"), ("gotchas", "Gotchas")):
                        value = " ".join(str(lesson.get(field, "")).split())[:500]
                        if value:
                            line += f" | {label} — {value}"
                    lines.append(line)
                return {**task, "text": "Known lessons\n" + "\n".join(lines) + "\n\n" + text}
        except Exception as exc:
            self._event("knowledge-error", task["task_id"], f"could not read lessons: {exc}")
        return task

    def _launch_input(self, task: dict[str, Any], claim: dict[str, Any]) -> dict[str, Any]:
        """The task as launched: a retry continues the failed attempt's work and, if it can, its conversation.

        The continuation is read from the task record, so it survives a dispatcher restart.
        """
        resume = task.get("attempt_continuation")
        if not resume:
            return self._with_lessons(task)
        resumes = _dispatch_launch._resumes(resume, claim.get("engine_family"))
        check_failures = resume.get("check_failures")
        if check_failures and not resumes:
            raise ValueError("Check repair requires the original engine conversation")
        branch = resume.get("preserved_branch")
        if branch:
            task = {**task, "preserved_branch": branch, "preserved_commit": resume.get("preserved_commit"),
                    "preserved_base": resume.get("preserved_base")}
        unresolved = list(resume.get("preserved_unresolved") or [])
        warning = ("" if not unresolved else
                   " The merge is UNRESOLVED: conflict markers remain in "
                   + ", ".join(unresolved[:20]) + (" ..." if len(unresolved) > 20 else "")
                   + ". Resolve every conflict first so the changes of both sides are kept, then commit;"
                   " the task is rejected while any marker remains.")
        if resumes:
            if check_failures:
                prompt = ("An acceptance check failed after your last turn. Fix the cause in this checkout, "
                          "rerun the failing checks and commit the fix." + warning + "\n\n" + check_failures[-1])
            else:
                prompt = _dispatch_launch.RESUME_PROMPT + warning
            return {**task, "text": prompt, "resume_session": resume.get("session"),
                    "resume_native_id": resume["native_id"]}
        task = self._with_lessons(task)
        if branch:
            changed = list(resume.get("preserved_files") or [])
            files = ", ".join(changed[:50]) + (" ..." if len(changed) > 50 else "")
            message = " ".join(str(resume.get("preserved_message") or "").split())[:500]
            task = {**task, "text": (
                "A previous attempt at this task was interrupted by the system. Its work is kept in this "
                f"checkout (also on branch {branch}). Files it changed: {files or 'none'}. Its last "
                f"commit message: {message}.{warning} Review that work, keep what is "
                "right and finish the task below.\n\n" + str(task["text"]))}
        return task

    def _retry(self, task: Task, cause: str, failure_reason: str, continuation: dict[str, Any],
               job_ref: str) -> bool:
        """Prepare the next attempt with its continuation stored durably in the task record."""
        current = next((item for item in self.task_reader(self.service, self.project_id)
                        if item.task_id == task.task_id), None)
        if current is None or current.revision != task.revision:
            return False
        reason = f"retry after {cause}: {failure_reason[:200]}"
        if continuation.get("preserved_branch"):
            reason += f" (work preserved on {continuation['preserved_branch']})"
        self.service.prepare_attempt(self.principal, task.task_id, current.revision, reason,
                                     continuation=AttemptContinuation(**continuation))
        self._event("retry", task.task_id, f"{cause}: {failure_reason[:120]}", job_id=job_ref)
        return True

    def _refused_resume(self, task: Task, job: dict[str, Any], continuation: dict[str, Any]) -> bool:
        """Settle a resume the bridge refused before submitting, then start a fresh conversation."""
        receipt = (task.dispatch or {})["receipt"]
        reason = str(job.get("error") or "resume refused").replace("\n", " ")
        self.service.fail_local_launch(self.principal, task.task_id, receipt["claim_token"],
                                       f"resume refused before submit: {reason}")
        if continuation.get("check_failures"):
            self._event("failed", task.task_id, f"resume refused before submit: {reason}", job_id=job.get("job_id"))
            return True
        # The refused job never ran: the checkout still holds the preserved work.
        return self._retry(task, "resume failed", reason, {**continuation, "native_id": None, "fresh": True},
                           str(job.get("job_id")))

    def _record_base(self, task_id: str, claim: dict[str, Any]) -> None:
        """Report a source that stayed stale and keep the checkout's base commit in the receipt."""
        why = getattr(self.launcher, "stale_bases", {}).pop(task_id, None)
        if why:
            self._event("waiting", task_id, f"source base is stale: {why}")
        base = getattr(self.launcher, "base_commits", {}).pop(task_id, None)
        if base:
            try:
                self.service.record_base_commit(self.principal, task_id, claim["claim_token"], base)
            except Exception as exc:
                self._event("receipt-error", task_id, f"could not record base commit: {exc}")

    def _record_lesson(self, task: Task, kind: str, reason: str) -> None:
        store = self.knowledge_store
        if store is None:
            return
        try:
            topic = f"{task.data.get('repo') or 'unknown repository'}: {kind}"
            slug = slugify(topic)
            key = DEFAULT_PREFIX + slug
            now = self.clock()
            gotchas = _cloud.redact(reason).strip()
            # LazyBridge requires what_worked in its public write API. Record unresolved
            # lessons with Store CAS so they stay searchable and can be revised once fixed.
            for _ in range(20):
                previous = store.read(key)
                lesson = previous or {
                    "topic": topic, "slug": slug, "what_worked": "", "status": "active",
                    "correction_reason": "", "created_at": now, "verified_at": now,
                }
                updated = {**lesson, "gotchas": gotchas, "updated_at": now,
                           "revision": lesson.get("revision", 0) + 1}
                if store.compare_and_swap(key, previous, updated):
                    return
            raise RuntimeError("concurrent lesson updates did not settle")
        except Exception as exc:
            self._event("knowledge-error", task.task_id, f"could not save lesson: {exc}")

    def _record_rejection(self, task: Task, job: dict[str, Any]) -> None:
        rejected_ticket_reason = getattr(self.launcher, "rejected_ticket_reason", None)
        if self.knowledge is None or rejected_ticket_reason is None:
            return
        try:
            reason = rejected_ticket_reason(job)
        except Exception as exc:
            self._event("knowledge-error", task.task_id, f"could not read rejected tickets: {exc}")
            return
        if reason:
            self._record_lesson(task, "rejected ticket", reason)

    def _surface_approvals(self, tasks: list[Task]) -> None:
        """Put every pending ticket of a running task into the event stream, once.

        The dispatcher does not decide tickets; it makes them visible to whoever
        orchestrates (`lazytools-code-bridge approve|reject <id>`), so a job never
        waits on a ticket nobody has seen.
        """
        pending = getattr(self.launcher, "pending", None)
        if pending is None:
            return
        by_job = {str((task.dispatch or {}).get("job_ref")): task.task_id
                  for task in tasks if task.status == "claimed" and (task.dispatch or {}).get("job_ref")}
        if not by_job:
            return
        for ticket in pending():
            approval_id = str(ticket.get("approval_id") or "")
            job_id = str(ticket.get("job_id") or "")
            task_id = by_job.get(job_id)
            if not approval_id or task_id is None or approval_id in self.seen_tickets:
                continue
            self.seen_tickets.add(approval_id)
            command = " ".join(str(ticket.get("command") or ticket.get("gist") or "").split())[:200]
            reason = " ".join(str(ticket.get("reason") or "").split())[:160]
            self._event("approval", task_id, f"{approval_id} {command} | {reason}", job_id=job_id)

    def _place(self, tasks: list[Task]) -> None:
        """Record local placement evidence for todo tasks whose dependencies are done.

        `next_ready` only returns tasks with fresh placement evidence; the quota router
        picks the engine family for the task's tier here, and the launch later pins it.
        """
        if self.router is None and self.cloud_placer is None:
            return
        status = {task.task_id.rsplit("/", 1)[1]: task.status for task in tasks}
        now = self.clock()
        picks: dict[tuple[str, str | None], str | None] = {}
        # Engine families already busy: claimed tasks plus tasks placed and not yet claimed.
        in_use: dict[str, int] = {}
        for task in tasks:
            if task.status == "claimed":
                receipt = (task.dispatch or {}).get("receipt") or {}
                # A cloud job takes no local engine slot.
                local = receipt.get("effective_placement", "local") == "local"
                family = str(receipt.get("engine_family") or "") if local else ""
            elif task.status == "todo":
                evidence = task.data.get("placement_evidence", {}).get("local", {})
                fresh = evidence.get("revision") == task.revision and                     evidence.get("evidence", {}).get("expires_at", 0) > now + 60
                family = str(evidence.get("evidence", {}).get("engine_family") or "") if fresh else ""
            else:
                family = ""
            if family:
                in_use[family] = in_use.get(family, 0) + 1
        for task in tasks:
            data = task.data
            requested = data.get("requested_placement", "local")
            placer = self.router if requested == "local" else self.cloud_placer if requested == "cloud" else None
            if (task.status != "todo" or placer is None
                    or data.get("review_of") or data.get("acceptance_contract")
                    or any(status.get(dep) != "done" for dep in data.get("dependency_ids", []))):
                continue
            record = data.get("placement_evidence", {}).get(requested, {})
            evidence = record.get("evidence", {})
            continuation = data.get("attempt_continuation") or {}
            required_engine = None
            if continuation.get("check_failures"):
                engine = continuation["engine"]
                required_engine = "claude_code" if engine == "claude" else engine
            if (record.get("revision") == task.revision and evidence.get("expires_at", 0) > now + 60
                    and (not required_engine or evidence.get("engine_family") == required_engine)):
                continue
            tier = data.get("tier") or "thinking"
            if requested == "cloud":
                self._place_cloud(task, tier, now)
                continue
            assert self.router is not None
            route_key = (tier, required_engine)
            if route_key not in picks:
                try:
                    picks[route_key] = (self.router(tier, required_engine) if required_engine and self.router_with_engine
                                        else self.router(tier))
                except Exception as exc:
                    picks[route_key] = None
                    self._event("waiting", task.task_id, f"routing failed: {str(exc).replace(chr(10), ' ')}")
            engine = picks[route_key]
            if required_engine and engine != required_engine:
                engine = None
            limit = self.capacity(engine) if engine and self.capacity else None
            if not required_engine and engine and limit and in_use.get(engine, 0) >= limit:
                # The routed engine is full: ask for the other one instead of queueing for a slot.
                other = "claude_code" if engine == "codex" else "codex"
                try:
                    alternative = self.router(tier, other) if self.router_with_engine else None
                except Exception:
                    alternative = None
                other_limit = self.capacity(other) if alternative and self.capacity else None
                if alternative and not (other_limit and in_use.get(alternative, 0) >= other_limit):
                    engine = alternative
            if engine is None:
                if task.task_id not in self.waiting:
                    self._event("waiting", task.task_id, f"no eligible engine for tier {tier}")
                    self.waiting.add(task.task_id)
                continue
            self.waiting.discard(task.task_id)
            self.service.record_placement(task.task_id, task.revision, PlacementEvidence(
                placement="local", eligible=True, reason=f"quota router picked {engine} for tier {tier}",
                reference=f"dispatch-{task.task_id}-{task.revision}-{int(now)}",
                expires_at=now + self.service.lease_seconds + 60, engine_family=engine,
            ))
            in_use[engine] = in_use.get(engine, 0) + 1
            self._event("placed", task.task_id, f"local {engine} tier={tier}")

    def _place_cloud(self, task: Task, tier: str, now: float) -> None:
        """Record cloud placement evidence: the routed engine and its cloud environment."""
        assert self.cloud_placer is not None
        try:
            engine, environment = self.cloud_placer(task.data, tier)
        except _dispatch_launch.RepositoryMismatch as exc:
            # Not a transient condition: the task is not placed, so it can never be claimed.
            self.waiting.discard(task.task_id)
            if task.task_id not in self.unsupported:
                self.unsupported.add(task.task_id)
                self._event("failed", task.task_id, f"cloud placement refused: {str(exc).replace(chr(10), ' ')}")
            return
        except Exception as exc:
            if task.task_id not in self.waiting:
                self._event("waiting", task.task_id, f"cloud placement failed: {str(exc).replace(chr(10), ' ')}")
                self.waiting.add(task.task_id)
            return
        self.waiting.discard(task.task_id)
        self.service.record_placement(task.task_id, task.revision, PlacementEvidence(
            placement="cloud", eligible=True,
            reason=f"quota router picked {engine} for tier {tier}; cloud environment {environment}",
            reference=f"dispatch-{task.task_id}-{task.revision}-{int(now)}-cloud",
            expires_at=now + self.service.lease_seconds + 60, engine_family=engine, environment_id=environment,
        ))
        self._event("placed", task.task_id, f"cloud {engine} env={environment} tier={tier}")

    def _blocked(self, tasks: list[Task]) -> None:
        failed = {task.task_id.rsplit("/", 1)[1] for task in tasks if task.status == "failed"}
        for task in tasks:
            if (task.status == "todo" and failed.intersection(task.data.get("dependency_ids", []))
                    and task.task_id not in self.reported_blocked):
                self._event("blocked", task.task_id)
                self.reported_blocked.add(task.task_id)

    def _orphan_expired(self, dispatch: dict[str, Any], receipt: dict[str, Any]) -> bool:
        now = self.clock()
        created = float(dispatch.get("created_at", 0))
        return float(receipt.get("lease_expires_at", float("inf"))) <= now and now - created >= self.orphan_grace

    def _settle_missing(self, task: Task, dispatch: dict[str, Any], receipt: dict[str, Any], *,
                        cloud_absent: bool = False) -> bool:
        """Release an expired claim after its job has been ruled out."""
        if not self._orphan_expired(dispatch, receipt):
            return False
        job_ref = dispatch.get("job_ref")
        observation: Literal["absent", "terminated"] = "terminated" if job_ref and not cloud_absent else "absent"
        evidence_ref = "dispatcher:cloud-submission-absent" if cloud_absent else "dispatcher:no-job-after-grace"
        previous_orphans = sum(
            old.get("reconciliation_ref") in ("dispatcher:cloud-submission-absent", "dispatcher:no-job-after-grace")
            and old["receipt"]["revision"] == receipt.get("revision")
            for old in task.data.get("dispatches", [])[:-1]
        )
        reason = "provider has no matching dispatch" if cloud_absent else "no job after dispatch grace period"
        retry = previous_orphans + self.retries.get(task.task_id, 0) < self.max_retries
        self.service.reconcile_dispatch(
            self.principal, task.task_id, receipt["attempt"], observation, evidence_ref=evidence_ref,
            job_ref=job_ref, failure_reason=None if retry else reason,
        )
        self._event("reconciled", task.task_id, observation, job_id=job_ref)
        self._record_lesson(task, "missing job", reason)
        self._event("failed", task.task_id, reason, job_id=job_ref)
        return True

    def _conclude(
        self, task: Task, *, reference: str, job_ref: str, accepted: bool, expired: bool,
        artifact_ref: str | None = None, failure_reason: str | None = None,
        verification_id: str | None = None, reviewed_sha: str | None = None,
        review_passed: bool = False, review_independence: Any = None,
        preserved_branch: str | None = None, pr_url: str | None = None, land_failure: str | None = None,
    ) -> CompletionEvidence:
        """Record completion evidence and complete the claim; return the evidence the task settled on.

        Evidence already recorded under the reference decides (a restart replays it). After lease
        expiry the claim is closed through the service's reconcile path, which only takes a blocked
        result: accepted evidence becomes blocked, under a new reference when the old one is bound.
        """
        dispatch = task.dispatch or {}
        receipt = dispatch.get("receipt", {})
        records = dispatch.get("evidence", {})
        recorded = records.get(reference)
        if recorded:
            evidence = CompletionEvidence.model_validate(recorded)
        else:
            evidence = CompletionEvidence(
                reference=reference, job_ref=job_ref, executor=receipt["executor"],
                engine_family=receipt["engine_family"], state="accepted" if accepted else "blocked",
                artifact_ref=artifact_ref, verification_id=verification_id, reviewed_sha=reviewed_sha,
                review_passed=review_passed, review_independence=review_independence,
                outcome="success" if accepted else "task_failure", failure_reason=failure_reason,
                preserved_branch=preserved_branch, pr_url=pr_url, land_failure=land_failure,
            )
        if expired and evidence.state == "accepted":
            if recorded:
                reference = f"{reference}:lease-expired"
                recorded = records.get(reference)
            evidence = (CompletionEvidence.model_validate(recorded) if recorded else evidence.model_copy(update={
                "reference": reference, "state": "blocked", "outcome": "task_failure",
                "failure_reason": "claim lease expired",
            }))
        if not recorded:
            self.service.record_completion_evidence(self.principal, task.task_id, receipt["attempt"], evidence)
        accepted = evidence.state == "accepted"
        complete_options = {"reconcile_failure": True} if expired else {}
        self.service.complete(self.principal, task.task_id, receipt["claim_token"], evidence.reference,
                              evidence.state, evidence.reference, "success" if accepted else "task_failure",
                              **complete_options)
        return evidence

    def _settle_cloud(self, task: Task) -> bool:
        """Settle a cloud dispatch once its job is terminal and its landing is resolved.

        Accepted only when the job landed (PR url and pushed commit recorded) and the
        task's checks pass in a fresh local clone of the landed branch.
        """
        dispatch = task.dispatch or {}
        receipt = dispatch.get("receipt", {})
        token = receipt["claim_token"]
        job_ref = str(dispatch.get("job_ref") or _dispatch_launch.cloud_job_id(token))
        job = self.launcher.cloud_job(job_ref)  # type: ignore[attr-defined]
        if job is None:
            # Still `intent` with no job: begin_submit never succeeded (the store was busy, or the
            # dispatcher stopped first), so nothing reached the provider and the submit is still owed.
            if (dispatch.get("state") == "intent" and not dispatch.get("job_ref")
                    and receipt.get("executor") == self.principal.actor):
                return self._launch_cloud(task, receipt, receipt)
            return self._settle_missing(task, dispatch, receipt)
        if job.get("status") == "submitting" and not job.get("cloud_handle"):
            if not self._orphan_expired(dispatch, receipt):
                return False
            try:
                job = self.launcher.reconcile_cloud_submission(  # type: ignore[attr-defined]
                    job, task.task_id, receipt["attempt"])
            except CloudError as exc:
                self._event("waiting", task.task_id, f"cloud submission lookup failed: {_cloud.redact(str(exc))}",
                            job_id=job_ref)
                return False
            if job.get("cloud_handle"):
                self.service.reconcile_dispatch(self.principal, task.task_id, receipt["attempt"], "submitted",
                                                evidence_ref=f"bridge-job:{job_ref}", job_ref=job_ref)
                self._event("reconciled", task.task_id, "adopted", job_id=job_ref)
                return False  # the next pass can poll or cancel the adopted handle
            if job.get("status") == "submitting":
                return False
        if job.get("cloud_submission_absent") and not job.get("cloud_handle"):
            return self._settle_missing(task, dispatch, receipt, cloud_absent=True)
        if not dispatch.get("job_ref"):
            # Restarted between the submit and its acknowledgement: adopt the job.
            self.service.record_submit(self.principal, task.task_id, token, job_ref)
            self._event("reconciled", task.task_id, "adopted", job_id=job_ref)
        status = job.get("status")
        expired = float(receipt.get("lease_expires_at", float("inf"))) <= self.clock()
        landing_owed = status == "done" and _cloud_land.needs_finishing(job) and not job.get("land_failure")
        if status not in TERMINAL or landing_owed:
            cancel = getattr(self.launcher, "cancel", None)
            if (expired and status not in TERMINAL and cancel is not None and job_ref not in self.cancelled
                    and cancel(job_ref)):
                self.cancelled.add(job_ref)
                self._event("cancelled", task.task_id, "claim lease expired; settling once the job has stopped",
                            job_id=job_ref)
            return False
        checks = task.data.get("checks")
        reference = f"cloud-job:{job_ref}:{status}" + (":checks" if checks else "")
        recorded = dispatch.get("evidence", {}).get(reference)
        landed = bool(status == "done" and job.get("landed_at") and job.get("pr_url") and job.get("pushed_sha"))
        land_error = job.get("land_failure") or job.get("land_error")
        land_failure = _cloud.redact(str(land_error)).strip() if land_error else None
        failure: str | None
        if recorded:  # restarted after recording: the evidence decides, the checks are not run again
            failure = None if recorded.get("state") == "accepted" else (
                recorded.get("failure_reason") or "cloud job failed")
        elif status != "done":
            failure = f"cloud job {status}: {_cloud.redact(str(job.get('error') or 'no error recorded'))}"
        elif not landed:
            detail = land_failure or "no PR url or pushed commit recorded"
            failure = f"cloud job did not land: {detail}"
        elif expired:
            failure = "claim lease expired"
        elif checks:
            try:
                cwd = self.launcher.cloud_checkout(task.task_id, job)  # type: ignore[attr-defined]
            except Exception as exc:
                failure = f"could not clone the landed branch: {_cloud.redact(str(exc))[:300]}"
            else:
                try:
                    failure = (
                        "\n".join(filter(None, (_dispatch_workspace._check_failure(command, cwd) for command in checks)))
                        or _dispatch_workspace._checks_changed_checkout(cwd, job.get("pushed_sha"))
                    )
                finally:
                    remove = getattr(self.launcher, "remove_cloud_checkout", None)
                    if remove is not None:
                        remove(cwd)
            expired = float(receipt.get("lease_expires_at", float("inf"))) <= self.clock()
            failure = failure or ("claim lease expired" if expired else None)
        else:
            failure = None
        settled = self._conclude(
            task, reference=reference, job_ref=job_ref, accepted=failure is None, expired=expired,
            artifact_ref=str(job["pushed_sha"]) if landed else None,
            failure_reason=failure, pr_url=job.get("pr_url"), land_failure=land_failure or None,
        )
        if settled.state == "accepted":
            self._event("published", task.task_id, settled.pr_url or str(job["pr_url"]), job_id=job_ref)
            self._event("done", task.task_id, job_id=job_ref)
            return True
        failure = settled.failure_reason or failure
        assert failure is not None
        self._record_lesson(task, "task failure", failure)
        self._event("failed", task.task_id, failure.split("\n", 1)[0], job_id=job_ref)
        return True

    def _launch_cloud(self, task: Task, summary: dict[str, Any], claim: dict[str, Any]) -> bool:
        """Submit a claimed cloud task through `run --where cloud`; record its job.

        True while the claim stays held: launched, or the submit is deferred to a later pass.
        """
        task_id, token = summary["task_id"], claim["claim_token"]
        job_id = _dispatch_launch.cloud_job_id(token)
        try:
            self.service.begin_submit(self.principal, task_id, token)
        except Exception as exc:
            if isinstance(exc, TaskServiceError) and exc.code == "busy":
                self._event("deferred", task_id, str(exc))  # the claim stays `intent`: see _settle_cloud
                return True
            reason = str(exc).replace("\n", " ")
            self.service.fail_dispatch_launch(self.principal, task_id, token, reason)  # back to todo
            self._event("failed", task_id, reason)
            return False
        try:
            job_ref = self.launcher.launch_cloud(  # type: ignore[attr-defined]
                self._with_lessons({**summary, **task.data}), claim, job_id)
        except Exception as exc:
            reason = _cloud.redact(str(exc)).replace("\n", " ")
            job = self.launcher.job(job_id)
            if job is None:  # nothing reached the provider
                try:
                    self.service.fail_local_launch(self.principal, task_id, token, reason)
                except Exception as release_exc:
                    reason = f"{reason}; claim release failed: {str(release_exc).replace(chr(10), ' ')}"
                self._record_lesson(task, "launch failure", reason)
                self._event("failed", task_id, reason)
                return False
            if job.get("status") == "submitting" and not job.get("cloud_handle"):
                return True  # settle the incomplete submission from provider evidence after expiry
            job_ref = job_id  # a failed submit is recorded; settlement reports it
        self.service.record_submit(self.principal, task_id, token, job_ref)
        self._event("launched", task_id, job_id=job_ref)
        return True

    def _settle(self, task: Task) -> bool:
        dispatch = task.dispatch or {}
        receipt = dispatch.get("receipt", {})
        if receipt.get("effective_placement") == "cloud":
            return self._settle_cloud(task)
        job_ref = dispatch.get("job_ref")
        job = self.launcher.job(job_ref) if job_ref else self.launcher.find(task.task_id, receipt.get("attempt", 0))
        continuation = task.data.get("attempt_continuation")
        resumed = _dispatch_launch._resumes(continuation, receipt.get("engine_family"))
        if (job and not job_ref and resumed and continuation and dispatch.get("state") == "intent"
                and job.get("status") in TERMINAL
                and any(marker in str(job.get("error") or "") for marker in _dispatch_launch.RESUME_FAILURES)):
            # Refused before submit (check_resume, governed resume guard): the dispatch never
            # reaches the provider boundary, so reconciliation below could never settle it.
            self._refused_resume(task, job, continuation)
            return True
        if job and not job_ref and dispatch.get("state") == "intent":
            pid = job.get("pid")
            if job.get("status") in TERMINAL or (isinstance(pid, int) and not _pid_alive(pid)):
                return self._settle_missing(task, dispatch, receipt)
        if job and not job_ref:
            try:
                self.service.reconcile_dispatch(self.principal, task.task_id, receipt["attempt"], "submitted",
                                                evidence_ref=f"bridge-job:{job['job_id']}", job_ref=job["job_id"])
            except TaskServiceError as exc:
                if "provider boundary" in str(exc):
                    return False  # the child is still waiting for an engine slot
                raise
            job_ref = str(job["job_id"])
            self._event("reconciled", task.task_id, "adopted", job_id=job_ref)
        if not job:
            return self._settle_missing(task, dispatch, receipt)
        if job.get("status") not in TERMINAL:
            now = self.clock()
            lease_end = float(receipt.get("lease_expires_at", float("inf")))
            cutoff = now - self.stale_seconds
            stale = _has_stale_heartbeat(job, cutoff, now=now)
            cancel = getattr(self.launcher, "cancel", None)
            if (not stale and lease_end > now) or cancel is None or str(job_ref) in self.cancelled:
                return False
            try:
                stopped = cancel(str(job_ref), stale_before=cutoff) if stale else cancel(str(job_ref))
            except Exception as exc:
                self._event("waiting", task.task_id, f"cancel failed: {str(exc).replace(chr(10), ' ')[:160]}",
                            job_id=str(job_ref))
                return False
            if not stopped:
                return False
            self.cancelled.add(str(job_ref))
            reason = STALE_HEARTBEAT if stale else "claim lease expired"
            self._event("cancelled", task.task_id, f"{reason}; settling once the job has stopped", job_id=str(job_ref))
            job = self.launcher.job(str(job_ref))
            if not job or job.get("status") not in TERMINAL:
                return False
        checks = task.data.get("checks")
        reference = f"bridge-job:{job_ref}:{job.get('status')}"
        if checks:
            reference += ":checks"  # evidence for this gate survives a restart between recording and completion
        recorded = dispatch.get("evidence", {}).get(reference, {})
        recorded_failure = recorded.get("failure_reason")
        cwd = Path(str(job["cwd"])) if job.get("cwd") else None
        accepted = job.get("status") == "done" and not recorded_failure
        if accepted and cwd is not None:
            workspace_error, merge_commit = _dispatch_workspace._conclude_workspace(cwd, task.task_id)
            if workspace_error:
                accepted = False
                job = {**job, "error": workspace_error}
            elif merge_commit:
                self._event("committed", task.task_id, f"{merge_commit[:12]} (engine sandbox keeps .git read-only)",
                            job_id=str(job_ref))
            if accepted and not workspace_error:
                # The dispatcher's clone is written by nobody else once the job ended, so its
                # HEAD is the task's artifact, also after a restart that lost an earlier commit.
                head = _dispatch_workspace._clone_head(cwd)
                if head:
                    job = {**job, "final_commit": head}
            if accepted and not _dispatch_workspace._has_new_commit(cwd):
                accepted = False
                job = {**job, "error": "no new commit since the task started"}
        verdict = job.get("review_verdict") or {}
        reviewed_sha = job.get("reviewed_sha") or verdict.get("reviewed_sha")
        review_passed = bool(job.get("review_passed", verdict.get("passed", False)))
        review_independence = job.get("review_independence") or verdict.get("independence")
        verification_id = job.get("verification_id")
        needs_evidence = accepted and (
            (task.data.get("review_of") and (not reviewed_sha or not review_passed))
            or (task.data.get("contract_id") and not verification_id)
        )
        expired = float(receipt.get("lease_expires_at", float("inf"))) <= self.clock()
        job_error = str(job.get("error") or job.get("status") or "job failed").replace("\n", " ")
        check_failure = recorded_failure
        if accepted and checks and not (needs_evidence or expired or recorded):
            check_failure = (
                "\n".join(filter(None, (_dispatch_workspace._check_failure(command, cwd, tail_lines=60)
                                        for command in checks))) or None
            )
            if not check_failure and cwd is not None:
                # Evidence must describe the artifact that is published: a check that edits
                # tracked files (e.g. `ruff --fix`) or commits would pass on unpublished content.
                check_failure = _dispatch_workspace._checks_changed_checkout(cwd, job.get("final_commit"))
            expired = float(receipt.get("lease_expires_at", float("inf"))) <= self.clock()
        if check_failure:
            accepted = False
            failure_reason = check_failure
        elif needs_evidence:
            accepted = False
            failure_reason = "needs review evidence"
        elif expired:
            accepted = False
            failure_reason = job_error if STALE_HEARTBEAT in job_error else "claim lease expired"
        else:
            failure_reason = job_error
        # Publish only after the lease and evidence checks; a failed publish fails the task.
        published: str | None = None
        if accepted and cwd is not None:
            published, publish_error = _dispatch_workspace._publish(cwd)
            if publish_error:
                accepted = False
                failure_reason = publish_error
            elif published:
                self._event("published", task.task_id, published, job_id=str(job_ref))
        failed_cleanly = not accepted and not check_failure
        retryable = failed_cleanly and any(marker in failure_reason for marker in RETRYABLE_FAILURES)
        resume_failed = (failed_cleanly and resumed
                         and any(marker in failure_reason for marker in _dispatch_launch.RESUME_FAILURES))
        check_failures = list((continuation or {}).get("check_failures", []))
        native_id = None if resume_failed or task.data.get("review_of") else _jobs._native_id(job)
        retry_check = bool(
            check_failure and check_failure.startswith("check failed: ") and cwd is not None and not expired
            and native_id and job.get("engine") and len(check_failures) < task.data.get(
                "max_check_retries", DEFAULT_MAX_CHECK_RETRIES)
        )
        preserved = None
        if (retryable or (resume_failed and not check_failures) or retry_check) and cwd is not None:
            # Keep the engine's work, without committing files the failed checks may have edited.
            preserved, preserve_error = _dispatch_workspace._preserve_attempt(
                cwd, task.task_id, int(receipt["attempt"]), commit_uncommitted=not retry_check)
            if preserve_error:
                self._event("preserve-failed", task.task_id, preserve_error, job_id=str(job_ref))
            elif preserved:
                self._event("preserved", task.task_id, preserved.branch, job_id=str(job_ref))
        evidence_failure = check_failure
        if check_failure and check_failures and not retry_check and not recorded:
            evidence_failure = check_failure + "\n\nPrevious check failures\n" + "\n\n".join(check_failures)
        settled = self._conclude(
            task, reference=reference, job_ref=str(job_ref), accepted=accepted, expired=expired,
            artifact_ref=job.get("final_commit") or job.get("candidate_commit"),
            verification_id=verification_id, reviewed_sha=reviewed_sha, review_passed=review_passed,
            review_independence=review_independence, failure_reason=evidence_failure,
            preserved_branch=preserved.branch if preserved else None,
        )
        self._record_rejection(task, job)
        if settled.state == "accepted":
            self._event("done", task.task_id, job_id=str(job_ref))
            if self.land and published and cwd is not None:
                # Landed by pass_once only after every claimed task is settled, so network
                # latency here never consumes another task's lease.
                self.landings.append((task, cwd, published, str(job_ref), list(checks or [])))
            return True
        if not (resume_failed or retry_check):
            self._record_lesson(task, "engine crash" if retryable else "task failure", failure_reason)
        # A failed resume is retried once more in a fresh conversation, outside the retry budget.
        if retry_check or (resume_failed and not check_failures) or (
                retryable and not resume_failed and self.retries.get(task.task_id, 0) < self.max_retries):
            kept: dict[str, Any] = {
                "preserved_branch": preserved.branch, "preserved_commit": preserved.commit,
                "preserved_base": preserved.base,
                "preserved_files": list(preserved.files), "preserved_message": preserved.message,
                "preserved_unresolved": list(preserved.unresolved),
            } if preserved else {key: (continuation or {}).get(key) for key in (
                "preserved_branch", "preserved_commit", "preserved_base", "preserved_files", "preserved_message",
                "preserved_unresolved",
            ) if resume_failed and (continuation or {}).get(key)}
            if retry_check:
                check_failures.append(failure_reason)
                cause = "check failed"
            else:
                cause = "resume failed" if resume_failed else "engine crash"
            if self._retry(task, cause, failure_reason, {
                **kept, "session": job.get("session_name"), "native_id": native_id,
                "engine": job.get("engine"), "fresh": resume_failed, "check_failures": check_failures,
            }, str(job_ref)):
                if not (resume_failed or retry_check):
                    self.retries[task.task_id] = self.retries.get(task.task_id, 0) + 1
                return True
        self._event("failed", task.task_id, check_failure.split("\n", 1)[0] if check_failure else failure_reason,
                    job_id=str(job_ref))
        return True

    def _land_settled(self) -> None:
        """Land the tasks accepted in this pass; outcomes are follow-up events only."""
        landings, self.landings = self.landings, []
        for task, cwd, branch, job_ref, checks in landings:
            try:
                pr_url, land_failure = _dispatch_workspace._land(cwd, branch, task.task_id, task.data, checks)
            except Exception as exc:
                pr_url, land_failure = None, " ".join(_cloud.redact(str(exc)).split())[:500] or "landing failed"
            if pr_url:
                self._event("landed", task.task_id, pr_url, job_id=job_ref)
            elif land_failure:
                self._event("land-failed", task.task_id, land_failure, job_id=job_ref)

    def pass_once(self) -> tuple[int, int]:
        tasks = self.task_reader(self.service, self.project_id)
        for task in tasks:
            if task.status == "claimed":
                try:
                    self._settle(task)
                except Exception as exc:
                    kind = "deferred" if isinstance(exc, TaskServiceError) and exc.code == "busy" else "failed"
                    self._event(kind, task.task_id, str(exc).replace(chr(10), " "),
                                job_id=(task.dispatch or {}).get("job_ref"))
        self._land_settled()
        tasks = self.task_reader(self.service, self.project_id)
        self._surface_approvals(tasks)
        self._place(tasks)
        tasks = self.task_reader(self.service, self.project_id)
        running = sum(task.status == "claimed" for task in tasks)
        ready = self.service.next_ready(self.principal, self.project_id, limit=100)["tasks"]
        by_id = {task.task_id: task for task in tasks}
        running_branches = {
            str(task.data["assigned_branch"])
            for task in tasks
            if task.status == "claimed" and task.data.get("assigned_branch")
        }
        for summary in ready:
            if running >= self.parallel:
                break
            board_task = by_id.get(summary["task_id"])
            if board_task is None:
                self._event("failed", summary["task_id"], "task is missing from board")
                continue
            data = board_task.data
            self._event("ready", summary["task_id"])
            cloud = summary.get("effective_placement") == "cloud"
            if cloud and not hasattr(self.launcher, "launch_cloud"):
                reason = "cloud placement not supported by this launcher"
                self.unsupported.add(summary["task_id"])
                self._event("failed", summary["task_id"], reason)
                continue
            if data.get("review_of") or data.get("contract_id"):
                reason = "review/contract tasks not supported by dispatch v1"
                self.unsupported.add(summary["task_id"])
                self._event("failed", summary["task_id"], reason)
                continue
            if cloud:
                evidence = data.get("placement_evidence", {}).get("cloud", {}).get("evidence", {})
                if not self.launcher.cloud_free(str(evidence.get("engine_family") or "")):  # type: ignore[attr-defined]
                    if summary["task_id"] not in self.waiting:
                        self._event("waiting", summary["task_id"], "cloud concurrency limit reached")
                        self.waiting.add(summary["task_id"])
                    continue
                self.waiting.discard(summary["task_id"])
            branch = data.get("assigned_branch")
            if branch and str(branch) in running_branches:
                self._event("failed", summary["task_id"], "assigned branch already running")
                continue
            attempt = len(data.get("dispatches", [])) + 1
            try:
                result = self.service.claim(
                    self.principal, summary["task_id"], summary["revision"],
                    f"dispatch:{summary['task_id']}:{summary['revision']}:{attempt}",
                )
            except Exception as exc:
                kind = "deferred" if isinstance(exc, TaskServiceError) and exc.code == "busy" else "failed"
                self._event(kind, summary["task_id"], str(exc).replace(chr(10), " "))
                continue
            if not result.get("claim_active", True):
                self._event("failed", summary["task_id"], "claim is not active")
                continue
            claim = result["claim"]
            self._event("claimed", summary["task_id"])
            if cloud:
                if self._launch_cloud(board_task, summary, claim):
                    running += 1
                continue
            try:
                job_ref = self.launcher.launch(self._launch_input({**summary, **data}, claim), claim)
            except Exception as exc:
                self._record_base(summary["task_id"], claim)
                reason = str(exc).replace("\n", " ")
                try:
                    self.service.fail_local_launch(
                        self.principal, summary["task_id"], claim["claim_token"], reason,
                    )
                except Exception as release_exc:
                    reason = f"{reason}; claim release failed: {str(release_exc).replace(chr(10), ' ')}"
                self._record_lesson(board_task, "launch failure", reason)
                self._event("failed", summary["task_id"], reason)
            else:
                self._record_base(summary["task_id"], claim)
                self._event("launched", summary["task_id"], job_id=job_ref)
                running += 1
                if branch:
                    running_branches.add(str(branch))
        tasks = self.task_reader(self.service, self.project_id)
        self._blocked(tasks)
        return sum(task.status == "todo" for task in tasks), sum(task.status == "claimed" for task in tasks)

    def run(self, *, once: bool = False) -> int:
        while True:
            try:
                _, running = self.pass_once()
                ready = [task for task in self.service.next_ready(
                    self.principal, self.project_id, limit=100
                )["tasks"] if task["task_id"] not in self.unsupported]
            except TaskServiceError as exc:
                if exc.code != "busy":
                    raise
                self._event("deferred", self.project_id, str(exc))
                if once:
                    return 0
                self.sleep(15)
                continue
            if once or (not ready and not running and not self.waiting):
                return 0
            self.sleep(15)


def _dry_run(service: ProjectTaskService, project_id: str, emit: Callable[[str], None] = print,
             task_reader: Callable[[ProjectTaskService, str], list[Task]] = _tasks) -> int:
    tasks = task_reader(service, project_id)
    # Same gate as ProjectTaskService.next_ready: a start_hold task is never dispatched,
    # so it stays out of the waves and its dependants can never become ready.
    held = {task.task_id.rsplit("/", 1)[1]: task for task in tasks
            if task.status == "todo" and task.data.get("start_hold")}
    for identifier, task in held.items():
        emit(f"held {project_id}/{identifier}" + (f" ({task.hold_reason})" if task.hold_reason else ""))
    remaining = {task.task_id.rsplit("/", 1)[1]: set(task.data.get("dependency_ids", []))
                 for task in tasks if task.status == "todo" and task.task_id.rsplit("/", 1)[1] not in held}
    done = {task.task_id.rsplit("/", 1)[1] for task in tasks if task.status == "done"}
    number = 0
    while remaining:
        wave = [identifier for identifier, dependencies in remaining.items() if dependencies <= done]
        if not wave:
            break
        number += 1
        emit(f"wave {number}: {' '.join(wave)}")
        for identifier in wave:
            emit(f"ready {project_id}/{identifier}")
            done.add(identifier)
            del remaining[identifier]
    for identifier in remaining:
        emit(f"blocked {project_id}/{identifier}")
    return 0


def usable_reading(reading: Any, *, max_age_seconds: float = 600.0) -> bool:
    """A quota reading the dispatcher may act on unattended: present, error-free, recent."""
    if reading is None or getattr(reading, "error", None) or not getattr(reading, "windows", ()):
        return False
    observed = getattr(reading, "observed_at", None)
    if observed is None:
        return False
    from datetime import datetime
    return reading.age_seconds(now=datetime.now(observed.tzinfo)) <= max_age_seconds


def dispatch(args: argparse.Namespace) -> int:
    store_db = _resolve_store_db(None)
    principal = TaskPrincipal(actor="code-bridge-dispatch", role="orchestrator", projects=(args.project,))
    service = ProjectTaskService(store_db, lease_seconds=max(1, int(getattr(args, "lease_minutes", 60))) * 60)
    if args.dry_run:
        return _dry_run(service, args.project)
    repo_roots: dict[str, Path] = {}
    for mapping in args.repo_root:
        slug, separator, path = mapping.partition("=")
        if not separator or not slug or not path:
            raise ValueError("--repo-root must be SLUG=PATH")
        repo_roots[slug] = Path(path).resolve()
    registry_db = str(args.registry_db) if getattr(args, "registry_db", None) else None
    launcher = _dispatch_launch.BridgeLauncher(
        args.db, principal, args.project, repo_roots=repo_roots,
        worktree_root=Path(args.worktree_root).resolve() if args.worktree_root else None,
        registry_db=registry_db, launch_mode=getattr(args, "launch_mode", None),
    )
    router, cloud_placer = _routers(Path(args.db) if args.db else None, repo_roots)
    limits = _concurrency.load_limits()

    def capacity(engine_family: str) -> int | None:
        return limits.get("claude" if engine_family == "claude_code" else engine_family) or None

    knowledge_db = getattr(args, "knowledge_db", None)
    knowledge_store = _store.build_store(Path(knowledge_db).expanduser()) if knowledge_db is not None else None
    try:
        return Dispatcher(service, principal, launcher, args.project, parallel=args.parallel, router=router,
                          capacity=capacity, router_with_engine=True, cloud_placer=cloud_placer,
                          json_events=bool(getattr(args, "json", False)),
                          knowledge_store=knowledge_store, land=bool(getattr(args, "land", False)),
                          stale_minutes=getattr(args, "stale_minutes", DEFAULT_STALE_MINUTES)).run(once=args.once)
    finally:
        if knowledge_store is not None:
            knowledge_store.close()


def _routers(
    db_path: Path | None, repo_roots: dict[str, Path],
) -> tuple[Callable[..., str | None], Callable[[dict[str, Any], str], tuple[str, str]]]:
    """The local quota router and the cloud placer used by `dispatch`."""

    def router(tier: str, engine_family: str | None = None) -> str | None:
        if engine_family is None:
            return _routing.choose(tier, cwd=os.getcwd(), db_path=db_path).decision.provider
        # Fallback to the other engine: an explicit engine enables the router's operator
        # override for unreadable telemetry, so require a usable, recent reading here.
        engine = "claude" if engine_family == "claude_code" else engine_family
        selection = _routing.choose(tier, cwd=os.getcwd(), db_path=db_path, engine=engine)
        provider = selection.decision.provider
        reading = selection.readings.get(provider) if provider else None
        return provider if usable_reading(reading) else None

    def cloud_placer(task: dict[str, Any], tier: str) -> tuple[str, str]:
        """The routed engine and its cloud environment, through the same decision `run --where cloud` uses."""
        repo = _dispatch_launch.cloud_repo(repo_roots, task)  # the repository the cloud job would act on
        # Local engine slots do not bound cloud work; cloud admission is checked separately (`cloud_free`).
        family = _routing.choose(tier, cwd=os.getcwd(), db_path=db_path, respect_slots=False).decision.provider
        if family is None:
            raise ValueError(f"no eligible engine for tier {tier}")
        engine = "claude" if family == "claude_code" else family
        config = _cloud.load_config()
        placement = _cloud_place.decide_for_repo("cloud", config=config, engine=engine, cwd=repo)
        if placement.where != "cloud":
            raise ValueError(placement.reason)
        routine = _cloud.select_routine(config, routine=None, model=None) if engine == "claude" else None
        if engine == "claude" and routine is None:
            raise ValueError("Claude cloud jobs need a configured routine ([cloud.claude] default_routine)")
        environment = _cloud.provider(engine).env_for(_cloud.repo_slug(_cloud.repo_root(repo)), config, routine)
        return family, environment

    return router, cloud_placer
