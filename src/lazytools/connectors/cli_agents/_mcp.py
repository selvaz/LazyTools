"""MCP-server variants of the Claude Code and Codex agents.

The :func:`claude_code` / :func:`codex` functions in this package shell out to
the CLIs and treat each one as a *whole agent* — you hand it a task, it runs its
own loop, you get a result string. This module is the other integration shape:
both CLIs can also run as **MCP servers**, exposing their surface over the
Model Context Protocol so your own agent orchestrates them as ordinary tools.

  from lazytools.connectors.cli_agents import claude_code_mcp, codex_mcp

  agent = Agent("claude-opus-4-8", tools=[claude_code_mcp(allow=["*"])])

Each factory returns an :class:`~lazytools.connectors.mcp.MCPServer` (a tool
provider) built on the existing :meth:`MCP.stdio` transport — so installing the
``mcp`` extra (``pip install lazytoolkit[mcp]``) is required, deny-by-default
filtering applies, and the tools appear namespaced (``claude_code.*`` /
``codex.*``) in ``Agent(tools=[...])``.

CLI tool vs. MCP server — pick by relationship:

- **CLI tool** (:func:`claude_code`, :func:`codex`): the CLI is the agent. One
  call = one delegated task; returns the final result, plus session id / cost
  for Claude. Use when you want to *delegate*.
- **MCP server** (here): the CLI exposes its primitives; *your* agent drives
  them step by step. Use when you want the tool surface inside your own loop.

Launch commands (verified against the official docs):

- Claude Code: ``claude mcp serve`` — exposes Claude's own tools (View, Edit,
  LS, Bash, …). The MCP client is responsible for per-call confirmation.
- Codex: ``codex mcp-server`` — exposes Codex as an agent-style tool. Marked
  **experimental** by OpenAI; the interface may change.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from lazytools.connectors.mcp import MCP

if TYPE_CHECKING:
    from lazytools.connectors.mcp import MCPServer


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
