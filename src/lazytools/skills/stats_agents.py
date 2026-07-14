"""Specialist statistical agents and a supervisor that composes them.

Three narrow specialists — each an :class:`~lazybridge.Agent` over one
hub-backed tool provider — plus a :func:`stats_supervisor` that uses them *as
tools* (the agent-as-tool pattern: each specialist's ``name`` + ``description``
become the tool the supervisor sees, so it knows what to delegate without a
hand-written recipe).

Everything reads exclusively from **market-data-hub**: the specialists wrap
:class:`~lazytools.statistical_analysis.StatisticalAnalysisTools` (filtered
two ways — one specialist for volatility/correlation/outliers, one for its
regression tools) and :class:`~lazytools.connectors.regimes.RegimeTools`;
none of them accept raw price or return vectors. They orchestrate
deterministic tools and *explain* the results — they never invent numbers.

These are deliberately lighter than :mod:`lazytools.skills.analyst` (no
blackboard, no report pipeline): the intent is a reusable statistical
sub-system an outer orchestrator can call. Build them with a cheap tier
(``deepseek-v4-flash``) — each has a single, well-scoped job.

    from lazytools.skills.stats_agents import stats_supervisor

    supervisor = stats_supervisor("deepseek-v4-flash")
    print(supervisor("Regress SPY weekly returns on TLT, GLD and QQQ for 2015-2024 "
                     "and tell me which factor dominates.").text())
"""

from __future__ import annotations

from typing import Any

_SHARED_PREFIX = (
    "You are a quantitative research assistant. Every number you state must come "
    "from a tool result — never estimate or invent figures, and never ask for the "
    "raw series (the tools read it from market-data-hub themselves). Instruments "
    "are canonical IDs like 'ticker:SPY'; a bare 'SPY' is also accepted. Present "
    "conclusions as draft research, not investment advice."
)

VOLATILITY_CORRELATION_ANALYST_SYSTEM = (
    _SHARED_PREFIX
    + "\n\nYou are the volatility & correlation analyst. Use "
    "statistical_return_volatility, statistical_return_correlation and "
    "statistical_return_outliers to quantify annualised volatility, pairwise "
    "correlations and return outliers over the requested window and frequency. "
    "Explain what the numbers mean (e.g. diversification from a low/negative "
    "correlation, fat tails from many outliers); cite only tool output."
)

REGIME_ANALYST_SYSTEM = (
    _SHARED_PREFIX
    + "\n\nYou are the regime analyst. You interpret hidden-Markov volatility "
    "regimes. When a fitted result already exists, inspect it with the read tools "
    "(regime_get_summary, regime_get_current, regime_get_changes, "
    "regime_store_list). When asked to model regimes and you are allowed to write, "
    "load returns with regime_load_from_datahub(symbols=..., frequency='W', "
    "data_key='ret'), then regime_fit(data_key='ret', result_key='hmm', "
    "model='panel', S_min=2, S_max=3, n_starts=8), then summarise with "
    "regime_get_summary(result_key='hmm') and regime_get_current. Report the "
    "number of regimes, each regime's volatility character and the current "
    "regime with its probability."
)

REGRESSION_ANALYST_SYSTEM = (
    _SHARED_PREFIX
    + "\n\nYou are the regression analyst. Use statistical_regression_ols "
    "for inference (coefficients, t/p-values, R^2, residual diagnostics; "
    "prefer robust_se='HAC' for return data) and statistical_regression_ridge "
    "/ statistical_regression_lasso when collinearity or variable selection "
    "matters (Lasso can zero out weak factors). 'dependent' is exactly ONE "
    "instrument spec ('<id>[|transform]', e.g. 'ticker:SPY' or "
    "'SPY|log_return'); 'regressors' is a comma-separated list of specs (max "
    "10), e.g. 'TLT,GLD,QQQ'. Each tool reads its series directly from "
    "market-data-hub — never pass raw data. Explain which regressors are "
    "significant, the sign and size of their betas, and the model's "
    "explanatory power; note when Ridge/Lasso shrink or drop a factor."
)

STATS_SUPERVISOR_SYSTEM = (
    "You are a statistical-analysis supervisor. You orchestrate three specialist "
    "sub-agents and synthesise their findings — you do not compute anything "
    "yourself. Route each part of the question to the right specialist:\n"
    "- volatility-correlation-analyst: annualised volatility, correlations, return "
    "outliers.\n"
    "- regime-analyst: hidden-Markov volatility-regime detection and its current "
    "state.\n"
    "- regression-analyst: OLS / Ridge / Lasso factor regressions.\n"
    "Call a specialist per sub-task, then combine their answers into one concise, "
    "well-organised response. Every figure must come from a specialist; never "
    "invent numbers, and present conclusions as draft research, not advice."
)


def _engine(model: str | None, engine: Any, system: str, *, max_turns: int, max_tool_calls_per_turn: int) -> Any:
    """Build an ``LLMEngine`` with this agent's system prompt, or reuse ``engine``.

    Each agent gets its OWN engine so per-agent turn budgets never collide
    (a shared engine would share one budget across the whole team).
    """
    if engine is not None:
        return engine
    if model is None:
        raise ValueError("provide model= (recommended, e.g. 'deepseek-v4-flash') or engine=")
    from lazybridge import LLMEngine

    return LLMEngine(
        model,
        system=system,
        max_turns=max_turns,
        max_tool_calls_per_turn=max_tool_calls_per_turn,
    )


def volatility_correlation_analyst(
    model: str | None = None,
    *,
    engine: Any = None,
    backend: Any = None,
    name: str = "volatility-correlation-analyst",
    max_turns: int = 10,
    max_tool_calls_per_turn: int = 3,
    session: Any = None,
) -> Any:
    """Specialist over :class:`StatisticalAnalysisTools` (vol / corr / outliers)."""
    from lazybridge import Agent

    from lazytools.statistical_analysis import StatisticalAnalysisTools

    tools: list[Any] = [StatisticalAnalysisTools(backend)]
    return Agent(
        name=name,
        engine=_engine(
            model, engine, VOLATILITY_CORRELATION_ANALYST_SYSTEM,
            max_turns=max_turns, max_tool_calls_per_turn=max_tool_calls_per_turn,
        ),
        tools=tools,
        description=(
            "Quantifies annualised volatility, pairwise correlations and return "
            "outliers for a set of instruments over a window (market-data-hub only). "
            "Use for questions about risk level, co-movement/diversification or "
            "extreme returns."
        ),
        session=session,
    )


def regime_analyst(
    model: str | None = None,
    *,
    engine: Any = None,
    allow_write: bool = False,
    name: str = "regime-analyst",
    max_turns: int = 16,
    max_tool_calls_per_turn: int = 2,
    session: Any = None,
) -> Any:
    """Specialist over :class:`RegimeTools`.

    Read-only by default (interprets already-fitted regimes). Pass
    ``allow_write=True`` to let it load returns from the hub and fit a new HMM.
    """
    from lazybridge import Agent

    from lazytools.connectors.regimes import RegimeTools

    tools: list[Any] = [RegimeTools(allow_write=allow_write)]
    return Agent(
        name=name,
        engine=_engine(
            model, engine, REGIME_ANALYST_SYSTEM,
            max_turns=max_turns, max_tool_calls_per_turn=max_tool_calls_per_turn,
        ),
        tools=tools,
        description=(
            "Detects and interprets hidden-Markov volatility regimes for an "
            "instrument (market-data-hub only): how many regimes, each regime's "
            "volatility character, and the current regime with its probability. "
            "Use for questions about volatility regimes / regime shifts."
        ),
        session=session,
    )


_REGRESSION_TOOL_NAMES = {
    "statistical_regression_ols",
    "statistical_regression_ridge",
    "statistical_regression_lasso",
}


def regression_analyst(
    model: str | None = None,
    *,
    engine: Any = None,
    backend: Any = None,
    name: str = "regression-analyst",
    max_turns: int = 10,
    max_tool_calls_per_turn: int = 3,
    session: Any = None,
) -> Any:
    """Specialist over :class:`StatisticalAnalysisTools`'s regression tools.

    ``StatisticalAnalysisTools`` now carries OLS/Ridge/Lasso alongside
    volatility/correlation/outliers; this specialist is the SAME provider
    filtered to just its ``statistical_regression_*`` tools, keeping the
    narrow single-purpose contract without a second tool provider.
    """
    from lazybridge import Agent

    from lazytools.statistical_analysis import StatisticalAnalysisTools

    tools: list[Any] = [
        t for t in StatisticalAnalysisTools(backend).as_tools() if t.name in _REGRESSION_TOOL_NAMES
    ]
    return Agent(
        name=name,
        engine=_engine(
            model, engine, REGRESSION_ANALYST_SYSTEM,
            max_turns=max_turns, max_tool_calls_per_turn=max_tool_calls_per_turn,
        ),
        tools=tools,
        description=(
            "Runs OLS / Ridge / Lasso factor regressions of one instrument's "
            "returns on others (market-data-hub only): betas, significance, R^2, "
            "diagnostics, and variable selection. Use for factor-exposure or "
            "explanatory-power questions."
        ),
        session=session,
    )


def stats_supervisor(
    model: str | None = None,
    *,
    engine: Any = None,
    specialists: list[Any] | None = None,
    specialist_model: str | None = None,
    regime_allow_write: bool = False,
    backend: Any = None,
    name: str = "stats-supervisor",
    max_turns: int = 16,
    session: Any = None,
) -> Any:
    """Orchestrator that uses the three specialists as tools (agent-as-tool).

    ``specialists`` lets you inject pre-built specialists (e.g. sharing a
    ``session`` or a fake ``backend`` for tests); otherwise they are built from
    ``specialist_model`` (falls back to ``model``). ``regime_allow_write``
    enables the regime specialist to fit new models.
    """
    from lazybridge import Agent

    if specialists is None:
        sub_model = specialist_model or model
        specialists = [
            volatility_correlation_analyst(sub_model, backend=backend, session=session),
            regime_analyst(sub_model, allow_write=regime_allow_write, session=session),
            regression_analyst(sub_model, backend=backend, session=session),
        ]

    if engine is not None:
        sup_engine = engine
    else:
        if model is None:
            raise ValueError("provide model= (recommended, e.g. 'deepseek-v4-flash') or engine=")
        from lazybridge import LLMEngine

        sup_engine = LLMEngine(model, system=STATS_SUPERVISOR_SYSTEM, max_turns=max_turns)
    return Agent(
        name=name,
        engine=sup_engine,
        tools=list(specialists),
        description="Orchestrates volatility/correlation, regime and regression specialists.",
        session=session,
    )


__all__ = [
    "VOLATILITY_CORRELATION_ANALYST_SYSTEM",
    "REGIME_ANALYST_SYSTEM",
    "REGRESSION_ANALYST_SYSTEM",
    "STATS_SUPERVISOR_SYSTEM",
    "volatility_correlation_analyst",
    "regime_analyst",
    "regression_analyst",
    "stats_supervisor",
]
