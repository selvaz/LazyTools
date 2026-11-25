"""MCP task tools bound to the identity of a dedicated invocation host."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from lazybridge import Tool

from lazytools.projects.task_approvals import Decision, ProjectTaskApprovals
from lazytools.projects.task_service import Outcome, ProjectTaskService, ResultState, TaskPrincipal
from lazytools.projects.task_store import TaskServiceError


class TaskTools:
    def __init__(
        self, store_db: str | None, principal: TaskPrincipal, *, allow_write: bool, quarantine_threshold: int = 3,
        approver_credential_file: Path | None = None,
    ):
        self.service = ProjectTaskService(store_db, quarantine_threshold=quarantine_threshold)
        self.approvals = ProjectTaskApprovals(self.service)
        self.principal = principal
        self.allow_write = allow_write
        self.approver_credential_file = approver_credential_file

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

    def tasks_queue_metrics(self, project_id: str | None = None) -> dict[str, Any]:
        """Report ready age and blocked and running counts within the authenticated scope."""
        return self._call(self.service.queue_metrics, project_id)

    def tasks_claim(self, task_id: str, expected_revision: str, idempotency_key: str) -> dict[str, Any]:
        """Reserve ready work and durably record its dispatch intent."""
        return self._call(self.service.claim, task_id, expected_revision, idempotency_key, write=True)

    def tasks_complete(
        self, task_id: str, claim_token: str, idempotency_key: str, result_state: ResultState, evidence_ref: str,
        outcome: Outcome | None = None,
    ) -> dict[str, Any]:
        """Complete the active attempt using an independently recorded result reference."""
        return self._call(
            self.service.complete, task_id, claim_token, idempotency_key, result_state, evidence_ref, outcome, write=True
        )

    def tasks_route_clear(self, task_id: str, reason: str) -> dict[str, Any]:
        """Clear the task's quarantined route using its fresh green preflight."""
        return self._call(self.service.route_clear, task_id, reason, write=True)

    def approvals_pending(
        self, project_id: str | None = None, task_id: str | None = None,
        limit: int = 20, cursor: str | None = None,
    ) -> dict[str, Any]:
        """List pending approval requests visible to the authenticated principal."""
        return self._call(self.approvals.pending, project_id, task_id, limit, cursor)

    def approvals_decide(
        self, request_id: str, expected_action_digest: str, decision: Decision, reason: str,
    ) -> dict[str, Any]:
        """Decide the exact action using the authenticated approver's project scope."""
        def decide(principal: TaskPrincipal) -> dict[str, Any]:
            principal.require_role("orchestrator", "owner")
            credential = None
            if self.approver_credential_file is not None:
                try:
                    credential = self.approver_credential_file.read_text(encoding="ascii").strip()
                except (OSError, UnicodeError) as exc:
                    raise TaskServiceError("unavailable", "The configured approver credential is unreadable") from exc
            return self.approvals.decide(
                principal, request_id, expected_action_digest, decision, reason, credential=credential,
            )

        return self._call(decide, write=True)

    def as_tools(self) -> list[Tool]:
        methods = [self.tasks_next_ready, self.tasks_queue_metrics, self.approvals_pending]
        if self.allow_write and self.principal.role in ("worker", "orchestrator"):
            methods.extend((self.tasks_claim, self.tasks_complete))
        if self.allow_write and self.principal.role in ("orchestrator", "owner"):
            methods.extend((self.approvals_decide, self.tasks_route_clear))
        return [Tool.wrap(method, name=method.__name__, strict=True) for method in methods]
