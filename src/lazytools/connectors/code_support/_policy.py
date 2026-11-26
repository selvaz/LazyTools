"""Core policy boundary shared by MCP dispatch and the code connectors."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import lru_cache, wraps
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from lazybridge import Tool
from lazybridge.policy import (
    EffectivePolicy,
    PolicyDenied,
    PolicyError,
    PolicyWidening,
    TaskGrant,
    child_within_parent,
    current_policy_context,
    load_profiles,
    policy_scope,
    resolve,
)
from lazybridge.policy.resolve import validate_child_policy

from lazytools.code_bridge import _launch

if TYPE_CHECKING:
    from lazybridge import Store
    from lazybridge.engines.sessions import SessionRegistry
    from lazybridge.policy.models import ProfileDocument

logger = logging.getLogger(__name__)

CODE_ACCESS = {
    "codex_ask": "read",
    "claude_ask": "read",
    "codex_code_review": "read",
    "claude_code_review": "read",
    "codex_review_changes": "read",
    "codex_write": "edit",
    "claude_code_write": "edit",
    "code_sessions_list": "read",
    "code_sessions_bind": "edit",
    "code_sessions_rename": "edit",
    "code_sessions_forget": "edit",
    "code_bridge_submit": "read",  # The requested job profile is checked at submission.
    "code_bridge_status": "read",
    "code_bridge_result": "read",
}


def profiles() -> ProfileDocument:
    configured = os.environ.get("LAZYTOOLS_CODE_BRIDGE_PROFILES")
    return load_profiles(configured) if configured else load_profiles()


@dataclass(frozen=True)
class CodeTaskContext:
    """A task grant resolved by the host after authenticating the caller."""

    task_id: str
    actor: str
    expires_at: datetime
    policy: EffectivePolicy
    operations: frozenset[str]
    attempt: int = 1
    projects_db: str | None = None
    codex_credential_id: UUID | None = None
    codex_api_key: str | None = field(default=None, repr=False)
    claude_credential_id: UUID | None = None
    claude_api_key: str | None = field(default=None, repr=False)

    def validate(self) -> None:
        document = profiles()
        if self.expires_at.tzinfo is None or self.expires_at <= datetime.now(UTC):
            raise PolicyDenied("code", "The MCP task context has expired.")
        if (self.policy.version, self.policy.revision) != (document.version, document.revision):
            raise PolicyDenied("code", "The MCP task context was resolved against a different policy revision.")
        if (self.codex_api_key is None) != (self.codex_credential_id is None):
            raise PolicyDenied("code", "A Codex credential requires its executor-assigned ID.")
        if (self.claude_api_key is None) != (self.claude_credential_id is None):
            raise PolicyDenied("code", "A Claude credential requires its executor-assigned ID.")

    def credential_id(self, engine: str) -> str | None:
        credential = self.codex_credential_id if engine == "codex" else self.claude_credential_id
        return str(credential) if credential else None


_task: ContextVar[CodeTaskContext | None] = ContextVar("mcp_code_task", default=None)


@contextmanager
def task_scope(task: CodeTaskContext) -> Iterator[None]:
    task.validate()
    with policy_scope(task.policy):
        token = _task.set(task)
        try:
            yield
        finally:
            _task.reset(token)


def current_task() -> CodeTaskContext | None:
    return _task.get()


def code_tool(tool: Tool, *, root: Path, operation: str | None = None) -> Tool:
    tool.policy_grant = CODE_ACCESS[operation or tool.name]
    tool._lazytools_code_root = root  # type: ignore[attr-defined]
    if not inspect.iscoroutinefunction(tool.func):
        func = tool.func

        @wraps(func)
        async def dispatch(**kwargs: Any) -> Any:
            return await asyncio.to_thread(func, **kwargs)

        tool.func = dispatch
    return tool


def authorize(operation: str, cwd: Path | None = None, *, access: str | None = None) -> EffectivePolicy | None:
    task = current_task()
    if task is not None:
        task.validate()
        if operation not in task.operations:
            raise PolicyDenied(operation)
    context = current_policy_context()
    # A budget-only context (tree budget, no policy) carries no authority ceiling.
    if context is None or context.delegable_grants is None:
        return None
    parent = context.delegable_grants
    document = profiles()
    if (parent.version, parent.revision) != (document.version, document.revision):
        raise PolicyDenied(operation, "The active context has a different policy revision.")
    if (access or CODE_ACCESS[operation]) not in parent.tool_classes or parent.placement != "local":
        raise PolicyDenied(operation)
    if cwd is not None and not cwd.resolve().is_relative_to(parent.folder):
        raise PolicyDenied(operation, "The requested directory is outside the task context.")
    return parent


def audit(operation: str, policy: EffectivePolicy, mode: str) -> None:
    logger.info(
        "Code tool %s policy_revision=%s policy_digest=%s launch_mode=%s",
        operation, policy.revision, policy.digest, mode,
        extra={"policy_revision": policy.revision, "policy_digest": policy.digest, "launch_mode": mode},
    )


def record_operation(operation: str, cwd: Path, *, access: str | None = None) -> EffectivePolicy | None:
    parent = authorize(operation, cwd, access=access)
    if parent is None:
        access = access or CODE_ACCESS[operation]
        profile = "edit-and-run" if access == "edit" else "read"
        classes = ("read", "edit") if access == "edit" else ("read",)
        policy = resolve(profile, TaskGrant(cwd, {"tool_classes": classes}, "off", "local"), profiles())
        if access not in policy.tool_classes:
            raise PolicyDenied(operation)
        audit(operation, policy, "ungoverned-legacy")
    else:
        audit(operation, parent, "governed")
    return parent


def _resolve_local_child(
    parent: EffectivePolicy, grant: TaskGrant, profile: str, document: ProfileDocument,
) -> EffectivePolicy:
    cwd = Path(grant.folder).resolve()
    scratch = parent.scratch_folder
    if not scratch.is_relative_to(cwd):
        scratch = cwd / ".scratch"
        if not scratch.is_relative_to(parent.scratch_folder):
            scratch = parent.scratch_folder / cwd.relative_to(parent.folder)
    scratch = scratch.resolve()
    if not scratch.is_relative_to(parent.scratch_folder):
        raise PolicyWidening("scratch folder outside parent scratch folder")

    request = replace(grant, folder=cwd, grants={**grant.grants, "scratch_folder": scratch})
    try:
        return child_within_parent(parent, request, profile, document)
    except PolicyError as exc:
        if scratch.is_relative_to(cwd) or str(exc) != "scratch_folder must be inside the assigned folder":
            raise

    # LazyBridge 1.9.0 applies the root scratch restriction to children before
    # checking the parent grant. Resolve the other fields and then validate the
    # real scratch against the parent, preserving the D188 canonical digest.
    child = resolve(profile, replace(request, grants={**request.grants, "scratch_folder": cwd}), document)
    canonical = {
        "profile": child.profile,
        "version": child.version,
        "revision": child.revision,
        "folder": str(child.folder),
        "scratch_folder": str(scratch),
        "tool_classes": sorted(child.tool_classes),
        "tools": sorted(child.tools),
        "network": child.network,
        "network_routes": child.network_routes,
        "placement": child.placement,
        "mcp_servers": dict(child.mcp_servers),
        "mcp_inventory": child.mcp_inventory,
        "mcp_inventory_tools": dict(child.mcp_inventory_tools),
    }
    digest = sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    child = replace(child, scratch_folder=scratch, digest=digest)
    validate_child_policy(parent, child)
    return child


def local_launch(
    operation: str, engine: str, cwd: Path, *, web: bool = False, shell: bool = False, discovery: bool = False,
) -> _launch.Launch:
    parent = authorize(operation, cwd)
    document = profiles()
    writing = CODE_ACCESS[operation] == "edit"
    profile = "edit-and-run" if writing else "read"
    classes = ("read", "edit") if writing else ("read",)
    report_setting = os.environ.get(f"LAZYTOOLS_CODE_BRIDGE_{engine.upper()}_RELEASE_REPORT")
    report = Path(report_setting).expanduser().resolve() if report_setting else None
    legacy = parent is None and report is None
    if legacy and shell:
        classes = (*classes, "shell")
    grants: dict[str, Any] = {"tool_classes": classes, "tools": ("web",) if legacy and web else ()}
    if parent is not None:
        grants["tool_classes"] = tuple(set(classes) & parent.tool_classes)
        grants["mcp_inventory"] = {
            name: {"tools": parent.mcp_inventory_tools[name]} for name in parent.mcp_inventory
        }
    grant = TaskGrant(cwd, grants, "on" if legacy and web else "off", "local")
    policy = resolve(profile, grant, document) if parent is None else _resolve_local_child(parent, grant, profile, document)
    mode = "ungoverned-legacy" if legacy else "governed"
    if not discovery:
        audit(operation, policy, mode)
    if not set(("read", "edit") if writing else ("read",)) <= policy.tool_classes:
        raise PolicyDenied(operation, "The effective policy is missing required tool classes.")
    compiled, receipt = _launch._compile(engine, policy, report)
    if not legacy and not (receipt and compiled["eligible"]):
        raise PolicyDenied(operation, "Governed local launch refused. " + " ".join(compiled["ineligibility_reasons"]))
    return _launch.Launch(policy, compiled, mode, report, receipt)


_RUNTIME_ENV = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL",
    "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMDATA",
})


@lru_cache(maxsize=8)
def _claude_client(report: Path) -> Any:
    from lazybridge.engines.claude_code.sdk_client import AgentSdkClient

    class ScopedClaudeClient(AgentSdkClient):
        @staticmethod
        def _sdk_options(options: Any) -> Any:
            sdk_options = AgentSdkClient._sdk_options(options)
            # The SDK overlays this on the inherited environment. Blank every
            # other value, including MCP bearer, proxy and publication secrets.
            masked = {key: "" for key in os.environ if key.upper() not in _RUNTIME_ENV}
            task = current_task()
            if task and task.claude_api_key:
                masked["ANTHROPIC_API_KEY"] = task.claude_api_key
            return replace(sdk_options, env={**masked, **sdk_options.env})

    return ScopedClaudeClient(policy_release_report=report)


def native_client(engine: str, launch: _launch.Launch) -> dict[str, Any]:
    if engine == "claude":
        if launch.report is None:
            return {}
        return {"client": _claude_client(launch.report)}

    from lazybridge.engines.codex.app_server import CodexAppServerClient

    class ShellDisabledClient(CodexAppServerClient):
        def _spawn_command(self, config_overrides: tuple[str, ...] = ()) -> tuple[str, ...]:
            return super()._spawn_command((*config_overrides, "features.shell_tool=false"))

    if launch.report is not None:
        task = current_task()
        return {"client": CodexAppServerClient(
            policy_release_report=launch.report,
            policy_state_dir=Path.home() / ".lazytools" / "mcp-codex-policy",
            policy_api_key=task.codex_api_key if task else None,
            policy_credential_id=task.codex_credential_id if task else None,
        )}
    return {"client": ShellDisabledClient()} if "shell" not in launch.policy.tool_classes else {}


@asynccontextmanager
async def native_scope(launch: _launch.Launch) -> AsyncIterator[None]:
    task = current_task()
    remaining = None
    if task is not None:
        task.validate()
        remaining = (task.expires_at - datetime.now(UTC)).total_seconds()
    with policy_scope(launch.policy if launch.mode == "governed" else None):
        async with asyncio.timeout(remaining):
            yield


@dataclass
class NativeBinding:
    """Fence governed native resume to the authenticated task and launch."""

    launch: _launch.Launch
    engine: str
    store: Store | None = None

    def __post_init__(self) -> None:
        if self.launch.mode == "governed":
            from lazytools.code_bridge._registry import registry_store

            task = current_task()
            self.store = registry_store(projects_db=task.projects_db if task else None)
            context = current_policy_context()
            self.binding = self.launch.binding()
            if task:
                self.binding.update(task_id=task.task_id, actor=task.actor, attempt=task.attempt)
                self.binding["credential_id"] = task.credential_id(self.engine)
            elif context:
                self.binding["invocation_id"] = context.invocation_id

    def _key(self, native_id: str) -> str:
        return f"mcp-code:native-policy:{self.engine}:{sha256(native_id.encode()).hexdigest()}"

    def check(self, native_id: str | None) -> None:
        if self.store is not None and native_id:
            _launch.check_binding(self.store.read(self._key(native_id)), self.binding)

    def record(self, native_id: str | None) -> None:
        if self.store is not None and native_id:
            key = self._key(native_id)
            if not self.store.compare_and_swap(key, None, self.binding):
                _launch.check_binding(self.store.read(key), self.binding)

    def resume(self, registry: SessionRegistry, cwd: str | Path, alias: str | None, native_id: str | None) -> str | None:
        if self.store is None:
            return native_id
        if native_id is None and alias:
            native_id = registry.resolve(self.engine, cwd, alias)
        self.check(native_id)
        return native_id

    def finish(self, registry: SessionRegistry, cwd: str | Path, alias: str | None, native_id: str | None) -> None:
        if self.store is not None and native_id:
            self.record(native_id)
            if alias:
                registry.bind(self.engine, cwd, alias, native_id)
