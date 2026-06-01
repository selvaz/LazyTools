"""Code Support Agent — delegate coding work to Claude Code and Codex.

Two agents, each with two modes:

* **Claude Code** — :func:`claude_code` (CLI mode) and :func:`claude_code_mcp`
  (MCP-server mode).
* **Codex** — :func:`codex` (CLI mode) and :func:`codex_mcp` (MCP-server mode).

Plus :func:`build_cli_collaboration`, which makes the two collaborate
(Claude Code analyses → Codex critiques → synthesizer plans → executor
implements) as a single Agent tool.

  from lazytools.connectors.code_support import (
      claude_code, claude_code_mcp,
      codex, codex_mcp,
      build_cli_collaboration, check_clis_available,
  )

  agent = Agent("claude-opus-4-8", tools=[claude_code, codex])

**CLI mode vs. MCP mode.** In CLI mode the binary *is* the agent: one call is
one delegated task that returns a result string. In MCP mode the binary exposes
its tool surface over the Model Context Protocol and *your* agent orchestrates
it; the MCP factories need the ``mcp`` extra (``pip install lazytoolkit[mcp]``).

Auth notes (left entirely to each CLI — the connector passes no custom env):
- **Claude Code**: the CLI uses its own on-disk login
  (``~/.claude/.credentials.json``); the inherited environment still carries
  ``CLAUDE_CODE_OAUTH_TOKEN`` (the token string from ``claude setup-token``) or
  ``ANTHROPIC_API_KEY`` if set. No extra setup needed with an active session.
- **Codex**: uses the auth configured via ``codex login``; the subprocess
  inherits the current shell environment.

Timeout guidance:
  Set ``tool_timeout=None`` on ``LLMEngine`` (or a value strictly greater than
  the ``timeout`` you pass to each call). The engine's ``asyncio.wait_for``
  cancels the coroutine but cannot interrupt a ``subprocess.run`` running in a
  thread pool — if the engine fires first the subprocess becomes a zombie until
  its own timeout fires. Using ``tool_timeout=None`` delegates all timeout
  control to the subprocess.
"""

from __future__ import annotations

import shutil

from lazytools.connectors.code_support._claude_code import claude_code, claude_code_mcp
from lazytools.connectors.code_support._codex import codex, codex_mcp
from lazytools.connectors.code_support._collaboration import build_cli_collaboration


def check_clis_available() -> dict[str, bool]:
    """Return availability of 'claude' and 'codex' in PATH.

    Returns a ``{"claude": bool, "codex": bool}`` dict. Call this at startup
    to surface missing CLIs immediately rather than at the first tool call.
    """
    return {
        "claude": shutil.which("claude") is not None,
        "codex": shutil.which("codex") is not None,
    }


__all__ = [
    "claude_code",
    "claude_code_mcp",
    "codex",
    "codex_mcp",
    "build_cli_collaboration",
    "check_clis_available",
]
