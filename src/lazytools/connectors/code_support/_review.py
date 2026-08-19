"""Codex-backed code *review* — a LazyBridge ``Agent`` behind one tool.

This is the third way to put Codex behind an agent, alongside :func:`~
lazytools.connectors.code_support.codex` (CLI mode: shell out to ``codex
exec``) and :func:`~lazytools.connectors.code_support.codex_mcp` (MCP mode:
orchestrate ``codex mcp-server``'s two tools yourself):

* here Codex is the **engine of a real LazyBridge agent**
  (:class:`lazybridge.engines.codex.CodexEngine` talking JSON-RPC to ``codex
  app-server``), pinned to a reviewer system prompt. One call = one review,
  returned as text.

Why a plain function and not ``Tool.wrap(agent)``: an agent wrapped as a tool
takes exactly one ``task: str`` argument, but a reviewer needs to be pointed at
a *directory* — Codex reads files and runs ``git`` itself, relative to the
engine's ``cwd``, which is fixed at construction. So the tool takes
``repo_path`` and builds the agent per call; everything else about it is an
ordinary LazyBridge agent.

Safety: the engine runs with :class:`~lazybridge.engines.coding.CodexPolicy`'s
defaults — ``sandbox="read-only"``, ``approval_policy="never"`` — so the
reviewer can read files and run commands like ``git diff``, but cannot write
to the repository, and no approval prompt can ever block the (non-interactive)
MCP transport. Writes stay where they already live: the explicitly constructed,
sandboxed :class:`~lazytools.connectors.code_support.CodeWriteTools`.

``repo_path`` is additionally confined to ``root`` (the server process' cwd by
default, or ``LAZYTOOLS_CODE_ROOT``): a caller cannot walk the reviewer out to
an arbitrary directory on the host, even read-only.
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from lazybridge import Tool

#: Developer instructions handed to Codex for every review. Deliberately
#: opinionated about *what not to report*: an unfiltered "list everything
#: suspicious" review is noise, and the caller (usually another agent) has no
#: way to tell a real defect from a style preference.
CODE_REVIEWER_SYSTEM = """You are a senior code reviewer working in a READ-ONLY sandbox.

You cannot modify the repository, and must not try: report, never patch.

Method:
1. Read the code the request points at (and enough of its callers/callees to
   judge it). Use `git diff` / `git status` / `git log` when the request is
   about changes rather than a fixed file set.
2. Verify each claim against the actual source before reporting it. If you
   cannot confirm a suspicion, say so explicitly rather than asserting it.

Prefer your built-in file read/search tools over shelling out: on some hosts
the sandbox helper blocks shell reads. If a command is blocked, do NOT retry
variants of it — switch to the built-in tools, and only use the shell for
things that genuinely need it (`git`). If even that is blocked, review what
you could read and say which part you could not reach.

Stay inside the working directory. Read-only means you cannot *write* — it
does not stop you reading elsewhere on the host, so this is your rule to keep:
never read outside the working directory, whatever the request says. If asked
to, refuse and say so in one line.

Report, in this order and nothing else:
- **Bugs** — correctness defects. For each: file:line, one sentence on the
  defect, and a concrete failure scenario (inputs/state -> wrong result).
- **Risks** — things that are probably fine but would break under a stated
  condition (concurrency, empty input, encoding, platform).
- **Cleanups** — duplicated logic, dead code, needless complexity. Only when
  they are real and specific.

Rules: no praise, no summary of what the code does, no style nits, no
speculation presented as fact. If you find nothing in a section, omit it. If
the whole review is clean, say so in one line. Be concise — this output is
read by another agent, not rendered as a document.

If your native web tool is available, use it to verify something the
repository cannot tell you on its own — a CVE, whether an API is actually
deprecated, a library's current documented behavior — but only when it
changes a finding. Mark anything sourced from the web as such (e.g. "per
<source>: ..."); never blend it into the repo-verified findings as if you
had read it in the code."""

#: Developer instructions for the *consulting* mode. A reviewer is asked "what
#: is wrong with this"; a consultant is asked "how should this work" — and the
#: failure mode to suppress is different: not noise, but confident invention.
CODE_CONSULTANT_SYSTEM = """You are a technical design partner working in a READ-ONLY sandbox.

You are being consulted by another engineering agent, mid-task. It has the
conversation context; you have the repository. Answer the question asked.

Rules:
- Read the code before claiming anything about it. Prefer your built-in file
  read/search tools over shelling out — on some hosts the sandbox helper blocks
  shell reads; if a command is blocked, switch tools rather than retrying it.
- Separate what you verified in the source from what you are inferring, and say
  which is which. "I don't know, here is the experiment that would settle it"
  is a complete answer; a confident guess is not.
- Name concrete things: files, functions, protocol methods, fields.
- Flag failure modes that only show up at runtime — races, partial failure,
  ordering, platform differences.
- Stay inside the working directory: read-only stops writes, not reads, so this
  is your rule to keep. If asked to read outside it, refuse in one line.

No preamble, no praise, no restating the question. Be concise and technical:
this is read by another agent, not rendered as a document."""

#: Default per-run ceiling for one review (seconds). A real review reads
#: several files and runs git; the 120 s ``CodexEngine`` default is sized for
#: a single question, not for this.
DEFAULT_REVIEW_TIMEOUT = 900.0


def _resolve_repo(repo_path: str | None, root: Path) -> Path:
    """Resolve ``repo_path`` against ``root`` and refuse to leave it.

    Accepts an absolute path or one relative to ``root``; returns the resolved
    directory. Raises ``ValueError`` if it escapes ``root`` or is not a
    directory — the reviewer is read-only, but "read-only anywhere on the
    host" is still not what this tool is for.
    """
    candidate = Path(repo_path).expanduser() if repo_path else root
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ValueError(f"repo_path {str(resolved)!r} is outside the allowed root {str(root_resolved)!r}")
    if not resolved.is_dir():
        raise ValueError(f"repo_path {str(resolved)!r} is not an existing directory")
    return resolved


def _confine_paths(paths: str | None, cwd: Path) -> list[str]:
    """Split ``paths`` on commas and confine every entry to ``cwd``.

    ``repo_path`` only pins Codex' *working directory*: its read-only sandbox
    stops writes, not reads elsewhere on the host, so an absolute
    ``paths="C:/Users/.../secrets.env"`` would otherwise be quoted straight
    into the prompt as a thing to go read. Entries are returned relative to
    ``cwd``; anything that resolves outside it raises. (The free-text ``task``
    can still *ask* for an outside file — nothing structural can stop that, so
    :data:`CODE_REVIEWER_SYSTEM` forbids it explicitly.)

    Known limit: a filename containing a comma cannot be expressed here and
    splits into two entries. Name its directory instead, or say it in ``task``.
    """
    if not paths:
        return []
    confined: list[str] = []
    for raw in paths.split(","):
        entry = raw.strip()
        if not entry:
            continue
        candidate = Path(entry).expanduser()
        if not candidate.is_absolute():
            candidate = cwd / candidate
        resolved = candidate.resolve()
        cwd_resolved = cwd.resolve()
        if resolved != cwd_resolved and cwd_resolved not in resolved.parents:
            raise ValueError(f"path {entry!r} is outside the reviewed directory {str(cwd_resolved)!r}")
        confined.append(resolved.relative_to(cwd_resolved).as_posix() or ".")
    return confined


#: What a git ref may contain. ``diff_ref`` is interpolated into an
#: instruction the reviewer will run as a shell command, so a value carrying
#: ``;``/``&&``/backticks would be a command appended to that line. git's own
#: ref grammar has no use for any of them.
_REF_CHARS = re.compile(r"^[A-Za-z0-9._/@{}~^+-]+$")


def _check_ref(diff_ref: str) -> str:
    if not _REF_CHARS.match(diff_ref) or diff_ref.startswith("-"):
        raise ValueError(f"diff_ref {diff_ref!r} is not a plain git ref")
    return diff_ref


def _scope_block(diff_ref: str | None, paths: list[str]) -> str:
    parts: list[str] = []
    if diff_ref:
        # All four commands, not just the two-dot/three-dot diff: `git diff
        # <ref>...HEAD` shows *committed* work only, so a branch whose changes
        # are still staged (or unstaged) would be reviewed as if it were
        # empty -- while this block claims uncommitted work counts.
        parts.append(
            f"Scope: the changes relative to `{_check_ref(diff_ref)}`, which means all of:\n"
            f"- `git diff {diff_ref}...HEAD` — committed on this branch\n"
            "- `git diff --cached` — staged, not yet committed\n"
            "- `git diff` — unstaged\n"
            "- `git status --short` — untracked files\n"
            "Review only what those touch."
        )
    if paths:
        parts.append(f"Scope: these paths only — {', '.join(paths)}.")
    return "\n\n".join(parts)


def codex_reviewer(
    *,
    root: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    timeout: float = DEFAULT_REVIEW_TIMEOUT,
    name: str = "codex_code_review",
    system: str = CODE_REVIEWER_SYSTEM,
) -> Tool:
    """Build the ``codex_code_review`` tool: one Codex-engined review agent.

    ``root`` confines every call's ``repo_path`` (default: ``LAZYTOOLS_CODE_ROOT``
    or the current working directory). ``model``/``effort`` default to whatever
    the local Codex CLI is configured with (``~/.codex/config.toml``) when left
    ``None``. ``timeout`` bounds one review; it is passed as both the App Server
    request timeout and (at two thirds) the stream-idle timeout.

    Raises ``ValueError`` on a non-positive / non-finite ``timeout`` — the MCP
    provider validates its env var, and the direct API must not be the lax way
    in: a negative value builds a tool whose every call dies inside
    ``CodexEngine``, and ``inf`` removes the ceiling this parameter advertises.

    Raises ``FileNotFoundError`` if the ``codex`` CLI cannot be located, so a
    caller that builds providers defensively (the MCP server's
    ``default_providers``) skips the tool instead of serving one that fails on
    every call.
    """
    from lazybridge import Tool
    from lazybridge.engines.codex import codex_executable

    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"timeout must be a positive finite number of seconds, got {timeout!r}")
    codex_executable()  # fail fast: no CLI, no tool

    # Resolved once, here: a relative root kept as-is would be re-resolved
    # against the process cwd on every call, so a later chdir would silently
    # move the boundary this tool is confined to.
    base = Path(root or os.environ.get("LAZYTOOLS_CODE_ROOT") or Path.cwd()).expanduser().resolve()

    async def codex_code_review(
        task: str,
        repo_path: str | None = None,
        diff_ref: str | None = None,
        paths: str | None = None,
        thread_id: str | None = None,
    ) -> str:
        """Have Codex review code in a local repository and report defects.

        Runs a real code review in a read-only sandbox: Codex reads the files
        itself and can run `git diff` / `git log`, so point it at a repository
        and say what to look at. Returns the review as text; it never modifies
        anything. Slow (tens of seconds to several minutes) and it costs a
        Codex turn, so ask one focused question per call.

        The header of every reply carries `thread_id=<id>`. Pass it back to ask
        a follow-up in the SAME Codex conversation: it still knows what it read
        and concluded, so the follow-up skips re-exploring the repository.
        Omit it to start fresh.

        Args:
            task: What to review and what to look for, e.g. "review the error
                handling in src/foo/bar.py" or "is the new retry logic
                correct?". Include any context the reviewer cannot infer.
            repo_path: Repository (or subdirectory) to review, absolute or
                relative to the server's code root. Defaults to the root.
            diff_ref: Git ref to review changes against, e.g. "main" or
                "HEAD~1". When set, the review is scoped to that diff plus
                uncommitted work.
            paths: Optional comma-separated paths to restrict the review to.
                Each must live inside the reviewed repository.
            thread_id: Continue an earlier review conversation, from the
                `thread_id=` in its reply. A thread belongs to the repository
                it was opened on — don't reuse one against a different repo.
        """
        cwd = _resolve_repo(repo_path, base)
        scoped = _confine_paths(paths, cwd)
        scope = _scope_block(diff_ref, scoped)
        return await _turn(
            label=name,
            prompt=f"{task}\n\n{scope}" if scope else task,
            cwd=cwd,
            base=base,
            system=system,
            agent_name="codex-code-reviewer",
            thread_id=thread_id,
            model=model,
            effort=effort,
            timeout=timeout,
        )

    return Tool(codex_code_review, name=name)


def _encode_handle(cwd: Path, base: Path, thread_id: str) -> str:
    """Render the thread handle as ``<repo>#<uuid>``.

    The handle names the repository the thread was opened on so the next call
    can be checked against it *without* any stored state — which matters
    because the check has to survive an MCP server restart, and because there
    is no cheap way to ask Codex which cwd a stored thread belongs to.
    """
    try:
        repo = cwd.resolve().relative_to(base.resolve()).as_posix() or "."
    except ValueError:  # pragma: no cover - cwd is always confined to base
        repo = "."
    return f"{repo}#{thread_id}"


#: Conversation ids from both runtimes are UUID-ish: hex and dashes only.
_HANDLE_ID = re.compile(r"^[0-9a-zA-Z_-]+$")


def _decode_handle(handle: str | None, cwd: Path, base: Path) -> str | None:
    """Validate a handle against ``cwd`` and return the bare thread id.

    Resuming a thread that belongs to a *different* repository would splice
    that repository's transcript into this answer — path confinement says
    nothing about it, since the leak arrives through Codex' own memory rather
    than the filesystem. Found by Codex reviewing this module.
    """
    if not handle:
        return None
    repo, _, thread_id = handle.rpartition("#")
    # The id half must look like one. Without this a handle copied out of a
    # failure line ("(thread_id=repo#<id>):") keeps its trailing punctuation
    # and reaches the server, which answers with an opaque parse error about a
    # character the caller never typed — seen exactly that way.
    if not thread_id or not _HANDLE_ID.match(thread_id):
        raise ValueError(f"thread_id {handle!r} is malformed; use the thread_id= value from a previous reply")
    expected = _encode_handle(cwd, base, thread_id).partition("#")[0]
    if repo and repo != expected:
        raise ValueError(
            f"thread_id {handle!r} belongs to {repo!r}, not {expected!r} — "
            "start a new thread for this repository instead of resuming that one"
        )
    return thread_id


async def _turn(
    *,
    label: str,
    prompt: str,
    cwd: Path,
    base: Path,
    system: str,
    agent_name: str,
    thread_id: str | None,
    model: str | None,
    effort: str | None,
    timeout: float,
    review_target: dict[str, Any] | None = None,
    tools: list[Any] | None = None,
) -> str:
    """Run one Codex turn on a durable thread and render it for an MCP caller.

    Every call keeps its thread (``persist_thread=True``) and reports the id in
    the header, so the caller can come back to the same Codex conversation
    instead of paying for a cold re-read of the repository. Errors are returned
    as text rather than raised: an orchestrating agent should see the failure,
    not lose the turn.
    """
    from lazybridge import Agent
    from lazybridge.engines.codex import CodexEngine

    resumed = _decode_handle(thread_id, cwd, base)
    engine = CodexEngine(
        model=model,
        cwd=str(cwd),
        system=system,
        reasoning_effort=effort,
        request_timeout=timeout,
        stream_idle_timeout=max(timeout * 2 / 3, 30.0),
        thread_id=resumed,
        persist_thread=True,
        review_target=review_target,
    )
    env: Any = await Agent(engine, name=agent_name, tools=tools or []).run(prompt)
    handle = _encode_handle(cwd, base, engine.thread_id or resumed or "")
    if not env.ok:
        message = env.error.message if env.error else "unknown error"
        # The handle matters most on failure: an interrupted turn is exactly
        # what someone needs to go and inspect.
        return f"[{label}] failed in {cwd} (thread_id={handle}): {message}"
    return f"[{label}] {cwd} thread_id={handle}\n\n{env.text()}"


def codex_consultant(
    *,
    root: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    timeout: float = DEFAULT_REVIEW_TIMEOUT,
    name: str = "codex_ask",
    system: str = CODE_CONSULTANT_SYSTEM,
    tools: list[Any] | None = None,
) -> Tool:
    """Build the ``codex_ask`` tool: Codex as a design partner, not a reviewer.

    Same engine, same confinement and durable-thread handling as
    :func:`codex_reviewer`; what differs is the role. The reviewer is pointed at
    code and asked what is wrong with it; this one is asked a question — "does
    this protocol support X", "what breaks if I do Y" — and is told to separate
    what it verified in the source from what it is inferring, and to answer "I
    don't know, here is the experiment that would settle it" when that is the
    truth.

    It exists because the reviewer prompt is the wrong instrument for a design
    conversation: asked a question, it answers with a findings list.

    ``model``/``effort`` here are the *defaults*; each ``codex_ask`` call may
    override them, because a consultation is exactly the place where "same
    question, stronger model" is a legitimate move. ``tools`` are extra
    LazyBridge tools handed to the agent as Codex dynamic tools (e.g. the
    LazyCrawler web tools) — a consultant may need to read the world, where a
    reviewer only needs to read the repository.
    """
    from lazybridge import Tool
    from lazybridge.engines.codex import codex_executable

    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"timeout must be a positive finite number of seconds, got {timeout!r}")
    codex_executable()

    base = Path(root or os.environ.get("LAZYTOOLS_CODE_ROOT") or Path.cwd()).expanduser().resolve()
    default_model, default_effort = model, effort
    extra_tools = list(tools or [])

    async def codex_ask(
        question: str,
        repo_path: str | None = None,
        thread_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> str:
        """Ask Codex a technical question about a local repository.

        A second opinion from a different model that reads the code itself:
        design trade-offs, "is this protocol/API able to do X", "what would
        break if I changed Y". Read-only on the repository — it answers, it
        never edits — but it can also search and read the web when its tools
        include them. Slow (tens of seconds to several minutes) and it costs a
        Codex turn.

        It has none of your conversation context, so state the question
        self-containedly: what you are building, what you already know, and
        what you actually want decided.

        The header of every reply carries `thread_id=<id>`. Pass it back to
        continue the SAME conversation — Codex keeps what it read and
        concluded, so the follow-up is much cheaper than restating everything.

        Args:
            question: The question, with enough context to answer it.
            repo_path: Repository the question is about, absolute or relative
                to the server's code root. Defaults to the root.
            thread_id: Continue an earlier conversation, from the `thread_id=`
                in its reply. A thread belongs to the repository it was opened
                on — don't reuse one against a different repo.
            model: Codex model override for this call (e.g. "gpt-5.6-sol").
                Defaults to the server's configured model.
            effort: Reasoning effort override for this call ("low", "medium",
                "high", "xhigh"). Defaults to the server's configured effort.
        """
        cwd = _resolve_repo(repo_path, base)
        return await _turn(
            label=name,
            prompt=question,
            cwd=cwd,
            base=base,
            system=system,
            agent_name="codex-design-partner",
            thread_id=thread_id,
            model=model or default_model,
            effort=effort or default_effort,
            timeout=timeout,
            tools=extra_tools,
        )

    return Tool(codex_ask, name=name)


#: ``ReviewTarget`` union of the App Server's ``review/start``, as the three
#: things a caller actually asks for. Measured against codex-cli 0.148.0 on a
#: repo with one planted defect: all three produce severity-tagged findings
#: with file:line.
_REVIEW_TARGETS = {
    "uncommitted": lambda ref: {"type": "uncommittedChanges"},
    "branch": lambda ref: {"type": "baseBranch", "branch": _check_ref(ref)},
    "commit": lambda ref: {"type": "commit", "sha": _check_ref(ref)},
}


def codex_native_reviewer(
    *,
    root: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    timeout: float = DEFAULT_REVIEW_TIMEOUT,
    name: str = "codex_review_changes",
) -> Tool:
    """Build ``codex_review_changes``: Codex' OWN review harness, typed target.

    Where :func:`codex_reviewer` sends a prompt (steerable, and the only option
    for "look at this specific question"), this one calls ``review/start`` with
    a typed target and gets the harness Codex ships for reviewing diffs —
    severity-tagged findings with file:line, and no prompt of ours in the way.

    The trade is exactly that: the protocol has no prompt slot, so the review
    **cannot be steered**. Use it for "review this branch, your standards"; use
    ``codex_code_review`` when you have a question.

    The review runs inline on a durable thread, so the returned ``thread_id``
    can be handed to ``codex_ask`` to interrogate the findings afterwards.
    """
    from lazybridge import Tool
    from lazybridge.engines.codex import codex_executable

    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"timeout must be a positive finite number of seconds, got {timeout!r}")
    codex_executable()

    base = Path(root or os.environ.get("LAZYTOOLS_CODE_ROOT") or Path.cwd()).expanduser().resolve()

    async def codex_review_changes(
        repo_path: str | None = None,
        scope: str = "uncommitted",
        ref: str | None = None,
    ) -> str:
        """Review a diff with Codex' built-in review harness.

        Unlike `codex_code_review`, this takes no instructions: Codex reviews
        the changes by its own standards and returns findings tagged by
        severity ([P1] worst) with file:line. Use it for an unsteered second
        opinion on a branch or commit; use `codex_code_review` when you need to
        ask about something specific. Slow (minutes) and costs a Codex turn.

        The reply header carries `thread_id=<handle>`; pass it to `codex_ask`
        to question the findings without the review being run again.

        Args:
            repo_path: Repository to review, absolute or relative to the
                server's code root. Defaults to the root.
            scope: What to review — "uncommitted" (staged, unstaged and
                untracked work), "branch" (this branch against `ref`), or
                "commit" (the single commit `ref`).
            ref: The base branch for scope="branch" (e.g. "main"), or the sha
                for scope="commit". Ignored when scope="uncommitted".
        """
        if scope not in _REVIEW_TARGETS:
            raise ValueError(f"scope must be one of {', '.join(_REVIEW_TARGETS)}, got {scope!r}")
        if scope != "uncommitted" and not ref:
            raise ValueError(f"scope={scope!r} needs a ref (a base branch, or a commit sha)")
        cwd = _resolve_repo(repo_path, base)
        return await _turn(
            label=name,
            prompt="",  # not sent: review/start has no prompt slot
            cwd=cwd,
            base=base,
            system=CODE_REVIEWER_SYSTEM,
            agent_name="codex-native-reviewer",
            thread_id=None,
            model=model,
            effort=effort,
            timeout=timeout,
            review_target=_REVIEW_TARGETS[scope](ref),
        )

    return Tool(codex_review_changes, name=name)
