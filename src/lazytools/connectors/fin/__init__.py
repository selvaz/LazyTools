"""Finance-domain agentic surface — tool providers + agent factories.

Two distinct sources live in this package, both wired to lazybridge here:

* **LazyFin** (``PortfolioTools``, ``RiskTools``, ``OptimizerTools``,
  ``ScoringTools``, and every agent factory in ``agents.py`` — the PM
  supervisor, filing analyst, macro/geo-risk/value-selection analysts): a
  pure Python library (kernel, model, scoring, resolve, workflows). Needs
  ``lazyfin`` (private git package, not a declared extra by design — same
  dependency-confusion rationale as market-data-hub):
  ``pip install "lazyfin @ git+https://github.com/selvaz/LazyFin.git"``.
* **LazyPortfolio** (``PortfolioOptimizationTools``, ``tree_tools.py``'s
  ``PortfolioTreeTools``, ``optimizer_agent.py``'s ``optimizer_specialist``):
  the hierarchical (V2) optimization engine, a *separate* package with no
  LazyFin dependency of its own — see each module's own docstring. If
  you're looking for the hierarchical/tree optimizer specifically, it lives
  in ``lazyportfolio``, not ``lazyfin``.

This package's own ``__init__.py`` unconditionally imports ``agents.py``, so
importing anything from ``lazytools.connectors.fin`` — even a
LazyPortfolio-only name — currently requires ``lazyfin`` to be installed too
(a pre-existing coupling, not fixed by this note; guard tests with
``pytest.importorskip("lazyfin")`` before importing from this package).
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
from lazytools.connectors.fin.optimizer_agent import (
    OPTIMIZER_SPECIALIST_SYSTEM,
    optimizer_specialist,
)
from lazytools.connectors.fin.tools import (
    OptimizerTools,
    PortfolioOptimizationTools,
    PortfolioTools,
    RiskTools,
    ScoringTools,
)
from lazytools.connectors.fin.tree_tools import PortfolioTreeTools

__all__ = [
    "PortfolioTools",
    "RiskTools",
    "OptimizerTools",
    "PortfolioOptimizationTools",
    "PortfolioTreeTools",
    "ScoringTools",
    "pm_supervisor",
    "filing_analyst",
    "value_selection",
    "macro_analyst",
    "geo_risk_analyst",
    "optimizer_specialist",
    "PM_SUPERVISOR_SYSTEM",
    "FILING_ANALYST_SYSTEM",
    "VALUE_SELECTION_SYSTEM",
    "MACRO_ANALYST_SYSTEM",
    "GEO_RISK_SYSTEM",
    "OPTIMIZER_SPECIALIST_SYSTEM",
]
