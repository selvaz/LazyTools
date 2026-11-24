"""MCP task tools bound to the identity of a dedicated invocation host."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from lazybridge import Tool

from lazytools.projects.task_approvals import ProjectTaskApprovals
from lazytools.projects.task_service import ProjectTaskService, ResultState, TaskPrincipal
from lazytools.projects.task_store import TaskServiceError


class TaskTools:
    def __init__(self, store_db: str | None, principal: TaskPrincipal, *, allow_write: bool):
        self.service = ProjectTaskService(store_db)
        self.approvals = ProjectTaskApprovals(self.service)
        self.principal = principal
        self.allow_write = allow_write

    def _call(self, operation: Callable[..., dict[str, Any]], *args: Any, write: bool = False) -> dict[str, Any]:
        try:
            if write and not self.allow_write:
                raise TaskServiceError("forbidden", "This host is read-only")
            return {"ok": True, **operation(self.principal, *args)}
        except TaskServiceError as exc:
            return {"ok": False, "error": {"code": exc.code, "message": str(exc)}}

    def tasks_next_ready(self, project_id: str, limit: int = 20, cursor: int | None = None) -> dict[str, Any]:
        """List ready tasks in the authenticated project scope, in stable plan order."""
        return self._call(self.service.next_ready, project_id, limit, cursor)

    def tasks_claim(self, task_id: str, expected_revision: str, idempotency_key: str) -> dict[str, Any]:
        """Reserve ready work and durably record its dispatch intent."""
        return self._call(self.service.claim, task_id, expected_revision, idempotency_key, write=True)

    def tasks_complete(
        self, task_id: str, claim_token: str, idempotency_key: str, result_state: ResultState, evidence_ref: str
    ) -> dict[str, Any]:
        """Complete the active attempt using an independently recorded result reference."""
        return self._call(
            self.service.complete, task_id, claim_token, idempotency_key, result_state, evidence_ref, write=True
        )

    def approvals_pending(
        self, project_id: str | None = None, task_id: str | None = None,
        limit: int = 20, cursor: str | None = None,
    ) -> dict[str, Any]:
        """List pending approval requests visible to the authenticated principal."""
        return self._call(self.approvals.pending, project_id, task_id, limit, cursor)

    def as_tools(self) -> list[Tool]:
        methods = [self.tasks_next_ready, self.approvals_pending]
        if self.allow_write and self.principal.role in ("worker", "orchestrator"):
            methods.extend((self.tasks_claim, self.tasks_complete))
        return [Tool.wrap(method, name=method.__name__, strict=True) for method in methods]
