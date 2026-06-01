"""Claude Code CLI tool — delegates a task to the claude CLI and returns the result."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

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


def _load_claude_env() -> dict[str, str]:
    """Return env dict with CLAUDE_CODE_OAUTH_TOKEN injected if available."""
    env = dict(os.environ)
    creds = Path.home() / ".claude" / ".credentials.json"
    if creds.exists():
        try:
            data = json.loads(creds.read_text())
            env["CLAUDE_CODE_OAUTH_TOKEN"] = json.dumps(data)
        except (json.JSONDecodeError, OSError):
            pass
    return env


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
            env=_load_claude_env(),
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
