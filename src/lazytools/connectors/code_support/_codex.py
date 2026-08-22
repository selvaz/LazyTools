"""Codex support agent — CLI and MCP-server modes.

Two ways to put Codex behind a LazyBridge agent:

* :func:`codex` (**CLI mode**) — shell out to ``codex exec`` and treat the CLI
  as a whole agent: one call = one delegated task. **Read-only**: the write
  capability lives in
  :class:`~lazytools.connectors.code_support.CodeWriteTools`, a gated provider
  you must construct explicitly — an orchestrating LLM cannot "choose" write
  mode through this function.
* :func:`codex_mcp` (**MCP mode**) — run ``codex mcp-server`` and expose Codex's
  two agent-level MCP tools (``codex`` / ``codex-reply``) for *your* agent to
  orchestrate. Requires the ``mcp`` extra; the interface is experimental.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from lazytools.connectors.mcp import MCP

if TYPE_CHECKING:
    from lazytools.connectors.mcp import MCPServer

_log = logging.getLogger(__name__)

#: ``codex exec`` only exposes the sandbox flag (-s / --sandbox); there is no
#: `-a` approval flag, so read-only sandbox is the whole story here.
_READ_FLAGS: list[str] = ["-s", "read-only"]

#: Write-mode flags — used only by ``CodeWriteTools`` (gated, sandboxed).
#: --full-auto pairs workspace-write with a non-interactive approval policy,
#: so a step that needs approval never blocks waiting on stdin.
_WRITE_FLAGS: list[str] = ["-s", "workspace-write", "--full-auto"]


def resolve_codex_bin() -> str | None:
    """Best-effort resolve the ``codex`` binary; ``None`` if none can be found.

    Prefers :func:`lazybridge.engines.codex.codex_executable` — it also finds
    the Codex desktop app's versioned install directory, which is never added
    to ``PATH`` (``%LOCALAPPDATA%\\OpenAI\\Codex\\bin\\<hash>\\codex.exe``) —
    over a bare :func:`shutil.which`, which only ever checked ``PATH``.

    Falls back to ``CODEX_BIN`` then :func:`shutil.which` when the resolver
    itself can't be imported: ``lazybridge.engines.codex`` is not available on
    every LazyBridge version this package supports (older pins lack it), and
    every caller here — including ``check_clis_available()`` and the MCP
    ``code_write`` provider — must keep working on those versions, just
    without the desktop-app-install-dir case. The error message below tells
    the user to set ``CODEX_BIN``, so this fallback must actually honor it
    too, not only the full resolver.
    """
    try:
        from lazybridge.engines.codex import codex_executable
    except ImportError:
        return os.environ.get("CODEX_BIN") or shutil.which("codex")
    try:
        return codex_executable()
    except FileNotFoundError:
        return None


def _run_codex(
    task: str,
    flags: list[str],
    *,
    cwd: str | None,
    resume_last: bool,
    skip_git_check: bool,
    timeout: float,
) -> str:
    """Run the ``codex`` CLI once and return its output (or an error string
    starting with ``[codex]``). Shared by the read-only tool and the gated
    writer.
    """
    codex_bin = resolve_codex_bin()
    if codex_bin is None:
        return (
            "[codex] CLI not found on PATH or in the Codex app install directory — "
            "install it (`npm install -g @openai/codex`) or set CODEX_BIN to its full path."
        )

    if resume_last:
        cmd = [codex_bin, "exec", "resume", "--last", task, *flags]
    else:
        cmd = [codex_bin, "exec", task, *flags]

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
        return f"[codex] resolved binary {codex_bin!r} could not be executed"

    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        _log.error("codex exit %d: %s", proc.returncode, stderr)
        return f"[codex] error (exit {proc.returncode}): {stderr[:500]}"

    return proc.stdout.strip()


def codex(
    task: str,
    *,
    cwd: str | None = None,
    resume_last: bool = False,
    timeout: float = 300.0,
    skip_git_check: bool = True,
) -> dict[str, Any] | str:
    """Delegate a read-only task to the Codex CLI (``-s read-only`` sandbox).

    Returns ``{"result": <text>, "content_is_untrusted": true}`` on success —
    the result is derived from whatever code/text the CLI read, so downstream
    consumers must treat it as third-party content. Connector-level failures
    (CLI missing, timeout, non-zero exit) return a plain ``"[codex] ..."``
    string.

    There is deliberately no write mode here: file edits live behind
    :class:`~lazytools.connectors.code_support.CodeWriteTools`, which requires
    an explicit ``base_dir`` sandbox and (by default) a one-shot confirmation
    per write call.

    Parameters
    ----------
    task:
        Instruction for Codex.
    cwd:
        Working directory for the subprocess. Read-only sandbox still reads
        anything the process user can read — point ``cwd`` at the project and
        prefer a low-privilege user on machines that hold secrets.
    resume_last:
        If True, continues the most recent Codex session in the working
        directory via ``exec resume --last``.
    timeout:
        Maximum seconds for the subprocess. Set ``tool_timeout=None`` on
        ``LLMEngine`` so the engine never cancels before the subprocess
        finishes (zombie-process hazard when engine fires first).
    skip_git_check:
        Pass ``--skip-git-repo-check``. Harmless in the read-only sandbox;
        the gated writer defaults this **off** so writes keep git as a
        recovery rail.
    """
    out = _run_codex(
        task,
        _READ_FLAGS,
        cwd=cwd,
        resume_last=resume_last,
        skip_git_check=skip_git_check,
        timeout=timeout,
    )
    if out.startswith("[codex]"):
        return out
    return {"result": out, "content_is_untrusted": True}


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

    Requires the ``mcp`` extra: ``pip install "lazytoolkit[mcp] @ git+https://github.com/selvaz/LazyTools.git"``.
    """
    return MCP.stdio(
        name,
        # resolve_codex_bin() also finds the Codex desktop app's un-PATH'd
        # install dir; falls back to the bare "codex" literal (the
        # pre-existing behavior) when nothing resolves, so MCP.stdio's own
        # launch failure still surfaces a normal "command not found" error.
        command=resolve_codex_bin() or "codex",
        args=["mcp-server", *(args or [])],
        env=env,
        allow=allow,
        deny=deny,
        namespace=namespace,
        prefix=prefix,
        cache_tools_ttl=cache_tools_ttl,
    )
