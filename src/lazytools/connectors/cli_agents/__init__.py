"""CLI Agent connectors — delegate tasks to Claude Code and Codex CLIs.

Three drop-in tools for ``Agent(tools=[...])``:

* :func:`claude_code` — delegate a task to the Claude Code CLI.
* :func:`codex` — delegate a task to the Codex CLI.
* :func:`build_cli_collaboration` — the two of them collaborating, packaged as a
  single multi-agent pipeline tool.

  from lazytools.connectors.cli_agents import (
      claude_code, codex, build_cli_collaboration, check_clis_available,
  )

Auth notes:
- **Claude Code**: reads ``~/.claude/.credentials.json`` for
  ``CLAUDE_CODE_OAUTH_TOKEN``, or falls back to ``ANTHROPIC_API_KEY`` in the
  environment. No extra setup needed if you have an active Claude Code session.
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

from lazytools.connectors.cli_agents._claude_code import claude_code
from lazytools.connectors.cli_agents._codex import codex
from lazytools.connectors.cli_agents._collaboration import build_cli_collaboration


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
    "codex",
    "build_cli_collaboration",
    "check_clis_available",
]
