"""MCP tools over the code bridge: ``code_bridge_submit`` / ``_status`` / ``_result``.

Lets another agent hand work to local OR cloud engines the same way a person
does with ``lazytools-code-bridge run|status|result`` -- each tool runs that
CLI command in a child process and returns its JSON, so validation, redaction,
placement, approvals and landing behave exactly as on the command line. The
provider is only served with ``--allow-unsafe`` (it starts coding agents that
write code); see ``lazytools.mcp_server.providers``.

Two guard rails the CLI does not need but an MCP caller does:

* ``cwd`` is confined to the code root (``--root``), like every other code tool;
* a task may not start with ``@``: the CLI reads ``@path`` as "task text from
  that file", which would let a caller read any file the server can.

Cloud jobs need ``allow_cloud=true`` (the CLI's ``--allow-cloud``). Cloud
results reach the repository only through the local landing step, whose checks
are the same whether it is invoked by ``apply``, ``land`` or ``cloud-sync``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from lazybridge.policy import PolicyDenied

from lazytools.code_bridge._credentials import job_environment
from lazytools.connectors.code_support._policy import _RUNTIME_ENV, authorize, code_tool, current_task, record_operation

SUBMIT_TIMEOUT = 900.0
QUERY_TIMEOUT = 600.0
_JOB_REF_RE = re.compile(r"[0-9A-Za-z-]{4,64}")
_scoped_launch: ContextVar[tuple[str, str] | None] = ContextVar("bridge_mcp_launch", default=None)
_BRIDGE_ENV = _RUNTIME_ENV | {
    "PYTHONPATH", "PYTHONUTF8", "CODEX_BIN", "CLAUDE_BIN", "LAZYBRIDGE_SESSIONS_FILE", "LAZYTOOLS_CODE_BRIDGE_DB",
    "LAZYTOOLS_PROJECTS_STORE_DB", "LAZYTOOLS_CODE_BRIDGE_PROFILES", "LAZYTOOLS_CODE_BRIDGE_CONFIG",
    "LAZYTOOLS_CODE_BRIDGE_CLAUDE_RELEASE_REPORT", "LAZYTOOLS_CODE_BRIDGE_CODEX_RELEASE_REPORT",
}


class CodeBridgeTools:
    """Submit / status / result for code-bridge jobs, local or cloud."""

    _is_lazy_tool_provider = True  # how lazytools.mcp_server.expand_tools recognises a provider

    def __init__(self, root: str | None = None, *, task_principal: str | None = None, projects_db: str | None = None) -> None:
        self._root = str(Path(root or Path.cwd()).expanduser().resolve())
        self._task_principal = task_principal
        self._projects_db = projects_db

    # -- plumbing ---------------------------------------------------------- #

    def _invoke(self, argv: list[str], timeout: float) -> tuple[int, str, str]:
        """Run one bridge CLI command in a child process. Patched in tests."""
        from lazytools.code_bridge._task import (
            MCP_CODEX_ID_ENV,
            MCP_CODEX_KEY_ENV,
            MCP_POLICY_DIGEST_ENV,
            PRINCIPAL_ENV,
        )

        context = current_task()
        env = job_environment(os.environ) if context is None else {
            key: value for key, value in os.environ.items() if key.upper() in _BRIDGE_ENV
        }
        for key in (PRINCIPAL_ENV, MCP_POLICY_DIGEST_ENV, MCP_CODEX_ID_ENV, MCP_CODEX_KEY_ENV):
            env.pop(key, None)
        launch = _scoped_launch.get()
        if context is not None and launch is not None:
            engine, digest = launch
            env[MCP_POLICY_DIGEST_ENV] = digest
            if engine == "codex" and context.codex_api_key and context.codex_credential_id:
                env[MCP_CODEX_ID_ENV] = str(context.codex_credential_id)
                env[MCP_CODEX_KEY_ENV] = context.codex_api_key
            elif engine == "claude" and context.claude_api_key:
                env["ANTHROPIC_API_KEY"] = context.claude_api_key
        principal, projects_db = self._task_settings()
        if principal is not None:
            env[PRINCIPAL_ENV] = principal
        if projects_db is not None:
            env["LAZYTOOLS_PROJECTS_STORE_DB"] = projects_db
        proc = subprocess.run(
            [sys.executable, "-m", "lazytools.code_bridge", *argv], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL, env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def _task_settings(self) -> tuple[str | None, str | None]:
        from lazytools.projects.task_service import TaskPrincipal

        context = current_task()
        if context is None:
            return self._task_principal, self._projects_db
        principal = TaskPrincipal(actor=context.actor, role="worker", projects=(context.task_id.partition("/")[0],))
        return principal.model_dump_json(), context.projects_db or self._projects_db

    def _call(self, argv: list[str], timeout: float) -> dict[str, Any]:
        try:
            code, out, err = self._invoke(argv, timeout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"the bridge command did not finish within {timeout:g}s"}
        payload: Any = None
        for line in reversed([ln for ln in out.splitlines() if ln.strip()]):
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            break
        if isinstance(payload, dict):
            return {"ok": code == 0, "exit_code": code, **payload, **({"stderr": err.strip()[:2000]} if err.strip() else {})}
        return {"ok": False, "exit_code": code, "error": (err or out).strip()[:2000] or f"exit code {code}"}

    @staticmethod
    def _job_ref(job_id: str) -> str:
        if not isinstance(job_id, str) or not _JOB_REF_RE.fullmatch(job_id):
            raise ValueError("job_id must be a job id or unique prefix (letters, digits, '-')")
        return job_id

    # -- tools ------------------------------------------------------------- #

    def code_bridge_submit(
        self,
        task: str,
        cwd: str | None = None,
        engine: str | None = None,
        where: str | None = None,
        tier: str | None = None,
        model: str | None = None,
        branch: str | None = None,
        pr: int | None = None,
        open_pr: bool = False,
        land: bool = False,
        rebase_ok: bool = False,
        review_of: str | None = None,
        allow_cloud: bool = False,
        profile: str = "default",
        network: str | None = None,
        launch_mode: str = "auto",
        task_id: str | None = None,
        claim_token: str | None = None,
        session: str | None = None,
    ) -> dict[str, Any]:
        """Submit a coding job; returns at once with its ``job_id``."""
        from lazytools.code_bridge import _cloud, _cloud_place, _engines, _launch

        if not isinstance(task, str) or (not task.strip() and not review_of):
            raise ValueError("task is required (except for a cloud review_of)")
        if task.lstrip().startswith("@"):
            raise ValueError("a task may not start with '@' here (the CLI would read it as a file path)")
        if profile not in _launch.PROFILES or launch_mode not in _launch.MODES:
            return {"ok": False, "error": "Unknown bridge profile or launch mode", "exit_code": 2}
        operation = "code_bridge_submit"
        parent = authorize(operation)
        cwd = cwd or (str(parent.folder) if parent else ".")
        resolved = _engines.resolve_cwd(cwd, self._root)
        scoped_launch = None
        read = profile in {"read", "review"} or bool(review_of)
        record_operation(operation, resolved, access="read" if read else "edit")
        if parent is not None:
            context = current_task()
            if context is None or context.task_id != task_id or not claim_token:
                raise PolicyDenied(operation, "A scoped bridge submission requires the authenticated task's claim.")
            if (where not in {None, "local", "auto"} or allow_cloud or network not in {None, "off"} or launch_mode == "legacy"
                    or tier or branch or review_of or pr is not None or open_pr or land or rebase_ok):
                raise PolicyDenied(operation, "This task context only admits governed local jobs with egress off.")
            if not (resolved / ".scratch").resolve().is_relative_to(parent.scratch_folder):
                raise PolicyDenied(operation, "The bridge scratch folder is outside the task context.")
            claimed_engine = self._check_claim(context.attempt, context.expires_at.timestamp(), task_id, claim_token, read)
            if engine is not None and engine != claimed_engine:
                raise PolicyDenied(operation, "The requested engine differs from the authenticated claim.")
            engine = claimed_engine
            where, network, launch_mode = "local", "off", "governed"
            profile = "read" if read else "edit"
            launch = _launch.resolve_launch(engine, resolved, profile, network, launch_mode)
            scoped_launch = (engine, launch.policy.digest)
        config = _cloud.load_config()
        requested = where or config.default_where
        try:
            placement = _cloud_place.decide_for_repo(
                requested, config=config, engine=engine,
                cwd=resolved if requested in ("auto", "cloud") and engine == "codex" else None,
            )
        except _cloud.CloudEnvUnmapped as exc:
            return {**exc.payload(), "exit_code": 2}
        argv = ["run", "--where", placement.where, "--cwd", cwd or ".", "--root", self._root, "--json"]
        argv += ["--profile", profile, "--launch-mode", launch_mode]
        if task.strip():
            argv += ["--task", task]
        for flag, value in (("--engine", engine), ("--tier", tier), ("--model", model), ("--branch", branch),
                            ("--review-of", review_of), ("--network", network), ("--task-id", task_id),
                            ("--claim-token", claim_token), ("--session", session)):
            if value:
                argv += [flag, str(value)]
        if placement.where == "cloud":
            if allow_cloud:
                argv.append("--allow-cloud")
            if pr is not None:
                argv += ["--pr", str(int(pr))]
            argv += [flag for flag, on in (("--open-pr", open_pr), ("--land", land), ("--rebase-ok", rebase_ok)) if on]
        else:
            argv.append("--detach")
            if pr is not None or open_pr or land or rebase_ok:
                raise ValueError("pr / open_pr / land / rebase_ok are cloud options, but this job was placed locally "
                                 f"({placement.reason})")
        authorize(operation, resolved)
        token = _scoped_launch.set(scoped_launch)
        try:
            result = self._call(argv, SUBMIT_TIMEOUT)
        finally:
            _scoped_launch.reset(token)
        result["placement"] = placement.payload()
        return result

    def _check_claim(self, attempt: int, expires: float, task_id: str, claim_token: str, read: bool) -> str:
        from lazytools.connectors.projects.tools import _resolve_store_db
        from lazytools.projects.task_service import ProjectTaskService, TaskPrincipal

        principal, projects_db = self._task_settings()
        assert principal is not None
        service = ProjectTaskService(_resolve_store_db(projects_db))
        receipt = service.local_claim(TaskPrincipal.model_validate_json(principal), task_id, claim_token, state="intent")
        permitted_profile = "read" if read else "edit"
        if receipt["attempt"] != attempt or receipt["requested_profile"] != permitted_profile:
            raise PolicyDenied("code_bridge_submit", "The claim attempt or profile differs from the scoped request.")
        if receipt["lease_expires_at"] > expires:
            raise PolicyDenied("code_bridge_submit", "The claim lease exceeds the MCP task deadline.")
        return str(receipt["engine_family"])

    def _query(self, verb: str, job_id: str) -> dict[str, Any]:
        from lazytools.code_bridge import _cloud, _jobs, _store

        operation = f"code_bridge_{verb}"
        job_id = self._job_ref(job_id)
        parent = authorize(operation)
        db_path = _store.default_db_path()
        job = None
        if db_path.is_file():
            store = _store.build_store(db_path)
            job = _jobs.find_job(store, _store.build_job_registry(store), job_id)
        if job is None:
            return {"ok": False, "exit_code": 1, "error": f"no job found matching {job_id!r}"}
        cwd = job.get("cwd")
        if not cwd or not Path(cwd).resolve().is_relative_to(Path(self._root)):
            raise PolicyDenied(operation, "The job is outside the code root.")
        record_operation(operation, Path(cwd))
        context = current_task()
        if parent is not None:
            if _cloud.is_cloud(job):
                raise PolicyDenied(operation, "Cloud polling may perform effects outside the scoped task.")
            if context and (job.get("task_id"), job.get("task_attempt"), job.get("task_executor")) != (
                context.task_id, context.attempt, context.actor,
            ):
                raise PolicyDenied(operation, "The job does not match the authenticated task attempt.")
        return self._call([verb, str(job["job_id"]), "--json"], QUERY_TIMEOUT)

    def code_bridge_status(self, job_id: str) -> dict[str, Any]:
        """One job's current record (cloud jobs are polled first)."""
        return self._query("status", job_id)

    def code_bridge_result(self, job_id: str) -> dict[str, Any]:
        """One job's result or error, with the pushed branch, PR and review-comment fields for cloud jobs."""
        return self._query("result", job_id)

    def as_tools(self) -> list[Any]:
        from lazybridge import Tool

        tools = [
            Tool.wrap(
                self.code_bridge_submit,
                name="code_bridge_submit",
                description=(
                    "Hand a coding task to Codex or Claude Code through lazytools-code-bridge and return its job_id at "
                    "once (poll code_bridge_status / code_bridge_result). Args: task (str); cwd (str, repository "
                    "under the code root); engine ('codex'|'claude'; required unless review_of); where "
                    "('local'|'cloud'|'auto'; default from config; auto = cloud inside a configured busy window or "
                    "when no local slot is free); tier ('basic'|'writing'|'thinking'|'critical'; picks the Claude "
                    "cloud model, or the local model); model; branch. Cloud only (needs allow_cloud=true, which "
                    "acknowledges that the cloud agent runs outside the local approval gate and sees only the git "
                    "remote): pr (int, work on that PR's head branch), open_pr (land into a new branch + PR), "
                    "land (land automatically when finished: commit, push, update/open the PR), rebase_ok, "
                    "review_of (job id; runs the OPPOSITE engine and posts one independent-review comment on its "
                    "PR). Returns job_id, status and the placement decision."
                ),
            ),
            Tool.wrap(
                self.code_bridge_status,
                name="code_bridge_status",
                description=(
                    "Current status of a code-bridge job (cloud jobs are polled against the provider first). "
                    "Args: job_id (full id or unique prefix)."
                ),
            ),
            Tool.wrap(
                self.code_bridge_result,
                name="code_bridge_result",
                description=(
                    "A code-bridge job's full record: status, result or error, and for cloud jobs the pushed "
                    "branch (landed_branch, pushed_sha), pr_url and review comment. Args: job_id."
                ),
            ),
        ]
        return [code_tool(tool, root=Path(self._root)) for tool in tools]


__all__ = ["CodeBridgeTools"]
