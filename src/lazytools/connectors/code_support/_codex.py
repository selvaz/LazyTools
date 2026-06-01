"""Codex support agent — CLI and MCP-server modes.

Two ways to put Codex behind a LazyBridge agent:

* :func:`codex` (**CLI mode**) — shell out to ``codex exec`` and treat the CLI
  as a whole agent: one call = one delegated task, returns a result string.
* :func:`codex_mcp` (**MCP mode**) — run ``codex mcp-server`` and expose Codex's
  two agent-level MCP tools (``codex`` / ``codex-reply``) for *your* agent to
  orchestrate. Requires the ``mcp`` extra; the interface is experimental.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Iterable
from typing import TYPE_CHECKING

from lazytools.connectors.mcp import MCP

if TYPE_CHECKING:
    from lazytools.connectors.mcp import MCPServer

_log = logging.getLogger(__name__)

_SANDBOX_FLAGS: dict[str, list[str]] = {
    # `codex exec` only exposes the sandbox flag (-s / --sandbox); there is no
    # `-a` approval flag, so read-only sandbox is the whole story here.
    "read": ["-s", "read-only"],
    # --full-auto pairs workspace-write with a non-interactive approval policy,
    # so a step that needs approval never blocks waiting on stdin.
    "write": ["-s", "workspace-write", "--full-auto"],
}


def codex(
    task: str,
    *,
    mode: str = "read",
    cwd: str | None = None,
    resume_last: bool = False,
    timeout: float = 300.0,
    skip_git_check: bool = True,
) -> str:
    """Delegate a task to the Codex CLI and return the result as a string.

    Parameters
    ----------
    task:
        Instruction for Codex.
    mode:
        ``"read"`` (default) — read-only sandbox (``-s read-only``).
        ``"write"`` — workspace-write sandbox, full-auto (no interactive
        confirmation prompts). Ideally run inside a git repo.
    cwd:
        Working directory for the subprocess.
    resume_last:
        If True, continues the most recent Codex session in the working
        directory via ``exec resume --last``.
    timeout:
        Maximum seconds for the subprocess. Set ``tool_timeout=None`` on
        ``LLMEngine`` so the engine never cancels before the subprocess
        finishes (zombie-process hazard when engine fires first).
    skip_git_check:
        Pass ``--skip-git-repo-check``. Required outside a git repo.
        In ``mode="write"`` a git repo is recommended for reliable behaviour.
    """
    if mode not in _SANDBOX_FLAGS:
        return f"[codex] invalid mode={mode!r}. Use 'read' or 'write'."

    sandbox = _SANDBOX_FLAGS[mode]

    if resume_last:
        cmd = ["codex", "exec", "resume", "--last", task, *sandbox]
    else:
        cmd = ["codex", "exec", task, *sandbox]

    if skip_git_check:
        cmd.append("--skip-git-repo-check")

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return f"[codex] timeout after {timeout}s"
    except FileNotFoundError:
        return "[codex] CLI 'codex' not found in PATH — install OpenAI Codex CLI first"

    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        _log.error("codex exit %d: %s", proc.returncode, stderr)
        return f"[codex] error (exit {proc.returncode}): {stderr[:500]}"

    return proc.stdout.strip()


def codex_mcp(
    *,
    name: str = "codex",
    allow: Iterable[str] | None = None,
    deny: Iterable[str] | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    namespace: bool = True,
    prefix: str | None = None,
    cache_tools_ttl: float | None = 60.0,
) -> MCPServer:
    """Codex as an MCP server (``codex mcp-server``).

    Returns an :class:`~lazytools.connectors.mcp.MCPServer` exposing Codex's
    MCP interface over stdio. Drop it into
    ``Agent(tools=[codex_mcp(allow=["*"])])``.

    !!! warning
        Codex's MCP-server interface is **experimental** (per OpenAI's docs)
        and may change without notice. Pin your Codex version if you depend
        on the exposed tool shape.

    ``allow=`` (or ``deny=``) is **required** — deny-by-default. Patterns match
    the namespaced tool name (``codex.*``). Tool names are not hardcoded because
    they depend on the installed Codex version; discover them with
    ``allow=["*"]`` once.

    Parameters
    ----------
    name:
        Server name and default namespace prefix (``"codex"``).
    allow / deny:
        fnmatch globs against the namespaced tool name (deny-by-default).
    args:
        Extra args appended after ``mcp-server`` (rarely needed).
    env:
        Extra environment for the subprocess. Auth is otherwise inherited
        from ``codex login`` / the current shell environment.
    namespace / prefix / cache_tools_ttl:
        Forwarded to :meth:`MCP.stdio` unchanged.

    Requires the ``mcp`` extra: ``pip install lazytoolkit[mcp]``.
    """
    return MCP.stdio(
        name,
        command="codex",
        args=["mcp-server", *(args or [])],
        env=env,
        allow=allow,
        deny=deny,
        namespace=namespace,
        prefix=prefix,
        cache_tools_ttl=cache_tools_ttl,
    )
