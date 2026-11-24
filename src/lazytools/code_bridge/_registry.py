"""Native bridge approvals in the shared projects registry."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from lazybridge import Store
from lazybridge.engines.coding import ApprovalDecision, ApprovalRequest
from lazybridge.ext.approval import (
    DEFAULT_PREFIX,
    ApprovalQueue,
    ApprovalTarget,
    ApprovalTicket,
    ApproverContext,
    ExecutorContext,
    TypedApprovalRequest,
)

from lazytools.code_bridge import _store
from lazytools.connectors.projects.tools import _resolve_store_db

_SOURCE = "code-bridge"
_request: ContextVar[ApprovalRequest] = ContextVar("bridge_approval_request")


def registry_store() -> Store:
    configured = _resolve_store_db(None)
    if configured == ":memory:":
        raise ValueError("The bridge approval registry must be persistent")
    path = Path(configured or Path.home() / ".lazytools" / "projects.sqlite").expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return Store(db=str(path))


def is_bridge_ticket(ticket: ApprovalTicket) -> bool:
    return ticket.request is not None and ticket.request.payload.get("registry") == _SOURCE


def job_ref(ticket: ApprovalTicket) -> str:
    if ticket.request is not None:
        return str(ticket.request.payload.get("job_ref") or "")
    return ticket.task_id


class Registry:
    def __init__(self, job_store: Any) -> None:
        self.store = registry_store()
        self.queue = ApprovalQueue(self.store)
        # A worker started by an older bridge can still be waiting here.
        self.legacy = _store.build_approval_queue(job_store)
        self.job_store = job_store

    def pending(self) -> list[ApprovalTicket]:
        current = [t for t in self.queue.list_pending_tickets(limit=sys.maxsize) if is_bridge_ticket(t)]
        return sorted([*current, *self.legacy.list_pending_tickets()], key=lambda t: t.created_at)

    def inspect(self, approval_id: str) -> ApprovalTicket | None:
        ticket = self.queue.get_ticket(approval_id)
        if ticket is not None and is_bridge_ticket(ticket):
            return ticket
        return self.legacy.get_ticket(approval_id)

    def decide(
        self, ticket: ApprovalTicket, verdict: Literal["approved", "rejected"], actor: str, reason: str | None,
    ) -> bool:
        if is_bridge_ticket(ticket):
            return self.queue.decide_typed_ticket(
                ticket.approval_id, approver=ApproverContext(actor=actor), verdict=verdict, reason=reason,
            )
        if verdict == "approved":
            return bool(self.legacy.approve_ticket(ticket.approval_id, actor=actor, channel="cli"))
        return bool(self.legacy.reject_ticket(ticket.approval_id, actor=actor, channel="cli", reason=reason or "rejected"))

    def open_for_job(self, job_id: str) -> list[ApprovalTicket]:
        current = [
            ticket for key, raw in self.store.items(prefix=DEFAULT_PREFIX)
            if isinstance(raw, dict) and raw.get("status") == "pending"
            and isinstance(raw.get("request"), dict)
            and raw["request"].get("payload", {}).get("job_ref") == job_id
            and (ticket := self.queue.get_ticket(key.removeprefix(DEFAULT_PREFIX))) is not None
            and is_bridge_ticket(ticket) and job_ref(ticket) == job_id
        ]
        return [
            *current,
            *(ApprovalTicket.model_validate(raw) for _, raw in self.job_store.items(prefix=_store.APPROVAL_PREFIX)
              if isinstance(raw, dict) and raw.get("task_id") == job_id and raw.get("status") == "pending"),
        ]

    def close(self, ticket: ApprovalTicket, reason: str) -> None:
        if not self.decide(ticket, "rejected", "bridge-cleanup", reason):
            queue = self.queue if is_bridge_ticket(ticket) else self.legacy
            queue.expire_ticket(ticket.approval_id)


@dataclass
class ApprovalChannel:
    registry: Registry
    job_id: str
    policy_revision: str
    policy_digest: str
    ttl: timedelta
    poll: float
    on_waiting: Callable[[], None]
    on_resumed: Callable[[], None]
    validate: Callable[[], None]
    task_id: str | None = None
    actor: str = "local-bridge"
    attempt: int = 1
    fenced_consume: Callable[[str, str, ExecutorContext], bool] | None = None
    accepting: bool = True
    name: str = "projects-registry"

    @contextmanager
    def bind(self, request: ApprovalRequest) -> Iterator[None]:
        token = _request.set(request)
        try:
            yield
        finally:
            _request.reset(token)

    def _file(
        self, request: ApprovalRequest, prompt: str, *, rule_id: str | None = None,
    ) -> tuple[ApprovalTicket, ExecutorContext]:
        self.validate()
        target = ApprovalTarget(path=request.cwd)
        typed = TypedApprovalRequest.create(
            task_id=self.task_id or self.job_id, attempt=self.attempt, requested_by=self.actor,
            action="native_" + request.kind, target=target, policy_revision=self.policy_revision,
            payload={
                "registry": _SOURCE, "job_ref": self.job_id, "invocation_id": uuid4().hex,
                "policy_digest": self.policy_digest, "provider": request.provider, "tool": request.name,
                "arguments": dict(request.arguments), "rule_id": rule_id,
            },
        )
        ticket = self.registry.queue.create_typed_ticket(typed, prompt=prompt, ttl=self.ttl, operator_only=True)
        try:
            meta = _store.read_meta(self.registry.job_store, self.job_id) or {}
            meta["registry_ticket_ids"] = [*meta.get("registry_ticket_ids", []), ticket.approval_id]
            _store.write_meta(self.registry.job_store, self.job_id, meta)
        except Exception:
            self.registry.close(ticket, "Job could not record the approval ID")
            raise
        executor = ExecutorContext(
            actor=self.actor, task_id=typed.task_id, attempt=typed.attempt, target=target,
            policy_revision=self.policy_revision, digest=typed.digest,
        )
        return ticket, executor

    def _consume(self, ticket: ApprovalTicket, executor: ExecutorContext) -> bool:
        if not self.accepting:
            return False
        self.validate()
        if self.fenced_consume is not None:
            return self.fenced_consume(self.job_id, ticket.approval_id, executor)
        return self.registry.queue.consume_typed_ticket(ticket.approval_id, executor=executor)

    async def ask(self, prompt: str) -> bool:
        if not self.accepting:
            return False
        ticket, executor = self._file(_request.get(), prompt)
        try:
            self.on_waiting()
            while self.accepting:
                current = self.registry.queue.get_ticket(ticket.approval_id)
                if current is None:
                    return False
                if current.status == "approved":
                    return self._consume(ticket, executor)
                if current.status != "pending":
                    return False
                if current.expires_at <= datetime.now(UTC):
                    self.registry.queue.expire_ticket(ticket.approval_id)
                    return False
                await asyncio.sleep(self.poll)
            return False
        finally:
            self.registry.close(ticket, "Native approval wait ended")
            self.on_resumed()

    def automatic(self, request: ApprovalRequest, rule_id: str) -> ApprovalDecision:
        if self.accepting:
            ticket, executor = self._file(request, f"[routine-auto:{rule_id}] {request.name}", rule_id=rule_id)
            if self.registry.decide(ticket, "approved", "routine-auto", rule_id) and self._consume(ticket, executor):
                return ApprovalDecision.allow()
        return ApprovalDecision.deny("Routine approval could not be consumed")
