"""Portfolio-optimizer specialist agent factory.

Wraps LazyPortfolio's deterministic optimizer surface — the flat-node
``PortfolioOptimizationTools`` and the multi-node ``PortfolioTreeTools``
(``tree_tools.py``) — with a LazyBridge agent whose expertise is *driving*
those tools correctly: choosing flat vs. tree, validating before persisting,
never second-guessing the tree's own mode derivation. The agent never decides
what to trade; it only estimates, backtests and explains.

Unlike ``connectors/fin/agents.py`` (LazyFin's PM-domain agents, which
hard-require ``lazyfin`` at module level), this module only needs
``lazybridge`` — no ``lazyfin`` import, matching ``tree_tools.py``'s
independence.
"""

from __future__ import annotations

from typing import Any

from lazybridge import Agent

__all__ = ["OPTIMIZER_SPECIALIST_SYSTEM", "optimizer_specialist"]

OPTIMIZER_SPECIALIST_SYSTEM = (
    "You are a portfolio-optimization specialist over LazyPortfolio's hierarchical "
    "(V2) engine. Decide between two surfaces: portfolio_optimizer_* for a single "
    "flat pool of instruments, portfolio_tree_* the moment there is more than one "
    "sleeve or sub-allocation to compose (parent/child hierarchy, per-node proxies). "
    "Never build a tree by hand without calling portfolio_tree_validate first, and "
    "never save or run one that hasn't validated cleanly. Never invent the "
    "flat/forward/forward_backward mode yourself — it is derived entirely from the "
    "tree's own backtest.forward_enabled/hierarchy_mode; if you want a different "
    "mode, change those fields in the config, not the tool call. Objectives are "
    "the fixed vocabulary returned by portfolio_optimizer_list_objectives "
    "(min_risk, max_return, max_ratio, max_utility, hrp) — never invent one, and "
    "call that tool if you are unsure a name is valid. Resolve and verify tickers "
    "through the datahub tools before referencing them in an optimization or a "
    "tree; never guess a symbol. Every weight, metric or figure you state must "
    "come directly from a tool result — never estimate or interpolate one "
    "yourself. This is decision support only: never frame output as an executed "
    "or recommended trade, and say plainly when a result is provisional or the "
    "data window is thin. Saved trees are visible in Tree Studio (a shared local "
    "GUI) immediately, so give each a clear, descriptive name — never a "
    "throwaway or ambiguous one."
)


def optimizer_specialist(
    engine: Any,
    *,
    tools: list[Any],
    name: str = "portfolio-optimizer-specialist",
) -> Agent:
    """An agent expert at LazyPortfolio's optimizer surface.

    ``tools`` is typically ``[DataHubTools(), PortfolioOptimizationTools(...),
    PortfolioTreeTools(...)]`` — ticker discovery plus both the flat and
    tree-shaped optimizer connectors. Pair ``engine`` with
    :data:`OPTIMIZER_SPECIALIST_SYSTEM` (e.g.
    ``LLMEngine(model, system=OPTIMIZER_SPECIALIST_SYSTEM)``).
    """

    return Agent(
        engine=engine,
        tools=tools,
        name=name,
        description=(
            "Specialist agent for LazyPortfolio's optimizer: resolves tickers, "
            "builds and validates flat or multi-node allocation trees, picks "
            "objectives and constraints, runs estimate/backtest, and explains "
            "the results in plain language. Give it a natural-language "
            "portfolio-research task; it drives the portfolio_optimizer_*/"
            "portfolio_tree_* tools itself rather than needing exact tool calls."
        ),
    )
