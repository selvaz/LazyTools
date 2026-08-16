"""Claude Code as a review/consulting agent — the mirror of :mod:`._review`.

Same shape as the Codex tools, same handles, same confinement, so the two are
interchangeable and their answers comparable:

* :func:`claude_reviewer` -> ``claude_code_review(task, repo_path, diff_ref,
  paths, session_id)``
* :func:`claude_consultant` -> ``claude_ask(question, repo_path, session_id)``

Under both is a ``lazybridge.Agent`` on
:class:`~lazybridge.engines.claude_code.ClaudeCodeEngine` (the locally
authenticated Claude Code runtime — no API key), on a **durable session** whose
id comes back in the reply header for follow-ups.

Two differences from the Codex side, both forced by the runtime rather than
chosen:

* **No shell.** The engine grants ``Read``/``Glob``/``Grep`` scoped to
  ``file_roots`` and nothing else — there is no ``Bash``, so the reviewer
  cannot run ``git`` the way Codex does. It gets :func:`_git_tools` instead:
  two ordinary LazyBridge tools running fixed, read-only git commands in the
  reviewed repository. Same capability, no shell.
* **No native review harness.** Codex exposes ``review/start``; the Agent SDK
  has no equivalent, so there is no ``claude_review_changes`` counterpart —
  the prompted reviewer is the whole surface here.
"""

from __future__ import annotations

import math
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lazytools.connectors.code_support._review import (
    DEFAULT_REVIEW_TIMEOUT,
    _check_ref,
    _confine_paths,
    _decode_handle,
    _encode_handle,
    _resolve_repo,
    _scope_block,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from lazybridge import Tool

#: The Codex reviewer's report contract, restated for a runtime with different
#: tools. Kept deliberately identical in *output shape* so a finding from
#: either reviewer reads the same and the two can be compared directly.
CLAUDE_REVIEWER_SYSTEM = """You are a senior code reviewer with read-only access to a repository.

You cannot modify anything, and must not try: report, never patch.

Method:
1. Read the code the request points at, and enough of its callers/callees to
   judge it. You have Read/Glob/Grep over the repository, plus `git_diff` and
   `git_status` tools when the request is about changes rather than files.
2. Verify each claim against the actual source before reporting it. If you
   cannot confirm a suspicion, say so explicitly rather than asserting it.

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
read by another agent, not rendered as a document."""

#: Consulting counterpart, same contract as ``CODE_CONSULTANT_SYSTEM``.
CLAUDE_CONSULTANT_SYSTEM = """You are a technical design partner with read-only access to a repository.

You are being consulted by another engineering agent, mid-task. It has the
conversation context; you have the repository. Answer the question asked.

Rules:
- Read the code before claiming anything about it (Read/Glob/Grep, plus
  `git_diff` / `git_status` for changes).
- Separate what you verified in the source from what you are inferring, and say
  which is which. "I don't know, here is the experiment that would settle it"
  is a complete answer; a confident guess is not.
- Name concrete things: files, functions, protocol methods, fields.
- Flag failure modes that only show up at runtime — races, partial failure,
  ordering, platform differences.

No preamble, no praise, no restating the question. Be concise and technical:
this is read by another agent, not rendered as a document."""


def _git_tools(cwd: Path) -> list[Any]:
    """Read-only git, as two plain tools bound to ``cwd``.

    The engine grants no shell, so these exist to give the reviewer the one
    capability it would otherwise miss. They are not a shell in disguise: the
    command list is fixed, the only free argument is a ref, and that goes
    through the same ``_check_ref`` validation the Codex path uses.
    """

    def _run(args: list[str]) -> str:
        try:
            proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=60)
        except FileNotFoundError:
            return "[git] not found on PATH"
        except subprocess.TimeoutExpired:
            return "[git] timed out"
        if proc.returncode != 0:
            return f"[git] exit {proc.returncode}: {proc.stderr.strip()[:500]}"
        return proc.stdout or "(empty)"

    def git_diff(ref: str = "", staged: bool = False) -> str:
        """Show a diff in the reviewed repository.

        Args:
            ref: Compare the current branch against this ref (e.g. "main",
                "HEAD~1"). Empty means the working tree against HEAD.
            staged: Show staged changes instead of unstaged ones. Ignored
                when `ref` is given.
        """
        if ref:
            return _run(["diff", f"{_check_ref(ref)}...HEAD"])
        return _run(["diff", "--cached"] if staged else ["diff"])

    def git_status() -> str:
        """List changed and untracked files in the reviewed repository."""
        return _run(["status", "--short"])

    return [git_diff, git_status]


async def _claude_turn(
    *,
    label: str,
    prompt: str,
    cwd: Path,
    base: Path,
    system: str,
    agent_name: str,
    session_id: str | None,
    model: str,
    thinking: str | int | None,
    timeout: float,
    with_git: bool,
) -> str:
    """One Claude Code turn on a durable session, rendered for an MCP caller."""
    from lazybridge import Agent
    from lazybridge.engines.claude_code import ClaudeCodeEngine
    from lazybridge.engines.coding import CodingAgentConfig

    resumed = _decode_handle(session_id, cwd, base)
    engine = ClaudeCodeEngine(
        model=model,
        cwd=str(cwd),
        file_roots=[str(cwd)],
        # Reading the web is not what a code review is for, and it is the one
        # capability here that reaches outside the machine.
        web=False,
        system=system,
        thinking=thinking,
        request_timeout=timeout,
        stream_idle_timeout=max(timeout * 2 / 3, 30.0),
        session_id=resumed,
        persist_session=True,
        config=CodingAgentConfig.reviewer(),
    )
    agent = Agent(engine, name=agent_name, tools=_git_tools(cwd) if with_git else [])
    env: Any = await agent.run(prompt)
    handle = _encode_handle(cwd, base, engine.session_id or resumed or "")
    if not env.ok:
        message = env.error.message if env.error else "unknown error"
        return f"[{label}] failed in {cwd} (session_id={handle}): {message}"
    return f"[{label}] {cwd} session_id={handle}\n\n{env.text()}"


def _build_root(root: str | None) -> Path:
    return Path(root or os.environ.get("LAZYTOOLS_CODE_ROOT") or Path.cwd()).expanduser().resolve()


def _check_timeout(timeout: float) -> None:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"timeout must be a positive finite number of seconds, got {timeout!r}")


def claude_reviewer(
    *,
    root: str | None = None,
    model: str = "sonnet",
    thinking: str | int | None = None,
    timeout: float = DEFAULT_REVIEW_TIMEOUT,
    name: str = "claude_code_review",
    system: str = CLAUDE_REVIEWER_SYSTEM,
) -> Tool:
    """Build ``claude_code_review``: Claude Code as a review agent.

    The Codex reviewer's twin — same arguments, same durable-handle protocol,
    same confinement — so the two can be pointed at one diff and compared.
    ``model`` is a Claude Code alias ("sonnet", "opus", …); ``thinking`` takes
    the engine's extended-thinking setting.
    """
    from lazybridge import Tool

    _check_timeout(timeout)
    base = _build_root(root)

    async def claude_code_review(
        task: str,
        repo_path: str | None = None,
        diff_ref: str | None = None,
        paths: str | None = None,
        session_id: str | None = None,
    ) -> str:
        """Have Claude Code review code in a local repository and report defects.

        A second reviewer alongside `codex_code_review`, on a different model
        family: it reads the files itself (Read/Glob/Grep) and can call
        `git_diff` / `git_status` in the repository. Returns the review as
        text; it never modifies anything. Slow (minutes) and it costs a Claude
        Code turn.

        The header carries `session_id=<handle>`. Pass it back for a follow-up
        in the SAME conversation — it still knows what it read and concluded.

        Args:
            task: What to review and what to look for. Include any context the
                reviewer cannot infer.
            repo_path: Repository (or subdirectory) to review, absolute or
                relative to the server's code root. Defaults to the root.
            diff_ref: Git ref to review changes against, e.g. "main". When set,
                the review is scoped to that diff plus uncommitted work.
            paths: Optional comma-separated paths to restrict the review to.
                Each must live inside the reviewed repository.
            session_id: Continue an earlier conversation, from the
                `session_id=` in its reply. A session belongs to the repository
                it was opened on.
        """
        cwd = _resolve_repo(repo_path, base)
        scope = _scope_block(diff_ref, _confine_paths(paths, cwd))
        return await _claude_turn(
            label=name,
            prompt=f"{task}\n\n{scope}" if scope else task,
            cwd=cwd,
            base=base,
            system=system,
            agent_name="claude-code-reviewer",
            session_id=session_id,
            model=model,
            thinking=thinking,
            timeout=timeout,
            with_git=True,
        )

    return Tool(claude_code_review, name=name)


def claude_consultant(
    *,
    root: str | None = None,
    model: str = "sonnet",
    thinking: str | int | None = None,
    timeout: float = DEFAULT_REVIEW_TIMEOUT,
    name: str = "claude_ask",
    system: str = CLAUDE_CONSULTANT_SYSTEM,
) -> Tool:
    """Build ``claude_ask``: Claude Code as a design partner.

    Counterpart of :func:`~lazytools.connectors.code_support.codex_consultant`,
    for the same reason it exists there: asked a design question, a reviewer
    prompt answers with a findings list.
    """
    from lazybridge import Tool

    _check_timeout(timeout)
    base = _build_root(root)

    async def claude_ask(
        question: str,
        repo_path: str | None = None,
        session_id: str | None = None,
    ) -> str:
        """Ask Claude Code a technical question about a local repository.

        A second opinion from the other model family: design trade-offs, "is
        this API able to do X", "what breaks if I change Y". Read-only — it
        answers, it never edits. Slow (minutes) and costs a Claude Code turn.

        It has none of your conversation context, so state the question
        self-containedly. The header carries `session_id=<handle>`; pass it
        back to continue the same conversation.

        Args:
            question: The question, with enough context to answer it.
            repo_path: Repository the question is about, absolute or relative
                to the server's code root. Defaults to the root.
            session_id: Continue an earlier conversation, from the
                `session_id=` in its reply.
        """
        cwd = _resolve_repo(repo_path, base)
        return await _claude_turn(
            label=name,
            prompt=question,
            cwd=cwd,
            base=base,
            system=system,
            agent_name="claude-design-partner",
            session_id=session_id,
            model=model,
            thinking=thinking,
            timeout=timeout,
            with_git=True,
        )

    return Tool(claude_ask, name=name)
