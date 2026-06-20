"""Risk / dispersion primitives — float math for the analysis layer.

Companion to :mod:`lazytools.lazyquant.returns`; mirrors LazyFin's Decimal
performance metrics in float, kept equivalent by numeric-equivalence tests.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from lazytools.lazyquant.returns import (
    _validated,
    annualized_return,
    cumulative_return,
    simple_returns,
)

__all__ = [
    "annualized_volatility",
    "max_drawdown",
    "performance_summary",
]


def annualized_volatility(values: Sequence[float], *, periods_per_year: int) -> float:
    """Sample standard deviation (``ddof=1``) of periodic simple returns,
    annualized by ``sqrt(periods_per_year)``.

    Needs at least 3 values (= 2 returns) for an unbiased sample deviation.
    """
    if periods_per_year <= 0:
        raise ValueError(f"periods_per_year must be positive, got {periods_per_year}")
    series = _validated(values, minimum=3)
    rets = simple_returns(series)
    n = len(rets)
    mean = math.fsum(rets) / n
    variance = math.fsum((r - mean) ** 2 for r in rets) / (n - 1)
    return math.sqrt(variance) * math.sqrt(periods_per_year)


def max_drawdown(values: Sequence[float]) -> float:
    """Largest peak-to-trough decline, as a positive fraction of the peak.

    ``0.0`` for a non-decreasing series; ``0.25`` means a -25% drawdown.
    """
    series = _validated(values, minimum=2)
    peak = series[0]
    worst = 0.0
    for v in series[1:]:
        if v > peak:
            peak = v
        else:
            drawdown = (peak - v) / peak
            if drawdown > worst:
                worst = drawdown
    return worst


def performance_summary(
    values: Sequence[float], *, periods_per_year: int = 252
) -> dict[str, float]:
    """One-call summary: cumulative/annualized return, volatility, drawdown.

    Volatility needs at least 3 values; with a 2-point series call the
    individual functions instead.
    """
    return {
        "cumulative_return": cumulative_return(values),
        "annualized_return": annualized_return(values, periods_per_year=periods_per_year),
        "annualized_volatility": annualized_volatility(
            values, periods_per_year=periods_per_year
        ),
        "max_drawdown": max_drawdown(values),
        "periods": float(len(values) - 1),
    }
