"""lazyquant — shared quantitative primitives (float, analysis layer).

One canonical implementation of the return/risk math previously duplicated
across market-data-hub (log returns, float) and LazyFin (Decimal performance
metrics). Numeric policy: **float** here for analysis/charts; LazyFin keeps
``Decimal`` for the money ledger, kept in lockstep by numeric-equivalence tests::

    from lazytools.lazyquant import log_returns, simple_returns, max_drawdown

Pure-Python and dependency-light: no numpy/pandas required.
"""

from __future__ import annotations

from lazytools.lazyquant.returns import (
    annualized_return,
    cumulative_return,
    log_returns,
    pct_change,
    simple_returns,
)
from lazytools.lazyquant.risk import (
    annualized_volatility,
    max_drawdown,
    performance_summary,
)

__all__ = [
    "log_returns",
    "simple_returns",
    "pct_change",
    "cumulative_return",
    "annualized_return",
    "annualized_volatility",
    "max_drawdown",
    "performance_summary",
]
