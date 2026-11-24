"""MCP task tools bound to the identity of a dedicated invocation host."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from lazybridge import Tool

from lazytools.projects.task_service import ProjectTaskService, TaskPrincipal
from lazytools.projects.task_store import TaskServiceError


class TaskTools:
    def __init__(self, store_db: str | None, principal: TaskPrincipal, *, allow_write: bool):
        self.service = ProjectTaskService(store_db)
        self.principal = principal
        self.allow_write = allow_write

    def _call(self, operation: Callable[..., dict[str, Any]], *args: Any) -> dict[str, Any]:
        try:
            return {"ok": True, **operation(self.principal, *args)}
        except TaskServiceError as exc:
            return {"ok": False, "error": {"code": exc.code, "message": str(exc)}}

    def tasks_next_ready(self, project_id: str, limit: int = 20, cursor: int | None = None) -> dict[str, Any]:
        """List ready tasks in the authenticated project scope, in stable plan order."""
        return self._call(self.service.next_ready, project_id, limit, cursor)

    def tasks_claim(self, task_id: str, expected_revision: str, idempotency_key: str) -> dict[str, Any]:
        """Reserve ready work and durably record its dispatch intent."""
        if not self.allow_write:
            return {"ok": False, "error": {"code": "forbidden", "message": "This host is read-only"}}
        return self._call(self.service.claim, task_id, expected_revision, idempotency_key)

    def as_tools(self) -> list[Tool]:
        methods = [self.tasks_next_ready]
        if self.allow_write and self.principal.role in ("worker", "orchestrator"):
            methods.append(self.tasks_claim)
        return [Tool.wrap(method, name=method.__name__, strict=True) for method in methods]
