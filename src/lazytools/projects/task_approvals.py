"""Task scoping and transaction binding for LazyBridge's ApprovalQueue."""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

from lazybridge import Store
from lazybridge.ext.approval import (
    DEFAULT_PREFIX,
    ApprovalDecision,
    ApprovalQueue,
    ApprovalTarget,
    ApprovalTicket,
    CardApproval,
    ExecutorContext,
    TypedApprovalRequest,
)
from lazybridge.policy import (
    ApprovalCard,
    ApprovalTier,
    CardAction,
    CardMatcher,
    TaskGrant,
    policy_scope,
    resolve,
)
from pydantic import BaseModel, ConfigDict

from lazytools.projects.task_service import (
    _COMMIT_SHA,
    BoardTask,
    Nonempty,
    ProjectTaskService,
    TaskPrincipal,
    _active_dispatch,
    _fenced_dispatch,
    _load_task,
    _require_open,
    _scoped_task,
    _validate_limit,
)
from lazytools.projects.task_store import TaskServiceError, TransactionStore, transaction

_REGISTRY = "projects-task"
Decision = Literal["allow", "deny"]
EffectResult = Literal["succeeded", "not_applied"]
_SCOPED_REPO_ACTIONS = ("local_commit", "push", "open_pr")
_ACTIONS = ("read", *_SCOPED_REPO_ACTIONS, "scoped_delete", "production_change", "merge_main")
_AUTOMATIC_ACTIONS = ("read", "local_commit")
_CARD_CLASSES: dict[str, ApprovalTier] = {f"projects_{action}": "routine" for action in _AUTOMATIC_ACTIONS}
_CARD_COMMAND = "lazytools-project-effect"
_CARD_SET_PREFIX = "projects-task:automatic-cards:"
_CARD_RECEIPT_PREFIX = "projects-task:card-receipt:"


class EffectAuthorization(BaseModel):
    """Positive evidence from the trusted host's current grant evaluator."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    policy_revision: Nonempty
    rule_id: Nonempty
    risk: Literal["low", "medium", "high"]
    production: bool = False
    verified_head: str | None = None


EffectPolicy = Callable[[TaskPrincipal, Mapping[str, Any], str, "ApprovalTarget"], EffectAuthorization | None]


def _fence(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _governed_request(ticket: ApprovalTicket) -> TypedApprovalRequest | None:
    request = ticket.request
    if request is None or request.payload.get("registry") != _REGISTRY:
        return None
    return request


def _require_request(ticket: ApprovalTicket) -> TypedApprovalRequest:
    request = _governed_request(ticket)
    if request is None:
        raise TaskServiceError("not_found", "Unknown governed approval request")
    return request


def _visible(principal: TaskPrincipal, ticket: ApprovalTicket) -> bool:
    request = _governed_request(ticket)
    if request is None or ticket.task_id.partition("/")[0] not in principal.projects:
        return False
    if principal.role == "worker":
        return principal.actor == request.requested_by
    return principal.role == request.payload["required_approver"] or principal.actor == request.requested_by


def _view(ticket: ApprovalTicket | None) -> dict[str, Any]:
    if ticket is None:
        raise TaskServiceError("unavailable", "The approval record is unavailable")
    request = _require_request(ticket)
    binding = request.payload
    return {
        "request_id": ticket.approval_id,
        "task_id": ticket.task_id,
        "project_id": ticket.task_id.partition("/")[0],
        "task_revision": binding["task_revision"],
        "attempt": request.attempt,
        "job_ref": binding["job_ref"],
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
        "effect_unresolved": ticket.consumed_at is not None and ticket.effect_outcome is None,
    }


def _active_binding(
    store: TransactionStore, ticket: ApprovalTicket, now: float
) -> tuple[BoardTask, dict[str, Any]]:
    request = _require_request(ticket)
    binding = request.payload
    task = _load_task(store, ticket.task_id)
    _require_open(store, task.project_id)
    dispatch = _active_dispatch(task)
    receipt = dispatch["receipt"]
    if (
        receipt["lease_expires_at"] <= now
        or request.attempt != receipt["attempt"]
        or request.requested_by != receipt["executor"]
        or binding["task_revision"] != task.revision
        or binding["job_ref"] != dispatch.get("job_ref")
        or binding["claim_fence"] != _fence(receipt["claim_token"])
        or dispatch.get("approval_requests", {}).get(binding["invocation_id"]) != ticket.approval_id
    ):
        raise TaskServiceError("stale", "Approval no longer matches an active claim")
    return task, dispatch


def _executor(ticket: ApprovalTicket) -> ExecutorContext:
    request = _require_request(ticket)
    return ExecutorContext(
        actor=request.requested_by, task_id=request.task_id, attempt=request.attempt,
        digest=request.digest, policy_revision=request.policy_revision, target=request.target,
    )


def _effect_history(
    queue: ApprovalQueue, task: BoardTask, action: str, target: ApprovalTarget, attempt: int,
) -> None:
    """Prevent another grant from authorizing an already settled or denied action."""
    for dispatch in task.data.get("dispatches", []):
        for request_id in dispatch.get("approval_requests", {}).values():
            other = queue.get_ticket(request_id)
            if other is None:
                raise TaskServiceError("unavailable", "Approval history is incomplete")
            request = _require_request(other)
            if request.action != action or request.target != target:
                continue
            if other.consumed_at is not None and other.effect_outcome is None:
                raise TaskServiceError("unresolved", "The effect must be reconciled before retry")
            if action != "read" and other.effect_outcome is not None and other.effect_outcome.succeeded:
                raise TaskServiceError("conflict", "This exact effect already succeeded")
            if other.status == "rejected" and request.attempt == attempt:
                raise TaskServiceError("forbidden", "This exact effect was denied for the attempt")


class ProjectTaskApprovals:
    def __init__(
        self, service: ProjectTaskService, *, authorize: EffectPolicy | None = None,
        owner_actor: str = "owner", orchestrator_actor: str = "operator",
    ):
        self.service = service
        self.authorize = authorize
        self.owner_actor = owner_actor
        self.orchestrator_actor = orchestrator_actor

    def _queue(self, store: TransactionStore, *, owner_actor: str | None = None) -> ApprovalQueue:
        # ApprovalQueue uses the adapter's read/write/items/CAS methods directly.
        return ApprovalQueue(
            cast(Store, store), owner_actor=owner_actor or self.owner_actor, action_tiers=_CARD_CLASSES,
            clock=lambda: datetime.fromtimestamp(self.service.clock(), UTC),
        )

    def provision_automatic_cards(self, project_id: str, *, credential: str) -> None:
        """Provision the owner's standing, nontransferable grants for one project."""
        folder = self._card_folder()
        with transaction(self.service.path, write=True) as store:
            queue = self._queue(store)
            cards = {
                action: queue.issue_card(
                    credential=credential, action_class=f"projects_{action}", tier="routine", project_id=project_id,
                    matcher=CardMatcher(command=(_CARD_COMMAND, action), path_prefix=folder),
                ).model_dump(mode="json") for action in _AUTOMATIC_ACTIONS
            }
            store.write(_CARD_SET_PREFIX + project_id, cards)

    def _card_folder(self) -> Path:
        if self.service.path is None:
            raise TaskServiceError("unavailable", "A persistent projects store is required")
        return self.service.path.parent

    def _approve_automatically(
        self, store: TransactionStore, queue: ApprovalQueue, ticket: ApprovalTicket, project_id: str,
    ) -> ApprovalTicket:
        request = _require_request(ticket)
        raw_card = store.read(_CARD_SET_PREFIX + project_id, {}).get(request.action)
        if raw_card is None:
            raise TaskServiceError(
                "unavailable", "The owner must provision this project's automatic approval cards with "
                f"`lazytools-code-bridge approver-credential provision-automatic {project_id}`",
            )
        card = ApprovalCard.model_validate(raw_card)
        folder = self._card_folder()
        # Bind the card to the host adapter operation and original digest. This
        # operation is dispatched by the effect adapter and has no shell command.
        card_request = TypedApprovalRequest.create(
            **request.model_dump(exclude={"digest", "target", "tier", "action_class", "card_action"}),
            target=ApprovalTarget(), tier="routine", action_class=f"projects_{request.action}",
            card_action=CardAction(
                argv=(_CARD_COMMAND, request.action, request.digest), cwd=folder,
                paths=(folder,), network="off", project_id=project_id,
            ),
        )
        policy = resolve("edit-and-run", TaskGrant(
            folder, {"tool_classes": ["shell"]}, "off", "local", approval_cards=(card,),
        ))
        with policy_scope(policy) as requester:
            assert requester is not None
            receipt = queue.submit_typed_request(card_request, requester=requester)
        if not isinstance(receipt, CardApproval) or receipt.approved_at >= ticket.expires_at:
            raise TaskServiceError("forbidden", "The project's automatic approval card did not authorize this effect")
        # Materialize the card-backed decision in the existing effect ledger so
        # claim binding, consumption and outcome recording stay transactional.
        decision = ApprovalDecision(
            decider=self.owner_actor, decided_at=receipt.approved_at, verdict="approved", reason=request.payload["rule_id"],
        )
        approved = ticket.model_copy(update={
            "status": "approved", "actor": decision.decider, "reason": decision.reason,
            "channel": "policy-card", "decision": decision,
        })
        key = DEFAULT_PREFIX + ticket.approval_id
        if not store.compare_and_swap(key, ticket.model_dump(mode="json"), approved.model_dump(mode="json")):
            raise TaskServiceError("conflict", "The automatic approval could not be recorded")
        store.write(_CARD_RECEIPT_PREFIX + ticket.approval_id, receipt.model_dump(mode="json"))
        return approved

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
            items: list[dict[str, Any]] = []
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

    def decide(
        self, principal: TaskPrincipal, request_id: str, expected_action_digest: str,
        decision: Decision, reason: str, *, credential: str | None = None,
    ) -> dict[str, Any]:
        principal.require_role("orchestrator", "owner")
        if decision not in ("allow", "deny") or not reason.strip():
            raise TaskServiceError("invalid", "A decision and reason are required")
        reason = reason.strip()
        verdict: Literal["approved", "rejected"] = "approved" if decision == "allow" else "rejected"
        with transaction(self.service.path, write=True) as store:
            queue = self._queue(store)
            ticket = queue.get_ticket(request_id)
            if ticket is None or not _visible(principal, ticket):
                raise TaskServiceError("not_found", "Unknown governed approval request")
            request = _require_request(ticket)
            if request.payload["required_approver"] != principal.role:
                raise TaskServiceError("forbidden", "The request requires a different approver role")
            if request.digest != expected_action_digest:
                raise TaskServiceError("stale", "Approval action digest changed")
            if not credential:
                raise TaskServiceError("forbidden", "An approver credential is required")
            approver = queue._authenticated_approver(credential)
            if approver is None or approver.actor != principal.actor or principal.role not in approver.roles:
                raise TaskServiceError("forbidden", "The approver credential does not match the authenticated principal")
            assignee = ticket.route.assignee if ticket.route else None
            actor = assignee.actor if assignee else (
                self.orchestrator_actor if principal.role == "orchestrator" else self.owner_actor
            )
            if actor != approver.actor or "owner" not in approver.roles:
                raise TaskServiceError("forbidden", "The approver credential cannot decide this ticket")
            previous = ticket.decision
            if previous is not None:
                if (
                    previous.decider != principal.actor or previous.verdict != verdict
                    or previous.reason != reason
                ):
                    raise TaskServiceError("conflict", "The request already has a different decision")
                return {"request": _view(ticket)}
            _active_binding(store, ticket, self.service.clock())
            if not self._queue(store, owner_actor=actor).decide_typed_ticket(
                request_id, credential=credential, verdict=verdict, reason=reason,
            ):
                raise TaskServiceError("stale", "The request is no longer pending or has expired")
            return {"request": _view(queue.get_ticket(request_id))}

    def _authorize_effect(
        self, principal: TaskPrincipal, task: BoardTask, action: str, target: ApprovalTarget,
    ) -> tuple[EffectAuthorization, str]:
        if action not in _ACTIONS:
            raise TaskServiceError("forbidden", "Unknown effect")
        if action in _SCOPED_REPO_ACTIONS:
            repo, branch = task.data.get("repo"), task.data.get("assigned_branch")
            if (
                not repo or not branch or repo != target.repo
                or branch.removeprefix("refs/heads/") != (target.ref or "").removeprefix("refs/heads/")
                or not _COMMIT_SHA.fullmatch(target.sha or "")
            ):
                raise TaskServiceError("forbidden", "Effect is outside the assigned repository, branch, or exact head")
        if action == "merge_main":
            principal.require_role("orchestrator")
            if (
                not target.repo or target.repo != task.data.get("repo")
                or not target.ref or not _COMMIT_SHA.fullmatch(target.sha or "")
            ):
                raise TaskServiceError("forbidden", "Main updates need an exact repository, ref and head")
        grant = self.authorize(principal, task.data, action, target) if self.authorize is not None else None
        if grant is None:
            raise TaskServiceError("forbidden", "The current effective grant does not authorize the effect")
        if action == "merge_main" and grant.verified_head != target.sha:
            raise TaskServiceError("forbidden", "Main updates require checks and review on the current head")
        if grant.production or action == "production_change":
            return grant, "owner"
        if action in _AUTOMATIC_ACTIONS:
            return grant, "automatic"
        return grant, "orchestrator"

    def create_request(
        self, principal: TaskPrincipal, task_id: str, claim_token: str, invocation_id: str,
        action: str, target: ApprovalTarget, *, ttl: timedelta = timedelta(hours=2),
    ) -> dict[str, Any]:
        """Create a task-bound ticket from the trusted executor adapter."""
        if not invocation_id.strip() or ttl <= timedelta(0):
            raise TaskServiceError("invalid", "A tool invocation ID and positive expiry are required")
        with transaction(self.service.path, write=True) as store:
            queue = self._queue(store)
            task = _scoped_task(store, principal, task_id)
            _require_open(store, task.project_id)
            now = self.service.clock()
            dispatch = _fenced_dispatch(task, principal, claim_token, now)
            if dispatch["state"] != "submitted":
                raise TaskServiceError("conflict", "Effect requires an acknowledged dispatch")
            grant, required = self._authorize_effect(principal, task, action, target)
            request = TypedApprovalRequest.create(
                task_id=task_id, attempt=dispatch["receipt"]["attempt"], action=action,
                target=target, requested_by=principal.actor, policy_revision=grant.policy_revision,
                payload={
                    "registry": _REGISTRY, "task_revision": task.revision, "claim_fence": _fence(claim_token),
                    "invocation_id": invocation_id, "required_approver": required, "job_ref": dispatch["job_ref"],
                    "risk": grant.risk, "rule_id": grant.rule_id,
                },
            )
            requests = dispatch.setdefault("approval_requests", {})
            if invocation_id in requests:
                ticket = queue.get_ticket(requests[invocation_id])
                if ticket is None or _require_request(ticket).digest != request.digest:
                    raise TaskServiceError("conflict", "Invocation ID already binds a different request")
                return _view(ticket)
            if dispatch.get("unresolved_effects"):
                raise TaskServiceError("unresolved", "Reconcile the outstanding effect before requesting another grant")
            _effect_history(queue, task, action, target, request.attempt)
            if required == "orchestrator":
                queue = self._queue(store, owner_actor=self.orchestrator_actor)
            ticket = queue.create_typed_ticket(request, ttl=ttl, operator_only=required != "automatic")
            requests[invocation_id] = ticket.approval_id
            if required == "automatic":
                ticket = self._approve_automatically(store, queue, ticket, task.project_id)
            task.save(store, now)
            return _view(ticket)

    def consume(
        self, principal: TaskPrincipal, request_id: str, claim_token: str, effect_key: str,
        action_digest: str, action: str, observed_target: ApprovalTarget,
    ) -> None:
        """Authorize exactly one execution at the effect boundary."""
        if not effect_key.strip():
            raise TaskServiceError("invalid", "An effect idempotency key is required")
        with transaction(self.service.path, write=True) as store:
            queue = self._queue(store)
            ticket = queue.get_ticket(request_id)
            if ticket is None:
                raise TaskServiceError("not_found", "Unknown governed approval request")
            request = _require_request(ticket)
            principal.require_project(ticket.task_id.partition("/")[0])
            now = self.service.clock()
            task, _ = _active_binding(store, ticket, now)
            dispatch = _fenced_dispatch(task, principal, claim_token, now)
            grant, required = self._authorize_effect(principal, task, action, observed_target)
            if (
                dispatch["state"] != "submitted" or request.action != action or request.digest != action_digest
                or request.target != observed_target or request.policy_revision != grant.policy_revision
                or request.payload["required_approver"] != required or request.payload["rule_id"] != grant.rule_id
                or request.payload["risk"] != grant.risk
            ):
                raise TaskServiceError("stale", "The current action, target, or policy differs from the approved request")
            if dispatch.get("unresolved_effects"):
                raise TaskServiceError("unresolved", "The outstanding effect must be reconciled before retry")
            if any(effect_key in attempt.get("effect_keys", {}) for attempt in task.data["dispatches"]):
                raise TaskServiceError("conflict", "The effect idempotency key has already been spent")
            _effect_history(queue, task, action, observed_target, request.attempt)
            if not queue.consume_typed_ticket(request_id, executor=_executor(ticket)):
                raise TaskServiceError("stale", "Approval is expired, unapproved, or already consumed")
            dispatch.setdefault("effect_keys", {})[effect_key] = request_id
            dispatch.setdefault("unresolved_effects", {})[request_id] = effect_key
            task.save(store, now)

    def record_outcome(
        self, principal: TaskPrincipal, request_id: str, effect_key: str,
        result: EffectResult, evidence_ref: str,
    ) -> dict[str, Any]:
        """Record only a conclusive provider observation. Unknown outcomes stay held."""
        if result not in ("succeeded", "not_applied") or not evidence_ref.strip():
            raise TaskServiceError("invalid", "A conclusive result and evidence reference are required")
        with transaction(self.service.path, write=True) as store:
            queue = self._queue(store)
            ticket = queue.get_ticket(request_id)
            if ticket is None:
                raise TaskServiceError("not_found", "Unknown governed approval request")
            request = _require_request(ticket)
            task = _scoped_task(store, principal, ticket.task_id)
            if principal.actor != request.requested_by:
                principal.require_role("orchestrator")
            dispatch = next((
                attempt for attempt in task.data.get("dispatches", [])
                if attempt.get("effect_keys", {}).get(effect_key) == request_id
            ), None)
            if dispatch is None:
                raise TaskServiceError("stale", "The effect has no matching consumption")
            succeeded = result == "succeeded"
            previous = ticket.effect_outcome
            if previous:
                if previous.succeeded != succeeded or previous.detail != evidence_ref:
                    raise TaskServiceError("conflict", "The effect has a different recorded outcome")
                return _view(ticket)
            if not queue.record_effect_outcome(
                request_id, executor=_executor(ticket), succeeded=succeeded, detail=evidence_ref
            ):
                raise TaskServiceError("conflict", "The effect outcome could not be recorded")
            del dispatch["unresolved_effects"][request_id]
            dispatch.setdefault("effect_observers", {})[request_id] = principal.actor
            task.save(store, self.service.clock())
            return _view(queue.get_ticket(request_id))
