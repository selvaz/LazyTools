"""The governed queue over the existing projects board and acceptance records."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, FiniteFloat, StringConstraints
from typing_extensions import Annotated

from lazytools.projects.plan_edit import _board_key
from lazytools.projects.records import get_project
from lazytools.projects.task_fields import Placement, task_id
from lazytools.projects.task_store import TaskServiceError, TransactionStore, transaction

Nonempty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


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


def _load_task(store: TransactionStore, identifier: str) -> BoardTask:
    project_id, separator, identifier_in_project = identifier.partition("/")
    board = store.read(_board_key(project_id)) if separator and identifier_in_project else None
    if board is not None:
        for index, task in enumerate(board["tasks"]):
            if task_id(task, index) == identifier_in_project:
                return BoardTask(project_id, board, index)
    raise TaskServiceError("not_found", "Task does not exist")


def _dependencies(task: BoardTask) -> list[dict[str, Any]]:
    by_id = {task_id(item, index): item for index, item in enumerate(task.board["tasks"])}
    return [
        {"task_id": f"{task.project_id}/{dependency}", "status": by_id.get(dependency, {}).get("status", "missing")}
        for dependency in task.data.get("dependency_ids", [])
    ]


def _placement(task: BoardTask, now: float) -> tuple[PlacementEvidence | None, str]:
    requested = task.data.get("requested_placement", "local")
    evidence = task.data.get("placement_evidence", {})

    def check(location: Placement) -> tuple[PlacementEvidence | None, str]:
        record = evidence.get(location)
        if record is None or record["revision"] != task.revision:
            return None, f"{location} placement evidence is missing"
        current = PlacementEvidence.model_validate(record["evidence"])
        if current.expires_at <= now:
            return None, f"{location} placement evidence is stale"
        return current if current.eligible else None, current.reason

    eligible, reason = check(requested)
    if eligible is None and requested == "cloud":
        eligible, local_reason = check("local")
        reason = f"{reason}. Local fallback: {local_reason}"
    return eligible, reason


def _ready(task: BoardTask, now: float) -> tuple[dict[str, Any], PlacementEvidence] | None:
    data = task.data
    if data.get("status") != "todo" or data.get("owner") or data.get("claimed_at") is not None or data.get("start_hold"):
        return None
    dependencies = _dependencies(task)
    if any(dep["status"] != "done" for dep in dependencies):
        return None
    placement, reason = _placement(task, now)
    if placement is None:
        return None
    return {
        "task_id": task.identifier,
        "project_id": task.project_id,
        "revision": task.revision,
        "text": data["text"],
        "tier": data.get("tier"),
        "dependencies": dependencies,
        "acceptance_criteria": data.get("acceptance_criteria", []),
        "requested_profile": data.get("requested_profile"),
        "requested_network": data.get("requested_network"),
        "requested_placement": data.get("requested_placement", "local"),
        "effective_placement": placement.placement,
        "placement_reason": reason,
        "preflight_ref": placement.reference,
    }, placement


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


def _release(task: BoardTask) -> None:
    task.data.update(active_queue_claim=False, status="todo", owner=None, claimed_at=None)


class ProjectTaskService:
    def __init__(self, store_db: str | None, *, lease_seconds: int = 900):
        self.path = Path(store_db).resolve() if store_db and store_db != ":memory:" else None
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.lease_seconds = lease_seconds

    def record_placement(self, task_id: str, revision: str, evidence: PlacementEvidence) -> None:
        """Persist an executor's preflight/runtime observation. Host operation."""
        with transaction(self.path, write=True) as store:
            task = _load_task(store, task_id)
            if task.revision != revision:
                raise TaskServiceError("stale", "Task revision changed")
            task.data.setdefault("placement_evidence", {})[evidence.placement] = {
                "revision": revision,
                "evidence": evidence.model_dump(mode="json"),
            }
            task.save(store, time.time())

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
            now = time.time()
            items = []
            next_cursor = None
            for index in range(0 if cursor is None else cursor + 1, len(board["tasks"])):
                ready = _ready(BoardTask(project_id, board, index), now)
                if ready is not None:
                    if len(items) == limit:
                        next_cursor = last_index
                        break
                    items.append(ready[0])
                    last_index = index
            return {"tasks": items, "next_cursor": next_cursor}

    def claim(
        self, principal: TaskPrincipal, task_id: str, expected_revision: str, idempotency_key: str
    ) -> dict[str, Any]:
        principal.require_role("worker", "orchestrator")
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
                    return {"claim": receipt}
            if task.revision != expected_revision:
                raise TaskServiceError("stale", "Task revision changed")
            _require_open(store, task.project_id)
            now = time.time()
            ready = _ready(task, now)
            if ready is None:
                raise TaskServiceError("conflict", "Task is no longer ready")
            summary, placement = ready
            attempt = max(task.data.get("attempts", 0), history[-1]["receipt"]["attempt"] if history else 0) + 1
            receipt = {
                **summary,
                "claim_token": uuid4().hex,
                "executor": principal.actor,
                "idempotency_key": idempotency_key,
                "lease_expires_at": now + self.lease_seconds,
                "attempt": attempt,
                "engine_family": placement.engine_family,
            }
            # A null claimed_at keeps the legacy board's lease timer from
            # independently recycling this claim. This receipt owns the lease.
            task.data.update(
                status="claimed", owner=principal.actor, claimed_at=None, attempts=attempt,
                active_queue_claim=True, effective_placement=placement.placement,
                placement_reason=summary["placement_reason"], preflight_ref=placement.reference,
                dispatches=[*history, {"receipt": receipt, "state": "intent", "created_at": now}],
            )
            task.save(store, now)
            return {"claim": receipt}

    def begin_submit(self, principal: TaskPrincipal, task_id: str, claim_token: str) -> None:
        """Reserve the provider call. The adapter may call submit only after this succeeds."""
        with transaction(self.path, write=True) as store:
            task = _scoped_task(store, principal, task_id)
            _require_open(store, task.project_id)
            now = time.time()
            dispatch = _fenced_dispatch(task, principal, claim_token, now)
            if dispatch["state"] != "intent":
                raise TaskServiceError("conflict", "Dispatch already started and must be reconciled")
            placement, _ = _placement(task, now)
            receipt = dispatch["receipt"]
            if (
                placement is None or placement.placement != receipt["effective_placement"]
                or placement.engine_family != receipt["engine_family"] or placement.reference != receipt["preflight_ref"]
                or any(dep["status"] != "done" for dep in _dependencies(task))
                or task.data.get("start_hold")
            ):
                raise TaskServiceError("stale", "Dispatch prerequisites changed after the claim")
            dispatch.update(state="submitting", submit_started_at=now)
            task.save(store, now)

    def record_submit(self, principal: TaskPrincipal, task_id: str, claim_token: str, job_ref: str) -> None:
        """Persist the provider's acknowledgement, including a late acknowledgement."""
        if not job_ref.strip():
            raise TaskServiceError("invalid", "Provider job reference is required")
        with transaction(self.path, write=True) as store:
            task = _scoped_task(store, principal, task_id)
            now = time.time()
            dispatch = _fenced_dispatch(task, principal, claim_token, now, allow_expired=True)
            if dispatch["state"] == "submitted" and dispatch["job_ref"] == job_ref:
                return
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

    def reconcile_dispatch(
        self, principal: TaskPrincipal, task_id: str, attempt: int,
        observation: Literal["absent", "submitted", "ambiguous", "terminated"],
        *, evidence_ref: str | None = None, job_ref: str | None = None,
    ) -> None:
        """Record trusted provider evidence. Release only after conclusively ruling out live work."""
        principal.require_role("orchestrator")
        with transaction(self.path, write=True) as store:
            task = _scoped_task(store, principal, task_id)
            dispatch = _active_dispatch(task)
            receipt = dispatch["receipt"]
            if receipt["attempt"] != attempt:
                raise TaskServiceError("stale", "Dispatch attempt has been superseded")
            now = time.time()
            if observation not in ("absent", "submitted", "ambiguous", "terminated"):
                raise TaskServiceError("invalid", "Unknown provider observation")
            if dispatch["state"] == "intent" and observation != "absent":
                raise TaskServiceError("conflict", "Dispatch has not reached the provider boundary")
            if dispatch["state"] != "intent" and not (evidence_ref or "").strip():
                raise TaskServiceError("invalid", "Provider reconciliation evidence is required")
            known_job = dispatch.get("job_ref")
            if observation == "absent" and known_job or known_job and job_ref and known_job != job_ref:
                raise TaskServiceError("conflict", "Provider observation conflicts with the recorded job")
            if observation in ("submitted", "terminated") and not (job_ref or "").strip():
                raise TaskServiceError("invalid", "Provider job reference is required")
            if observation in ("absent", "terminated"):
                if receipt["lease_expires_at"] > now:
                    raise TaskServiceError("conflict", "The active lease has not expired")
                if dispatch.get("unresolved_effects"):
                    raise TaskServiceError("unresolved", "Reconcile outstanding effects before releasing the claim")
                _release(task)
            if job_ref:
                dispatch["job_ref"] = job_ref
            dispatch.update(state=observation, reconciliation_ref=evidence_ref, reconciled_at=now)
            task.save(store, now)
