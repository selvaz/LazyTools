"""Registry selection and lifecycle for native bridge approvals."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from lazybridge import Store
from lazybridge.engines.coding import ApprovalRequest
from lazybridge.ext.approval import (
    DEFAULT_PREFIX,
    ApprovalQueue,
    ApprovalTarget,
    ApprovalTicket,
    CardApproval,
    ExecutorContext,
    TypedApprovalRequest,
    redact_approval_payload,
)
from lazybridge.policy import policy_scope

from lazytools.code_bridge import _coder, _credentials, _store
from lazytools.connectors.projects.tools import _resolve_store_db

_SOURCE = "code-bridge"
_CARD_AUDIT = "code-bridge:card-request:"
_request: ContextVar[ApprovalRequest] = ContextVar("bridge_approval_request")


def isolated_path(db_path: Path | None, registry_db: Path | None) -> Path | None:
    """Resolve a registry override or derive a sibling for an explicit custom job store."""
    if registry_db is not None:
        return registry_db.expanduser().resolve()
    if db_path is not None:
        path = db_path.expanduser().resolve()
        if path != _store.default_db_path().resolve():
            return path.with_name(f"{path.stem}.registry.sqlite")
    return None


def _registry_path(projects_db: str | None = None) -> Path | None:
    configured = _resolve_store_db(projects_db)
    if configured == ":memory:":
        return None
    return Path(configured or Path.home() / ".lazytools" / "projects.sqlite").expanduser().resolve()


def registry_store(registry_db: Path | None = None, *, projects_db: str | None = None) -> Store:
    path = registry_db if registry_db is not None else _registry_path(projects_db)
    if path is None:
        raise ValueError("The bridge approval registry must be persistent")
    path.parent.mkdir(parents=True, exist_ok=True)
    return Store(db=str(path))


def is_bridge_ticket(ticket: ApprovalTicket) -> bool:
    return ticket.request is not None and ticket.request.payload.get("registry") == _SOURCE


def _operator_ticket(ticket: ApprovalTicket) -> bool:
    return ticket.request is not None and ticket.request.payload.get("registry") in {_SOURCE, "mcp-code"}


def job_ref(ticket: ApprovalTicket) -> str:
    if ticket.request is not None:
        return str(ticket.request.payload.get("job_ref") or "")
    return ticket.task_id


def recorded_path(job: dict[str, Any]) -> Path | None:
    path = job.get("registry_db")
    return Path(path) if isinstance(path, str) and path else None


class Registry:
    def __init__(self, job_store: Any, registry_db: Path | None = None) -> None:
        self._store: Store | None = None
        self.path = registry_db if registry_db is not None else _registry_path()
        # A worker started by an older bridge can still be waiting here.
        self.legacy = _store.build_approval_queue(job_store)
        self.job_store = job_store

    @classmethod
    def for_job(cls, store: Any, job_id: str, registry_db: Path | None = None) -> Registry:
        return cls(store, recorded_path(_store.read_meta(store, job_id) or {}) or registry_db)

    @property
    def queue(self) -> ApprovalQueue:
        """Open the registry for a worker filing or consuming its own tickets."""
        if self._store is None:
            if self.path is None:
                raise ValueError("The bridge approval registry must be persistent")
            self._store = registry_store(self.path)
        return ApprovalQueue(self._store, action_tiers=_coder.TIERS)

    def _existing_store(self) -> Store | None:
        if self._store is None and self.path is not None and self.path.is_file():
            self._store = Store(db=str(self.path))
        return self._store

    def pending(self) -> list[ApprovalTicket]:
        store = self._existing_store()
        current = [] if store is None else [
            t for t in ApprovalQueue(store).list_pending_tickets(limit=sys.maxsize) if _operator_ticket(t)
        ]
        return sorted([*current, *self.legacy.list_pending_tickets()], key=lambda t: t.created_at)

    def inspect(self, approval_id: str) -> ApprovalTicket | None:
        store = self._existing_store()
        ticket = ApprovalQueue(store).get_ticket(approval_id) if store is not None else None
        if ticket is not None and _operator_ticket(ticket):
            return ticket
        return self.legacy.get_ticket(approval_id)

    def decide(
        self, ticket: ApprovalTicket, verdict: Literal["approved", "rejected"], actor: str, reason: str | None,
    ) -> bool:
        if _operator_ticket(ticket):
            store = self._existing_store()
            if store is None or self.path is None or ticket.status != "pending":
                return False
            queue = ApprovalQueue(store)
            credential = _credentials.load(self.path)
            approver = queue._authenticated_approver(credential)
            if approver is None or approver.actor != "owner" or "owner" not in approver.roles:
                raise ValueError("Invalid owner approver credential for this registry")
            return queue.decide_typed_ticket(ticket.approval_id, credential=credential, verdict=verdict, reason=reason)
        if verdict == "approved":
            return bool(self.legacy.approve_ticket(ticket.approval_id, actor=actor, channel="cli"))
        return bool(self.legacy.reject_ticket(ticket.approval_id, actor=actor, channel="cli", reason=reason or "rejected"))

    def initialize_credential(self, *, replace: bool = False) -> bool:
        if self.path is None:
            raise ValueError("The bridge approval registry must be persistent")
        with registry_store(self.path) as store:
            return _credentials.initialize(
                self.path, ApprovalQueue(store, allow_credential_issuance=True), replace=replace,
            )

    def copy_coder_hand(
        self, job_id: str, task_id: str, cwd: Path, temp: Path | None, expires: datetime, *, network: str,
    ) -> _coder.Hand | None:
        store = self._existing_store()
        if store is None:
            return None

        def remember(card_id: str) -> None:
            meta = _store.read_meta(self.job_store, job_id) or {}
            assert self.path is not None
            meta["registry_db"] = str(self.path)
            meta["coder_card_ids"] = [*meta.get("coder_card_ids", []), card_id]
            try:
                _store.write_meta(self.job_store, job_id, meta)
            except Exception:
                self.queue.revoke_card(card_id, credential=_credentials.load(self.path))
                raise

        return _coder.copy_for_job(store, self.queue, cwd, temp, task_id, expires, remember, network=network)

    def revoke_coder_hand(self, job_id: str) -> None:
        meta = _store.read_meta(self.job_store, job_id) or {}
        ids = meta.get("coder_card_ids", [])
        if not ids or meta.get("coder_cards_revoked"):
            return
        assert self.path is not None
        credential = _credentials.load(self.path)
        for card_id in ids:
            self.queue.revoke_card(card_id, credential=credential)
        meta["coder_cards_revoked"] = True
        _store.write_meta(self.job_store, job_id, meta)

    def card_audit(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        ids = set(job.get("coder_card_ids", []))
        if not ids:
            return []
        return [
            {**self.job_store.read(_CARD_AUDIT + receipt.request_digest, {}), **receipt.model_dump(mode="json")}
            for receipt in self.queue.list_card_approvals() if receipt.card_id in ids
        ]

    def open_for_job(self, job_id: str) -> list[ApprovalTicket]:
        store = self._existing_store()
        current = [] if store is None else [
            ticket for key, raw in store.items(prefix=DEFAULT_PREFIX)
            if isinstance(raw, dict) and raw.get("status") == "pending"
            and isinstance(raw.get("request"), dict)
            and raw["request"].get("payload", {}).get("job_ref") == job_id
            and (ticket := ApprovalQueue(store).get_ticket(key.removeprefix(DEFAULT_PREFIX))) is not None
            and is_bridge_ticket(ticket) and job_ref(ticket) == job_id
        ]
        return [
            *current,
            *(ApprovalTicket.model_validate(raw) for _, raw in self.job_store.items(prefix=_store.APPROVAL_PREFIX)
              if isinstance(raw, dict) and raw.get("task_id") == job_id and raw.get("status") == "pending"),
        ]

    def close(self, ticket: ApprovalTicket, reason: str) -> None:
        if is_bridge_ticket(ticket):
            self.queue.expire_ticket(ticket.approval_id)
        elif not self.legacy.reject_ticket(ticket.approval_id, actor="bridge-cleanup", channel="cli", reason=reason):
            self.legacy.expire_ticket(ticket.approval_id)


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
    hand: _coder.Hand | None = None
    fenced_queue: Callable[[], AbstractContextManager[ApprovalQueue]] | None = None
    accepting: bool = True
    name: str = "projects-registry"

    @contextmanager
    def bind(self, request: ApprovalRequest) -> Iterator[None]:
        token = _request.set(request)
        try:
            yield
        finally:
            _request.reset(token)

    def _submit(self, request: ApprovalRequest, prompt: str) -> tuple[ApprovalTicket, ExecutorContext] | CardApproval:
        self.validate()
        action = self.hand.workspace.action(request) if self.hand is not None else None
        target = ApprovalTarget(path=request.cwd if action is None else None)
        typed = TypedApprovalRequest.create(
            task_id=self.task_id or self.job_id, attempt=self.attempt, requested_by=self.actor,
            action="native_" + request.kind, target=target, policy_revision=self.policy_revision,
            tier="routine" if action else None, action_class=_coder.ACTION if action else None,
            card_action=action[1] if action else None,
            payload={
                "registry": _SOURCE, "job_ref": self.job_id, "invocation_id": uuid4().hex,
                "policy_digest": self.policy_digest, "provider": request.provider, "tool": request.name,
                "arguments": dict(request.arguments), "rule_id": None,
            },
        )
        if action is not None and self.hand is not None:
            root, card_action = action
            self.registry.job_store.write(_CARD_AUDIT + typed.digest, redact_approval_payload({
                "tool": request.name, "command": request.arguments.get("command"),
                "cwd": request.cwd, "paths": [str(path) for path in card_action.paths],
            }))
            with (
                self.fenced_queue() if self.fenced_queue else nullcontext(self.registry.queue) as queue,
                policy_scope(self.hand.policies[root], approval_actor=self.actor) as requester,
            ):
                assert requester is not None
                decision = queue.submit_typed_request(typed, requester=requester, prompt=prompt, ttl=self.ttl)
            if isinstance(decision, CardApproval):
                return decision
            ticket = decision
        else:
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
        decision = self._submit(_request.get(), prompt)
        if isinstance(decision, CardApproval):
            return True
        ticket, executor = decision
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
