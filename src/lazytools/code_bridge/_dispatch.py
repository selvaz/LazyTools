"""Restart-safe dispatch of a projects task graph through bridge jobs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from threading import Event, Thread, current_thread, main_thread
from typing import Any, Literal, TypeVar
from uuid import uuid4

from lazybridge import Store
from lazybridge.ext.knowledge import DEFAULT_PREFIX, DurableKnowledgeBase, slugify

from lazytools.code_bridge import (
    _cloud,
    _cloud_land,
    _concurrency,
    _dispatch_launch,
    _dispatch_workspace,
    _jobs,
    _lockfile,
    _routing,
    _store,
)
from lazytools.code_bridge._cloud import CloudError
from lazytools.code_bridge._lockfile import _pid_alive
from lazytools.connectors.projects.tools import _resolve_store_db
from lazytools.projects.admission import ENGINES, Engine
from lazytools.projects.plan_edit import _board_key
from lazytools.projects.records import _key as project_key
from lazytools.projects.task_fields import RequestedPlacement, requested_placement, routing_fields, touches_overlap
from lazytools.projects.task_fields import task_id as board_task_id
from lazytools.projects.task_service import (
    AttemptContinuation,
    CompletionEvidence,
    DispatcherOwner,
    DispatchLanding,
    FailureClass,
    PlacementEvidence,
    ProjectTaskService,
    TaskPrincipal,
)
from lazytools.projects.task_store import TaskServiceError, transaction
from lazytools.routing.router import Fallback, RoutingDecision

TERMINAL = frozenset({"done", "partial", "failed", "interrupted", "cancelled"})
DEFAULT_STALE_MINUTES = 10
DEFAULT_TOOL_STALE_MINUTES = 60  # one tool call running longer than this is stale
DEFAULT_MAX_CHECK_RETRIES = 2
PLACEMENT_SECONDS = 3660  # placement stays valid while a detached job waits for its engine slot
STALE_HEARTBEAT = "engine heartbeat went stale"
NEXT_TIER = {"basic": "writing", "writing": "thinking", "thinking": "critical"}
INFRA_ERRORS = (
    "database is locked", "database is busy", "store is busy", "store is unavailable",
    "connection refused", "connection reset", "connection timed out", "network is unreachable",
    "could not resolve host", "temporary failure in name resolution", "unable to access",
    "service unavailable", "provider outage", "bad gateway", "gateway timeout",
    "failed to connect", "remote end hung up", "git remote", "missing task checkout",
    "network error", "http 502", "http 503", "http 504",
)
BUDGET_ERRORS = (
    "quota exceeded", "quota exhausted", "insufficient quota", "insufficient_quota", "quota limit",
    "rate limit", "ratelimit", "rate_limit", "usage limit", "usage_limit", "hit your limit",
    "budget exceeded", "budget exhausted", "absolute_ceiling", "too many requests", "http 429",
    "429 client error", "status code 429",
)
ORPHAN_EVIDENCE = ("dispatcher:cloud-submission-absent", "dispatcher:no-job-after-grace",
                   "dispatcher:abandoned-claim")


def _error_class(reason: str, default: FailureClass = "infra") -> FailureClass:
    error = reason.casefold()
    if any(marker in error for marker in BUDGET_ERRORS):
        return "budget"
    if any(marker in error for marker in ("ticket rejected", "rejected ticket", "ticket refused", "approval denied")):
        return "policy"
    if any(marker in error for marker in INFRA_ERRORS):
        return "infra"
    return default


def _check_class(reason: str) -> FailureClass:
    header, _, output = reason.partition("\n")
    if "missing task checkout" in header:
        return "infra"
    if re.search(r"(?m)^(?:FAILED .* - |\s*E\s+|\s*)(?:AssertionError\b|assert\s)", output):
        return "check"
    return _error_class(output, "check")


def _budget_ready_at(job: dict[str, Any], reason: str, now: float) -> float:
    reset = job.get("quota_resets_at") or job.get("resets_at")
    if isinstance(reset, (int, float)) and reset > now:
        return float(reset)
    if isinstance(reset, str):
        try:
            return max(now + 1, datetime.fromisoformat(reset).timestamp())
        except ValueError:
            pass
    delay = _cloud._retry_after_seconds(reason)
    # Without a provider hint, back off and let quota routing check admission again.
    return now + (max(1, delay) if delay is not None else 60)


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


def _awaiting_approval(job: dict[str, Any], tickets: list[dict[str, Any]]) -> bool:
    """A pending ticket holds a running job only while its worker is alive."""
    pid = job.get("pid")
    return (job.get("status") == "running" and isinstance(pid, int) and _pid_alive(pid)
            and any(ticket.get("job_id") == job.get("job_id") and ticket.get("approval_id") for ticket in tickets))


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
        project = store.read(project_key(project_id), {})
        routing_defaults = {key: project[key] for key in ("engine_preference", "fallback") if key in project}
        holds: dict[int, str] = {}
        for event in board.get("schedule_events", []):
            if event.get("hold") and event.get("reason") and "task_index" in event:
                holds[event["task_index"]] = str(event["reason"])  # later events win
        return [Task(f"{project_id}/{board_task_id(data, index)}", data.get("revision", "0"),
                     data.get("status", "todo"), {**routing_defaults, **data},
                     data.get("dispatches", [None])[-1] if data.get("dispatches") else None,
                     hold_reason=holds.get(index))
                for index, data in enumerate(board["tasks"])]


def _routing_options(data: dict[str, Any]) -> tuple[tuple[Engine, ...], Fallback]:
    fields = routing_fields(data)
    return tuple(map(_routing.provider, fields.get("engine_preference", ()))), fields.get("fallback", "wait")


def _placement_detail(decision: RoutingDecision) -> str:
    parts = [decision.reason]
    for engine in ENGINES:
        percent = decision.weekly_projections.get(engine)
        projected = f"{percent:g}%" if percent is not None else "unavailable"
        parts.append(f"{_routing.bridge_engine(engine)} weekly projected {projected}")
    parts.extend(decision.busy.values())
    return " | ".join(parts)


LocalRouter = Callable[[str, dict[Engine, str], tuple[Engine, ...], Fallback, Engine | None], RoutingDecision]
CloudPlacer = Callable[[dict[str, Any], str], tuple[RoutingDecision, str]]
RouteCache = dict[tuple[str, tuple[Engine, ...], Fallback, tuple[tuple[Engine, str], ...], Engine | None],
                  RoutingDecision]
T = TypeVar("T")


class NoCloudEnvironment(CloudError):
    """The task's repository has no usable cloud environment."""


class Dispatcher:
    def __init__(self, service: ProjectTaskService, principal: TaskPrincipal, launcher: _dispatch_launch.Launcher,
                 project_id: str, *, parallel: int | None = None, emit: Callable[[str], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 task_reader: Callable[[ProjectTaskService, str], list[Task]] = _tasks,
                 clock: Callable[[], float] = time.time, orphan_grace: float = 30.0,
                 router: LocalRouter | None = None, max_retries: int = 1,
                 json_events: bool = False, knowledge_store: Store | None = None,
                 stale_minutes: int = DEFAULT_STALE_MINUTES,
                 capacity: Callable[[str], int | None] | None = None,
                 cloud_placer: CloudPlacer | None = None,
                 local_holders: Callable[[str], set[str]] | None = None,
                 land: bool = False):
        if parallel is not None and parallel < 1:
            raise ValueError("--parallel must be at least 1")
        if stale_minutes < 1:
            raise ValueError("--stale-minutes must be at least 1")
        if max_retries < 0:
            raise ValueError("max_retries must be nonnegative")
        self.service, self.principal, self.launcher = service, principal, launcher
        self.project_id, self.parallel, self.sleep = project_id, parallel, sleep
        self.stop_requested: Callable[[], bool] = lambda: False
        # Flush each event: the dispatcher is usually piped into a log or a watcher.
        self.emit: Callable[[str], None] = emit or (lambda line: print(line, flush=True))
        self.task_reader = task_reader
        self.clock, self.orphan_grace = clock, orphan_grace
        pid = os.getpid()
        self.owner = DispatcherOwner(instance_id=uuid4().hex, host=socket.gethostname().casefold(),
                                     pid=pid, started_at=_lockfile.process_start_time(pid))
        self.stale_seconds = stale_minutes * 60
        self.router = router
        self.capacity = capacity  # engine family -> local slot limit (None = unlimited)
        self.cloud_placer = cloud_placer
        self.local_holders = local_holders
        self.land = land  # push each published branch to origin and open its PR
        # Accepted, published tasks awaiting landing; drained after every claimed task is settled.
        self.landings: list[tuple[Task, Path, str, str, list[str]]] = []
        self.unsupported: set[str] = set()
        self.waiting: dict[str, str] = {}
        self.seen_tickets: set[str] = set()
        self.waiting_tickets: set[str] = set()
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
        if tier := resume.get("escalation_tier"):
            task = {**task, "tier": tier}
        resumes = _dispatch_launch._resumes(resume, claim.get("engine_family")) and _dispatch_launch.resumable(
            resume, getattr(self.launcher, "launch_mode", None)
        )
        check_failures = resume.get("check_failures")
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
        if check_failures:
            introduction = (
                f"This is the escalation at tier {tier}. Review all previous acceptance failures below."
                if tier else "A previous conversation left work in this checkout and an acceptance check failed."
            )
            failures = check_failures if tier else check_failures[-1:]
            prompt = (introduction + " Fix the cause in this checkout, rerun the failing checks and commit the fix."
                      + warning + "\n\n" + "\n\n".join(failures) + "\n\nOriginal task\n" + str(task["text"]))
            return {**task, "text": prompt}
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

    def _refused_resume(self, task: Task, job: dict[str, Any], continuation: dict[str, Any]) -> bool:
        """Settle a resume the bridge refused before submitting, then start a fresh conversation."""
        receipt = (task.dispatch or {})["receipt"]
        reason = str(job.get("error") or "resume refused").replace("\n", " ")
        self.service.fail_local_launch(
            self.principal, task.task_id, receipt["claim_token"], f"resume refused before submit: {reason}",
            dispatcher_owner=receipt.get("dispatcher_owner"), failure_class="infra",
            continuation=AttemptContinuation(**{**continuation, "native_id": None, "fresh": True}),
        )
        self._event("retry", task.task_id, f"resume failed: {reason} [infra]", job_id=job.get("job_id"))
        return True

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

    def _rejection_reason(self, job: dict[str, Any]) -> str:
        rejected_ticket_reason = getattr(self.launcher, "rejected_ticket_reason", None)
        return _cloud.redact(rejected_ticket_reason(job)).strip() if rejected_ticket_reason else ""

    def _backoff(self, retries: int) -> None:
        self.sleep(min(0.25 * 2 ** min(retries, 5), 8))

    def _store_step(self, task_id: str, operation: Callable[[], T]) -> T:
        """Retry store reads and transactional writes without restarting a worker."""
        for retry in range(self.max_retries + 1):
            try:
                return operation()
            except Exception as exc:
                transient = (
                    isinstance(exc, TaskServiceError) and exc.code in ("busy", "unavailable")
                ) or _error_class(str(exc), "check") == "infra"
                if not transient or retry == self.max_retries:
                    raise
                self._event("retry", task_id, f"{str(exc).splitlines()[0]} [infra]")
                self._backoff(retry)
        raise AssertionError("max_retries must be nonnegative")

    def _infra_retry(self, task: Task) -> AttemptContinuation | None:
        continuation = AttemptContinuation.model_validate(task.data.get("attempt_continuation") or {})
        if continuation.infra_retries >= self.max_retries:
            return None
        self._backoff(continuation.infra_retries)
        return continuation.model_copy(update={
            "infra_retries": continuation.infra_retries + 1, "not_before": None, "wait_for_budget": False,
        })

    def _budget_retry(self, task: Task, job: dict[str, Any], reason: str) -> AttemptContinuation:
        continuation = AttemptContinuation.model_validate(task.data.get("attempt_continuation") or {})
        return continuation.model_copy(update={
            "engine": job.get("engine") or continuation.engine,
            "wait_for_budget": True, "not_before": _budget_ready_at(job, reason, self.clock()),
        })

    def _fail_launch(self, task: Task, claim: dict[str, Any], reason: str) -> None:
        cls = _error_class(reason)
        followup = None
        if cls == "budget":
            followup = self._budget_retry(task, {"engine": _routing.bridge_engine(claim["engine_family"])}, reason)
        elif cls == "infra" and _error_class(reason, "check") == "infra":
            followup = self._infra_retry(task)
        self.service.fail_local_launch(
            self.principal, task.task_id, claim["claim_token"], reason,
            dispatcher_owner=claim.get("dispatcher_owner"), failure_class=cls, continuation=followup,
        )
        self._record_lesson(task, "rejected ticket" if cls == "policy" else "launch failure", reason)
        tier = (task.data.get("attempt_continuation") or {}).get("escalation_tier")
        kind = "waiting" if cls == "budget" else "retry" if followup else "escalation_failed" if tier else "failed"
        self._event(kind, task.task_id, f"{reason} [{cls}]")

    def _run_checks(
        self, task: Task, job_ref: str, cwd: Path | None, expected_head: Any,
    ) -> tuple[str | None, FailureClass | None, tuple[str, ...]]:
        failures: list[str] = []
        failed_classes: list[FailureClass] = []
        flaky: list[str] = []
        recovered_infra = False
        for command in task.data.get("checks") or ():
            failure = _dispatch_workspace._check_failure(command, cwd, tail_lines=60)
            if failure is None:
                continue
            unchanged = cwd is not None and not _dispatch_workspace._checks_changed_checkout(cwd, expected_head)
            retries = 0
            while failure and _check_class(failure) == "infra" and unchanged and retries < self.max_retries:
                self._event("retry", task.task_id, f"{failure.splitlines()[0]} [infra]", job_id=job_ref)
                self._backoff(retries)
                retries += 1
                failure = _dispatch_workspace._check_failure(command, cwd, tail_lines=60)
                unchanged = not _dispatch_workspace._checks_changed_checkout(cwd, expected_head) if cwd else False
            if failure is None:
                recovered_infra = True
                continue
            kind = _check_class(failure)
            if unchanged and kind == "check":
                repeated = _dispatch_workspace._check_failure(command, cwd, tail_lines=60)
                if repeated is None and cwd is not None and not _dispatch_workspace._checks_changed_checkout(
                    cwd, expected_head
                ):
                    flaky.extend(re.findall(r"(?m)^FAILED (.+?::.+?)(?: - .*|$)", failure) or [command])
                    continue
                if repeated is not None:
                    failure, kind = repeated, _check_class(repeated)
            failures.append(failure)
            failed_classes.append(kind)
        changed = _dispatch_workspace._checks_changed_checkout(cwd, expected_head) if cwd is not None else None
        if changed:
            failures.append(changed)
            failed_classes.append("check")
        if failures:
            priority: tuple[FailureClass, ...] = ("policy", "check", "infra", "budget")
            kind = next(kind for kind in priority if kind in failed_classes)
            return "\n".join(failures), kind, tuple(dict.fromkeys(flaky))
        return None, "flaky" if flaky else "infra" if recovered_infra else None, tuple(dict.fromkeys(flaky))

    def _defer_check(self, task: Task, job: dict[str, Any], reason: str) -> None:
        receipt = (task.dispatch or {})["receipt"]
        self.service.defer_dispatch_step(
            self.principal, task.task_id, receipt["attempt"], _budget_ready_at(job, reason, self.clock()),
            dispatcher_owner=receipt.get("dispatcher_owner"),
        )
        self._event("waiting", task.task_id, f"{reason.splitlines()[0]} [budget]", job_id=job["job_id"])

    def _repair_retry(self, task: Task, previous: AttemptContinuation, reason: str) -> AttemptContinuation | None:
        if previous.escalation_tier:
            return None
        exhausted = len(previous.check_failures) >= task.data.get("max_check_retries", DEFAULT_MAX_CHECK_RETRIES)
        tier = NEXT_TIER.get(task.data.get("tier") or "thinking") if exhausted else None
        if exhausted and not tier:
            return None
        return previous.model_copy(update={
            "check_failures": (*previous.check_failures, reason), "escalation_tier": tier,
        })

    def _report_settlement(self, task: Task, evidence: CompletionEvidence, reason: str) -> None:
        cls, followup, job_ref = evidence.failure_class, evidence.continuation, evidence.job_ref
        for check in evidence.flaky_checks:
            self._record_lesson(task, f"flaky check {check}", f"{check} failed, then passed on the unchanged checkout")
            self._event("flaky", task.task_id, f"{check} [flaky]", job_id=job_ref)
        if evidence.state == "accepted":
            self._event("done", task.task_id, job_id=job_ref)
            return
        if cls in ("check", "infra", "policy"):
            lesson = {"check": "check failure", "infra": "engine crash", "policy": "rejected ticket"}[cls]
            self._record_lesson(task, lesson, reason)
        detail = f"{reason.splitlines()[0]} [{cls}]"
        tier = (task.data.get("attempt_continuation") or {}).get("escalation_tier")
        if followup and followup.wait_for_budget:
            self._event("waiting", task.task_id, detail, job_id=job_ref)
        elif followup and followup.escalation_tier and not tier:
            self._event("escalated", task.task_id, f"tier={followup.escalation_tier} | {detail}", job_id=job_ref)
        elif followup:
            prefix = "resume failed: " if "resume refused" in reason or any(
                marker in reason for marker in _dispatch_launch.RESUME_FAILURES
            ) else ""
            self._event("retry", task.task_id, prefix + detail, job_id=job_ref)
        else:
            self._event("escalation_failed" if tier else "failed", task.task_id, detail, job_id=job_ref)

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

    def _local_busy(self, claims: list[dict[str, Any]]) -> dict[Engine, str]:
        busy: dict[Engine, str] = {}
        for engine in ENGINES:
            limit = self.capacity(engine) if self.capacity else None
            if not limit:
                continue
            holders = self.local_holders(engine) if self.local_holders else set()
            # Include claims whose detached workers have yet to acquire their slots.
            # A worker already holding a machine-wide slot only counts once.
            pending = sum(
                claim.get("effective_placement", "local") == "local"
                and claim.get("engine_family") == engine
                and claim.get("job_ref") not in holders
                and (not claim.get("claim_token") or _dispatch_launch.local_job_id(claim["claim_token"]) not in holders)
                for claim in claims
            )
            count = len(holders) + pending
            if count >= limit:
                busy[engine] = f"engine_at_concurrency_limit: {_routing.bridge_engine(engine)} {count}/{limit}"
        return busy

    def _required_engine(self, task: Task) -> Engine | None:
        continuation = task.data.get("attempt_continuation") or {}
        if engine := continuation.get("engine"):
            family = _routing.provider(engine)
            if (_dispatch_launch._resumes(continuation, family)
                    and _dispatch_launch.resumable(continuation, getattr(self.launcher, "launch_mode", None))):
                return family
        return None

    def _requested(self, task: Task) -> RequestedPlacement:
        continuation = task.data.get("attempt_continuation") or {}
        if continuation.get("repair_checkout"):
            return "local"
        requested = requested_placement(task.data)
        if requested == "auto" and (continuation.get("preserved_branch") or self._required_engine(task)):
            return "local"  # a local retry needs its preserved work or native conversation
        return requested

    def _place(
        self, task: Task, requested: RequestedPlacement, busy: dict[Engine, str], picks: RouteCache,
    ) -> PlacementEvidence | None:
        """Choose a concrete placement using current capacity and record evidence for the claim."""
        data = task.data
        continuation = data.get("attempt_continuation") or {}
        tier = continuation.get("escalation_tier") or data.get("tier") or "thinking"
        required_engine = self._required_engine(task)
        where, environment = "local", None
        if requested in ("local", "auto") and self.router is not None:
            try:
                preference, fallback = _routing_options(data)
                key = (tier, preference, fallback, tuple(busy.items()), required_engine)
                if key not in picks:
                    picks[key] = self.router(tier, busy, preference, fallback, required_engine)
                decision = picks[key]
            except Exception as exc:
                self._wait(task.task_id, f"routing failed: {str(exc).replace(chr(10), ' ')}")
                return None
            if decision.provider is None or decision.provider in busy or (
                required_engine and decision.provider != required_engine
            ):
                slot_wait = decision.reason == "wait_for_slot" or decision.provider in busy
                if requested == "auto" and slot_wait and self.cloud_placer is not None:
                    where = "cloud"
                else:
                    cause = "local slot" if slot_wait else "quota/pace"
                    detail = _placement_detail(decision)
                    exclusions = " | ".join(f"{name}: {why}" for name, why in decision.ineligible.items())
                    extra = f" | {exclusions}" if exclusions and not slot_wait else ""
                    self._wait(task.task_id, f"local tier={tier} | {detail} | {cause}{extra}",
                               key=f"{cause}: {decision.reason}:{decision.waiting_for}:"
                                   f"{tuple((name, why.partition(':')[0]) for name, why in decision.ineligible.items())}")
                    return None
        else:
            where = "cloud"
        if where == "cloud":
            assert self.cloud_placer is not None
            try:
                decision, environment = self.cloud_placer(data, tier)
            except _dispatch_launch.RepositoryMismatch as exc:
                if requested == "auto":
                    self._wait(task.task_id, f"no cloud environment: {exc}")
                elif task.task_id not in self.unsupported:
                    self.unsupported.add(task.task_id)
                    self._event("failed", task.task_id, f"cloud placement refused: {str(exc).replace(chr(10), ' ')}")
                return None
            except NoCloudEnvironment as exc:
                self._wait(task.task_id, f"no cloud environment: {exc}")
                return None
            except Exception as exc:
                self._wait(task.task_id, f"cloud placement failed: {str(exc).replace(chr(10), ' ')}")
                return None
            if decision.provider is None:
                reason = "cloud concurrency limit reached" if decision.reason == "wait_for_slot" else "cloud quota/pace"
                self._wait(task.task_id, f"{reason} | {_placement_detail(decision)}", key=reason)
                return None
            if not self._cloud_available(task.task_id, decision.provider):
                return None
        engine = decision.provider
        assert engine is not None
        now = self.clock()
        dispatch = task.dispatch or {}
        if dispatch.get("state") == "absent" and dispatch.get("failure_class") == "infra":
            # Reconciliation carries valid preflight forward. Reuse it only after routing
            # confirms that its engine and placement can still take the retry.
            record = data.get("placement_evidence", {}).get(where, {})
            if record.get("revision") == task.revision:
                previous_evidence = PlacementEvidence.model_validate(record["evidence"])
                if (previous_evidence.eligible and previous_evidence.expires_at > now
                        and previous_evidence.engine_family == engine and previous_evidence.environment_id == environment):
                    return previous_evidence
        detail = _placement_detail(decision)
        evidence = PlacementEvidence(
            placement="cloud" if where == "cloud" else "local", eligible=True, reason=detail,
            reference=f"dispatch-{task.task_id}-{task.revision}-{int(now)}-{where}",
            expires_at=now + PLACEMENT_SECONDS, engine_family=engine, environment_id=environment,
            model=decision.model if where == "local" else None, effort=decision.effort if where == "local" else None,
        )
        self.service.record_placement(task.task_id, task.revision, evidence)
        previous = (dispatch.get("receipt") or {}).get("engine_family")
        if where == "cloud":
            self._event("placed", task.task_id, f"cloud {engine} env={environment} tier={tier} | {detail}")
        elif previous and not required_engine:
            self._event("rerouted", task.task_id, f"local {previous} -> {engine} tier={tier} | {detail}")
        else:
            self._event("placed", task.task_id, f"local {engine} tier={tier} | {detail}")
        return evidence

    def _wait(self, task_id: str, detail: str, *, key: str | None = None) -> None:
        reason = key or detail
        if self.waiting.get(task_id) != reason:
            self._event("waiting", task_id, detail)
            self.waiting[task_id] = reason

    def _cloud_available(self, task_id: str, engine: str) -> bool:
        if not hasattr(self.launcher, "launch_cloud"):
            self.unsupported.add(task_id)
            self._event("failed", task_id, "cloud placement not supported by this launcher")
            return False
        if not self.launcher.cloud_free(engine):  # type: ignore[attr-defined]
            self._wait(task_id, "cloud concurrency limit reached")
            return False
        return True

    def _ready_tasks(self) -> dict[str, dict[str, Any]]:
        page = self.service.next_ready(self.principal, self.project_id, limit=100)
        tasks = {task["task_id"]: task for task in page["tasks"]}
        while (cursor := page.get("next_cursor")) is not None:
            page = self.service.next_ready(self.principal, self.project_id, limit=100, cursor=cursor)
            tasks.update((task["task_id"], task) for task in page["tasks"])
        return tasks

    def _blocked(self) -> None:
        for identifier, dependency in self.service.block_failed_dependencies(self.principal, self.project_id):
            self._event("blocked", f"{self.project_id}/{identifier}", f"dependency {dependency} failed")

    def _owner_is_dead(self, owner: DispatcherOwner) -> bool:
        return owner["host"] == self.owner["host"] and _lockfile.process_has_exited(owner["pid"], owner["started_at"])

    def _refresh_claims(self, tasks: list[Task]) -> bool:
        owners = {
            owner["instance_id"]: owner
            for task in tasks
            if task.status == "claimed" or (task.dispatch or {}).get("pending_landing")
            if (owner := (task.dispatch or {}).get("receipt", {}).get("dispatcher_owner")) is not None
        }
        if owners:
            dead = {key for key, owner in owners.items() if owner != self.owner and self._owner_is_dead(owner)}
            self.service.renew_dispatcher_claims(self.principal, self.project_id, self.owner, dead_instances=dead)
        return bool(owners)

    def _expired(self, receipt: dict[str, Any]) -> bool:
        # The snapshot can predate several heartbeats during a slow check. The
        # service checks the current owner and lease when committing settlement.
        return not receipt.get("dispatcher_owner") and receipt.get("lease_expires_at", float("inf")) <= self.clock()

    def _orphan_expired(self, dispatch: dict[str, Any], receipt: dict[str, Any]) -> bool:
        now = self.clock()
        created = float(dispatch.get("created_at", 0))
        if dispatch.get("dispatcher_recovered"):
            return True
        return self._expired(receipt) and now - created >= self.orphan_grace

    def _settle_missing(self, task: Task, dispatch: dict[str, Any], receipt: dict[str, Any], *,
                        cloud_absent: bool = False) -> bool:
        """Release an abandoned claim after its job has been ruled out."""
        owned = receipt.get("dispatcher_owner") == self.owner
        past_startup = owned and self.clock() - dispatch["created_at"] >= self.orphan_grace
        if not ((cloud_absent and owned) or self._orphan_expired(dispatch, receipt) or past_startup):
            return False
        job_ref = dispatch.get("job_ref")
        observation: Literal["absent", "terminated"] = "terminated" if job_ref and not cloud_absent else "absent"
        evidence_ref = ORPHAN_EVIDENCE[0] if cloud_absent else ORPHAN_EVIDENCE[2 if owned else 1]
        reason = "provider has no matching dispatch" if cloud_absent else (
            "abandoned claim has no bridge job" if owned else "no job after dispatch grace period")
        followup = self._infra_retry(task)
        self.service.reconcile_dispatch(
            self.principal, task.task_id, receipt["attempt"], observation, evidence_ref=evidence_ref,
            job_ref=job_ref, failure_reason=reason, failure_class="infra", continuation=followup,
            dispatcher_owner=self.owner if owned else None,
        )
        self._event("reconciled", task.task_id, observation, job_id=job_ref)
        self._record_lesson(task, "missing job", reason)
        tier = (task.data.get("attempt_continuation") or {}).get("escalation_tier")
        self._event("retry" if followup else "escalation_failed" if tier else "failed",
                    task.task_id, f"{reason} [infra]", job_id=job_ref)
        return True

    def _conclude(
        self, task: Task, *, reference: str, job_ref: str, accepted: bool, expired: bool,
        artifact_ref: str | None = None, failure_reason: str | None = None,
        verification_id: str | None = None, reviewed_sha: str | None = None,
        review_passed: bool = False, review_independence: Any = None,
        preserved_branch: str | None = None, pr_url: str | None = None, land_failure: str | None = None,
        landing: DispatchLanding | None = None,
        failure_class: FailureClass | None = None, continuation: AttemptContinuation | None = None,
        flaky_checks: tuple[str, ...] = (),
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
                failure_class=failure_class, continuation=continuation, flaky_checks=flaky_checks,
                landing=landing,
            )
        if expired and evidence.state == "accepted":
            if recorded:
                reference = f"{reference}:lease-expired"
                recorded = records.get(reference)
            evidence = (CompletionEvidence.model_validate(recorded) if recorded else evidence.model_copy(update={
                "reference": reference, "state": "blocked", "outcome": "task_failure",
                "failure_reason": "claim lease expired",
                "failure_class": "infra",
            }))
        if not recorded:
            owner_options: dict[str, Any] = {"dispatcher_owner": self.owner} if receipt.get("dispatcher_owner") else {}
            self._store_step(task.task_id, lambda: self.service.record_completion_evidence(
                self.principal, task.task_id, receipt["attempt"], evidence, **owner_options,
            ))
        accepted = evidence.state == "accepted"
        complete_options: dict[str, Any] = {"reconcile_failure": True} if expired else {}
        if receipt.get("dispatcher_owner"):
            complete_options.update(dispatcher_owner=self.owner, landing=evidence.landing if accepted else None)
        self._store_step(task.task_id, lambda: self.service.complete(
            self.principal, task.task_id, receipt["claim_token"], evidence.reference,
            evidence.state, evidence.reference, "success" if accepted else "task_failure",
            **complete_options,
        ))
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
        cloud_job = self.launcher.cloud_job  # type: ignore[attr-defined]
        job = self._store_step(task.task_id, lambda: cloud_job(job_ref))
        if job is None:
            # Still `intent` with no job: begin_submit never succeeded (the store was busy, or the
            # dispatcher stopped first), so nothing reached the provider and the submit is still owed.
            if (dispatch.get("state") == "intent" and not dispatch.get("job_ref")
                    and not dispatch.get("dispatcher_recovered")
                    and receipt.get("executor") == self.principal.actor):
                return self._launch_cloud(task, receipt, receipt)
            return self._settle_missing(task, dispatch, receipt)
        for retry in range(self.max_retries):
            land_error = str(job.get("land_failure") or "")
            if _error_class(land_error, "check") != "infra":
                break
            self._event("retry", task.task_id, f"{land_error} [infra]", job_id=job_ref)
            self._backoff(retry)
            job = self._store_step(task.task_id, lambda: cloud_job(job_ref)) or job
        if job.get("status") == "submitting" and not job.get("cloud_handle"):
            if not (receipt.get("dispatcher_owner") == self.owner or self._orphan_expired(dispatch, receipt)):
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
        expired = self._expired(receipt)
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
        cls: FailureClass | None = None
        followup = None
        flaky: tuple[str, ...] = ()
        rejection = "" if recorded else self._rejection_reason(job)
        if recorded:  # restarted after recording: the evidence decides, the checks are not run again
            failure = None if recorded.get("state") == "accepted" else (
                recorded.get("failure_reason") or "cloud job failed")
        elif rejection:
            failure, cls = rejection, "policy"
        elif status != "done":
            failure = f"cloud job {status}: {_cloud.redact(str(job.get('error') or 'no error recorded'))}"
            cls = _error_class(failure)
            if cls == "budget":
                followup = self._budget_retry(task, job, failure)
            elif cls == "infra" and not expired and _error_class(failure, "check") == "infra":
                followup = self._infra_retry(task)
        elif not landed:
            detail = land_failure or "no PR url or pushed commit recorded"
            failure = f"cloud job did not land: {detail}"
            if not expired and _error_class(failure) == "budget":
                self._defer_check(task, job, failure)
                return False
        elif expired:
            failure = "claim lease expired"
        elif checks:
            cwd, failure = self._cloud_check_checkout(task, job, job_ref)
            if cwd is not None:
                try:
                    failure, cls, flaky = self._run_checks(task, job_ref, cwd, job.get("pushed_sha"))
                    expired = self._expired(receipt)
                    if failure and cls == "budget" and not expired:
                        self._defer_check(task, job, failure)
                        return False
                    prepare = getattr(self.launcher, "prepare_cloud_repair", None)
                    if failure and cls == "check" and prepare and not expired:
                        previous = AttemptContinuation.model_validate(task.data.get("attempt_continuation") or {})
                        repair = self._repair_retry(task, previous, failure)
                        if repair:
                            prepare(cwd, task.data, job)
                            followup = repair.model_copy(update={
                                "repair_checkout": str(cwd), "cloud_branch": job["landed_branch"],
                                "cloud_pr_url": job["pr_url"], "engine": _routing.bridge_engine(receipt["engine_family"]),
                                "fresh": True, "wait_for_budget": False, "not_before": None,
                            })
                finally:
                    remove = getattr(self.launcher, "remove_cloud_checkout", None)
                    if remove is not None and followup is None:
                        remove(cwd)
            expired = self._expired(receipt)
            failure = failure or ("claim lease expired" if expired else None)
        else:
            failure = None
        if failure and cls is None:
            cls = _error_class(failure)
        settled = self._conclude(
            task, reference=reference, job_ref=job_ref, accepted=failure is None, expired=expired,
            artifact_ref=str(job["pushed_sha"]) if landed else None,
            failure_reason=failure, pr_url=job.get("pr_url"), land_failure=land_failure or None,
            failure_class=cls, continuation=followup, flaky_checks=flaky,
        )
        if settled.state == "accepted":
            self._event("published", task.task_id, settled.pr_url or str(job["pr_url"]), job_id=job_ref)
        self._report_settlement(task, settled, settled.failure_reason or "cloud job failed")
        return True

    def _cloud_check_checkout(
        self, task: Task, job: dict[str, Any], job_ref: str,
    ) -> tuple[Path | None, str | None]:
        for retry in range(self.max_retries + 1):
            try:
                return self.launcher.cloud_checkout(task.task_id, job), None  # type: ignore[attr-defined,no-any-return]
            except Exception as exc:
                failure = f"could not clone the landed branch: {_cloud.redact(str(exc))[:300]}"
                if retry == self.max_retries:
                    return None, failure
                self._event("retry", task.task_id, f"{failure} [infra]", job_id=job_ref)
                self._backoff(retry)
        raise AssertionError("max_retries must be nonnegative")

    def _launch_cloud(self, task: Task, summary: dict[str, Any], claim: dict[str, Any]) -> bool:
        """Submit a claimed cloud task through `run --where cloud`; record its job.

        True while the claim stays held: launched, or the submit is deferred to a later pass.
        """
        task_id, token = summary["task_id"], claim["claim_token"]
        job_id = _dispatch_launch.cloud_job_id(token)
        owner = self.owner if claim.get("dispatcher_owner") else None
        try:
            self.service.begin_submit(self.principal, task_id, token, dispatcher_owner=owner)
        except Exception as exc:
            if isinstance(exc, TaskServiceError) and exc.code == "busy":
                self._event("deferred", task_id, str(exc))  # the claim stays `intent`: see _settle_cloud
                return True
            reason = str(exc).replace("\n", " ")
            try:
                self._fail_launch(task, claim, reason)
            except TaskServiceError as release_exc:
                self._event("deferred", task_id, str(release_exc))
                return True
            return False
        try:
            job_ref = self.launcher.launch_cloud(  # type: ignore[attr-defined]
                self._with_lessons({**summary, **task.data}), claim, job_id)
        except Exception as exc:
            reason = _cloud.redact(str(exc)).replace("\n", " ")
            job = self.launcher.job(job_id)
            if job is None:  # nothing reached the provider
                try:
                    if isinstance(exc, _dispatch_launch.CloudSlotUnavailable):
                        continuation = AttemptContinuation.model_validate(task.data.get("attempt_continuation") or {})
                        self.service.fail_local_launch(
                            self.principal, task_id, token, reason, dispatcher_owner=owner, continuation=continuation,
                        )
                        self._wait(task_id, "cloud concurrency limit reached")
                    else:
                        self._fail_launch(task, claim, reason)
                except Exception as release_exc:
                    self._event("deferred", task_id, f"{release_exc} [infra]", job_id=job_id)
                    return True
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
        if dispatch.get("retry_at", 0) > self.clock() and not self._expired(receipt):
            return False
        if receipt.get("effective_placement") == "cloud":
            return self._settle_cloud(task)
        job_ref = dispatch.get("job_ref")
        job = self._store_step(task.task_id, lambda: self._local_job(task))
        continuation = task.data.get("attempt_continuation")
        resumed = _dispatch_launch._resumes(continuation, receipt.get("engine_family"))
        if (job and not job_ref and resumed and continuation and dispatch.get("state") == "intent"
                and job.get("status") in TERMINAL
                and any(marker in str(job.get("error") or "") for marker in _dispatch_launch.RESUME_FAILURES)):
            # Refused before submit (check_resume, governed resume guard): the dispatch never
            # reaches the provider boundary, so reconciliation below could never settle it.
            self._refused_resume(task, job, continuation)
            return True
        pid = job.get("pid") if job else None
        worker_gone = isinstance(pid, int) and not _pid_alive(pid)
        if job and job.get("status") not in TERMINAL and worker_gone:
            cancel = getattr(self.launcher, "cancel", None)
            if cancel is None or not cancel(str(job["job_id"])):
                return False
            job = self.launcher.job(str(job["job_id"]))
            if not job or job.get("status") not in TERMINAL:
                return False
        if job and not job_ref and dispatch.get("state") == "intent":
            if job.get("status") not in TERMINAL:
                return False  # the child is still waiting for its engine slot
            if not receipt.get("dispatcher_owner"):
                return self._settle_missing(task, dispatch, receipt)
        if job and not job_ref:
            try:
                self.service.reconcile_dispatch(self.principal, task.task_id, receipt["attempt"], "submitted",
                                                evidence_ref=f"bridge-job:{job['job_id']}", job_ref=job["job_id"],
                                                dispatcher_owner=self.owner if receipt.get("dispatcher_owner") else None)
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
            cutoff = now - self.stale_seconds
            pending = getattr(self.launcher, "pending", None)
            tickets = pending() if pending else []
            awaiting = _awaiting_approval(job, tickets)
            if awaiting:
                for ticket in tickets:
                    ticket_id = str(ticket.get("approval_id") or "")
                    if ticket.get("job_id") == job.get("job_id") and ticket_id and ticket_id not in self.waiting_tickets:
                        self.waiting_tickets.add(ticket_id)
                        self._event("waiting", task.task_id, f"awaiting approval {ticket_id}", job_id=str(job_ref))
            stale = not awaiting and _has_stale_heartbeat(job, cutoff, now=now)
            cancel = getattr(self.launcher, "cancel", None)
            if (not stale and not self._expired(receipt)) or cancel is None or str(job_ref) in self.cancelled:
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
        if recorded:
            settled = self._conclude(task, reference=reference, job_ref=str(job_ref),
                                     accepted=recorded["state"] == "accepted", expired=self._expired(receipt))
            self._report_settlement(task, settled, settled.failure_reason or "job failed")
            return True
        cwd = Path(str(job["cwd"])) if job.get("cwd") else None
        rejection = self._rejection_reason(job)
        accepted = job.get("status") == "done" and not rejection
        workspace_error = None
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
        expired = self._expired(receipt)
        job_error = str(job.get("error") or job.get("status") or "job failed").replace("\n", " ")
        check_failure: str | None = None
        cls: FailureClass | None = None
        flaky: tuple[str, ...] = ()
        if accepted and checks and not (needs_evidence or expired):
            check_failure, cls, flaky = self._run_checks(task, str(job_ref), cwd, job.get("final_commit"))
            expired = self._expired(receipt)
            if check_failure and cls == "budget" and not expired:
                self._defer_check(task, job, check_failure)
                return False
        if rejection:
            failure_reason, cls = rejection, "policy"
        elif check_failure:
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
        previous = AttemptContinuation.model_validate(continuation or {})
        # Publish only after the lease and evidence checks; a failed publish fails the task.
        published: str | None = None
        if accepted and cwd is not None:
            published, publish_error = _dispatch_workspace._publish(cwd)
            for retry in range(self.max_retries):
                if not publish_error or _error_class(publish_error) != "infra":
                    break
                self._event("retry", task.task_id, f"{publish_error} [infra]", job_id=str(job_ref))
                self._backoff(retry)
                published, publish_error = _dispatch_workspace._publish(cwd)
            if not publish_error and previous.cloud_branch:
                for retry in range(self.max_retries + 1):
                    try:
                        _cloud_land._push(cwd, "HEAD", previous.cloud_branch)
                        publish_error = None
                        break
                    except CloudError as exc:
                        publish_error = _cloud.redact(str(exc))
                        if retry < self.max_retries:
                            self._event("retry", task.task_id, f"{publish_error} [infra]", job_id=str(job_ref))
                            self._backoff(retry)
            if publish_error:
                if not self._expired(receipt) and _error_class(publish_error) == "budget":
                    self._defer_check(task, job, publish_error)
                    return False
                accepted = False
                failure_reason = publish_error
                cls = _error_class(publish_error)
            elif published:
                self._event("published", task.task_id, published, job_id=str(job_ref))
        else:
            publish_error = None
        followup, resume_failed = None, False
        if not accepted:
            cls = cls if cls and cls != "flaky" else _error_class(failure_reason)
            resume_failed = (cls == "infra" and resumed and
                             any(marker in failure_reason for marker in _dispatch_launch.RESUME_FAILURES))
            if cls == "budget":
                followup = self._budget_retry(task, job, failure_reason)
            elif resume_failed:
                followup = previous.model_copy(update={"native_id": None, "fresh": True})
            elif cls == "infra" and not (publish_error or check_failure or needs_evidence or expired or workspace_error):
                followup = self._infra_retry(task)
            elif cls == "check" and cwd is not None and not expired:
                followup = self._repair_retry(task, previous, failure_reason)
        preserved = None
        if followup and cwd is not None and not resume_failed:
            # Keep the engine's work, without committing files the failed checks may have edited.
            preserved, preserve_error = _dispatch_workspace._preserve_attempt(
                cwd, task.task_id, int(receipt["attempt"]), commit_uncommitted=not bool(check_failure))
            if preserve_error:
                self._event("preserve-failed", task.task_id, preserve_error, job_id=str(job_ref))
            elif preserved:
                self._event("preserved", task.task_id, preserved.branch, job_id=str(job_ref))
        if followup and not resume_failed:
            engine = job.get("engine") or _routing.bridge_engine(receipt["engine_family"])
            native_id = _jobs._native_id(job)
            if (followup.escalation_tier and cls == "check") or task.data.get("review_of") or not (
                _dispatch_launch.resumable({**job, "engine": engine}, getattr(self.launcher, "launch_mode", None))
            ):
                native_id = None
            followup = followup.model_copy(update={
                "session": job.get("session_name"), "native_id": native_id, "engine": engine,
                "fresh": not bool(native_id),
                "wait_for_budget": cls == "budget",
                "not_before": followup.not_before if cls == "budget" else None,
                **({
                    "preserved_branch": preserved.branch, "preserved_commit": preserved.commit,
                    "preserved_base": preserved.base, "preserved_files": preserved.files,
                    "preserved_message": preserved.message, "preserved_unresolved": preserved.unresolved,
                } if preserved else {}),
            })
        evidence_failure = None if accepted else failure_reason
        if evidence_failure and previous.check_failures and not followup:
            evidence_failure += "\n\nPrevious check failures\n" + "\n\n".join(previous.check_failures)
        landing = (DispatchLanding(cwd=str(cwd), branch=published, checks=tuple(checks or ()))
                   if self.land and published and cwd is not None and not previous.cloud_branch else None)
        settled = self._conclude(
            task, reference=reference, job_ref=str(job_ref), accepted=accepted, expired=expired,
            artifact_ref=job.get("final_commit") or job.get("candidate_commit"),
            verification_id=verification_id, reviewed_sha=reviewed_sha, review_passed=review_passed,
            review_independence=review_independence, failure_reason=evidence_failure,
            preserved_branch=preserved.branch if preserved else None, landing=landing,
            pr_url=previous.cloud_pr_url if accepted else None,
            failure_class=cls, continuation=followup, flaky_checks=flaky,
        )
        self._report_settlement(task, settled, failure_reason)
        if settled.state == "accepted" and landing is not None and not receipt.get("dispatcher_owner"):
            self.landings.append((task, Path(landing.cwd), landing.branch, str(job_ref), list(landing.checks)))
        return True

    def _local_job(self, task: Task) -> dict[str, Any] | None:
        dispatch = task.dispatch or {}
        receipt = dispatch["receipt"]
        if job_ref := dispatch.get("job_ref"):
            return self.launcher.job(job_ref)
        if receipt.get("dispatcher_owner"):
            return self.launcher.job(_dispatch_launch.local_job_id(receipt["claim_token"]))
        return self.launcher.find(task.task_id, receipt["attempt"])

    def _land_settled(self) -> None:
        """Land accepted tasks, including publication left pending by a previous process."""
        landings, self.landings = self.landings, []
        for task in self.task_reader(self.service, self.project_id):
            dispatch = task.dispatch or {}
            if (pending := dispatch.get("pending_landing")) and dispatch["receipt"].get("dispatcher_owner") == self.owner:
                landing = DispatchLanding.model_validate(pending)
                landings.append((task, Path(landing.cwd), landing.branch, dispatch["job_ref"], list(landing.checks)))
        for task, cwd, branch, job_ref, checks in landings:
            try:
                pr_url, land_failure = _dispatch_workspace._land(cwd, branch, task.task_id, task.data, checks)
            except Exception as exc:
                pr_url, land_failure = None, " ".join(_cloud.redact(str(exc)).split())[:500] or "landing failed"
            dispatch = task.dispatch or {}
            if dispatch.get("pending_landing"):
                self.service.finish_dispatch_landing(
                    self.principal, task.task_id, dispatch["receipt"]["attempt"], self.owner,
                    pr_url=pr_url, error=land_failure,
                )
            if pr_url:
                self._event("landed", task.task_id, pr_url, job_id=job_ref)
            elif land_failure:
                self._event("land-failed", task.task_id, land_failure, job_id=job_ref)

    def pass_once(self) -> tuple[int, int]:
        tasks = self.task_reader(self.service, self.project_id)
        if self._refresh_claims(tasks):
            tasks = self.task_reader(self.service, self.project_id)
        for task in tasks:
            owner = (task.dispatch or {}).get("receipt", {}).get("dispatcher_owner")
            if task.status == "claimed" and task.dispatch and owner in (None, self.owner):
                try:
                    self._settle(task)
                except Exception as exc:
                    kind = "deferred" if isinstance(exc, TaskServiceError) and exc.code == "busy" else "failed"
                    self._event(kind, task.task_id, f"{str(exc).replace(chr(10), ' ')} [infra]",
                                job_id=(task.dispatch or {}).get("job_ref"))
        self._land_settled()
        tasks = self.task_reader(self.service, self.project_id)
        self._surface_approvals(tasks)
        self.waiting = {task.task_id: self.waiting[task.task_id] for task in tasks
                        if task.status == "todo" and not task.data.get("start_hold")
                        and task.task_id in self.waiting and task.task_id not in self.unsupported}
        preplaced = self._ready_tasks() if self.router is None or self.cloud_placer is None else {}
        active = [task for task in tasks if task.status in ("claimed", "running")]
        claims = [{**task.dispatch["receipt"], "job_ref": task.dispatch.get("job_ref")}
                  for task in active if task.dispatch and task.dispatch.get("receipt")]
        status = {task.task_id.rsplit("/", 1)[1]: task.status for task in tasks}
        awaiting_submission = {
            task.task_id.rsplit("/", 1)[1] for task in active
            if (dispatch := task.dispatch) and dispatch.get("state") == "intent"
            and (receipt := dispatch.get("receipt")) and not receipt.get("dispatcher_owner")
            and not self._orphan_expired(dispatch, receipt)
        }
        running_branches = {
            str(task.data["assigned_branch"])
            for task in tasks
            if task.data.get("assigned_branch")
            and (task.status in ("claimed", "running") or (task.dispatch or {}).get("pending_landing"))
        }
        picks: RouteCache = {}
        for board_task in tasks:
            task_id, data = board_task.task_id, board_task.data
            if (board_task.status != "todo" or data.get("start_hold") or data.get("owner")
                    or data.get("claimed_at") is not None or task_id in self.unsupported):
                continue
            if data.get("review_of") or data.get("contract_id") or data.get("acceptance_contract"):
                self.unsupported.add(task_id)
                self._event("failed", task_id, "review/contract tasks not supported by dispatch v1")
                continue
            pending = [dep for dep in data.get("dependency_ids", []) if status.get(dep) != "done"]
            if pending:
                # A legacy intent can still submit during its grace period.
                if any(dep not in awaiting_submission for dep in pending):
                    self._wait(task_id, "dependency not done: " + ", ".join(pending))
                continue
            if ((data.get("attempt_continuation") or {}).get("not_before") or 0) > self.clock():
                self._wait(task_id, "waiting for the quota window [budget]")
                continue
            overlap = next((other for other in active if touches_overlap(data, other.data)), None)
            if overlap:
                self._wait(task_id, f"touches overlap with {overlap.task_id}")
                continue
            branch = data.get("assigned_branch")
            if branch and str(branch) in running_branches:
                self._wait(task_id, f"assigned branch already running: {branch}")
                continue
            if self.parallel is not None and len(active) >= self.parallel:
                self._wait(task_id, f"--parallel limit reached ({self.parallel})")
                continue
            requested = self._requested(board_task)
            managed = self.router is not None if requested != "cloud" else self.cloud_placer is not None
            busy = self._local_busy(claims) if requested != "cloud" else {}
            expected = self._place(board_task, requested, busy, picks) if managed else None
            if managed:
                if expected is None:
                    continue
                summary: dict[str, Any] = {}
                cloud = expected.placement == "cloud"
            else:
                summary = preplaced.get(task_id, {})
                if not summary:
                    self.waiting.pop(task_id, None)
                    continue
                cloud = summary["effective_placement"] == "cloud"
                if (requested == "cloud" and not cloud) or (requested == "local" and cloud):
                    self._wait(task_id, f"{requested} placement evidence is missing")
                    continue
                location = summary["effective_placement"]
                record = data.get("placement_evidence", {}).get(location, {}).get("evidence")
                expected = PlacementEvidence.model_validate(record) if record else None
                engine = expected.engine_family if expected else None
                if cloud and not self._cloud_available(task_id, engine or ""):
                    continue
                if not cloud and engine in busy:
                    self._wait(task_id, f"local slot: {busy[engine]}", key=f"local slot: {engine}")
                    continue
            self._event("ready", task_id)
            attempt = len(data.get("dispatches", [])) + 1
            try:
                result = self.service.claim(
                    self.principal, task_id, board_task.revision,
                    f"dispatch:{task_id}:{board_task.revision}:{attempt}",
                    dispatcher_owner=self.owner, expected_placement=expected,
                )
            except TaskServiceError as exc:
                self._wait(task_id, str(exc).replace(chr(10), " "))
                continue
            except Exception as exc:
                self._event("failed", task_id, str(exc).replace(chr(10), " "))
                continue
            if not result.get("claim_active", True):
                self._wait(task_id, "claim is not active")
                continue
            if "dispatch_state" in result:
                self._wait(task_id, "task already claimed")
                continue
            claim = result["claim"]
            summary = {**summary, **claim}
            self._event("claimed", task_id)
            job_ref = None
            if cloud:
                started = self._launch_cloud(board_task, summary, claim)
            else:
                try:
                    job_ref = self.launcher.launch(self._launch_input({**summary, **data}, claim), claim)
                except Exception as exc:
                    self._record_base(task_id, claim)
                    reason = str(exc).replace("\n", " ")
                    job_id = _dispatch_launch.local_job_id(claim["claim_token"])
                    started = self.launcher.job(job_id) is not None
                    if started:
                        job_ref = job_id
                        self._event("deferred", task_id, reason, job_id=job_id)
                    else:
                        try:
                            self._fail_launch(board_task, claim, reason)
                        except Exception as release_exc:
                            started = True  # reconciliation still owns this claim
                            self._event("deferred", task_id, f"{release_exc} [infra]")
                else:
                    self._record_base(task_id, claim)
                    self._event("launched", task_id, job_id=job_ref)
                    started = True
            if started:
                self.waiting.pop(task_id, None)
                active.append(board_task)
                claims.append({**claim, "job_ref": job_ref})
                if branch:
                    running_branches.add(str(branch))
        self._blocked()
        tasks = self.task_reader(self.service, self.project_id)
        return sum(task.status == "todo" for task in tasks), sum(
            task.status in ("claimed", "running") or bool((task.dispatch or {}).get("pending_landing")) for task in tasks
        )

    def shutdown(self) -> None:
        """Release owned claims which have no bridge job to continue."""
        for task in self.task_reader(self.service, self.project_id):
            dispatch = task.dispatch or {}
            receipt = dispatch.get("receipt", {})
            if task.status != "claimed" or receipt.get("dispatcher_owner") != self.owner or dispatch.get("job_ref"):
                continue
            try:
                if receipt["effective_placement"] == "cloud":
                    job = self.launcher.job(_dispatch_launch.cloud_job_id(receipt["claim_token"]))
                else:
                    job = self._local_job(task)
                if job is None:
                    self.service.reconcile_dispatch(
                        self.principal, task.task_id, receipt["attempt"], "absent",
                        evidence_ref="dispatcher:shutdown", dispatcher_owner=self.owner,
                    )
                    self._event("reconciled", task.task_id, "released on shutdown")
            except Exception as exc:
                self._event("deferred", task.task_id, str(exc).replace(chr(10), " "))

    def _keep_leases(self, stop: Event, interval: float) -> None:
        # Checks, provider calls and Git operations may occupy the main thread
        # for several ticks. Keep its claims alive while those calls run.
        while not stop.wait(interval):
            try:
                self.service.renew_dispatcher_claims(self.principal, self.project_id, self.owner)
            except TaskServiceError as exc:
                self._event("deferred", self.project_id, str(exc))

    def _run(self, once: bool, interval: float) -> int:
        while True:
            if self.stop_requested():
                raise KeyboardInterrupt
            stop = Event()
            heartbeat = Thread(target=self._keep_leases, args=(stop, interval), daemon=True)
            try:
                heartbeat.start()
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
            else:
                budget_wait = any(
                    task.status == "todo" and not task.data.get("start_hold")
                    and (task.data.get("attempt_continuation") or {}).get("wait_for_budget")
                    for task in self.task_reader(self.service, self.project_id)
                )
                if once or (not ready and not running and not budget_wait
                            and not any(not reason.startswith("dependency ") for reason in self.waiting.values())):
                    return int(any(task.status == "failed" for task in self.task_reader(self.service, self.project_id)))
            finally:
                stop.set()
                if heartbeat.ident is not None:
                    heartbeat.join()
            self.sleep(interval)

    def run(self, *, once: bool = False) -> int:
        signum = signal.SIGINT

        def terminate(received: int, _frame: Any) -> None:
            nonlocal signum
            signum = signal.Signals(received)
            raise KeyboardInterrupt

        previous = signal.signal(signal.SIGTERM, terminate) if current_thread() is main_thread() else None
        try:
            return self._run(once, min(15.0, self.service.lease_seconds / 3))
        except KeyboardInterrupt:
            try:
                self.shutdown()
            except TaskServiceError as exc:
                self._event("deferred", self.project_id, str(exc))
            return 128 + int(signum)
        finally:
            if previous is not None:
                signal.signal(signal.SIGTERM, previous)


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


def owner_lock(project: str, store_db: str | None) -> _lockfile.JobLock:
    """The project owner lock, shared by CLI and MCP control."""
    location = str(Path(store_db).expanduser().resolve()) if store_db else ":memory:"
    key = hashlib.sha256(f"{location}\0{project}".encode()).hexdigest()
    return _lockfile.JobLock(_store.locks_dir(), Path(key))


def live_owner(lock: _lockfile.JobLock) -> dict[str, Any] | None:
    try:
        owner = json.loads(lock.path.read_text(encoding="utf-8"))
        if (isinstance(owner, dict) and isinstance(owner.get("pid"), int)
                and owner.get("host") == socket.gethostname().casefold()
                and not _lockfile.process_has_exited(owner["pid"], owner.get("started_at"))):
            return owner
    except (OSError, ValueError, TypeError):
        pass
    return None


def stop_path(lock: _lockfile.JobLock) -> Path:
    return lock.path.with_suffix(".stop")


def dispatch(args: argparse.Namespace) -> int:
    if args.dry_run:
        return _dispatch_main(args)
    lock = owner_lock(args.project, _resolve_store_db(None))
    pid = os.getpid()
    identity = uuid4().hex
    lock.acquire(identity, metadata={"host": socket.gethostname().casefold(),
                                     "started_at": _lockfile.process_start_time(pid), "instance_id": identity})
    try:
        return _dispatch_main(args, lock=lock, identity=identity)
    finally:
        lock.release()


def _dispatch_main(args: argparse.Namespace, *, lock: _lockfile.JobLock | None = None, identity: str = "") -> int:
    store_db = _resolve_store_db(None)
    principal = TaskPrincipal(actor="code-bridge-dispatch", role="orchestrator", projects=(args.project,))
    lease_minutes = getattr(args, "lease_minutes", None)
    lease_seconds = getattr(args, "lease_seconds", None)
    if lease_minutes is not None:
        lease_seconds = lease_minutes * 60
    service = (ProjectTaskService(store_db) if lease_seconds is None
               else ProjectTaskService(store_db, lease_seconds=lease_seconds))
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
    router, cloud_placer = _routers(Path(args.db) if args.db else None, repo_roots, launcher.cloud_free)
    config = _concurrency.load_config()

    def capacity(engine_family: str) -> int | None:
        return config.limits.get(_routing.bridge_engine(_routing.provider(engine_family)))

    def local_holders(engine_family: str) -> set[str]:
        engine = _routing.bridge_engine(_routing.provider(engine_family))
        return {str(holder["job_id"]) for holder in _concurrency.live_holders(engine, config.slot_dir)}

    knowledge_db = getattr(args, "knowledge_db", None)
    knowledge_store = _store.build_store(Path(knowledge_db).expanduser()) if knowledge_db is not None else None
    try:
        dispatcher = Dispatcher(service, principal, launcher, args.project, parallel=args.parallel, router=router,
                          capacity=capacity, cloud_placer=cloud_placer, local_holders=local_holders,
                          json_events=bool(getattr(args, "json", False)),
                          knowledge_store=knowledge_store, land=bool(getattr(args, "land", False)),
                          stale_minutes=getattr(args, "stale_minutes", DEFAULT_STALE_MINUTES))
        if lock is not None:
            def stop_requested() -> bool:
                try:
                    return stop_path(lock).read_text(encoding="utf-8") == identity
                except OSError:
                    return False

            def interruptible_sleep(seconds: float) -> None:
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    if stop_requested():
                        raise KeyboardInterrupt
                    time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))

            dispatcher.stop_requested = stop_requested
            dispatcher.sleep = interruptible_sleep
        delay = 5.0
        while True:
            try:
                return dispatcher.run(once=args.once)
            except Exception as exc:
                if not getattr(args, "supervise", False):
                    raise
                dispatcher._event("restart", args.project, f"{type(exc).__name__}: {exc}; retrying in {delay:g}s")
                try:
                    dispatcher.sleep(delay)
                except KeyboardInterrupt:
                    dispatcher.shutdown()
                    return 130
                delay = min(delay * 2, 300.0)
    finally:
        if knowledge_store is not None:
            knowledge_store.close()


def _routers(
    db_path: Path | None, repo_roots: dict[str, Path], cloud_free: Callable[[str], bool] | None = None,
) -> tuple[LocalRouter, CloudPlacer]:
    """The local quota router and the cloud placer used by `dispatch`."""

    def router(tier: str, busy: dict[Engine, str], preference: tuple[Engine, ...], fallback: Fallback,
               required_engine: Engine | None = None) -> RoutingDecision:
        selection = _routing.choose(
            tier, cwd=os.getcwd(), db_path=db_path, occupied_slots=busy,
            engine=_routing.bridge_engine(required_engine) if required_engine else None,
            engine_preference=preference, fallback=fallback, fallback_max_age_seconds=600.0,
        )
        decision = selection.decision
        # An explicit engine enables the router's operator override for missing or stale telemetry;
        # an unattended, engine-pinned repair still needs a recent, usable quota reading.
        if required_engine and decision.provider and not usable_reading(selection.readings.get(decision.provider)):
            return replace(
                decision, provider=None, model=None, effort=None,
                reason="engine-pinned repair needs a recent, usable quota reading",
                ineligible={**decision.ineligible, decision.provider: "telemetry missing, unreadable or stale"},
            )
        return decision

    def cloud_placer(task: dict[str, Any], tier: str) -> tuple[RoutingDecision, str]:
        """Route among providers with a cloud environment for this repository."""
        repo = _dispatch_launch.cloud_repo(repo_roots, task)
        slug = _cloud.repo_slug(_cloud.repo_root(repo))
        config = _cloud.load_config()
        environments: dict[Engine, str] = {}
        errors = []
        for family in ENGINES:
            engine = _routing.bridge_engine(family)
            routine = config.default_routine if engine == "claude" else None
            if engine == "claude" and routine is None:
                errors.append("Claude cloud has no default routine")
                continue
            try:
                environments[family] = _cloud.provider(engine).env_for(slug, config, routine)
            except CloudError as exc:
                errors.append(str(exc))
        if not environments:
            raise NoCloudEnvironment(f"{slug}: {' | '.join(errors)}")
        busy = {family: f"{_routing.bridge_engine(family)} cloud concurrency limit reached"
                for family in environments if cloud_free is not None and not cloud_free(family)}
        preference, fallback = _routing_options(task)
        decision = _routing.choose(
            tier, cwd=os.getcwd(), db_path=db_path, respect_slots=False, available_engines=frozenset(environments),
            occupied_slots=busy, engine_preference=preference, fallback=fallback,
        ).decision
        return decision, environments[decision.provider] if decision.provider is not None else ""

    return router, cloud_placer
