"""Codex CLI tool — delegates a task to the codex CLI and returns the result."""

from __future__ import annotations

import logging
import subprocess

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
