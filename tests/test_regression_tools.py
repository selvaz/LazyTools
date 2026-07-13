"""Regression tools + generic series layer over a fake hub backend."""

from __future__ import annotations

import json
import math

import pytest

pytest.importorskip("lazystats", reason="the statistical tools delegate to lazystats")
pytest.importorskip("statsmodels", reason="regression needs lazystats[regression]")
pytest.importorskip("sklearn", reason="regression needs lazystats[regression]")

from lazytools.statistical_analysis import ReturnDataset, StatisticalAnalysisTools


class _FakeSeriesBackend:
    """Implements the transformation-layer seam (load_series + load_returns)."""

    def __init__(self, dataset: ReturnDataset) -> None:
        self.dataset = dataset
        self.calls: list[dict[str, str]] = []

    def load_returns(self, instruments: str, *, start="", end="", frequency="D"):
        raise AssertionError("tools must prefer load_series when available")

    def load_series(self, specs: str, *, start="", end="", frequency="D"):
        self.calls.append(
            {"specs": specs, "start": start, "end": end, "frequency": frequency}
        )
        return self.dataset


class _LegacyBackend:
    """Pre-transformation-layer backend: only load_returns exists."""

    def __init__(self, dataset: ReturnDataset) -> None:
        self.dataset = dataset

    def load_returns(self, instruments: str, *, start="", end="", frequency="D"):
        return self.dataset


def _regression_dataset(n: int = 40) -> ReturnDataset:
    rows = []
    for i in range(n):
        x1 = 0.01 * math.sin(0.7 * i)
        x2 = 0.01 * math.cos(1.3 * i)
        eps = 0.0005 * math.sin(2.9 * i + 1.0)
        rows.append(
            {
                "date": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}",
                "ticker:SPY": round(0.001 + 2.0 * x1 - 0.5 * x2 + eps, 12),
                "ticker:TLT": round(x1, 12),
                "factor:FF5_daily/Mkt-RF": round(x2, 12),
            }
        )
    return ReturnDataset(
        instruments=["ticker:SPY", "ticker:TLT", "factor:FF5_daily/Mkt-RF"],
        rows=rows,
        metadata={
            "source": "market-data-hub",
            "series": {
                "ticker:SPY": {"domain": "ticker", "transform": "log_return"},
                "ticker:TLT": {"domain": "ticker", "transform": "log_return"},
                "factor:FF5_daily/Mkt-RF": {"domain": "factor", "transform": "level"},
            },
        },
    )


def _tools(backend):
    return backend, {t.name: t for t in StatisticalAnalysisTools(backend).as_tools()}


def test_ols_end_to_end_with_mixed_domains() -> None:
    backend, tools = _tools(_FakeSeriesBackend(_regression_dataset()))
    result = json.loads(
        tools["statistical_regression_ols"].run_sync(
            dependent="ticker:SPY",
            regressors="ticker:TLT,factor:FF5_daily/Mkt-RF",
            start="2024-01-01",
            frequency="D",
        )
    )
    assert backend.calls == [
        {
            "specs": "ticker:SPY,ticker:TLT,factor:FF5_daily/Mkt-RF",
            "start": "2024-01-01",
            "end": "",
            "frequency": "D",
        }
    ]
    payload = result["payload"]
    assert payload["model"] == "ols"
    assert payload["dependent"] == "ticker:SPY"
    assert payload["coefficients"]["ticker:TLT"]["coef"] == pytest.approx(2.0, abs=0.05)
    assert payload["coefficients"]["factor:FF5_daily/Mkt-RF"]["coef"] == pytest.approx(
        -0.5, abs=0.05
    )
    assert payload["r_squared"] > 0.99
    assert payload["data"]["series"]["factor:FF5_daily/Mkt-RF"]["transform"] == "level"


def test_ols_hac_flows_through() -> None:
    _, tools = _tools(_FakeSeriesBackend(_regression_dataset()))
    result = json.loads(
        tools["statistical_regression_ols"].run_sync(
            dependent="ticker:SPY", regressors="ticker:TLT", robust_se="HAC"
        )
    )
    assert result["payload"]["cov_type"] == "HAC"
    assert result["payload"]["hac_lags"] >= 1


def test_ridge_and_lasso_alpha_semantics() -> None:
    _, tools = _tools(_FakeSeriesBackend(_regression_dataset()))
    ridge_cv = json.loads(
        tools["statistical_regression_ridge"].run_sync(
            dependent="ticker:SPY", regressors="ticker:TLT,factor:FF5_daily/Mkt-RF"
        )
    )
    assert ridge_cv["payload"]["alpha_selection"] == "cv"
    ridge_fixed = json.loads(
        tools["statistical_regression_ridge"].run_sync(
            dependent="ticker:SPY", regressors="ticker:TLT", alpha="0.5"
        )
    )
    assert ridge_fixed["payload"]["alpha_selection"] == "fixed"
    assert ridge_fixed["payload"]["alpha"] == pytest.approx(0.5)
    lasso = json.loads(
        tools["statistical_regression_lasso"].run_sync(
            dependent="ticker:SPY", regressors="ticker:TLT,factor:FF5_daily/Mkt-RF"
        )
    )
    assert lasso["payload"]["alpha_selection"] == "cv"
    assert "selected_regressors" in lasso["payload"]


def test_regression_results_never_include_series() -> None:
    _, tools = _tools(_FakeSeriesBackend(_regression_dataset()))
    raw = tools["statistical_regression_ols"].run_sync(
        dependent="ticker:SPY", regressors="ticker:TLT"
    )
    assert "rows" not in raw
    assert "2024-01-05" not in raw  # no dated observations leak
    payload = json.loads(raw)["payload"]
    for value in payload.values():
        assert not (isinstance(value, list) and len(value) > 12)


def test_regression_input_validation() -> None:
    _, tools = _tools(_FakeSeriesBackend(_regression_dataset()))
    with pytest.raises(ValueError, match="exactly one"):
        tools["statistical_regression_ols"].run_sync(
            dependent="ticker:SPY,ticker:QQQ", regressors="ticker:TLT"
        )
    with pytest.raises(ValueError, match="at least one"):
        tools["statistical_regression_ols"].run_sync(dependent="ticker:SPY", regressors=" ")
    with pytest.raises(ValueError, match="at most 10"):
        tools["statistical_regression_ols"].run_sync(
            dependent="ticker:SPY",
            regressors=",".join(f"ticker:R{i}" for i in range(11)),
        )
    with pytest.raises(ValueError, match="hac_lags"):
        tools["statistical_regression_ols"].run_sync(
            dependent="ticker:SPY", regressors="ticker:TLT", hac_lags=-1
        )
    with pytest.raises(ValueError, match="alpha must be a number"):
        tools["statistical_regression_ridge"].run_sync(
            dependent="ticker:SPY", regressors="ticker:TLT", alpha="abc"
        )
    with pytest.raises(ValueError, match="frequency"):
        tools["statistical_regression_ols"].run_sync(
            dependent="ticker:SPY", regressors="ticker:TLT", frequency="Y"
        )


def test_statistics_tools_use_series_layer_and_specs() -> None:
    backend, tools = _tools(_FakeSeriesBackend(_regression_dataset()))
    json.loads(
        tools["statistical_return_correlation"].run_sync(
            instruments="ticker:SPY|level,macro:FEDFUNDS|diff"
        )
    )
    assert backend.calls[-1]["specs"] == "ticker:SPY|level,macro:FEDFUNDS|diff"


def test_volatility_annualization_dropped_for_non_return_series() -> None:
    dataset = ReturnDataset(
        instruments=["ticker:SPY", "macro:FEDFUNDS"],
        rows=[
            {"date": "2024-01-02", "ticker:SPY": 0.01, "macro:FEDFUNDS": 5.25},
            {"date": "2024-01-03", "ticker:SPY": 0.02, "macro:FEDFUNDS": 5.30},
            {"date": "2024-01-04", "ticker:SPY": 0.03, "macro:FEDFUNDS": 5.20},
        ],
        metadata={
            "source": "market-data-hub",
            "series": {
                "ticker:SPY": {"domain": "ticker", "transform": "log_return"},
                "macro:FEDFUNDS": {"domain": "macro", "transform": "level"},
            },
        },
    )
    _, tools = _tools(_FakeSeriesBackend(dataset))
    payload = json.loads(
        tools["statistical_return_volatility"].run_sync(
            instruments="ticker:SPY,macro:FEDFUNDS|level"
        )
    )["payload"]
    spy = payload["volatility"]["ticker:SPY"]
    fed = payload["volatility"]["macro:FEDFUNDS"]
    assert spy["annualized_volatility"] is not None
    assert fed["annualized_volatility"] is None
    assert "period_std" in fed and "period_volatility" not in fed
    assert "mean_value" in fed and "mean_log_return" not in fed


def test_legacy_backend_still_works_for_plain_specs() -> None:
    dataset = _regression_dataset()
    _, tools = _tools(_LegacyBackend(dataset))
    result = json.loads(
        tools["statistical_return_volatility"].run_sync(instruments="ticker:SPY")
    )
    assert result["payload"]["volatility"]["ticker:SPY"]["observations"] == 40
    with pytest.raises(ValueError, match="does not support"):
        tools["statistical_return_volatility"].run_sync(instruments="ticker:SPY|level")


def test_backend_spec_parsing_with_lazydatacore() -> None:
    """Spec parsing golden tests (requires the real hub id classes)."""
    pytest.importorskip("market_data_hub")
    from market_data_hub.lazydatacore import Domain, InstrumentId

    from lazytools.statistical_analysis.backend import _parse_specs

    parsed = _parse_specs(
        "SPY, ticker:AAPL|level, macro:FEDFUNDS, factor:FF5_daily/Mkt-RF",
        Domain,
        InstrumentId,
    )
    assert [(item.label, item.transform) for item in parsed] == [
        ("ticker:SPY", "log_return"),
        ("ticker:AAPL", "level"),
        ("macro:FEDFUNDS", "diff"),
        ("factor:FF5_daily/Mkt-RF", "level"),
    ]
    with pytest.raises(ValueError, match="unknown transform"):
        _parse_specs("ticker:SPY|bogus", Domain, InstrumentId)
    with pytest.raises(ValueError, match="already returns"):
        _parse_specs("factor:FF5_daily/Mkt-RF|diff", Domain, InstrumentId)
    with pytest.raises(ValueError, match="unique"):
        _parse_specs("SPY,ticker:SPY|level", Domain, InstrumentId)
    with pytest.raises(ValueError, match="ticker:, factor: and macro:"):
        _parse_specs("crypto:BTCUSDT@1h", Domain, InstrumentId)
