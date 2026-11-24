"""Resolve the authenticated code task on each MCP request."""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from lazybridge.policy import TaskGrant, resolve
from pydantic import BaseModel, ConfigDict, Field

from lazytools.connectors.code_support._policy import CODE_ACCESS, CodeTaskContext, profiles

CodeTaskResolver = Callable[[Any], CodeTaskContext | None]


class _ConfiguredTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    expires_at: datetime
    folder: Path
    profile: Literal["read", "edit", "edit-and-run"]
    tool_classes: tuple[Literal["read", "edit", "shell"], ...]
    operations: frozenset[str]
    attempt: int = Field(default=1, ge=1)
    http_token_env: str | None = None
    codex_api_key_env: str | None = None
    codex_credential_id: UUID | None = None
    claude_api_key_env: str | None = None
    claude_credential_id: UUID | None = None


def configured_task_resolver(
    config: dict[str, Any], *, http: bool,
) -> CodeTaskResolver | None:
    raw = config.get("code_task_context")
    if raw is None:
        return None
    settings = _ConfiguredTask.model_validate(raw)
    if not settings.operations <= CODE_ACCESS.keys():
        raise ValueError("code_task_context contains unknown code operations.")
    if not settings.folder.is_absolute() or not settings.folder.is_dir():
        raise ValueError("code_task_context.folder must be an existing absolute directory.")
    if http and not settings.http_token_env:
        raise ValueError("An HTTP code_task_context requires http_token_env.")
    if settings.http_token_env and settings.http_token_env in {settings.codex_api_key_env, settings.claude_api_key_env}:
        raise ValueError("The MCP bearer and native provider credentials must use different variables.")
    policy = resolve(
        settings.profile, TaskGrant(settings.folder, {"tool_classes": settings.tool_classes}, "off", "local"),
        profiles(),
    )
    task = CodeTaskContext(
        settings.task_id, settings.actor, settings.expires_at, policy, settings.operations,
        settings.attempt, config.get("projects_store_db"),
        codex_credential_id=settings.codex_credential_id,
        codex_api_key=os.environ.get(settings.codex_api_key_env or ""),
        claude_credential_id=settings.claude_credential_id,
        claude_api_key=os.environ.get(settings.claude_api_key_env or ""),
    )

    def resolve_request(request: Any) -> CodeTaskContext | None:
        if not http:
            return task if request is None else None
        token = os.environ.get(settings.http_token_env or "")
        supplied = request.headers.get("authorization", "") if request is not None else ""
        if token and secrets.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
            return task
        return None

    return resolve_request
