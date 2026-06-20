"""Tests for lazytools.lazyquant — correctness, identities, and numeric
equivalence with LazyFin's Decimal performance math.

The equivalence block re-implements LazyFin's exact Decimal algorithms (50-digit
context) inline and asserts the float lazyquant results match within a tight
relative tolerance — the "test di equivalenza numerica" the ecosystem plan
requires before the two implementations are treated as one.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from decimal import Decimal, localcontext

import pytest

from lazytools.lazyquant import (
    annualized_return,
    annualized_volatility,
    cumulative_return,
    log_returns,
    max_drawdown,
    pct_change,
    performance_summary,
    simple_returns,
)

# A representative non-trivial value series (oldest first), with a drawdown.
VALUES = [100.0, 102.0, 99.0, 105.0, 110.0, 104.0, 112.0]


# --------------------------------------------------------------------------- #
# correctness & identities                                                    #
# --------------------------------------------------------------------------- #
def test_simple_returns_basic() -> None:
    assert simple_returns([100.0, 110.0, 99.0]) == pytest.approx([0.10, -0.10])


def test_pct_change_is_simple_returns() -> None:
    assert pct_change is simple_returns


def test_log_returns_additive_identity() -> None:
    # sum of log returns == ln(V_n / V_0) == ln(1 + cumulative_return)
    total = math.fsum(log_returns(VALUES))
    assert total == pytest.approx(math.log(VALUES[-1] / VALUES[0]))
    assert total == pytest.approx(math.log1p(cumulative_return(VALUES)))


def test_cumulative_return() -> None:
    assert cumulative_return([100.0, 112.0]) == pytest.approx(0.12)


def test_max_drawdown_known() -> None:
    # peak 110 -> trough 104 is the worst on this series: 6/110
    assert max_drawdown(VALUES) == pytest.approx(6.0 / 110.0)
    assert max_drawdown([1.0, 2.0, 3.0]) == 0.0  # non-decreasing


def test_performance_summary_keys() -> None:
    summ = performance_summary(VALUES, periods_per_year=252)
    assert set(summ) == {
        "cumulative_return",
        "annualized_return",
        "annualized_volatility",
        "max_drawdown",
        "periods",
    }
    assert summ["periods"] == float(len(VALUES) - 1)


# --------------------------------------------------------------------------- #
# validation                                                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("fn", [simple_returns, log_returns, cumulative_return, max_drawdown])
def test_too_short_rejected(fn) -> None:
    with pytest.raises(ValueError):
        fn([100.0])


def test_non_positive_rejected() -> None:
    with pytest.raises(ValueError):
        simple_returns([100.0, 0.0])
    with pytest.raises(ValueError):
        max_drawdown([100.0, -5.0])


def test_bad_periods_per_year_rejected() -> None:
    with pytest.raises(ValueError):
        annualized_return(VALUES, periods_per_year=0)
    with pytest.raises(ValueError):
        annualized_volatility(VALUES, periods_per_year=-1)


def test_volatility_needs_three_points() -> None:
    with pytest.raises(ValueError):
        annualized_volatility([100.0, 101.0], periods_per_year=252)


# --------------------------------------------------------------------------- #
# numeric equivalence with LazyFin's Decimal math                             #
# --------------------------------------------------------------------------- #
_PREC = 50


def _dec(values: Sequence[float]) -> list[Decimal]:
    return [Decimal(str(v)) for v in values]


def _cumulative_dec(values: Sequence[float]) -> Decimal:
    s = _dec(values)
    with localcontext() as ctx:
        ctx.prec = _PREC
        return s[-1] / s[0] - 1


def _annualized_return_dec(values: Sequence[float], ppy: int) -> Decimal:
    s = _dec(values)
    with localcontext() as ctx:
        ctx.prec = _PREC
        growth = s[-1] / s[0]
        exponent = Decimal(ppy) / Decimal(len(s) - 1)
        return (growth.ln() * exponent).exp() - 1


def _annualized_vol_dec(values: Sequence[float], ppy: int) -> Decimal:
    s = _dec(values)
    with localcontext() as ctx:
        ctx.prec = _PREC
        rets = [s[i] / s[i - 1] - 1 for i in range(1, len(s))]
        n = Decimal(len(rets))
        mean = sum(rets, Decimal(0)) / n
        variance = sum(((r - mean) ** 2 for r in rets), Decimal(0)) / (n - 1)
        return variance.sqrt() * Decimal(ppy).sqrt()


def _max_drawdown_dec(values: Sequence[float]) -> Decimal:
    s = _dec(values)
    with localcontext() as ctx:
        ctx.prec = _PREC
        peak = s[0]
        worst = Decimal(0)
        for v in s[1:]:
            if v > peak:
                peak = v
            else:
                dd = (peak - v) / peak
                if dd > worst:
                    worst = dd
        return worst


@pytest.mark.parametrize("ppy", [252, 52, 12])
def test_equivalence_with_decimal(ppy: int) -> None:
    rel = 1e-12
    assert cumulative_return(VALUES) == pytest.approx(float(_cumulative_dec(VALUES)), rel=rel)
    assert annualized_return(VALUES, periods_per_year=ppy) == pytest.approx(
        float(_annualized_return_dec(VALUES, ppy)), rel=rel
    )
    assert annualized_volatility(VALUES, periods_per_year=ppy) == pytest.approx(
        float(_annualized_vol_dec(VALUES, ppy)), rel=rel
    )
    assert max_drawdown(VALUES) == pytest.approx(float(_max_drawdown_dec(VALUES)), rel=rel)
