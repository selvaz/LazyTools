"""Claude Code support agent — CLI and MCP-server modes.

Two ways to put Claude Code behind a LazyBridge agent:

* :func:`claude_code` (**CLI mode**) — shell out to ``claude -p`` and treat the
  CLI as a whole agent: one call = one delegated task, returns a result string.
* :func:`claude_code_mcp` (**MCP mode**) — run ``claude mcp serve`` and expose
  Claude Code's own tools (View, Edit, LS, Bash, …) for *your* agent to
  orchestrate. Requires the ``mcp`` extra.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Iterable
from typing import TYPE_CHECKING

from lazytools.connectors.mcp import MCP

if TYPE_CHECKING:
    from lazytools.connectors.mcp import MCPServer

_log = logging.getLogger(__name__)

_TOOL_FLAGS: dict[str, list[str]] = {
    "read": ["--allowedTools", "Read,Bash,Grep,Glob"],
    "write": [
        "--allowedTools",
        "Read,Write,Edit,Bash,Grep,Glob",
        "--permission-mode",
        "acceptEdits",
    ],
    "plan": ["--permission-mode", "plan"],
}


def claude_code(
    task: str,
    *,
    mode: str = "read",
    cwd: str | None = None,
    session_id: str | None = None,
    timeout: float = 300.0,
) -> str:
    """Delegate a task to Claude Code CLI and return the result as a string.

    Parameters
    ----------
    task:
        Instruction for Claude Code.
    mode:
        ``"read"`` (default) — read-only analysis (Read, Bash, Grep, Glob).
        ``"write"`` — may edit files (acceptEdits permission mode).
        ``"plan"`` — plan mode, no file modifications.
    cwd:
        Working directory for the subprocess.
    session_id:
        If given, resumes an existing Claude Code session via ``--resume``.
    timeout:
        Maximum seconds for the subprocess. Set ``tool_timeout=None`` on
        ``LLMEngine`` so the engine never cancels before the subprocess
        finishes (zombie-process hazard when engine fires first).

    Notes
    -----
    Auth is left to the Claude Code CLI itself: it reads its own on-disk
    login (``~/.claude/.credentials.json``), and the subprocess inherits the
    current environment, so ``CLAUDE_CODE_OAUTH_TOKEN`` (from
    ``claude setup-token``) or ``ANTHROPIC_API_KEY`` are honoured if set. We do
    not synthesize ``CLAUDE_CODE_OAUTH_TOKEN`` from the credentials file — that
    env var is a token *string*, not the JSON store, and overriding it would
    break a valid disk login.
    """
    if mode not in _TOOL_FLAGS:
        return f"[claude_code] invalid mode={mode!r}. Use 'read', 'write', or 'plan'."

    cmd = ["claude", "-p", task, "--output-format", "json", *_TOOL_FLAGS[mode]]
    if session_id:
        cmd += ["--resume", session_id]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return f"[claude_code] timeout after {timeout}s"
    except FileNotFoundError:
        return "[claude_code] CLI 'claude' not found in PATH — install Claude Code first"

    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        _log.error("claude_code exit %d: %s", proc.returncode, stderr)
        return f"[claude_code] error (exit {proc.returncode}): {stderr[:500]}"

    try:
        data = json.loads(proc.stdout)
        if data.get("subtype") == "error":
            return f"[claude_code] {data.get('result', 'unknown error')}"
        return data.get("result", "")
    except json.JSONDecodeError:
        return proc.stdout.strip()


def claude_code_mcp(
    *,
    name: str = "claude_code",
    allow: Iterable[str] | None = None,
    deny: Iterable[str] | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    namespace: bool = True,
    prefix: str | None = None,
    cache_tools_ttl: float | None = 60.0,
) -> MCPServer:
    """Claude Code as an MCP server (``claude mcp serve``).

    Returns an :class:`~lazytools.connectors.mcp.MCPServer` exposing Claude
    Code's own tools (View, Edit, LS, Bash, …) over stdio. Drop it straight into
    ``Agent(tools=[claude_code_mcp(allow=["*"])])``.

    ``allow=`` (or ``deny=``) is **required** — deny-by-default, the same
    posture as :meth:`MCP.stdio`. The patterns match the *namespaced* tool
    names, e.g. ``allow=["claude_code.View", "claude_code.LS"]`` (or
    ``allow=["*"]`` after auditing the surface). Tool names are not hardcoded
    here because they are owned by the Claude Code version you have installed;
    discover them by running with ``allow=["*"]`` once and inspecting the map.

    Parameters
    ----------
    name:
        Server name and default namespace prefix (``"claude_code"``).
    allow / deny:
        fnmatch globs against the namespaced tool name (deny-by-default).
    args:
        Extra args appended after ``mcp serve`` (rarely needed).
    env:
        Extra environment for the subprocess. Auth is otherwise inherited
        from the parent environment / the CLI's own on-disk login.
    namespace / prefix / cache_tools_ttl:
        Forwarded to :meth:`MCP.stdio` unchanged.

    Requires the ``mcp`` extra: ``pip install lazytoolkit[mcp]``.
    """
    return MCP.stdio(
        name,
        command="claude",
        args=["mcp", "serve", *(args or [])],
        env=env,
        allow=allow,
        deny=deny,
        namespace=namespace,
        prefix=prefix,
        cache_tools_ttl=cache_tools_ttl,
    )
