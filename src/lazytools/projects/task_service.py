"""The governed queue over the existing projects board and acceptance records."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict
from uuid import uuid4

from lazybridge.policy.review import ReviewIndependence
from pydantic import BaseModel, ConfigDict, FiniteFloat, StringConstraints

from lazytools.projects.contracts import find_contract_for_task
from lazytools.projects.plan_edit import _board_key
from lazytools.projects.records import get_project, touch_project_progress
from lazytools.projects.task_fields import (
    Placement,
    plan_fields,
    requested_placement,
    task_id,
    touches_overlap,
    validate_dependencies,
)
from lazytools.projects.task_store import TaskServiceError, TransactionStore, transaction
from lazytools.projects.verification import CheckResult, get_verification, get_verification_for_contract

Nonempty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ResultState = Literal["accepted", "blocked", "rework"]
Outcome = Literal["success", "task_failure", "infrastructure_failure"]
_COMMIT_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
DEFAULT_LEASE_SECONDS = 180


class DispatcherOwner(TypedDict):
    instance_id: str
    host: str
    pid: int
    started_at: str | None


class DispatchLanding(BaseModel):
    """The local publication still owed after accepting a task."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    cwd: Nonempty
    branch: Nonempty
    checks: tuple[str, ...]


class TaskPrincipal(BaseModel):
    """Identity and project scope supplied by the trusted host."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    actor: Nonempty
    role: Literal["worker", "orchestrator", "owner"]
    projects: tuple[Nonempty, ...]

    def require_project(self, project_id: str) -> None:
        if project_id not in self.projects:
            raise TaskServiceError("forbidden", "Project is outside the authenticated scope")

    def require_role(self, *roles: str) -> None:
        if self.role not in roles:
            raise TaskServiceError("forbidden", "The authenticated role cannot perform this operation")


class PlacementEvidence(BaseModel):
    """A trusted runtime check for one placement and one task revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    placement: Placement
    eligible: bool
    reason: Nonempty
    reference: Nonempty
    expires_at: FiniteFloat
    engine_family: Nonempty
    environment_id: Nonempty | None = None
    model: Nonempty | None = None
    effort: Nonempty | None = None


FailureClass = Literal["check", "infra", "flaky", "budget", "policy"]


class AttemptContinuation(BaseModel):
    """How a retry continues its predecessor, stored with the attempt it prepares."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    preserved_branch: Nonempty | None = None
    preserved_commit: Nonempty | None = None
    preserved_base: Nonempty | None = None  # the interrupted attempt's original dispatch-base
    preserved_files: tuple[str, ...] = ()
    preserved_message: str = ""
    preserved_unresolved: tuple[str, ...] = ()  # files the preserved work left with conflict markers
    session: Nonempty | None = None  # the failed attempt's session alias
    native_id: Nonempty | None = None  # its engine conversation; None starts a fresh one
    engine: Nonempty | None = None
    fresh: bool = False
    check_failures: tuple[str, ...] = ()  # one diagnostic per verification retry already prepared
    escalation_tier: Nonempty | None = None
    infra_retries: int = 0
    not_before: FiniteFloat | None = None
    wait_for_budget: bool = False
    repair_checkout: Nonempty | None = None
    cloud_branch: Nonempty | None = None
    cloud_pr_url: Nonempty | None = None


class CompletionEvidence(BaseModel):
    """An independently observed terminal provider receipt."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    reference: Nonempty
    job_ref: Nonempty
    executor: Nonempty
    engine_family: Nonempty
    state: ResultState
    artifact_ref: Nonempty | None = None
    verification_id: Nonempty | None = None
    reviewed_sha: Nonempty | None = None
    review_passed: bool = False
    review_independence: ReviewIndependence | None = None
    outcome: Outcome | None = None
    failure_reason: Nonempty | None = None
    failure_class: FailureClass | None = None
    continuation: AttemptContinuation | None = None
    flaky_checks: tuple[str, ...] = ()
    landing: DispatchLanding | None = None
    preserved_branch: Nonempty | None = None  # work of a failed attempt, kept for the retry
    pr_url: Nonempty | None = None  # `dispatch --land`: the PR the accepted branch was pushed to
    land_failure: Nonempty | None = None  # `dispatch --land`: why push or PR failed (the task stays accepted)


def _route_key(repo: str, placement: str, environment_id: str) -> str:
    encoded = json.dumps([repo, placement, environment_id], ensure_ascii=True, separators=(",", ":"))
    return "task-route:" + hashlib.sha256(encoded.encode()).hexdigest()


def _route(task: BoardTask, placement: PlacementEvidence) -> tuple[str, str, str] | None:
    repo = task.data.get("repo")
    if not repo or not placement.environment_id:
        return None
    return repo, placement.placement, placement.environment_id


@dataclass
class BoardTask:
    project_id: str
    board: dict[str, Any]
    index: int

    @property
    def data(self) -> dict[str, Any]:
        return self.board["tasks"][self.index]

    @property
    def identifier(self) -> str:
        return f"{self.project_id}/{task_id(self.data, self.index)}"

    @property
    def revision(self) -> str:
        return self.data.get("revision", "0")

    def save(self, store: TransactionStore, now: float) -> None:
        self.board["updated_at"] = now
        store.write(_board_key(self.project_id), self.board)


@dataclass(frozen=True)
class AcceptedRun:
    task_id: str
    revision: str
    attempt: int
    job_ref: str
    evidence: CompletionEvidence


@dataclass(frozen=True)
class PublicationReview:
    repository: str
    branch: str
    writer: AcceptedRun | None = None
    reviewer: AcceptedRun | None = None
    independence: ReviewIndependence | None = None
    checks: tuple[CheckResult, ...] = ()


def _accepted_run(task: BoardTask) -> AcceptedRun | None:
    history = task.data.get("dispatches", [])
    if not history or task.data.get("status") != "done":
        return None
    dispatch = history[-1]
    result = dispatch.get("completion", {}).get("result", {})
    if result.get("state") != "accepted" or result.get("revision") != task.revision:
        return None
    evidence = dispatch.get("evidence", {}).get(result["evidence_ref"])
    if evidence is None:
        return None
    return AcceptedRun(
        task.identifier,
        task.revision,
        result["attempt"],
        dispatch["job_ref"],
        CompletionEvidence.model_validate(evidence),
    )


def _review_independence(
    writer_engine: str,
    reviewer: CompletionEvidence,
    writer_executor: str,
) -> ReviewIndependence | None:
    engines = {writer_engine, reviewer.engine_family}
    if reviewer.executor == writer_executor:
        return None
    if engines == {"codex", "claude_code"} and reviewer.review_independence in (None, "opposite-engine"):
        return "opposite-engine"
    if engines in ({"codex"}, {"claude_code"}) and reviewer.review_independence == "same-provider-fresh":
        return "same-provider-fresh"
    return None


def _load_task(store: TransactionStore, identifier: str) -> BoardTask:
    project_id, separator, identifier_in_project = identifier.partition("/")
    board = store.read(_board_key(project_id)) if separator and identifier_in_project else None
    if board is not None:
        for index, task in enumerate(board["tasks"]):
            if task_id(task, index) == identifier_in_project:
                return BoardTask(project_id, board, index)
    raise TaskServiceError("not_found", "Task does not exist")


def _verify_landing(repo: str, pr_url: str, merge_commit: str) -> bool:
    """Check the PR in its recorded repository using GitHub's current default branch."""
    from lazytools.code_bridge._cloud import CloudError
    from lazytools.code_bridge._gh import _json, default_branch

    match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/pull/([0-9]+)", pr_url)
    if not match or match.group(1).casefold() != repo.casefold():
        return False
    try:
        branch = default_branch(repo)
        pr = _json("pr", "view", match.group(2), "--repo", repo, "--json",
                   "state,baseRefName,mergeCommit")
        return (isinstance(pr, dict) and pr.get("state") == "MERGED"
                and pr.get("baseRefName") == branch
                and isinstance(pr.get("mergeCommit"), dict)
                and pr["mergeCommit"].get("oid") == merge_commit)
    except CloudError as exc:
        raise TaskServiceError("unavailable", "Could not verify the PR with GitHub") from exc


def _dependencies(task: BoardTask) -> list[dict[str, Any]]:
    by_id = {task_id(item, index): item for index, item in enumerate(task.board["tasks"])}
    dependencies = []
    for dependency in task.data.get("dependency_ids", []):
        entry = by_id.get(dependency, {})
        status = entry.get("status", "missing")
        summary = {"task_id": f"{task.project_id}/{dependency}", "status": status}
        if (entry.get("dispatches") and status == "done"
                and not (entry.get("landing_evidence", {}).get("revision") == entry.get("revision", "0")
                         and entry.get("landing_evidence", {}).get("dispatch_count") == len(entry["dispatches"]))):
            completed = entry["dispatches"][-1].get("completion", {}).get("result", {})
            if completed.get("state") != "accepted":
                summary["status"] = "unverified"
            else:
                summary.update(
                    artifact_ref=completed["artifact_ref"],
                    engine_family=completed["engine_family"],
                    executor=completed["executor"],
                    attempt=completed["attempt"],
                )
        dependencies.append(summary)
    return dependencies


def _check_placement(
    task: BoardTask, location: Placement, now: float, *, check_expiry: bool = True
) -> tuple[PlacementEvidence | None, str]:
    record = task.data.get("placement_evidence", {}).get(location)
    if record is None or record["revision"] != task.revision:
        return None, f"{location} placement evidence is missing"
    current = PlacementEvidence.model_validate(record["evidence"])
    if check_expiry and current.expires_at <= now:
        return None, f"{location} placement evidence is stale"
    if location == "cloud" and current.eligible and not current.environment_id:
        return None, "cloud placement evidence is missing environment_id"
    return current if current.eligible else None, current.reason


def _placement(task: BoardTask, now: float) -> tuple[PlacementEvidence | None, str]:
    if (task.data.get("attempt_continuation") or {}).get("repair_checkout"):
        return _check_placement(task, "local", now)
    requested = requested_placement(task.data)
    preferred = "local" if requested == "auto" else requested
    eligible, reason = _check_placement(task, preferred, now)
    if eligible is None and requested == "auto":
        return _check_placement(task, "cloud", now)
    if eligible is None and requested == "cloud":
        eligible, local_reason = _check_placement(task, "local", now)
        reason = f"{reason}. Local fallback: {local_reason}"
    return eligible, reason


def _ready(
    task: BoardTask, now: float, location: Placement | None = None,
) -> tuple[dict[str, Any], PlacementEvidence] | None:
    data = task.data
    continuation = data.get("attempt_continuation") or {}
    if (
        data.get("status") != "todo"
        or data.get("owner")
        or data.get("claimed_at") is not None
        or data.get("start_hold")
        or (continuation.get("not_before") or 0) > now
    ):
        return None
    dependencies = _dependencies(task)
    if any(dep["status"] != "done" for dep in dependencies):
        return None
    requested = "local" if continuation.get("repair_checkout") else requested_placement(data)
    if location is not None and requested not in ("auto", location):
        return None
    placement, reason = _check_placement(task, location, now) if location else _placement(task, now)
    if placement is None:
        return None
    summary = {
        "task_id": task.identifier,
        "project_id": task.project_id,
        "revision": task.revision,
        "text": data["text"],
        "tier": continuation.get("escalation_tier") or data.get("tier"),
        "dependencies": dependencies,
        "acceptance_criteria": data.get("acceptance_criteria", []),
        "requested_profile": data.get("requested_profile"),
        "requested_network": data.get("requested_network"),
        "requested_placement": requested_placement(data),
        "effective_placement": placement.placement,
        "placement_reason": reason,
        "preflight_ref": placement.reference,
    }
    if target := data.get("review_target"):
        summary["review_target"] = target
    return summary, placement


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= 100:
        raise TaskServiceError("invalid", "Limit must be between 1 and 100")


def _scoped_task(store: TransactionStore, principal: TaskPrincipal, task_id: str) -> BoardTask:
    principal.require_project(task_id.partition("/")[0])
    return _load_task(store, task_id)


def _require_open(store: TransactionStore, project_id: str) -> None:
    project = get_project(store, project_id)
    if project is None or project.status != "open":
        raise TaskServiceError("conflict", "Project is not open")


def _active_dispatch(task: BoardTask) -> dict[str, Any]:
    if not task.data.get("active_queue_claim") or task.data["status"] != "claimed":
        raise TaskServiceError("stale", "Task has no active claim")
    dispatch = task.data["dispatches"][-1]
    receipt = dispatch["receipt"]
    if receipt["revision"] != task.revision or receipt["executor"] != task.data.get("owner"):
        raise TaskServiceError("stale", "Active claim no longer matches the task")
    return dispatch


def _fenced_dispatch(
    task: BoardTask, principal: TaskPrincipal, claim_token: str, now: float, *, allow_expired: bool = False
) -> dict[str, Any]:
    dispatch = _active_dispatch(task)
    receipt = dispatch["receipt"]
    if receipt["claim_token"] != claim_token:
        raise TaskServiceError("stale", "Claim token has been superseded")
    if receipt["executor"] != principal.actor:
        raise TaskServiceError("forbidden", "Claim belongs to a different executor")
    if not allow_expired and receipt["lease_expires_at"] <= now:
        raise TaskServiceError("stale", "Claim lease has expired")
    return dispatch


def _require_dispatcher_owner(receipt: dict[str, Any], owner: DispatcherOwner | None) -> None:
    if owner is not None and receipt.get("dispatcher_owner") != owner:
        raise TaskServiceError("stale", "Dispatch is no longer owned by this dispatcher")


def _release(task: BoardTask) -> None:
    task.data.update(active_queue_claim=False, status="todo", owner=None, claimed_at=None)


def _queue_continuation(task: BoardTask, continuation: AttemptContinuation, reason: str) -> None:
    task.data.update(
        revision=uuid4().hex, completed_at=None, result="", error="", attempt_reason=reason,
        attempt_continuation=continuation.model_dump(mode="json"),
    )


class ProjectTaskService:
    def __init__(
        self,
        store_db: str | None,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        quarantine_threshold: int = 3,
        clock: Callable[[], float] = time.time,
        landing_check: Callable[[str, str, str], bool] = _verify_landing,
    ):
        self.path = Path(store_db).resolve() if store_db and store_db != ":memory:" else None
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.lease_seconds = lease_seconds
        if (
            isinstance(quarantine_threshold, bool)
            or not isinstance(quarantine_threshold, int)
            or quarantine_threshold < 1
        ):
            raise ValueError("quarantine_threshold must be a positive integer")
        self.quarantine_threshold = quarantine_threshold
        self.clock = clock
        self.landing_check = landing_check

    def mark_landed(
        self, principal: TaskPrincipal, project: str, task_id: str, pr_url: str, merge_commit: str, reason: str,
    ) -> dict[str, Any]:
        """Record independently verified landing of an unclaimed task."""
        principal.require_role("orchestrator", "owner")
        principal.require_project(project)
        if (not project or "/" in project or not task_id or "/" in task_id or not reason.strip()
                or not _COMMIT_SHA.fullmatch(merge_commit)):
            raise TaskServiceError("invalid", "A project, task id, full merge commit and reason are required")
        identifier = f"{project}/{task_id}"

        def eligible(task: BoardTask) -> str:
            if (task.data.get("status") not in ("failed", "todo") or task.data.get("owner")
                    or task.data.get("claimed_at") is not None or task.data.get("active_queue_claim")):
                raise TaskServiceError("conflict", "Task must be failed or todo and unclaimed")
            repo = task.data.get("repo")
            if not isinstance(repo, str) or not repo:
                raise TaskServiceError("invalid", "Task must specify a repository")
            return repo

        with transaction(self.path) as store:
            task = _scoped_task(store, principal, identifier)
            repo, revision = eligible(task), task.revision
            snapshot = copy.deepcopy(task.data)
        if not self.landing_check(repo, pr_url, merge_commit):
            raise TaskServiceError("invalid", "PR is not merged into the default branch at the given commit")
        with transaction(self.path, write=True) as store:
            task = _scoped_task(store, principal, identifier)
            if eligible(task) != repo or task.data != snapshot:
                raise TaskServiceError("stale", "Task changed during landing verification")
            now = self.clock()
            landing = {"pr_url": pr_url, "merge_commit": merge_commit, "actor": principal.actor,
                       "reason": reason.strip(), "revision": revision, "at": now,
                       "dispatch_count": len(task.data.get("dispatches", []))}
            task.data.update(status="done", completed_at=now, result=pr_url, error="", landing_evidence=landing)
            touch_project_progress(store, project)
            task.save(store, now)
            return {"task_id": identifier, "landing": landing}

    def edit_task(
        self, principal: TaskPrincipal, project: str, task_id: str, expected_revision: str,
        changes: dict[str, Any], reason: str,
    ) -> dict[str, Any]:
        """Edit an unclaimed task atomically and retain its identity and audit trail."""
        principal.require_role("orchestrator", "owner")
        principal.require_project(project)
        allowed = {"text", "title", "checks", "acceptance_criteria", "tier", "dependency_ids", "touches", "engine_preference"}
        if not reason.strip() or not changes or set(changes) - allowed:
            raise TaskServiceError("invalid", "A reason and supported changes are required")
        try:
            fields = plan_fields({k: v for k, v in changes.items() if k not in ("text", "touches")})
            if set(fields) != set(changes) - {"text", "touches"}:
                raise ValueError("Edited fields cannot be null")
            if "text" in changes:
                if not isinstance(changes["text"], str) or not changes["text"].strip():
                    raise ValueError("text must be a nonempty string")
                fields["text"] = changes["text"].strip()
            if "touches" in changes:
                values = changes["touches"]
                if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
                    raise ValueError("touches must be a list of nonempty strings")
                fields["touches"] = [v.strip() for v in values]
        except (ValueError, TypeError) as exc:
            raise TaskServiceError("invalid", str(exc)) from exc
        if "/" in project or "/" in task_id or not task_id:
            raise TaskServiceError("invalid", "Expected a project and a task id within that project")
        with transaction(self.path, write=True) as store:
            task = _scoped_task(store, principal, f"{project}/{task_id}")
            if task.revision != expected_revision:
                raise TaskServiceError("stale", "Task revision changed")
            if (task.data.get("status") != "todo" or task.data.get("owner")
                    or task.data.get("claimed_at") is not None or task.data.get("active_queue_claim")):
                raise TaskServiceError("conflict", "Task must be unclaimed and todo")
            previous = {key: task.data.get(key) for key in fields}
            task.data.update(fields)
            try:
                validate_dependencies(task.board["tasks"])
            except ValueError as exc:
                raise TaskServiceError("invalid", str(exc)) from exc
            now = self.clock()
            new_revision = uuid4().hex
            task.data.setdefault("edit_history", []).append({
                "at": now, "actor": principal.actor, "reason": reason.strip(),
                "previous_values": previous, "previous_revision": task.revision, "revision": new_revision,
            })
            task.data.update(revision=new_revision, effective_placement=None, placement_reason=None, preflight_ref=None)
            task.save(store, now)
            return {"task_id": task.identifier, "revision": new_revision}

    def publication_review(
        self,
        task_id: str,
        *,
        store: TransactionStore | None = None,
    ) -> PublicationReview:
        """Read the current writer acceptance and a review of that exact run."""
        if store is None:
            with transaction(self.path) as snapshot:
                return self.publication_review(task_id, store=snapshot)
        task = _load_task(store, task_id)
        writer = _accepted_run(task)
        repository = task.data.get("repo") or ""
        branch = task.data.get("assigned_branch") or ""
        if writer is None:
            return PublicationReview(repository, branch)
        checks = []
        if writer.evidence.verification_id:
            verification = get_verification(store, writer.evidence.verification_id)
            if verification and verification.status == "accepted":
                checks = verification.checks
        reviews: list[tuple[AcceptedRun, ReviewIndependence | None]] = []
        for index, entry in enumerate(task.board["tasks"]):
            if entry.get("review_of") != task.identifier.split("/", 1)[1]:
                continue
            review_task = BoardTask(task.project_id, task.board, index)
            reviewer = _accepted_run(review_task)
            if reviewer is None or not reviewer.evidence.review_passed:
                continue
            dependencies = entry["dispatches"][-1]["receipt"]["dependencies"]
            if not any(dep.get("task_id") == task_id and dep.get("attempt") == writer.attempt for dep in dependencies):
                continue
            sha = writer.evidence.artifact_ref
            if not sha or reviewer.evidence.reviewed_sha != sha or reviewer.evidence.artifact_ref != sha:
                continue
            independence = _review_independence(
                writer.evidence.engine_family, reviewer.evidence, writer.evidence.executor
            )
            if writer.job_ref == reviewer.job_ref:
                independence = None
            reviews.append((reviewer, independence))
        if not reviews:
            return PublicationReview(repository, branch, writer, checks=tuple(checks))
        order = {None: 0, "same-provider-fresh": 1, "opposite-engine": 2}
        reviewer, independence = max(reviews, key=lambda review: order[review[1]])
        return PublicationReview(repository, branch, writer, reviewer, independence, tuple(checks))

    def record_placement(self, task_id: str, revision: str, evidence: PlacementEvidence) -> None:
        """Persist an executor's preflight/runtime observation. Host operation."""
        with transaction(self.path, write=True) as store:
            task = _load_task(store, task_id)
            _require_open(store, task.project_id)
            if task.revision != revision:
                raise TaskServiceError("stale", "Task revision changed")
            now = self.clock()
            old = task.data.get("placement_evidence", {}).get(evidence.placement, {})
            previous = old.get("evidence", {})
            eligible = evidence.eligible and (evidence.placement != "cloud" or bool(evidence.environment_id))
            was_eligible = previous.get("eligible") and (
                evidence.placement != "cloud" or bool(previous.get("environment_id"))
            )
            since = now
            if old.get("revision") == revision and was_eligible and eligible and previous.get("expires_at", 0) > now:
                since = old.get("eligible_since", now)
            ineligible_since = now
            if old.get("revision") == revision and previous and not was_eligible and not eligible:
                ineligible_since = old.get("ineligible_since", old.get("recorded_at", now))
            route = _route(task, evidence)
            fresh_for_route = False
            if route:
                key = _route_key(*route)
                state = store.read(
                    key, {"route": list(route), "consecutive_failures": 0, "quarantined": False, "history": []}
                )
                references = state.setdefault("preflight_refs", [])
                fresh_for_route = evidence.reference not in references
                if fresh_for_route:
                    references.append(evidence.reference)
                    store.write(key, state)
            task.data.setdefault("placement_evidence", {})[evidence.placement] = {
                "revision": revision,
                "evidence": evidence.model_dump(mode="json"),
                "eligible_since": since,
                "ineligible_since": ineligible_since if not eligible else None,
                "recorded_at": now,
                "fresh_for_route": fresh_for_route,
            }
            if task.data["status"] == "todo":
                effective, reason = _placement(task, now)
                task.data.update(
                    effective_placement=effective.placement if effective else None,
                    placement_reason=reason,
                    preflight_ref=effective.reference if effective else None,
                )
            task.save(store, now)

    def queue_metrics(self, principal: TaskPrincipal, project_id: str | None = None) -> dict[str, Any]:
        """Count scoped open work; unknown historical ready ages are reported separately."""
        if project_id is not None:
            principal.require_project(project_id)
        projects = [project_id] if project_id is not None else sorted(set(principal.projects))

        def empty() -> dict[str, Any]:
            return {
                "ready": [],
                "blocked": {
                    key: 0
                    for key in (
                        "unmet_dependency",
                        "failed_dependency",
                        "hold",
                        "missing_placement_evidence",
                        "route_quarantined",
                    )
                },
                "running": 0,
            }

        with transaction(self.path) as store:
            now = self.clock()
            result = {pid: empty() for pid in projects}
            overall = empty()
            for pid in projects:
                project, board = get_project(store, pid), store.read(_board_key(pid))
                if project is None or project.status != "open" or not board:
                    continue
                for index, data in enumerate(board["tasks"]):
                    task = BoardTask(pid, board, index)
                    targets = (result[pid], overall)
                    if data.get("status") == "claimed":
                        for target in targets:
                            target["running"] += 1
                        continue
                    if data.get("status") != "todo" or data.get("owner") or data.get("claimed_at") is not None:
                        continue
                    deps = _dependencies(task)
                    reasons = []
                    if any(d["status"] in ("failed", "cancelled") for d in deps):
                        reasons.append("failed_dependency")
                    if any(d["status"] not in ("done", "failed", "cancelled") for d in deps):
                        reasons.append("unmet_dependency")
                    if data.get("start_hold"):
                        reasons.append("hold")
                    ready = _ready(task, now)
                    eligible, _ = _placement(task, now)
                    if eligible is None:
                        reasons.append("missing_placement_evidence")
                    route = _route(task, eligible) if eligible else None
                    route_state = store.read(_route_key(*route), {}) if route else {}
                    if route_state.get("quarantined"):
                        reasons.append("route_quarantined")
                    for target in targets:
                        for reason in reasons:
                            target["blocked"][reason] += 1
                    if ready is None or route_state.get("quarantined"):
                        continue
                    placement = ready[1].placement
                    record = data["placement_evidence"][placement]
                    timestamps = [record.get("eligible_since")]
                    if data.get("requested_placement") == "cloud" and placement == "local":
                        cloud = data.get("placement_evidence", {}).get("cloud")
                        if cloud and cloud.get("revision") == task.revision:
                            observed = cloud.get("evidence", {})
                            timestamps.append(
                                observed.get("expires_at")
                                if observed.get("eligible") and observed.get("environment_id")
                                else cloud.get("ineligible_since", cloud.get("recorded_at"))
                            )
                    if route_state.get("history"):
                        timestamps.append(route_state["history"][-1]["at"])
                    if data.get("attempts"):
                        timestamps.append(data.get("queue_released_at"))
                    by_id = {task_id(item, i): item for i, item in enumerate(board["tasks"])}
                    timestamps.extend(by_id[d].get("completed_at") for d in data.get("dependency_ids", []))
                    timestamps.extend(
                        e["at"]
                        for e in board.get("schedule_events", [])
                        if e.get("task_index") == index and not e.get("hold") and "at" in e
                    )
                    timestamps.extend(
                        e["at"]
                        for e in board.get("plan_events", [])
                        if e.get("task_index") == index and e.get("event") == "reopened" and "at" in e
                    )
                    timestamps.extend(
                        d.get("completion", {}).get("result", {}).get("completed_at")
                        for d in data.get("dispatches", [])
                        if d.get("completion")
                    )
                    age = (
                        max(0.0, now - max(timestamps))
                        if all(isinstance(t, (int, float)) for t in timestamps)
                        else None
                    )
                    for target in targets:
                        group = next(
                            (
                                g
                                for g in target["ready"]
                                if g["tier"] == data.get("tier") and g["effective_placement"] == placement
                            ),
                            None,
                        )
                        if group is None:
                            group = {
                                "tier": data.get("tier"),
                                "effective_placement": placement,
                                "count": 0,
                                "oldest_ready_age_seconds": None,
                                "unknown_age_count": 0,
                            }
                            target["ready"].append(group)
                        group["count"] += 1
                        if age is None:
                            group["unknown_age_count"] += 1
                        else:
                            group["oldest_ready_age_seconds"] = max(group["oldest_ready_age_seconds"] or 0, age)
            return {"projects": result, "overall": overall, "as_of": now}

    def next_ready(
        self, principal: TaskPrincipal, project_id: str, limit: int = 20, cursor: int | None = None
    ) -> dict[str, Any]:
        principal.require_project(project_id)
        _validate_limit(limit)
        if cursor is not None and cursor < 0:
            raise TaskServiceError("invalid", "Cursor must be a nonnegative task index")
        with transaction(self.path) as store:
            project = get_project(store, project_id)
            board = store.read(_board_key(project_id))
            if project is None or project.status != "open" or board is None:
                return {"tasks": [], "next_cursor": None}
            now = self.clock()
            items: list[dict[str, Any]] = []
            next_cursor = None
            last_index = cursor
            for index in range(0 if cursor is None else cursor + 1, len(board["tasks"])):
                ready = _ready(BoardTask(project_id, board, index), now)
                if ready is not None:
                    route = _route(BoardTask(project_id, board, index), ready[1])
                    if route and store.read(_route_key(*route), {}).get("quarantined"):
                        continue
                if ready is not None:
                    if len(items) == limit:
                        next_cursor = last_index
                        break
                    items.append(ready[0])
                    last_index = index
            return {"tasks": items, "next_cursor": next_cursor}

    def prepare_attempt(
        self, principal: TaskPrincipal, task_id: str, expected_revision: str, reason: str, *,
        review_target: str | None = None, continuation: AttemptContinuation | None = None,
    ) -> str:
        """Queue another attempt while retaining the previous dispatch and its evidence.

        A continuation is stored with the task until the next prepared attempt, so a
        dispatcher that restarts before the launch still continues the failed attempt.
        """
        principal.require_role("orchestrator", "owner")
        if not reason.strip():
            raise TaskServiceError("invalid", "An attempt reason is required")
        with transaction(self.path, write=True) as store:
            task = _scoped_task(store, principal, task_id)
            _require_open(store, task.project_id)
            if task.revision != expected_revision:
                raise TaskServiceError("stale", "Task revision changed")
            history = task.data.get("dispatches", [])
            if (
                task.data.get("status") not in ("todo", "done", "failed")
                or task.data.get("active_queue_claim")
                or (
                    history
                    and (history[-1]["state"] not in ("completed", "absent", "terminated")
                         or history[-1].get("unresolved_effects"))
                )
            ):
                raise TaskServiceError("conflict", "Settle the current dispatch before preparing another attempt")
            if review_target is not None and (not task.data.get("review_of") or not _COMMIT_SHA.fullmatch(review_target)):
                raise TaskServiceError("invalid", "A review target requires a review task and a full commit SHA")
            _release(task)
            task.data.update(
                revision=uuid4().hex, completed_at=None, result="", error="", attempt_reason=reason.strip(),
                review_target=review_target,
                attempt_continuation=continuation.model_dump(mode="json") if continuation else None,
            )
            task.save(store, self.clock())
            return task.revision

    def claim(
        self, principal: TaskPrincipal, task_id: str, expected_revision: str, idempotency_key: str,
        *, dispatcher_owner: DispatcherOwner | None = None, expected_placement: PlacementEvidence | None = None,
    ) -> dict[str, Any]:
        principal.require_role("worker", "orchestrator")
        if dispatcher_owner is not None or expected_placement is not None:
            principal.require_role("orchestrator")
        if not idempotency_key.strip():
            raise TaskServiceError("invalid", "A dispatch idempotency key is required")
        with transaction(self.path, write=True) as store:
            task = _scoped_task(store, principal, task_id)
            history = task.data.get("dispatches", [])
            for dispatch in history:
                receipt = dispatch["receipt"]
                if receipt["idempotency_key"] == idempotency_key:
                    if receipt["executor"] != principal.actor or receipt["revision"] != expected_revision:
                        raise TaskServiceError("conflict", "Dispatch idempotency key has already been used")
                    if receipt.get("dispatcher_owner") != dispatcher_owner:
                        raise TaskServiceError("conflict", "Claim belongs to a different dispatcher instance")
                    active = (
                        task.data.get("active_queue_claim") is True
                        and task.data.get("status") == "claimed"
                        and history[-1] is dispatch
                        and receipt["lease_expires_at"] > self.clock()
                    )
                    return {"claim": receipt, "dispatch_state": dispatch["state"], "claim_active": active}
            if task.revision != expected_revision:
                raise TaskServiceError("stale", "Task revision changed")
            _require_open(store, task.project_id)
            now = self.clock()
            ready = _ready(task, now, expected_placement.placement if expected_placement else None)
            if ready is None:
                raise TaskServiceError("conflict", "Task is no longer ready")
            summary, placement = ready
            if expected_placement is not None and placement != expected_placement:
                raise TaskServiceError("conflict", "Task placement changed")
            if dispatcher_owner is not None:
                for index, other in enumerate(task.board["tasks"]):
                    if other.get("status") in ("claimed", "running") and touches_overlap(task.data, other):
                        other_id = BoardTask(task.project_id, task.board, index).identifier
                        raise TaskServiceError("conflict", f"touches overlap with {other_id}")
            route = _route(task, placement)
            if route and store.read(_route_key(*route), {}).get("quarantined"):
                raise TaskServiceError("route_quarantined", "Route is quarantined pending a fresh green preflight")
            attempt = max(task.data.get("attempts", 0), history[-1]["receipt"]["attempt"] if history else 0) + 1
            receipt = {
                **summary,
                "claim_token": uuid4().hex,
                "executor": principal.actor,
                "idempotency_key": idempotency_key,
                "lease_expires_at": now + self.lease_seconds,
                "attempt": attempt,
                "engine_family": placement.engine_family,
                "route": list(route) if route is not None else None,
            }
            if placement.model is not None:
                receipt["model"] = placement.model
            if placement.effort is not None:
                receipt["effort"] = placement.effort
            if dispatcher_owner is not None:
                receipt["dispatcher_owner"] = dispatcher_owner
            # A null claimed_at keeps the legacy board's lease timer from
            # independently recycling this claim. This receipt owns the lease.
            task.data.update(
                status="claimed",
                owner=principal.actor,
                claimed_at=None,
                attempts=attempt,
                active_queue_claim=True,
                effective_placement=placement.placement,
                placement_reason=summary["placement_reason"],
                preflight_ref=placement.reference,
                dispatches=[*history, {"receipt": receipt, "state": "intent", "created_at": now}],
            )
            task.save(store, now)
            return {"claim": receipt}

    def renew_dispatcher_claims(
        self, principal: TaskPrincipal, project_id: str, owner: DispatcherOwner,
        *, dead_instances: set[str] | None = None,
    ) -> None:
        """Renew a dispatcher's claims and take over abandoned work in one board write."""
        principal.require_role("orchestrator")
        principal.require_project(project_id)
        with transaction(self.path, write=True) as store:
            key = _board_key(project_id)
            board = store.read(key, {"tasks": []})
            now = self.clock()
            changed = False
            for data in board["tasks"]:
                history = data.get("dispatches")
                if not history:
                    continue
                dispatch = history[-1]
                if not (data.get("active_queue_claim") or dispatch.get("pending_landing")):
                    continue
                receipt = dispatch["receipt"]
                previous = receipt.get("dispatcher_owner")
                if previous is None or receipt["executor"] != principal.actor:
                    continue
                if previous != owner:
                    if dead_instances is None or (
                        previous["instance_id"] not in dead_instances and receipt["lease_expires_at"] > now
                    ):
                        continue
                    receipt["dispatcher_owner"] = owner
                    dispatch["dispatcher_recovered"] = True
                    changed = True
                expires = now + self.lease_seconds
                if receipt["lease_expires_at"] != expires:
                    receipt["lease_expires_at"] = expires
                    changed = True
            if changed:
                board["updated_at"] = now
                store.write(key, board)

    def finish_dispatch_landing(
        self, principal: TaskPrincipal, task_id: str, attempt: int, owner: DispatcherOwner,
        *, pr_url: str | None, error: str | None,
    ) -> None:
        principal.require_role("orchestrator")
        with transaction(self.path, write=True) as store:
            task = _scoped_task(store, principal, task_id)
            dispatch = task.data["dispatches"][-1]
            receipt = dispatch["receipt"]
            if (receipt["attempt"] != attempt or receipt["executor"] != principal.actor
                    or receipt.get("dispatcher_owner") != owner or not dispatch.get("pending_landing")):
                raise TaskServiceError("stale", "Landing is no longer owned by this dispatcher")
            dispatch.pop("pending_landing")
            dispatch["landing_result"] = {"pr_url": pr_url, "error": error}
            task.save(store, self.clock())

    def record_base_commit(self, principal: TaskPrincipal, task_id: str, claim_token: str, base_commit: str) -> None:
        """Add the commit the task's checkout started from to that claim's receipt."""
        principal.require_role("orchestrator")
        with transaction(self.path, write=True) as store:
            task = _scoped_task(store, principal, task_id)
            for dispatch in task.data.get("dispatches", []):
                if dispatch["receipt"]["claim_token"] == claim_token:
                    dispatch["receipt"]["base_commit"] = base_commit
                    task.save(store, self.clock())
                    return
            raise TaskServiceError("stale", "No dispatch holds this claim token")

    def begin_submit(
        self, principal: TaskPrincipal, task_id: str, claim_token: str,
        *, dispatcher_owner: DispatcherOwner | None = None,
    ) -> None:
        """Reserve the provider call. The adapter may call submit only after this succeeds."""
        with transaction(self.path, write=True) as store:
            task = _scoped_task(store, principal, task_id)
            _require_open(store, task.project_id)
            now = self.clock()
            dispatch = _fenced_dispatch(task, principal, claim_token, now)
            if dispatch["state"] != "intent":
                raise TaskServiceError("conflict", "Dispatch already started and must be reconciled")
            placement, _ = _placement(task, now)
            receipt = dispatch["receipt"]
            _require_dispatcher_owner(receipt, dispatcher_owner)
            current_route = _route(task, placement) if placement is not None else None
            if (
                placement is None
                or placement.placement != receipt["effective_placement"]
                or placement.engine_family != receipt["engine_family"]
                or placement.reference != receipt["preflight_ref"]
                or (list(current_route) if current_route is not None else None) != receipt.get("route")
                or _dependencies(task) != receipt["dependencies"]
                or task.data.get("start_hold")
            ):
                raise TaskServiceError("stale", "Dispatch prerequisites changed after the claim")
            dispatch.update(state="submitting", submit_started_at=now)
            task.save(store, now)

    def check_cloud_submit(
        self, principal: TaskPrincipal, task_id: str, claim_token: str, instance_id: str,
    ) -> None:
        """Fence a cloud launcher that reaches the provider after its dispatcher was replaced."""
        principal.require_role("orchestrator")
        with transaction(self.path) as store:
            task = _scoped_task(store, principal, task_id)
            dispatch = _fenced_dispatch(task, principal, claim_token, self.clock())
            receipt = dispatch["receipt"]
            if (dispatch["state"] != "submitting" or receipt["effective_placement"] != "cloud"
                    or (receipt.get("dispatcher_owner") or {}).get("instance_id") != instance_id):
                raise TaskServiceError("stale", "Cloud submit no longer belongs to this dispatcher")

    def local_claim(
        self,
        principal: TaskPrincipal,
        task_id: str,
        claim_token: str,
        *,
        state: Literal["intent", "submitting", "submitted"],
        job_ref: str | None = None,
    ) -> dict[str, Any]:
        """Recheck the local dispatch fence and its current placement evidence."""
        with self.local_dispatch(principal, task_id, claim_token, state=state, job_ref=job_ref) as (_, receipt):
            return receipt

    @contextmanager
    def local_dispatch(
        self,
        principal: TaskPrincipal,
        task_id: str,
        claim_token: str,
        *,
        state: Literal["intent", "submitting", "submitted"],
        job_ref: str | None = None,
        write: bool = False,
    ) -> Iterator[tuple[TransactionStore, dict[str, Any]]]:
        """Hold the dispatch fence through a local registry transition."""
        with transaction(self.path, write=write) as store:
            task = _scoped_task(store, principal, task_id)
            _require_open(store, task.project_id)
            now = self.clock()
            dispatch = _fenced_dispatch(task, principal, claim_token, now)
            receipt = dispatch["receipt"]
            if dispatch["state"] != state or (job_ref is not None and dispatch.get("job_ref") != job_ref):
                raise TaskServiceError("stale", "The dispatch no longer belongs to this local launch")
            # Placement evidence admits the launch. Once the job is submitted, its expiry
            # no longer matters: the evidence must stay unchanged, and the claim
            # lease must still be current.
            if state == "submitted":
                placement, _ = _check_placement(task, "local", now, check_expiry=False)
            else:
                placement, _ = _placement(task, now)
            if (
                placement is None
                or placement.placement != "local"
                or receipt["effective_placement"] != "local"
                or placement.reference != receipt["preflight_ref"]
                or placement.engine_family != receipt["engine_family"]
                or _dependencies(task) != receipt["dependencies"]
                or task.data.get("start_hold")
            ):
                raise TaskServiceError("stale", "Local placement evidence or dispatch prerequisites changed")
            yield (
                store,
                {
                    **{key: value for key, value in receipt.items() if key != "claim_token"},
                    **{key: task.data.get(key) for key in ("repo", "assigned_branch", "review_of")},
                },
            )

    def record_submit(self, principal: TaskPrincipal, task_id: str, claim_token: str, job_ref: str) -> None:
        """Persist the provider's acknowledgement, including a late acknowledgement."""
        if not job_ref.strip():
            raise TaskServiceError("invalid", "Provider job reference is required")
        with transaction(self.path, write=True) as store:
            task = _scoped_task(store, principal, task_id)
            now = self.clock()
            dispatch = _fenced_dispatch(task, principal, claim_token, now, allow_expired=True)
            if dispatch["state"] == "submitted" and dispatch["job_ref"] == job_ref:
                return
            if dispatch.get("job_ref") not in (None, job_ref):
                raise TaskServiceError("conflict", "Acknowledgement conflicts with the known provider job")
            if dispatch["state"] not in ("submitting", "ambiguous"):
                raise TaskServiceError("conflict", "Acknowledgement conflicts with recorded dispatch")
            dispatch.update(state="submitted", job_ref=job_ref)
            task.save(store, now)

    def dispatch_status(self, principal: TaskPrincipal, task_id: str) -> dict[str, Any]:
        """Return the provider lookup key and state to the trusted reconciler."""
        principal.require_role("orchestrator")
        with transaction(self.path) as store:
            dispatch = _active_dispatch(_scoped_task(store, principal, task_id))
            receipt = dispatch["receipt"]
            return {
                "task_id": task_id,
                "attempt": receipt["attempt"],
                "idempotency_key": receipt["idempotency_key"],
                "lease_expires_at": receipt["lease_expires_at"],
                "state": dispatch["state"],
                "job_ref": dispatch.get("job_ref"),
            }

    def fail_local_launch(
        self, principal: TaskPrincipal, task_id: str, claim_token: str, error: str,
        *, dispatcher_owner: DispatcherOwner | None = None, failure_class: FailureClass | None = None,
        continuation: AttemptContinuation | None = None,
    ) -> None:
        """Settle a local or cloud dispatch after the reconciler rules out a submitted job."""
        principal.require_role("orchestrator")
        if not error.strip():
            raise TaskServiceError("invalid", "Launch failure evidence is required")
        with transaction(self.path, write=True) as store:
            task = _scoped_task(store, principal, task_id)
            _require_open(store, task.project_id)
            dispatch = _active_dispatch(task)
            receipt = dispatch["receipt"]
            if receipt["claim_token"] != claim_token:
                raise TaskServiceError("stale", "Claim token has been superseded")
            _require_dispatcher_owner(receipt, dispatcher_owner)
            if (
                receipt["effective_placement"] not in ("local", "cloud")
                or dispatch["state"] not in ("intent", "submitting")
                or dispatch.get("job_ref")
            ):
                raise TaskServiceError("conflict", "Only an unsubmitted launch can be settled as failed")
            if dispatch.get("unresolved_effects"):
                raise TaskServiceError("unresolved", "Reconcile outstanding effects before releasing the claim")
            now = self.clock()
            dispatch.update(state="absent", launch_error=error.strip(), reconciled_at=now)
            _release(task)
            task.data["queue_released_at"] = now
            if failure_class:
                dispatch["failure_class"] = failure_class
            if continuation is not None:
                _queue_continuation(task, continuation, dispatch["launch_error"])
            else:
                task.data.update(status="failed", completed_at=now, error=dispatch["launch_error"])
            task.save(store, now)

    def fail_dispatch_launch(
        self, principal: TaskPrincipal, task_id: str, claim_token: str, error: str,
        *, dispatcher_owner: DispatcherOwner | None = None,
    ) -> None:
        """Release an intent that the dispatcher deliberately did not submit."""
        principal.require_role("orchestrator")
        if not error.strip():
            raise TaskServiceError("invalid", "Launch failure evidence is required")
        with transaction(self.path, write=True) as store:
            task = _scoped_task(store, principal, task_id)
            dispatch = _active_dispatch(task)
            if dispatch["receipt"]["claim_token"] != claim_token or dispatch["state"] != "intent":
                raise TaskServiceError("stale", "Only the current unsubmitted dispatch can be released")
            _require_dispatcher_owner(dispatch["receipt"], dispatcher_owner)
            now = self.clock()
            dispatch.update(state="absent", launch_error=error.strip(), reconciled_at=now)
            _release(task)
            task.data["queue_released_at"] = now
            task.save(store, now)

    def reconcile_dispatch(
        self,
        principal: TaskPrincipal,
        task_id: str,
        attempt: int,
        observation: Literal["absent", "submitted", "ambiguous", "terminated"],
        *,
        evidence_ref: str | None = None,
        job_ref: str | None = None,
        failure_reason: str | None = None,
        dispatcher_owner: DispatcherOwner | None = None,
        failure_class: FailureClass | None = None,
        continuation: AttemptContinuation | None = None,
    ) -> None:
        """Record trusted provider evidence and queue a continuation or settle the claim."""
        principal.require_role("orchestrator")
        with transaction(self.path, write=True) as store:
            task = _scoped_task(store, principal, task_id)
            dispatch = _active_dispatch(task)
            receipt = dispatch["receipt"]
            if receipt["attempt"] != attempt:
                raise TaskServiceError("stale", "Dispatch attempt has been superseded")
            _require_dispatcher_owner(receipt, dispatcher_owner)
            now = self.clock()
            if observation not in ("absent", "submitted", "ambiguous", "terminated"):
                raise TaskServiceError("invalid", "Unknown provider observation")
            if (dispatch["state"] == "intent" and observation != "absent"
                    and not (dispatcher_owner is not None and observation == "submitted"
                             and receipt["effective_placement"] == "local")):
                raise TaskServiceError("conflict", "Dispatch has not reached the provider boundary")
            if dispatch["state"] != "intent" and not (evidence_ref or "").strip():
                raise TaskServiceError("invalid", "Provider reconciliation evidence is required")
            known_job = dispatch.get("job_ref")
            # A cloud acknowledgement can refer to a bridge record that never got a provider handle.
            missing_cloud_submission = receipt["effective_placement"] == "cloud" and job_ref == known_job
            if known_job and ((observation == "absent" and not missing_cloud_submission)
                              or (job_ref and known_job != job_ref)):
                raise TaskServiceError("conflict", "Provider observation conflicts with the recorded job")
            if observation in ("submitted", "terminated") and not (job_ref or "").strip():
                raise TaskServiceError("invalid", "Provider job reference is required")
            if observation in ("absent", "terminated"):
                if dispatcher_owner is None and receipt["lease_expires_at"] > now:
                    raise TaskServiceError("conflict", "The active lease has not expired")
                if dispatch.get("unresolved_effects"):
                    raise TaskServiceError("unresolved", "Reconcile outstanding effects before releasing the claim")
                _release(task)
                task.data["queue_released_at"] = now
                if failure_class:
                    dispatch["failure_class"] = failure_class
                if continuation is not None:
                    # An unsubmitted infra retry can keep its preflight until the original expiry.
                    placement = None
                    if observation == "absent" and failure_class == "infra":
                        placement, _ = _check_placement(task, receipt["effective_placement"], now)
                    _queue_continuation(task, continuation, failure_reason or "dispatch released")
                    if placement is not None and placement.engine_family == receipt["engine_family"]:
                        task.data["placement_evidence"][placement.placement]["revision"] = task.revision
                elif failure_reason:
                    task.data.update(status="failed", completed_at=now, error=failure_reason)
            if job_ref:
                dispatch["job_ref"] = job_ref
            dispatch.update(state=observation, reconciliation_ref=evidence_ref, reconciled_at=now)
            task.save(store, now)

    def defer_dispatch_step(
        self, principal: TaskPrincipal, task_id: str, attempt: int, retry_at: float, *,
        dispatcher_owner: DispatcherOwner | None = None,
    ) -> None:
        """Keep the same submitted attempt while verification waits for a quota window."""
        principal.require_role("orchestrator")
        with transaction(self.path, write=True) as store:
            task = _scoped_task(store, principal, task_id)
            dispatch = _active_dispatch(task)
            receipt = dispatch["receipt"]
            _require_dispatcher_owner(receipt, dispatcher_owner)
            if receipt["attempt"] != attempt or dispatch["state"] != "submitted":
                raise TaskServiceError("stale", "Only the current submitted dispatch can wait")
            dispatch.update(retry_at=retry_at, failure_class="budget")
            task.save(store, self.clock())

    def record_completion_evidence(
        self, principal: TaskPrincipal, task_id: str, attempt: int, evidence: CompletionEvidence,
        *, dispatcher_owner: DispatcherOwner | None = None,
    ) -> None:
        """Attach a trusted reconciler's immutable result to the matching provider job."""
        principal.require_role("orchestrator")
        with transaction(self.path, write=True) as store:
            task = _scoped_task(store, principal, task_id)
            dispatch = _active_dispatch(task)
            receipt = dispatch["receipt"]
            if receipt["attempt"] != attempt:
                raise TaskServiceError("stale", "Dispatch attempt has been superseded")
            _require_dispatcher_owner(receipt, dispatcher_owner)
            if (
                dispatch["state"] != "submitted"
                or evidence.job_ref != dispatch["job_ref"]
                or evidence.executor != receipt["executor"]
                or evidence.engine_family != receipt["engine_family"]
            ):
                raise TaskServiceError("stale", "Evidence does not match the submitted job, executor and engine")
            record = evidence.model_dump(mode="json")
            for optional in ("failure_reason", "preserved_branch", "pr_url", "land_failure"):
                if record[optional] is None:
                    record.pop(optional)  # keep existing evidence replays compatible
            records = dispatch.setdefault("evidence", {})
            if evidence.reference in records:
                if records[evidence.reference] != record:
                    raise TaskServiceError("conflict", "Evidence reference already binds a different result")
                return
            records[evidence.reference] = record
            dispatch["current_evidence"] = evidence.reference
            task.save(store, self.clock())

    def complete(
        self,
        principal: TaskPrincipal,
        task_id: str,
        claim_token: str,
        idempotency_key: str,
        result_state: ResultState,
        evidence_ref: str,
        outcome: Outcome | None = None,
        *,
        reconcile_failure: bool = False,
        dispatcher_owner: DispatcherOwner | None = None,
        landing: DispatchLanding | None = None,
    ) -> dict[str, Any]:
        """Complete a claim, or let a trusted reconciler close it with blocked evidence after lease expiry."""
        principal.require_role("worker", "orchestrator")
        if dispatcher_owner is not None or landing is not None:
            principal.require_role("orchestrator")
        if landing is not None and (dispatcher_owner is None or result_state != "accepted"):
            raise TaskServiceError("invalid", "Landing requires an accepted dispatcher claim")
        if reconcile_failure:
            principal.require_role("orchestrator")
            if result_state != "blocked":
                raise TaskServiceError("invalid", "Recovered completion must be blocked")
        if (
            result_state not in ("accepted", "blocked", "rework")
            or not idempotency_key.strip()
            or not evidence_ref.strip()
        ):
            raise TaskServiceError("invalid", "Completion requires a result, idempotency key and evidence reference")
        with transaction(self.path, write=True) as store:
            task = _scoped_task(store, principal, task_id)
            replay_binding = [claim_token, principal.actor, result_state, evidence_ref]
            if outcome is not None:
                replay_binding.append(outcome)
            for previous in task.data.get("dispatches", []):
                completed = previous.get("completion")
                if completed is not None and completed["idempotency_key"] == idempotency_key:
                    if completed["binding"] != replay_binding:
                        raise TaskServiceError("conflict", "Completion idempotency key has already been used")
                    return {"completion": completed["result"]}
            now = self.clock()
            if reconcile_failure:
                dispatch = _active_dispatch(task)
                if dispatch["receipt"]["claim_token"] != claim_token:
                    raise TaskServiceError("stale", "Claim token has been superseded")
            else:
                dispatch = _fenced_dispatch(task, principal, claim_token, now)
            _require_dispatcher_owner(dispatch["receipt"], dispatcher_owner)
            _require_open(store, task.project_id)
            if dispatch.get("unresolved_effects"):
                raise TaskServiceError("unresolved", "Reconcile outstanding effects before completing the task")
            if dispatch["state"] != "submitted" or dispatch.get("current_evidence") != evidence_ref:
                raise TaskServiceError("stale", "Completion needs current evidence for the submitted job")
            evidence = CompletionEvidence.model_validate(dispatch["evidence"][evidence_ref])
            if evidence.state != result_state:
                raise TaskServiceError("conflict", "Result state differs from the trusted evidence")
            effective_outcome = outcome or ("success" if result_state == "accepted" else "task_failure")
            if effective_outcome not in ("success", "task_failure", "infrastructure_failure"):
                raise TaskServiceError("invalid", "Unknown completion outcome")
            if (result_state == "accepted") != (effective_outcome == "success"):
                raise TaskServiceError("invalid", "Outcome is inconsistent with result state")
            if evidence.outcome is not None and evidence.outcome != effective_outcome:
                raise TaskServiceError("conflict", "Outcome differs from the trusted evidence")
            if effective_outcome == "infrastructure_failure" and evidence.outcome != effective_outcome:
                raise TaskServiceError("invalid", "Infrastructure failure requires trusted outcome evidence")
            route = dispatch["receipt"].get("route")
            if effective_outcome == "infrastructure_failure" and not route:
                raise TaskServiceError("invalid", "Infrastructure failure requires repo and environment identity")
            if result_state == "accepted":
                _validate_acceptance(store, task, dispatch, evidence)
            result = {
                "task_id": task_id,
                "revision": task.revision,
                "attempt": dispatch["receipt"]["attempt"],
                "state": result_state,
                "evidence_ref": evidence_ref,
                "artifact_ref": evidence.artifact_ref,
                "verification_id": evidence.verification_id,
                "engine_family": evidence.engine_family,
                "executor": evidence.executor,
                "reviewed_sha": evidence.reviewed_sha,
                "completed_at": now,
                "outcome": effective_outcome,
            }
            if route:
                key = _route_key(*route)
                state = store.read(
                    key, {"route": route, "consecutive_failures": 0, "quarantined": False, "history": []}
                )
                if effective_outcome == "infrastructure_failure":
                    state["consecutive_failures"] += 1
                    if state["consecutive_failures"] >= self.quarantine_threshold and not state["quarantined"]:
                        state.update(
                            quarantined=True,
                            quarantined_at=now,
                            failed_preflight_ref=dispatch["receipt"]["preflight_ref"],
                        )
                elif not state["quarantined"]:
                    state["consecutive_failures"] = 0
                store.write(key, state)
            dispatch.update(
                state="completed",
                completion={"idempotency_key": idempotency_key, "binding": replay_binding, "result": result},
            )
            if evidence.failure_class:
                dispatch["failure_class"] = evidence.failure_class
            if landing is not None:
                dispatch["pending_landing"] = landing.model_dump(mode="json")
            _release(task)
            task.data["queue_released_at"] = now
            if result_state == "accepted":
                task.data.update(status="done", completed_at=now, result=evidence_ref, error="")
                touch_project_progress(store, task.project_id)
            elif result_state == "blocked" and evidence.continuation is not None:
                _queue_continuation(task, evidence.continuation, evidence.failure_reason or evidence_ref)
            elif result_state == "blocked":
                task.data.update(status="failed", completed_at=now, error=evidence.failure_reason or evidence_ref)
            else:
                task.data.update(completed_at=None, error=evidence_ref)
            task.save(store, now)
            return {"completion": result}

    def route_clear(self, principal: TaskPrincipal, task_id: str, reason: str) -> dict[str, Any]:
        """Clear a route only with this task's fresh, green recorded preflight."""
        principal.require_role("orchestrator", "owner")
        if not reason.strip():
            raise TaskServiceError("invalid", "A reason is required")
        with transaction(self.path, write=True) as store:
            task = _scoped_task(store, principal, task_id)
            now = self.clock()
            placement, _ = _placement(task, now)
            route = _route(task, placement) if placement else None
            if route is None:
                raise TaskServiceError("invalid", "A green route preflight with repo and environment is required")
            if placement is None:
                raise TaskServiceError("invalid", "A green route preflight is required")
            record = task.data["placement_evidence"][placement.placement]
            key = _route_key(*route)
            state = store.read(key)
            if not state or not state.get("quarantined"):
                raise TaskServiceError("conflict", "Route is not quarantined")
            if (
                not record.get("fresh_for_route")
                or record.get("recorded_at", 0) <= state["quarantined_at"]
                or placement.reference == state.get("failed_preflight_ref")
            ):
                raise TaskServiceError("stale", "A fresh green preflight recorded after quarantine is required")
            event = {
                "actor": principal.actor,
                "reason": reason.strip(),
                "at": now,
                "preflight_ref": placement.reference,
                "task_id": task_id,
            }
            state.update(quarantined=False, consecutive_failures=0, history=[*state.get("history", []), event])
            store.write(key, state)
            return {"route": route, "clearance": event}


def _validate_acceptance(
    store: TransactionStore, task: BoardTask, dispatch: dict[str, Any], evidence: CompletionEvidence
) -> None:
    dependencies = _dependencies(task)
    if dependencies != dispatch["receipt"]["dependencies"]:
        raise TaskServiceError("stale", "Dependency acceptance changed during the run")
    contract = find_contract_for_task(store, task.project_id, task.index)
    marker = task.data.get("contract_id")
    if marker and (contract is None or contract.contract_id != marker):
        raise TaskServiceError("unavailable", "The task's acceptance contract is unavailable")
    review_of = task.data.get("review_of")
    code_task = task.data.get("repo") or contract is not None or review_of
    if not evidence.artifact_ref or (code_task and not _COMMIT_SHA.fullmatch(evidence.artifact_ref)):
        raise TaskServiceError("invalid", "Accepted completion requires an immutable artifact reference")
    if contract is not None:
        verification = get_verification(store, evidence.verification_id or "")
        if (
            verification is None
            or verification.verification_id != evidence.verification_id
            or verification.status != "accepted"
            or verification.job_id != evidence.job_ref
            or verification.contract_id != contract.contract_id
            or get_verification_for_contract(store, contract.contract_id) != verification
        ):
            raise TaskServiceError("stale", "The job needs its latest accepted contract verification")
    if review_of:
        writer = next(dep for dep in dependencies if dep["task_id"] == f"{task.project_id}/{review_of}")
        writer_sha = writer.get("artifact_ref")
        # An explicit target can be reviewed independently. Publication still
        # requires the latest review to match the writer's accepted artifact.
        target = dispatch["receipt"].get("review_target") or writer_sha
        if (
            writer_sha is None
            or evidence.reviewed_sha != target
            or evidence.artifact_ref != target
            or not _review_independence(writer.get("engine_family") or "", evidence, writer.get("executor") or "")
            or not evidence.review_passed
        ):
            raise TaskServiceError(
                "invalid", "Review must pass on the exact commit with the opposite engine or a fresh reviewer"
            )
