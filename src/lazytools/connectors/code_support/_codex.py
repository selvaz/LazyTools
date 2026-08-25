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
#: ``codex exec`` has no ``--full-auto``/``-a`` flag at all on current Codex
#: CLI builds (verified live against ``codex exec --help``: it exposes only
#: ``-s/--sandbox`` and generic ``-c key=value`` overrides; passing
#: ``--full-auto`` is a hard argument-parsing error). ``exec`` defaults to
#: ``approval_policy=never`` on its own — verified live, including a task
#: that runs a shell command, which completed without blocking — but that
#: default could differ on another CLI version or a user's own
#: ``~/.codex/config.toml``. ``-c approval_policy=never`` pins it explicitly,
#: which is what ``--full-auto`` was originally meant to guarantee: pairing
#: the sandbox with a policy that never blocks waiting on stdin.
_WRITE_FLAGS: list[str] = ["-s", "workspace-write", "-c", "approval_policy=never"]


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

    # `-s`/`-c`/`--skip-git-repo-check` are flags of `codex exec` ITSELF, not
    # of its `resume` subcommand. `codex exec <task> -s ... --skip...` works
    # because `exec`'s own parser accepts them after the positional task —
    # verified live. But once `resume` is on the command line, the CLI hands
    # remaining argv to `resume`'s OWN parser, which has none of these flags:
    # `codex exec resume --last <task> -s workspace-write` fails immediately
    # with "unexpected argument '-s'" (found live — this is why resume_last
    # looked like a hang from the caller's side: the actual failure was
    # instant, but arrived shaped like every other error string, easy to
    # miss next to the *real* hang below). Every exec-level flag therefore
    # has to be placed BEFORE the `resume` subcommand, never after.
    if resume_last:
        cmd = [codex_bin, "exec", *flags]
        if skip_git_check:
            cmd.append("--skip-git-repo-check")
        cmd += ["resume", "--last", task]
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
            # Found live: without this, `codex exec` inherits this process's
            # stdin. In a non-interactive MCP server, nothing is ever there
            # to answer a prompt this CLI build might emit (auth, a config
            # migration notice, a trust dialog, anything version-specific)
            # — the subprocess blocks on a read that can never complete,
            # burning the full `timeout` with zero output and no partial
            # result, indistinguishable from the process just being slow.
            # DEVNULL makes any such read fail/return EOF immediately
            # instead of hanging, so a real prompt surfaces as a fast error
            # (or a `--skip-git-repo-check`-style CLI question about how to
            # proceed) rather than a silent full-timeout hang.
            stdin=subprocess.DEVNULL,
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
    # resolve_codex_bin() also finds the Codex desktop app's un-PATH'd install
    # dir, but it resolves against *this* process's environment. A caller
    # passing env={"PATH": ...} to MCP.stdio is explicitly selecting which
    # Codex install the child subprocess should see, so that override must be
    # searched explicitly rather than resolved from the parent's environment
    # -- and, on Windows, rather than left for the child to resolve on its
    # own: CreateProcess resolves a bare command name against the *calling*
    # process's PATH/search rules, not the child env's PATH, so a bare
    # "codex" would silently launch whatever this process's real PATH finds
    # instead of failing, ignoring the override either way. Verified live:
    # subprocess.run(["foo"], env={"PATH": <dir containing only foo.cmd>})
    # raises FileNotFoundError on this platform. The same reasoning applies
    # if the override itself finds nothing: falling back to a bare "codex"
    # would let CreateProcess silently resolve a *different* install from
    # this process's real PATH, so that failure is raised instead.
    #
    # PATH's key is matched case-insensitively only on Windows, where env var
    # names are themselves case-insensitive and "Path" is the common
    # spelling: on POSIX, names are case-sensitive, so an unrelated "Path"
    # entry must not be misread as a PATH override.
    key_matches = (lambda k: k.upper() == "PATH") if os.name == "nt" else (lambda k: k == "PATH")
    path_override = next((v for k, v in (env or {}).items() if key_matches(k)), None)
    if path_override is not None:
        resolved = shutil.which("codex", path=path_override)
        if resolved is None:
            raise FileNotFoundError(f"codex CLI not found on the overridden PATH {path_override!r}")
        # shutil.which() can return a relative path when path_override itself
        # has a relative entry (e.g. "tools" or "."). MCP.stdio launches the
        # subprocess lazily, so a relative command's meaning could change if
        # the process's cwd changes between now and the first tool call --
        # normalize against *this* moment's cwd instead.
        command = os.path.abspath(resolved)
    else:
        command = resolve_codex_bin() or "codex"
    return MCP.stdio(
        name,
        command=command,
        args=["mcp-server", *(args or [])],
        env=env,
        allow=allow,
        deny=deny,
        namespace=namespace,
        prefix=prefix,
        cache_tools_ttl=cache_tools_ttl,
    )
