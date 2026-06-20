"""Canonical return primitives — float math for the analysis layer.

Single source of truth for the return calculations previously duplicated in
market-data-hub's ``extract.py`` (log returns, pandas/float) and LazyFin's
``kernel/returns.py`` (simple returns, Decimal). Per the ecosystem numeric
policy these are **float**: float for analysis/charts, Decimal only for money —
the LazyFin ledger keeps its Decimal versions, kept in lockstep with these by
numeric-equivalence tests.

Inputs are a value/price series, oldest first, strictly positive, at a regular
periodicity; ``periods_per_year`` says which (252 trading days, 52 weeks, ...).
External cash flows are not modelled: feed flow-adjusted values when flows exist.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

__all__ = [
    "log_returns",
    "simple_returns",
    "pct_change",
    "cumulative_return",
    "annualized_return",
]


def _validated(values: Sequence[float], *, minimum: int) -> list[float]:
    series = [float(v) for v in values]
    if len(series) < minimum:
        raise ValueError(
            f"value series needs at least {minimum} points, got {len(series)}"
        )
    for i, v in enumerate(series):
        if v <= 0:
            raise ValueError(
                f"value series must be strictly positive (values[{i}] = {v}); "
                "return math on a zero/negative value is undefined"
            )
    return series


def log_returns(values: Sequence[float]) -> list[float]:
    """Continuously-compounded (log) returns: ``ln(V_t / V_{t-1})``.

    The additive return market-data-hub stores/derives; ``len(values) - 1``
    values, oldest first. Additive across time: their sum is
    ``ln(V_n / V_0)`` = ``ln(1 + cumulative_return)``.
    """
    series = _validated(values, minimum=2)
    return [math.log(series[i] / series[i - 1]) for i in range(1, len(series))]


def simple_returns(values: Sequence[float]) -> list[float]:
    """Period-over-period simple returns: ``V_t / V_{t-1} - 1`` (fractions)."""
    series = _validated(values, minimum=2)
    return [series[i] / series[i - 1] - 1.0 for i in range(1, len(series))]


#: pandas-style alias for :func:`simple_returns`.
pct_change = simple_returns


def cumulative_return(values: Sequence[float]) -> float:
    """Total return over the whole series: ``V_n / V_0 - 1``."""
    series = _validated(values, minimum=2)
    return series[-1] / series[0] - 1.0


def annualized_return(values: Sequence[float], *, periods_per_year: int) -> float:
    """Geometric annualized return: ``(V_n / V_0) ** (ppy / n_periods) - 1``.

    ``n_periods = len(values) - 1``. With external cash flows this is not a
    rate of return on capital — use flow-adjusted values.
    """
    if periods_per_year <= 0:
        raise ValueError(f"periods_per_year must be positive, got {periods_per_year}")
    series = _validated(values, minimum=2)
    n_periods = len(series) - 1
    growth = series[-1] / series[0]
    return growth ** (periods_per_year / n_periods) - 1.0
