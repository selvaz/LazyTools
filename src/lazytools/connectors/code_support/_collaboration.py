"""Code-support collaboration pipeline — Claude Code + Codex, as a single tool.

The two connectors (:func:`claude_code`, :func:`codex`) are function tools you
drop into ``Agent(tools=[...])``. This module packages the multi-agent pipeline
the same way: :func:`build_cli_collaboration` returns a named
:class:`lazybridge.Agent` (a ``Plan`` engine) that is itself a tool — pass it in
``tools=[build_cli_collaboration()]`` exactly like the connectors.

**Default flow — three sessions, nothing is written** (two read-only CLI
sessions plus one synthesizer that writes the *plan*, not code)::

    claude_analyst  — analyses with claude_code (mode='read'); proposes an approach
    codex_analyst   — critiques/confirms with codex (read-only); sees claude's notes
    synthesizer     — merges the two analyses into one concrete written plan

Execution is **opt-in**: pass ``execute=True`` together with a ``base_dir``
sandbox and a fourth step implements the plan through the gated
:class:`~lazytools.connectors.code_support.CodeWriteTools` writer::

    executor        — implements the plan (claude_code_write, sandboxed to base_dir)

Why ``Plan`` and not ``AgentPool``? The flow is fixed and sequential
(analyse → critique → synthesise [→ execute]), so ``Plan`` with ``from_step``
is simpler and each step frees memory before the next. Use ``AgentPool`` only
when you need dynamic routing or multi-round dialogue.

Every sub-agent engine sets ``tool_timeout=None`` so the engine never cancels a
running CLI subprocess (which would orphan it); each subprocess enforces its own
``timeout`` instead.

The ``lazybridge`` imports are deliberately deferred into
:func:`build_cli_collaboration` so that merely importing this module (e.g. for
mkdocstrings, or to grab :func:`claude_code` / :func:`codex`) never eagerly
pulls the heavier orchestration surface — matching the package's
"no eager heavy imports" design.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from lazytools.connectors.code_support._claude_code import claude_code
from lazytools.connectors.code_support._codex import codex
from lazytools.connectors.code_support._writer import CodeWriteTools

if TYPE_CHECKING:
    from lazybridge import Agent

_DEFAULT_DESCRIPTION = (
    "Delegate a coding task to a Claude Code + Codex collaboration. Claude Code "
    "analyses the codebase (read-only), Codex critiques the approach (read-only), "
    "and a synthesizer merges both into one concrete written plan. Nothing is "
    "modified unless the pipeline was built with execute=True. "
    "Input: a single natural-language task string. Returns the final result."
)


def build_cli_collaboration(
    *,
    name: str = "cli_collaboration",
    description: str | None = None,
    claude_model: str = "claude-opus-4-8",
    codex_model: str = "gpt-5.4",
    synthesizer_model: str = "claude-opus-4-8",
    executor_model: str = "claude-opus-4-8",
    execute: bool = False,
    base_dir: str | None = None,
    require_write_confirmation: bool = False,
) -> Agent:
    """Build the Claude Code + Codex collaboration pipeline as a reusable tool.

    Returns a named :class:`~lazybridge.Agent` (``Plan`` engine) that you drop
    straight into ``Agent(tools=[build_cli_collaboration()])`` — the same way you
    pass the :func:`claude_code` / :func:`codex` function tools. Because an
    ``Agent`` *is* a tool in LazyBridge, the whole multi-agent pipeline appears
    to the parent agent as a single callable taking one ``task`` string.

    **The default is read-only**: three sessions — two read-only CLI analysts
    and one synthesizer that writes the *plan*. The codebase is never modified
    unless you opt in with ``execute=True`` + ``base_dir=``.

    Parameters
    ----------
    name:
        Tool name the parent agent sees. Must be explicit (used as the tool-map
        key); defaults to ``"cli_collaboration"``.
    description:
        Tool description shown to the parent LLM. Defaults to a summary of the
        pipeline's behaviour.
    claude_model:
        Model driving the Claude-Code analyst (step 1, read-only).
    codex_model:
        Model driving the Codex analyst/critic (step 2, read-only).
    synthesizer_model:
        Model that merges the two analyses into one plan (step 3).
    executor_model:
        Model that implements the plan via the gated writer (step 4, only
        when ``execute=True``).
    execute:
        Default ``False`` — the pipeline ends at the written plan. Pass
        ``True`` (with ``base_dir=``) to append the executor step, which
        implements the plan via ``claude_code_write`` sandboxed to
        ``base_dir``.
    base_dir:
        Required when ``execute=True``: the sandbox root the executor may
        write inside (see :class:`CodeWriteTools`). Ideally a git checkout.
    require_write_confirmation:
        Default ``False`` for the executor — the pipeline is autonomous, so
        the ``base_dir`` sandbox (plus git) is the safety rail; a one-shot
        confirmation would block mid-pipeline. Set ``True`` if a human is
        watching and will call ``confirm_write()`` per executor write.

    Notes
    -----
    The ``claude_analyst`` writes its analysis into a shared ``Memory`` that the
    ``codex_analyst`` reads via ``sources=``. This is safe because ``Plan`` runs
    steps strictly sequentially — there is no concurrent writer/reader race.
    """
    # Deferred imports: keep module import stdlib-light (see module docstring).
    from lazybridge import Agent, LLMEngine, Memory, Plan, Step, from_step

    # DeduplicateGuard shipped after lazybridge 0.9.0; degrade gracefully on
    # older installs (the guard is an optimisation — it stops an analyst from
    # re-issuing an identical CLI call — not a correctness requirement).
    try:
        from lazybridge import DeduplicateGuard  # type: ignore[attr-defined]

        def _dedup():
            return DeduplicateGuard(verbose=False)
    except ImportError:  # lazybridge <= 0.9.0

        def _dedup():
            return None

    if execute and base_dir is None:
        raise ValueError(
            "build_cli_collaboration(execute=True) requires base_dir=: the executor "
            "writes through the CodeWriteTools sandbox and must know its root. "
            "Omit execute (default False) for the read-only analyse+plan pipeline."
        )

    # Shared dialogue: claude_analyst writes (memory=), codex_analyst reads
    # (sources=). Safe under Plan's sequential execution — no parallel access.
    dialogue = Memory(strategy="summary")

    claude_analyst = Agent(
        name="claude_analyst",
        engine=LLMEngine(
            claude_model,
            tool_timeout=None,
            system=(
                "Analyse the task using claude_code in mode='read'. "
                "Propose a concrete implementation approach. Be concise."
            ),
        ),
        tools=[claude_code],
        memory=dialogue,
        guard=_dedup(),
    )

    codex_analyst = Agent(
        name="codex_analyst",
        engine=LLMEngine(
            codex_model,
            tool_timeout=None,
            system=(
                "Analyse the task using the read-only codex tool. "
                "Critique or confirm claude_analyst's approach. Be concise."
            ),
        ),
        tools=[codex],
        sources=[dialogue],  # sees claude_analyst's analysis as context
        guard=_dedup(),
    )

    synthesizer = Agent(
        name="synthesizer",
        engine=LLMEngine(
            synthesizer_model,
            system=(
                "You receive two code analyses (Claude Code and Codex). "
                "Produce a single, concrete, step-by-step implementation plan."
            ),
        ),
    )

    steps = [
        Step("claude_analyst"),
        Step("codex_analyst", context=from_step("claude_analyst")),
        Step("synthesizer", context=from_step("codex_analyst")),
    ]
    # Annotated as the Agent(tools=) element type: a bare list[Agent] is
    # rejected because list is invariant against that wider union.
    tools: list[Any] = [claude_analyst, codex_analyst, synthesizer]

    if execute:
        assert base_dir is not None  # narrowed by the ValueError above
        writer = CodeWriteTools(
            base_dir=base_dir,
            claude=True,
            codex=False,
            require_confirmation=require_write_confirmation,
        )
        executor = Agent(
            name="executor",
            engine=LLMEngine(
                executor_model,
                tool_timeout=None,
                system="Implement the plan you receive using the claude_code_write tool.",
            ),
            tools=[*writer.as_tools()],
        )
        steps.append(Step("executor", context=from_step("synthesizer")))
        tools.append(executor)

    return Agent(
        name=name,
        description=description or _DEFAULT_DESCRIPTION,
        engine=Plan(*steps),
        tools=tools,
    )
