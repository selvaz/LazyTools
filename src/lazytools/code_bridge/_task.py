"""Bind a local worker to an existing projects task claim."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from lazybridge import Store
from lazybridge.ext.approval import ApprovalQueue, ExecutorContext

from lazytools.code_bridge import _cloud, _coder, _launch, _registry
from lazytools.connectors.projects.tools import _resolve_store_db
from lazytools.projects.task_service import ProjectTaskService, TaskPrincipal
from lazytools.projects.task_store import TaskServiceError

PRINCIPAL_ENV = "LAZYTOOLS_CODE_BRIDGE_TASK_PRINCIPAL"
MCP_POLICY_DIGEST_ENV = "LAZYTOOLS_CODE_BRIDGE_MCP_POLICY_DIGEST"
MCP_CODEX_KEY_ENV = "LAZYTOOLS_CODE_BRIDGE_MCP_CODEX_API_KEY"
MCP_CODEX_ID_ENV = "LAZYTOOLS_CODE_BRIDGE_MCP_CODEX_CREDENTIAL_ID"


def principal() -> TaskPrincipal:
    raw = os.environ.get(PRINCIPAL_ENV)
    if not raw:
        raise ValueError("A trusted bridge task principal must be configured")
    return TaskPrincipal.model_validate_json(raw)


@dataclass(frozen=True)
class Claim:
    service: ProjectTaskService
    principal: TaskPrincipal
    task_id: str
    token: str
    receipt: dict[str, Any]

    def binding(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_revision": self.receipt["revision"],
            "task_attempt": self.receipt["attempt"],
            "task_preflight_ref": self.receipt["preflight_ref"],
            "task_executor": self.principal.actor,
        }

    def launch(self, engine: str, cwd: Path, profile: str, network: str | None, mode: str, review: bool) -> _launch.Launch:
        receipt = self.receipt
        # Projects records the provider family; the local CLI calls Claude `claude`.
        claimed_engine = receipt["engine_family"]
        if claimed_engine != engine and (claimed_engine, engine) != ("claude_code", "claude"):
            raise ValueError("Local engine differs from the task claim")
        if receipt["repo"] and _cloud.repo_slug(cwd).casefold() != receipt["repo"].casefold():
            raise ValueError("Local repository differs from the task claim")
        if receipt["assigned_branch"] and _cloud.current_branch(cwd) != receipt["assigned_branch"].removeprefix("refs/heads/"):
            raise ValueError("Local branch differs from the task claim")
        review = review or profile in {"read", "review"} or bool(receipt["review_of"])
        claimed_profile = receipt["requested_profile"]
        if claimed_profile and not review and profile != "default" and _launch.LEGACY_PROFILES.get(profile, profile) != claimed_profile:
            raise ValueError("Local profile differs from the task claim")
        if claimed_profile:
            profile = claimed_profile
        claimed_network = receipt["requested_network"]
        if claimed_network is not None:
            if network == "on" and not claimed_network:
                raise ValueError("Network-on exceeds the task claim")
            network = network or ("on" if claimed_network else "off")
        if mode == "auto":
            mode = "governed"
        launch = _launch.resolve_launch(engine, cwd, profile, network, mode, review=review)
        expected = os.environ.get(MCP_POLICY_DIGEST_ENV)
        if expected and (launch.mode != "governed" or launch.policy.digest != expected):
            raise ValueError("The launch no longer matches the MCP task's policy digest")
        return launch

    def recheck(self, job_id: str) -> None:
        self.service.local_claim(self.principal, self.task_id, self.token, state="submitted", job_ref=job_id)

    def consume_approval(
        self, job_id: str, approval_id: str, executor: ExecutorContext, *, registry: _registry.Registry | None = None,
    ) -> bool:
        with self.service.local_dispatch(
            self.principal, self.task_id, self.token, state="submitted", job_ref=job_id, write=True,
        ) as (store, _):
            queue = registry.queue if registry is not None and registry.path != self.service.path else ApprovalQueue(
                cast(Store, store)
            )
            return queue.consume_typed_ticket(approval_id, executor=executor)

    @contextmanager
    def card_queue(self, job_id: str, registry: _registry.Registry) -> Iterator[ApprovalQueue]:
        with self.service.local_dispatch(
            self.principal, self.task_id, self.token, state="submitted", job_ref=job_id, write=True,
        ) as (store, _):
            yield registry.queue if registry.path != self.service.path else ApprovalQueue(
                cast(Store, store), action_tiers=_coder.TIERS,
            )


def load(
    task_id: str | None, token: str | None, *, state: Literal["intent", "submitting"],
) -> Claim | None:
    if task_id is None and token is None:
        return None
    if not task_id or not token:
        raise ValueError("--task-id and --claim-token are required together")
    actor = principal()
    service = ProjectTaskService(_resolve_store_db(None))
    try:
        receipt = service.local_claim(actor, task_id, token, state=state)
    except TaskServiceError as exc:
        raise ValueError(str(exc)) from exc
    return Claim(service, actor, task_id, token, receipt)
