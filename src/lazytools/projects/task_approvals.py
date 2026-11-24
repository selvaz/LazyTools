"""Task scoping and transaction binding for LazyBridge's ApprovalQueue."""

from __future__ import annotations

import hashlib
import sys
from datetime import UTC, datetime
from typing import Any

from lazybridge.ext.approval import ApprovalQueue

from lazytools.projects.task_service import (
    BoardTask,
    ProjectTaskService,
    TaskPrincipal,
    _active_dispatch,
    _load_task,
    _validate_limit,
)
from lazytools.projects.task_store import TaskServiceError, TransactionStore, transaction

_REGISTRY = "projects-task"


def _fence(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _binding(ticket: Any) -> dict[str, Any] | None:
    request = ticket.request
    if request is None or request.payload.get("registry") != _REGISTRY:
        return None
    return request.payload


def _visible(principal: TaskPrincipal, ticket: Any) -> bool:
    binding = _binding(ticket)
    if binding is None or ticket.task_id.partition("/")[0] not in principal.projects:
        return False
    if principal.role == "worker":
        return principal.actor == ticket.request.requested_by
    return principal.role == binding["required_approver"] or principal.actor == ticket.request.requested_by


def _view(ticket: Any) -> dict[str, Any]:
    request = ticket.request
    binding = request.payload
    return {
        "request_id": ticket.approval_id,
        "task_id": ticket.task_id,
        "project_id": ticket.task_id.partition("/")[0],
        "task_revision": binding["task_revision"],
        "attempt": request.attempt,
        "invocation_id": binding["invocation_id"],
        "action": request.action,
        "action_digest": request.digest,
        "target": request.target.model_dump(mode="json"),
        "requester": request.requested_by,
        "intended_executor": request.requested_by,
        "required_approver": binding["required_approver"],
        "risk": binding["risk"],
        "policy_revision": request.policy_revision,
        "rule_id": binding["rule_id"],
        "created_at": ticket.created_at.isoformat(),
        "expires_at": ticket.expires_at.isoformat(),
        "status": ticket.status,
        "decision": ticket.decision.model_dump(mode="json") if ticket.decision else None,
        "consumed_at": ticket.consumed_at.isoformat() if ticket.consumed_at else None,
        "effect_outcome": ticket.effect_outcome.model_dump(mode="json") if ticket.effect_outcome else None,
    }


def _active_binding(
    store: TransactionStore, ticket: Any, now: float
) -> tuple[BoardTask, dict[str, Any]]:
    binding = _binding(ticket)
    if binding is None:
        raise TaskServiceError("not_found", "Unknown governed approval request")
    task = _load_task(store, ticket.task_id)
    dispatch = _active_dispatch(task)
    receipt = dispatch["receipt"]
    if (
        receipt["lease_expires_at"] <= now
        or ticket.request.attempt != receipt["attempt"]
        or ticket.request.requested_by != receipt["executor"]
        or binding["task_revision"] != task.revision
        or binding["claim_fence"] != _fence(receipt["claim_token"])
        or dispatch.get("approval_requests", {}).get(binding["invocation_id"]) != ticket.approval_id
    ):
        raise TaskServiceError("stale", "Approval no longer matches an active claim")
    return task, dispatch


class ProjectTaskApprovals:
    def __init__(self, service: ProjectTaskService):
        self.service = service

    def _queue(self, store: TransactionStore) -> ApprovalQueue:
        if not hasattr(ApprovalQueue, "create_typed_ticket"):
            raise TaskServiceError("unavailable", "This LazyBridge version lacks the typed approval registry")
        return ApprovalQueue(store, clock=lambda: datetime.fromtimestamp(self.service.clock(), UTC))

    def pending(
        self, principal: TaskPrincipal, project_id: str | None = None, task_id: str | None = None,
        limit: int = 20, cursor: str | None = None,
    ) -> dict[str, Any]:
        _validate_limit(limit)
        if project_id is not None:
            principal.require_project(project_id)
        if task_id is not None:
            task_project = task_id.partition("/")[0]
            principal.require_project(task_project)
            if project_id is not None and task_project != project_id:
                raise TaskServiceError("invalid", "Task and project scopes differ")
        with transaction(self.service.path) as store:
            now = self.service.clock()
            tickets = self._queue(store).list_pending_tickets(limit=sys.maxsize)
            items = []
            next_cursor = None
            for ticket in sorted(tickets, key=lambda entry: entry.approval_id):
                if cursor is not None and ticket.approval_id <= cursor:
                    continue
                if project_id is not None and ticket.task_id.partition("/")[0] != project_id:
                    continue
                if task_id is not None and ticket.task_id != task_id:
                    continue
                if not _visible(principal, ticket) or ticket.expires_at.timestamp() <= now:
                    continue
                try:
                    _active_binding(store, ticket, now)
                except TaskServiceError:
                    continue
                if len(items) == limit:
                    next_cursor = items[-1]["request_id"]
                    break
                items.append(_view(ticket))
            return {"requests": items, "next_cursor": next_cursor}

    def get(self, principal: TaskPrincipal, request_id: str) -> dict[str, Any]:
        """Read the canonical decision and effect history from an executor adapter."""
        with transaction(self.service.path) as store:
            ticket = self._queue(store).get_ticket(request_id)
            if ticket is None or not _visible(principal, ticket):
                raise TaskServiceError("not_found", "Unknown governed approval request")
            return _view(ticket)
