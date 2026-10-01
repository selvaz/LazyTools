"""Gated write access to Claude Code and Codex — the only path to ``write`` mode.

The plain :func:`~lazytools.connectors.code_support.claude_code` /
:func:`~lazytools.connectors.code_support.codex` tools are read-only by
construction. ``CodeWriteTools`` is the capability boundary for everything
else: file edits and command execution. The same safety model as the Gmail
send tools, applied to the highest-risk action in the toolkit.

Both writers run on the LazyBridge engines (``ClaudeCodeEngine`` /
``CodexEngine``), not on CLI subprocesses, so they share the review tools'
per-call ``model`` / ``effort`` and their durable, renamable ``session_name``
(LazyBridge's ``SessionRegistry``, scoped to the call's working directory).

* **Capability, not argument.** Write access exists only if the developer
  constructs this provider and passes it in ``tools=[...]`` — an
  orchestrating LLM cannot reach write mode through a parameter.
* **``base_dir`` sandbox (mandatory).** Every write call runs with ``cwd``
  inside ``base_dir``; a ``cwd`` escaping it raises
  :class:`CodeWriteBlocked`. (This bounds where the agent is *aimed*; the
  engine's own sandbox — ``acceptEdits`` plus ``file_roots`` for Claude Code,
  ``workspace-write`` for Codex — bounds what it touches from there. Claude
  Code's ``Bash`` cannot be path-confined; see ``claude_bash``.)
* **One-shot confirmation (default on).** Each write call consumes one
  outstanding :meth:`CodeWriteTools.confirm_write` grant — approving one
  write never authorizes a flood, exactly like ``gmail_send``. Grants can be
  scope-bound to a task id. For autonomous pipelines, pass
  ``require_confirmation=False`` and rely on the ``base_dir`` sandbox plus a
  git checkout as the recovery rail.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from lazytools.connectors.code_support._claude_review import _make_claude_engine
from lazytools.connectors.code_support._common import (
    CLAUDE_EFFORTS,
    CODEX_EFFORTS,
    check_effort,
    check_session_name,
    clean,
)
from lazytools.connectors.code_support._common import session_registry as _session_registry
from lazytools.connectors.code_support._review import _make_codex_engine, _session_header
from lazytools.safety import ActionBlocked, ConfirmationGate, current_scope

if TYPE_CHECKING:
    from lazybridge import Tool
    from lazybridge.engines.coding import ApprovalDecision, ApprovalRequest
    from lazybridge.engines.sessions import SessionRegistry


#: One hour. Was five minutes for writes and fifteen for reviews, which
#: is shorter than a real refactor across a large repository: a long job
#: was killed mid-work and the caller was told only that the call
#: failed -- indistinguishable from a job that never started. Override
#: per deployment with LAZYTOOLS_CODE_WRITE_TIMEOUT /
#: LAZYTOOLS_CODE_REVIEW_TIMEOUT.
DEFAULT_TIMEOUT = 3600.0


#: Claude's writer needs room to finish a real change: the SDK default of 20
#: turns ended live objectives before a single file was written.
DEFAULT_CLAUDE_WRITE_MAX_TURNS = 60

#: Built-in tools the parity approval gate lets the Claude writer use. This is
#: exactly what the old ``claude -p`` writer pre-approved (read/search, edit,
#: and an unrestricted shell); anything else is denied.
_CLAUDE_WRITE_ALLOWED = frozenset({"Read", "Glob", "Grep", "Write", "Edit", "Bash"})


def _claude_writer_gate(request: ApprovalRequest) -> ApprovalDecision:
    """Approval policy for the Claude writer: the old CLI's tool set, nothing wider.

    Only ``Bash`` actually reaches this gate under ``acceptEdits`` (file edits
    are auto-approved, and confined to ``cwd`` by ``file_roots``); the
    allow-list exists so that a tool outside the writer's documented surface is
    refused rather than waved through.
    """
    from lazybridge.engines.coding import ApprovalDecision

    if request.name in _CLAUDE_WRITE_ALLOWED:
        return ApprovalDecision.allow()
    return ApprovalDecision.deny(f"{request.name!r} is not part of the claude_code_write tool surface")


def _inside_git_repo(path: Path) -> bool:
    """True when ``path`` or one of its parents holds a ``.git`` (dir or worktree file)."""
    return any((candidate / ".git").exists() for candidate in (path, *path.parents))


class CodeWriteBlocked(ActionBlocked):
    """A write call was blocked (no confirmation, or cwd outside base_dir)."""


class CodeWriteTools:
    """Tool provider for **gated, sandboxed** writes via Claude Code / Codex.

    Synopsis::

        from lazytools.connectors.code_support import CodeWriteTools, claude_code

        writer = CodeWriteTools(base_dir="/path/to/project")
        agent = Agent(engine=..., tools=[claude_code, writer])

        writer.confirm_write()             # human approves exactly ONE write call
        agent("fix the failing test")      # the agent may now call claude_code_write once

    Tools exposed by :meth:`as_tools`:

    * ``claude_code_write`` — Claude Code with edits + Bash (``acceptEdits``).
    * ``codex_write`` — Codex with the ``workspace-write`` sandbox (only when
      ``codex=True``), non-interactive (``approval_policy="never"``).

    Both take, per call, ``model`` and ``effort`` (default: the provider's own)
    and ``session_name`` — a renamable alias for a durable session, kept in
    LazyBridge's ``SessionRegistry`` and scoped to the call's working
    directory, so a later call with the same name continues the same
    conversation. ``session_id`` (Claude) / ``thread_id`` (Codex) keep
    working; an explicit id wins and rebinds the alias. Manage the names with
    the ``code_sessions_*`` tools.

    **Behaviour differences from the former CLI writers.** Claude Code's
    ``Write``/``Edit`` are now confined to the call's ``cwd`` (``file_roots``);
    ``Bash`` stays unconfined, exactly as before, and is approved by a gate that
    allows only Read/Glob/Grep/Write/Edit/Bash -- pass ``claude_bash=False`` to
    drop ``Bash`` altogether. Claude runs at most 60 turns and never retries a
    write. The default Claude model is the engine's (``"sonnet"``).
    ``resume_last`` is gone from ``codex_write``; name the session instead.

    Parameters
    ----------
    base_dir:
        Mandatory sandbox root. Must exist. Every write call's ``cwd`` must
        resolve inside it.
    default_cwd:
        Where a write call runs when its own ``cwd`` argument is omitted.
        Relative to ``base_dir``, same resolution/confinement rules as a
        per-call ``cwd``. Defaults to ``base_dir`` itself — set this when
        writes should land in a specific project by default (e.g. the one
        the orchestrating agent is actually working in) rather than the
        (possibly much wider) sandbox root, so a caller that forgets to pass
        ``cwd`` doesn't silently operate somewhere unexpected.
    claude / codex:
        Which writer tools :meth:`as_tools` exposes (default: Claude Code
        only — add Codex deliberately).
    require_confirmation:
        When ``True`` (default), each write call must consume one outstanding
        :meth:`confirm_write` grant or it raises :class:`CodeWriteBlocked`.
        Set ``False`` only for autonomous pipelines running in a disposable /
        git-tracked ``base_dir``.
    codex_skip_git_check:
        Default ``False``: Codex writes refuse to run outside a git repo so
        there is always a recovery rail. Flip only for throwaway directories.
    timeout:
        Per-call budget in seconds for the whole run (engine ``request_timeout``;
        the idle-stream watchdog is two thirds of it).
    claude_bash:
        Default ``True`` (parity with the former CLI writer): Claude may run
        shell commands, which cannot be confined to ``cwd``. ``False`` leaves
        Claude with Read/Glob/Grep/Write/Edit only, all confined to ``cwd``.
    session_registry:
        Where ``session_name`` aliases live; default is LazyBridge's
        (``$LAZYBRIDGE_SESSIONS_FILE`` or ``~/.lazybridge/sessions.json``).
        Mainly for tests.
    """

    _is_lazy_tool_provider = True

    def __init__(
        self,
        *,
        base_dir: str,
        default_cwd: str | None = None,
        claude: bool = True,
        codex: bool = False,
        require_confirmation: bool = True,
        codex_skip_git_check: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
        claude_bash: bool = True,
        session_registry: SessionRegistry | None = None,
    ) -> None:
        root = Path(base_dir).resolve()
        if not root.is_dir():
            raise ValueError(f"CodeWriteTools(base_dir={base_dir!r}): not an existing directory")
        self._base_dir = root
        # Stored as the raw, unresolved component (not a cached resolved Path):
        # _checked_cwd re-resolves it against base_dir on every call, so a
        # symlink swapped in after construction can't bypass the sandbox check
        # with a stale resolution. Validate eagerly so bad config fails fast.
        self._default_cwd = default_cwd
        if default_cwd:
            self._checked_cwd(default_cwd)
        self._claude = claude
        self._codex = codex
        self._gate = ConfirmationGate(enabled=require_confirmation)
        self._codex_skip_git_check = codex_skip_git_check
        self._timeout = timeout
        self._claude_bash = claude_bash
        self._session_registry = session_registry

    # ------------------------------------------------------------------ #
    # Confirmation surface (mirrors GmailTools)
    # ------------------------------------------------------------------ #
    @property
    def require_confirmation(self) -> bool:
        """Whether a write call needs an outstanding confirmation."""
        return self._gate.enabled

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def confirm_write(self, *, task_id: str | None = None) -> None:
        """Authorize exactly **one** write call (optionally bound to a task).

        Each grant is consumed by a single ``claude_code_write`` /
        ``codex_write`` invocation. Grant N times for N calls. Pass
        ``task_id=`` to bind the grant to one running task so a concurrent
        task cannot spend it.
        """
        self._gate.grant_any(scope=task_id)

    # ------------------------------------------------------------------ #
    # ToolProvider
    # ------------------------------------------------------------------ #
    def as_tools(self) -> list[Tool]:
        from lazybridge import Tool

        tools: list[Tool] = []
        if self._claude:
            tools.append(
                Tool.wrap(
                    self._claude_write,
                    name="claude_code_write",
                    description=(
                        "Delegate a coding task to Claude Code WITH write access "
                        "(file edits + commands), sandboxed to the configured project "
                        "directory. Requires an outstanding write confirmation unless "
                        "the provider was built with require_confirmation=False. Omitting "
                        "cwd runs in the provider's configured default directory, not "
                        "necessarily the sandbox root — pass cwd explicitly to target a "
                        "specific project when the sandbox spans more than one. "
                        "Args: task (str); cwd (str, optional — must stay inside the "
                        "sandbox); model (str, optional — default: Claude Code's); effort "
                        "(str, optional — low/medium/high/xhigh/max); session_name (str, "
                        "optional — a name for a durable session: reuse it to continue the "
                        "same conversation, letters/digits/._- starting with a letter); "
                        "session_id (str, optional — resume a native session; wins over "
                        "session_name). File writes stay inside cwd; shell commands are not "
                        "confined."
                    ),
                )
            )
        if self._codex:
            tools.append(
                Tool.wrap(
                    self._codex_write,
                    name="codex_write",
                    description=(
                        "Delegate a coding task to Codex WITH write access "
                        "(workspace-write sandbox), sandboxed to the "
                        "configured project directory. Requires an outstanding write "
                        "confirmation unless the provider was built with "
                        "require_confirmation=False. Omitting cwd runs in the provider's "
                        "configured default directory, not necessarily the sandbox root — "
                        "pass cwd explicitly to target a specific project when the sandbox "
                        "spans more than one. Args: task (str); cwd (str, "
                        "optional — must stay inside the sandbox); model (str, optional — "
                        "default: Codex's); effort (str, optional — none/minimal/low/medium/"
                        "high/xhigh/max); session_name (str, optional — a name for a durable "
                        "thread: reuse it to continue the same conversation, letters/digits/"
                        "._- starting with a letter); thread_id (str, optional — resume a "
                        "native thread; wins over session_name)."
                    ),
                )
            )
        return tools

    # ------------------------------------------------------------------ #
    # Guarded implementations
    # ------------------------------------------------------------------ #
    def _checked_cwd(self, cwd: str | None) -> str:
        # No per-call cwd: fall back to the configured default_cwd (itself
        # base_dir when unset) rather than always resolving to base_dir — so a
        # caller that omits cwd lands where the writer was pointed at
        # construction time, not implicitly at the (possibly much wider)
        # sandbox root. Resolved fresh here rather than cached, so a symlink
        # swapped in after construction is caught by the check below instead
        # of silently trusting a stale resolution.
        component = cwd or getattr(self, "_default_cwd", None)
        resolved = (self._base_dir / component).resolve() if component else self._base_dir
        if not resolved.is_relative_to(self._base_dir):
            raise CodeWriteBlocked(
                f"write blocked: cwd {component!r} resolves outside base_dir {str(self._base_dir)!r}"
            )
        if not resolved.is_dir():
            raise CodeWriteBlocked(f"write blocked: cwd {component!r} is not a directory inside the sandbox")
        return str(resolved)

    def _consume_grant(self, tool_name: str) -> None:
        if not self._gate.consume("write", scope=current_scope()):
            raise CodeWriteBlocked(
                f"{tool_name} blocked: no outstanding write confirmation. "
                "A human must call CodeWriteTools.confirm_write() first — "
                "one grant authorizes exactly one write call."
            )

    def _failure(
        self,
        label: str,
        run_cwd: str,
        handle_kind: str,
        handle: str | None,
        name: str | None,
        reason: str,
    ) -> str:
        """``[label] failed in <cwd> (handle, session_name): reason`` -- the handle survives failure."""
        parts = []
        if handle:
            parts.append(f"{handle_kind}={handle}")
        if name:
            parts.append(f"session_name={name}")
        where = f" ({', '.join(parts)})" if parts else ""
        return f"[{label}] failed in {run_cwd}{where}: {reason}"

    def _stream_idle(self) -> float:
        return max(self._timeout * 2 / 3, 30.0)

    async def _claude_write(
        self,
        task: str,
        cwd: str | None = None,
        session_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        session_name: str | None = None,
    ) -> dict[str, Any] | str:
        # Async on purpose: the ambient task scope (lazytools.safety.context)
        # propagates into async tools only, so cwd check + grant consume run
        # here, in-context. Arguments are validated BEFORE the grant is spent:
        # a mistyped effort or alias must not burn a human's one-shot approval.
        from lazybridge import Agent
        from lazybridge.engines.coding import ClaudeCodePolicy, CodingAgentConfig

        run_cwd = self._checked_cwd(cwd)
        effort = check_effort(effort, CLAUDE_EFFORTS, provider="Claude Code")
        reg = _session_registry(self._session_registry)
        alias = check_session_name("claude", run_cwd, session_name, reg)
        self._consume_grant("claude_code_write")

        tools = ("Write", "Edit", "Bash") if self._claude_bash else ("Write", "Edit")
        config = CodingAgentConfig(
            claude=ClaudeCodePolicy(permission_mode="acceptEdits", extra_tools=tools),
            approval_gate=_claude_writer_gate if self._claude_bash else None,
        )
        kwargs: dict[str, Any] = {}
        if clean(model):
            kwargs["model"] = clean(model)
        resumed = clean(session_id)
        engine = _make_claude_engine(
            **kwargs,
            reasoning_effort=effort,
            session_alias=alias,
            session_registry=reg,
            cwd=run_cwd,
            file_roots=[run_cwd],
            web=False,
            max_turns=DEFAULT_CLAUDE_WRITE_MAX_TURNS,
            session_id=resumed,
            persist_session=True,
            # The default 120 s bounds the whole run *including retries*, and a
            # retry would replay a write that may already have landed.
            request_timeout=self._timeout,
            stream_idle_timeout=self._stream_idle(),
            max_retries=0,
            config=config,
        )
        try:
            env: Any = await Agent(engine, name="claude_code_write").run(task)
        except Exception as exc:  # the handle matters most when the run blew up
            return self._failure(
                "claude_code",
                run_cwd,
                "session_id",
                engine.session_id or resumed,
                alias,
                f"{type(exc).__name__}: {exc}",
            )
        handle = engine.session_id or resumed
        if not env.ok:
            message = env.error.message if env.error else "unknown error"
            return self._failure("claude_code", run_cwd, "session_id", handle, alias, message)
        header = _session_header("claude_code", Path(run_cwd), handle or "", "session_id", alias)
        return {"result": f"{header}\n\n{env.text()}", "content_is_untrusted": True}

    async def _codex_write(
        self,
        task: str,
        cwd: str | None = None,
        thread_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        session_name: str | None = None,
    ) -> dict[str, Any] | str:
        from lazybridge import Agent
        from lazybridge.engines.coding import CodexPolicy, CodingAgentConfig

        run_cwd = self._checked_cwd(cwd)
        effort = check_effort(effort, CODEX_EFFORTS, provider="Codex")
        reg = _session_registry(self._session_registry)
        alias = check_session_name("codex", run_cwd, session_name, reg)
        self._consume_grant("codex_write")
        # The Codex App Server has no --skip-git-repo-check: the recovery rail
        # the CLI writer enforced is enforced here instead.
        if not self._codex_skip_git_check and not _inside_git_repo(Path(run_cwd)):
            return (
                f"[codex] refusing to write in {run_cwd}: not inside a git repository "
                "(no recovery rail). Run `git init` there, or build CodeWriteTools with "
                "codex_skip_git_check=True for a throwaway directory."
            )

        resumed = clean(thread_id)
        engine = _make_codex_engine(
            model=clean(model),
            cwd=run_cwd,
            reasoning_effort=effort,
            request_timeout=self._timeout,
            stream_idle_timeout=self._stream_idle(),
            max_retries=0,
            thread_id=resumed,
            persist_thread=True,
            session_alias=alias,
            session_registry=reg,
            config=CodingAgentConfig(codex=CodexPolicy(sandbox="workspace-write", approval_policy="never")),
        )
        try:
            env: Any = await Agent(engine, name="codex_write").run(task)
        except Exception as exc:
            return self._failure(
                "codex", run_cwd, "thread_id", engine.thread_id or resumed, alias, f"{type(exc).__name__}: {exc}"
            )
        handle = engine.thread_id or resumed
        if not env.ok:
            message = env.error.message if env.error else "unknown error"
            return self._failure("codex", run_cwd, "thread_id", handle, alias, message)
        header = _session_header("codex", Path(run_cwd), handle or "", "thread_id", alias)
        return {"result": f"{header}\n\n{env.text()}", "content_is_untrusted": True}
