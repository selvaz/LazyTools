"""LazyFin agentic surface — tool providers + agent factories (plan v3.1 Fase 5).

LazyFin is a pure Python library (kernel, model, scoring, resolve, workflows);
everything that touches lazybridge — ``Tool`` wrapping and ``Agent`` factories
— lives HERE, in the single LLM bridge. The providers wrap LazyFin's pure
functions; the factories wire them to an engine.

Requires ``lazyfin`` (private git package, not a declared extra by design —
same dependency-confusion rationale as market-data-hub):

    pip install "lazyfin @ git+https://github.com/selvaz/LazyFin.git"
"""

from lazytools.connectors.fin.agents import (
    FILING_ANALYST_SYSTEM,
    GEO_RISK_SYSTEM,
    MACRO_ANALYST_SYSTEM,
    PM_SUPERVISOR_SYSTEM,
    VALUE_SELECTION_SYSTEM,
    filing_analyst,
    geo_risk_analyst,
    macro_analyst,
    pm_supervisor,
    value_selection,
)
from lazytools.connectors.fin.tools import (
    OptimizerTools,
    PortfolioOptimizationTools,
    PortfolioTools,
    RiskTools,
    ScoringTools,
)

__all__ = [
    "PortfolioTools",
    "RiskTools",
    "OptimizerTools",
    "PortfolioOptimizationTools",
    "ScoringTools",
    "pm_supervisor",
    "filing_analyst",
    "value_selection",
    "macro_analyst",
    "geo_risk_analyst",
    "PM_SUPERVISOR_SYSTEM",
    "FILING_ANALYST_SYSTEM",
    "VALUE_SELECTION_SYSTEM",
    "MACRO_ANALYST_SYSTEM",
    "GEO_RISK_SYSTEM",
]
