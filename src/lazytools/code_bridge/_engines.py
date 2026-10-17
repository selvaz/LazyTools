"""Engine construction for the async code bridge.

Same engines, same session-registry plumbing, same confinement style as
``lazytools.connectors.code_support`` -- this module is the bridge's own
construction seam (so tests can fake it the same way
``tests/_code_fakes.py`` fakes ``_review``/``_writer``'s), not a copy of
their logic: the writer-specific configuration (which built-in tools are
granted, which sandbox, which approval gate) mirrors
``lazybridge.ext.delegation.writers.make_codex_writer`` /
``make_claude_writer``, because those are built for a *background*,
Tool-wrapped delegate inside a persistent agent (see that module's own
``_track`` docstring) and this bridge runs its engine in the *foreground*
of a one-shot CLI process instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from lazybridge.engines.coding import ApprovalGate
    from lazybridge.engines.sessions import SessionRegistry

#: Same ceiling ``CodeWriteTools`` uses for a real, possibly multi-file
#: change -- the SDK/App-Server default is sized for a short back-and-forth.
DEFAULT_TIMEOUT = 3600.0

#: Claude Code's SDK default of 20 agentic turns ends a real objective
#: before a single file is written; see ``_writer.py``'s own constant.
DEFAULT_CLAUDE_MAX_TURNS = 60

#: Built-in tools granted to the Claude writer, gated (not pre-approved) by
#: the TieredGate -- same set ``make_claude_writer`` grants.
_CLAUDE_WRITE_TOOLS = ("Write", "Edit", "Bash")


def _make_codex_engine(**kwargs: Any) -> Any:
    """Build the ``CodexEngine`` for one job. Patched by tests (see ``_code_fakes.py``)."""
    from lazybridge.engines.codex import CodexEngine

    return CodexEngine(**kwargs)


def _make_claude_engine(**kwargs: Any) -> Any:
    """Build the ``ClaudeCodeEngine`` for one job. Patched by tests."""
    from lazybridge.engines.claude_code import ClaudeCodeEngine

    return ClaudeCodeEngine(**kwargs)


def claude_writer_instructions(temp_dir: str | None) -> str:
    """System instructions for the Claude writer (option B of the 17/10 approvals work).

    The approval gate matches ``Bash`` command TEXT, so file content written
    through a shell heredoc or ``echo >`` is matched as if it were commands
    (a script containing ``delete=False`` asks). Writing files with the
    Write/Edit tools avoids that and is already allowed. Hiding heredoc bodies
    from the gate instead was tried and removed: it could hide executed
    commands (see ``_approvals.BridgeGate``).
    """
    lines = [
        "File-writing rules for this job (they keep approval prompts for real decisions only):",
        "- Create and change files ONLY with the Write and Edit tools. Never write file content through"
        " the shell: no heredocs (cat > f <<EOF), no echo/printf redirection, no python -c that writes files.",
        "- Use Bash to run programs (tests, linters, git, your own helper scripts), not to author files.",
    ]
    if temp_dir:
        lines.append(
            f"- Put temporary helper files (scratch scripts, notes, outputs) under {temp_dir}, creating them"
            " with Write. That directory is yours for this job and is removed when it ends; do not leave"
            " helpers in the repository."
        )
    else:
        lines.append(
            "- No dedicated temp directory is available for this job: avoid temporary helper files in the"
            " repository where you can, and list any you create in your final answer."
        )
    return "\n".join(lines)


def build_codex_engine(
    *,
    cwd: str,
    gate: ApprovalGate,
    model: str | None,
    effort: str | None,
    thread_id: str | None,
    session_alias: str | None,
    session_registry: SessionRegistry,
    timeout: float = DEFAULT_TIMEOUT,
    temp_dir: str | None = None,
) -> Any:
    """A ``CodexEngine`` for one foreground job: workspace-write, on-request escalations gated.

    Live web search is set explicitly rather than inherited from the user's
    ``~/.codex/config.toml``, so a job can research what it is building
    whatever that file says.

    ``temp_dir`` -- the job's own temp directory (``_tempdir``), already
    vetted -- is the ONE extra writable root; ``None`` adds none. Sandbox
    NETWORK access is deliberately left off: with no network inside the
    sandbox, every ``git push`` and ``gh`` write has to escalate, and the
    escalation is what the approval gate sees. (Live web search is Codex's
    own tool, not shell network access.)"""
    import dataclasses

    from lazybridge.engines.coding import CodingAgentConfig

    config = CodingAgentConfig.writer(gate)
    codex_policy = dataclasses.replace(config.codex, web_search="live")
    if temp_dir:
        codex_policy = dataclasses.replace(codex_policy, writable_roots=(temp_dir,))
    config = dataclasses.replace(config, codex=codex_policy)
    return _make_codex_engine(
        model=model,
        cwd=cwd,
        reasoning_effort=effort,
        request_timeout=timeout,
        stream_idle_timeout=max(timeout * 2 / 3, 30.0),
        max_retries=0,
        thread_id=thread_id,
        persist_thread=True,
        session_alias=session_alias,
        session_registry=session_registry,
        config=config,
    )


def build_claude_engine(
    *,
    cwd: str,
    gate: ApprovalGate,
    model: str | None,
    effort: str | None,
    session_id: str | None,
    session_alias: str | None,
    session_registry: SessionRegistry,
    timeout: float = DEFAULT_TIMEOUT,
    temp_dir: str | None = None,
) -> Any:
    """A ``ClaudeCodeEngine`` for one foreground job: Write/Edit/Bash, every call gated.

    ``temp_dir`` (the job's own, vetted temp directory) becomes a second file
    root, so helper files can be created there with Write as the
    instructions ask (:func:`claude_writer_instructions`)."""
    from lazybridge.engines.coding import ClaudeCodePolicy, CodingAgentConfig

    config = CodingAgentConfig(
        claude=ClaudeCodePolicy(
            permission_mode="default",
            preapprove_application_tools=False,
            extra_tools=_CLAUDE_WRITE_TOOLS,
        ),
        approval_gate=gate,
    )
    kwargs: dict[str, Any] = {}
    if model:
        kwargs["model"] = model
    return _make_claude_engine(
        **kwargs,
        reasoning_effort=effort,
        cwd=cwd,
        file_roots=[cwd, temp_dir] if temp_dir else [cwd],
        system=claude_writer_instructions(temp_dir),
        # WebSearch/WebFetch on: jobs may look things up while they work.
        # Both are allowed by the bridge's rule table, so they run unprompted.
        web=True,
        max_turns=DEFAULT_CLAUDE_MAX_TURNS,
        session_id=session_id,
        session_alias=session_alias,
        session_registry=session_registry,
        persist_session=True,
        request_timeout=timeout,
        stream_idle_timeout=max(timeout * 2 / 3, 30.0),
        max_retries=0,
        config=config,
    )


def resolve_cwd(cwd: str, root: str | None) -> Path:
    """Resolve ``cwd`` against the confinement ``root`` and refuse to leave it.

    Reuses ``lazytools.connectors.code_support``'s own confinement helpers
    (same ``LAZYTOOLS_CODE_ROOT`` convention) rather than a second copy of
    the same escape check.
    """
    from lazytools.connectors.code_support._claude_review import _build_root
    from lazytools.connectors.code_support._review import _resolve_repo

    base = _build_root(root)
    return _resolve_repo(cwd, base)


__all__ = [
    "DEFAULT_CLAUDE_MAX_TURNS",
    "DEFAULT_TIMEOUT",
    "build_claude_engine",
    "build_codex_engine",
    "claude_writer_instructions",
    "resolve_cwd",
]
