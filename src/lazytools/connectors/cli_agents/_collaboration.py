"""CLI-agent collaboration pipeline — Claude Code + Codex, as a single tool.

The two connectors (:func:`claude_code`, :func:`codex`) are function tools you
drop into ``Agent(tools=[...])``. This module packages the multi-agent *Phase 3*
pipeline the same way: :func:`build_cli_collaboration` returns a named
:class:`lazybridge.Agent` (a ``Plan`` engine) that is itself a tool — pass it in
``tools=[build_cli_collaboration()]`` exactly like the connectors.

Flow (four sequential steps)::

    claude_analyst  — analyses with claude_code (mode='read'); proposes an approach
    codex_analyst   — critiques/confirms with codex (mode='read'); sees claude's notes
    synthesizer     — merges the two analyses into one concrete plan
    executor        — implements the plan with claude_code (mode='write')

Why ``Plan`` and not ``AgentPool``? The flow is fixed and sequential
(analyse → critique → synthesise → execute), so ``Plan`` with ``from_step`` is
simpler and each step frees memory before the next. Use ``AgentPool`` only when
you need dynamic routing or multi-round dialogue.

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

from lazytools.connectors.cli_agents._claude_code import claude_code
from lazytools.connectors.cli_agents._codex import codex

if TYPE_CHECKING:
    from lazybridge import Agent

_DEFAULT_DESCRIPTION = (
    "Delegate a coding task to a Claude Code + Codex collaboration. Claude Code "
    "analyses the codebase, Codex critiques the approach, a synthesizer merges "
    "both into one concrete plan, and (optionally) an executor implements it. "
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
    execute: bool = True,
) -> Agent:
    """Build the Claude Code + Codex collaboration pipeline as a reusable tool.

    Returns a named :class:`~lazybridge.Agent` (``Plan`` engine) that you drop
    straight into ``Agent(tools=[build_cli_collaboration()])`` — the same way you
    pass the :func:`claude_code` / :func:`codex` function tools. Because an
    ``Agent`` *is* a tool in LazyBridge, the whole multi-agent pipeline appears
    to the parent agent as a single callable taking one ``task`` string.

    Parameters
    ----------
    name:
        Tool name the parent agent sees. Must be explicit (used as the tool-map
        key); defaults to ``"cli_collaboration"``.
    description:
        Tool description shown to the parent LLM. Defaults to a summary of the
        pipeline's behaviour.
    claude_model:
        Model driving the Claude-Code analyst (step 1).
    codex_model:
        Model driving the Codex analyst/critic (step 2).
    synthesizer_model:
        Model that merges the two analyses into one plan (step 3).
    executor_model:
        Model that implements the plan via ``claude_code(mode='write')`` (step 4).
    execute:
        When ``True`` (default) the pipeline ends by implementing the plan
        (writes files). When ``False`` it stops after synthesis — a read-only
        "analyse + plan" pipeline that never modifies the codebase.

    Notes
    -----
    The ``claude_analyst`` writes its analysis into a shared ``Memory`` that the
    ``codex_analyst`` reads via ``sources=``. This is safe because ``Plan`` runs
    steps strictly sequentially — there is no concurrent writer/reader race.
    """
    # Deferred imports: keep module import stdlib-light (see module docstring).
    from lazybridge import Agent, LLMEngine, Memory, Plan, Step, from_step
    from lazybridge.dedup_guard import DeduplicateGuard

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
        guard=DeduplicateGuard(verbose=False),
    )

    codex_analyst = Agent(
        name="codex_analyst",
        engine=LLMEngine(
            codex_model,
            tool_timeout=None,
            system=(
                "Analyse the task using codex in mode='read'. "
                "Critique or confirm claude_analyst's approach. Be concise."
            ),
        ),
        tools=[codex],
        sources=[dialogue],  # sees claude_analyst's analysis as context
        guard=DeduplicateGuard(verbose=False),
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
        executor = Agent(
            name="executor",
            engine=LLMEngine(
                executor_model,
                tool_timeout=None,
                system="Implement the plan you receive using claude_code in mode='write'.",
            ),
            tools=[claude_code],
        )
        steps.append(Step("executor", context=from_step("synthesizer")))
        tools.append(executor)

    return Agent(
        name=name,
        description=description or _DEFAULT_DESCRIPTION,
        engine=Plan(*steps),
        tools=tools,
    )
