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

Cloud jobs need ``allow_cloud=true`` (the CLI's ``--allow-cloud``). Approving a
gated action up front (``--approve-cloud``) is deliberately not exposed: a
brief that asks for a push, merge or delete is refused with the CLI's message,
and a person decides.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

SUBMIT_TIMEOUT = 900.0
QUERY_TIMEOUT = 600.0
_JOB_REF_RE = re.compile(r"[0-9A-Za-z-]{4,64}")


class CodeBridgeTools:
    """Submit / status / result for code-bridge jobs, local or cloud."""

    _is_lazy_tool_provider = True  # how lazytools.mcp_server.expand_tools recognises a provider

    def __init__(self, root: str | None = None) -> None:
        self._root = str(Path(root or Path.cwd()).expanduser().resolve())

    # -- plumbing ---------------------------------------------------------- #

    def _invoke(self, argv: list[str], timeout: float) -> tuple[int, str, str]:
        """Run one bridge CLI command in a child process. Patched in tests."""
        proc = subprocess.run(
            [sys.executable, "-m", "lazytools.code_bridge", *argv], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
        )
        return proc.returncode, proc.stdout, proc.stderr

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
    ) -> dict[str, Any]:
        """Submit a coding job; returns at once with its ``job_id``."""
        from lazytools.code_bridge import _cloud, _cloud_place

        if not isinstance(task, str) or (not task.strip() and not review_of):
            raise ValueError("task is required (except for a cloud review_of)")
        if task.lstrip().startswith("@"):
            raise ValueError("a task may not start with '@' here (the CLI would read it as a file path)")
        config = _cloud.load_config()
        placement = _cloud_place.decide(where or config.default_where, windows=config.busy_windows, engine=engine)
        argv = ["run", "--where", placement.where, "--cwd", cwd or ".", "--root", self._root, "--json"]
        if task.strip():
            argv += ["--task", task]
        for flag, value in (("--engine", engine), ("--tier", tier), ("--model", model), ("--branch", branch),
                            ("--review-of", review_of)):
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
        result = self._call(argv, SUBMIT_TIMEOUT)
        result["placement"] = placement.payload()
        return result

    def code_bridge_status(self, job_id: str) -> dict[str, Any]:
        """One job's current record (cloud jobs are polled first)."""
        return self._call(["status", self._job_ref(job_id), "--json"], QUERY_TIMEOUT)

    def code_bridge_result(self, job_id: str) -> dict[str, Any]:
        """One job's result or error, with the pushed branch, PR and review-comment fields for cloud jobs."""
        return self._call(["result", self._job_ref(job_id), "--json"], QUERY_TIMEOUT)

    def as_tools(self) -> list[Any]:
        from lazybridge import Tool

        return [
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


__all__ = ["CodeBridgeTools"]
