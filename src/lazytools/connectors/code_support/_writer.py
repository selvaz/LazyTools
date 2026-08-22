"""Gated write access to the coding CLIs — the only path to ``write`` mode.

The plain :func:`~lazytools.connectors.code_support.claude_code` /
:func:`~lazytools.connectors.code_support.codex` tools are read-only by
construction. ``CodeWriteTools`` is the capability boundary for everything
else: file edits and command execution. The same safety model as the Gmail
send tools, applied to the highest-risk action in the toolkit:

* **Capability, not argument.** Write access exists only if the developer
  constructs this provider and passes it in ``tools=[...]`` — an
  orchestrating LLM cannot reach write mode through a parameter.
* **``base_dir`` sandbox (mandatory).** Every write call runs with ``cwd``
  inside ``base_dir``; a ``cwd`` escaping it raises
  :class:`CodeWriteBlocked`. (This bounds where the CLI is *aimed*; the CLI's
  own sandbox — ``acceptEdits`` allowed-tools for Claude Code,
  ``workspace-write`` for Codex — bounds what it touches from there.)
* **One-shot confirmation (default on).** Each write call consumes one
  outstanding :meth:`CodeWriteTools.confirm_write` grant — approving one
  write never authorizes a flood, exactly like ``gmail_send``. Grants can be
  scope-bound to a task id. For autonomous pipelines, pass
  ``require_confirmation=False`` and rely on the ``base_dir`` sandbox plus a
  git checkout as the recovery rail.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lazytools.connectors.code_support._claude_code import _WRITE_FLAGS as _CLAUDE_WRITE_FLAGS
from lazytools.connectors.code_support._claude_code import _run_claude
from lazytools.connectors.code_support._codex import _WRITE_FLAGS as _CODEX_WRITE_FLAGS
from lazytools.connectors.code_support._codex import _run_codex
from lazytools.safety import ActionBlocked, ConfirmationGate, current_scope

if TYPE_CHECKING:
    from lazybridge import Tool


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
      ``codex=True``). ``codex exec`` is non-interactive by construction and
      has no separate approval flag to pair with the sandbox.

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
        Per-call subprocess timeout in seconds. As with the read tools, set
        ``tool_timeout=None`` on the engine so it never orphans a running CLI.
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
        timeout: float = 300.0,
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
                        "sandbox); session_id (str, optional — resume a session)."
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
                        "optional — must stay inside the sandbox); resume_last (bool)."
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

    async def _claude_write(
        self,
        task: str,
        cwd: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any] | str:
        # Async on purpose: the ambient task scope (lazytools.safety.context)
        # propagates into async tools only, so cwd check + grant consume run
        # here, in-context; only the blocking subprocess hops to a thread.
        run_cwd = self._checked_cwd(cwd)
        self._consume_grant("claude_code_write")
        out = await asyncio.to_thread(
            _run_claude, task, _CLAUDE_WRITE_FLAGS, cwd=run_cwd, session_id=session_id, timeout=self._timeout
        )
        if out.startswith("[claude_code]"):
            return out
        return {"result": out, "content_is_untrusted": True}

    async def _codex_write(
        self,
        task: str,
        cwd: str | None = None,
        resume_last: bool = False,
    ) -> dict[str, Any] | str:
        run_cwd = self._checked_cwd(cwd)
        self._consume_grant("codex_write")
        out = await asyncio.to_thread(
            _run_codex,
            task,
            _CODEX_WRITE_FLAGS,
            cwd=run_cwd,
            resume_last=resume_last,
            skip_git_check=self._codex_skip_git_check,
            timeout=self._timeout,
        )
        if out.startswith("[codex]"):
            return out
        return {"result": out, "content_is_untrusted": True}
