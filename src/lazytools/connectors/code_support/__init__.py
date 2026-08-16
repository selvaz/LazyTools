"""Code Support Agent — delegate coding work to Claude Code and Codex.

**Capability model (read-only by default).** ``claude_code`` and ``codex``
are read/plan-only; the LLM cannot reach write mode through an argument.
Writes live behind :class:`CodeWriteTools` — a provider you construct
explicitly with a mandatory ``base_dir`` sandbox and (by default) a one-shot
``confirm_write()`` gate per write call, mirroring the Gmail send tools.

Two agents, each with two modes:

* **Claude Code** — :func:`claude_code` (CLI mode) and :func:`claude_code_mcp`
  (MCP-server mode).
* **Codex** — :func:`codex` (CLI mode) and :func:`codex_mcp` (MCP-server mode).

Plus :func:`codex_reviewer` — Codex as the *engine* of a LazyBridge agent
(``CodexEngine`` over ``codex app-server``) pinned to a reviewer prompt and
exposed as a single ``codex_code_review(task, repo_path, ...)`` tool. This is
the shape the LazyTools MCP server mounts (provider id ``code_review``) so an
MCP host can hand a review to Codex; unlike the two functions above it takes a
repository path per call, since a review has to happen somewhere.

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
from lazytools.connectors.code_support._claude_review import (
    CLAUDE_CONSULTANT_SYSTEM,
    CLAUDE_REVIEWER_SYSTEM,
    claude_consultant,
    claude_reviewer,
)
from lazytools.connectors.code_support._codex import codex, codex_mcp
from lazytools.connectors.code_support._collaboration import build_cli_collaboration
from lazytools.connectors.code_support._review import (
    CODE_CONSULTANT_SYSTEM,
    CODE_REVIEWER_SYSTEM,
    DEFAULT_REVIEW_TIMEOUT,
    codex_consultant,
    codex_native_reviewer,
    codex_reviewer,
)
from lazytools.connectors.code_support._writer import CodeWriteBlocked, CodeWriteTools


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
    "CLAUDE_CONSULTANT_SYSTEM",
    "CLAUDE_REVIEWER_SYSTEM",
    "CODE_CONSULTANT_SYSTEM",
    "CODE_REVIEWER_SYSTEM",
    "DEFAULT_REVIEW_TIMEOUT",
    "CodeWriteBlocked",
    "CodeWriteTools",
    "claude_code",
    "claude_code_mcp",
    "claude_consultant",
    "claude_reviewer",
    "codex",
    "codex_consultant",
    "codex_mcp",
    "codex_native_reviewer",
    "codex_reviewer",
    "build_cli_collaboration",
    "check_clis_available",
]
