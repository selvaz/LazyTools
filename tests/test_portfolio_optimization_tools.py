"""Tool schema tests for the bounded LazyPortfolio (V2) optimization surface."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("lazyfin", reason="fin connector requires lazyfin")
pytest.importorskip("lazyportfolio", reason="portfolio optimization requires lazyportfolio")

from lazyportfolio import OptimizationDataset

from lazytools.connectors.fin import PortfolioOptimizationTools


def test_portfolio_optimizer_provider_exposes_no_raw_data_tool() -> None:
    provider = PortfolioOptimizationTools()
    assert {tool.name for tool in provider.as_tools()} == {
        "portfolio_optimizer_list_objectives",
        "portfolio_optimizer_run",
        "portfolio_optimizer_backtest",
    }


def test_list_objectives_reports_the_v2_vocabulary() -> None:
    provider = PortfolioOptimizationTools()
    tools = {tool.name: tool for tool in provider.as_tools()}
    payload = json.loads(tools["portfolio_optimizer_list_objectives"].run_sync())
    assert payload["objectives"] == ["min_risk", "max_return", "max_ratio", "max_utility", "hrp"]


class _FakeOptimizationBackend:
    def __init__(self, frame) -> None:
        self.frame = frame
        self.calls: list[dict[str, object]] = []

    def load_returns(self, instruments, *, start="", end="", frequency="D", currency=None):
        self.calls.append(
            {"instruments": instruments, "start": start, "end": end, "frequency": frequency, "currency": currency}
        )
        return OptimizationDataset(
            returns=self.frame.loc[:, instruments],
            metadata={"source": "fake-hub", "n_rows": len(self.frame), "raw_rows": "never expose"},
        )


def test_run_accepts_bare_tickers_and_returns_bounded_weights() -> None:
    pd = pytest.importorskip("pandas")
    pytest.importorskip("skfolio")
    frame = pd.DataFrame(
        {
            "ticker:SPY": [0.001 * ((index % 7) - 3) for index in range(180)],
            "ticker:TLT": [0.0005 * ((index % 5) - 2) for index in range(180)],
        },
        index=pd.date_range("2024-01-02", periods=180, freq="B"),
    )
    backend = _FakeOptimizationBackend(frame)
    provider = PortfolioOptimizationTools(backend=backend)
    tools = {tool.name: tool for tool in provider.as_tools()}

    payload = json.loads(
        tools["portfolio_optimizer_run"].run_sync(
            instruments="SPY, TLT",
            objective="min_risk",
            frequency="W",
        )
    )
    assert backend.calls[0]["instruments"] == ["ticker:SPY", "ticker:TLT"]
    assert backend.calls[0]["frequency"] == "D"
    assert payload["status"] == "optimal"
    assert "raw_rows" not in json.dumps(payload)
    weights = {item["security_id"]: item["weight"] for item in payload["target_weights"]}
    assert set(weights) == {"ticker:SPY", "ticker:TLT"}
    assert abs(sum(weights.values()) - 1.0) < 1e-6


def test_backtest_runs_through_connector_without_return_rows() -> None:
    pd = pytest.importorskip("pandas")
    pytest.importorskip("skfolio")
    frame = pd.DataFrame(
        {
            "ticker:SPY": [0.001 * ((index % 7) - 3) for index in range(400)],
            "ticker:TLT": [0.0005 * ((index % 5) - 2) for index in range(400)],
        },
        index=pd.date_range("2024-01-02", periods=400, freq="B"),
    )
    backend = _FakeOptimizationBackend(frame)
    provider = PortfolioOptimizationTools(backend=backend)
    tools = {tool.name: tool for tool in provider.as_tools()}

    payload = json.loads(
        tools["portfolio_optimizer_backtest"].run_sync(
            instruments="SPY,TLT",
            frequency="W",
            train_size=10,
            rebalance_frequency="M",
        )
    )
    assert payload["status"] == "optimal"
    assert payload["n_folds"] > 0
    assert payload["provenance"]["estimation_frequency"] == "W"
    assert payload["provenance"]["rebalance_frequency"] == "M"
    assert "cagr" in payload["metrics"]
    assert "raw_rows" not in json.dumps(payload)


def _run_optimizer(**kwargs):
    pd = pytest.importorskip("pandas")
    pytest.importorskip("skfolio")
    frame = pd.DataFrame(
        {
            "ticker:SPY": [0.001 * ((index % 7) - 3) + 0.0004 for index in range(180)],
            "ticker:TLT": [0.0005 * ((index % 5) - 2) for index in range(180)],
        },
        index=pd.date_range("2024-01-02", periods=180, freq="B"),
    )
    provider = PortfolioOptimizationTools(backend=_FakeOptimizationBackend(frame))
    tools = {tool.name: tool for tool in provider.as_tools()}
    return json.loads(tools["portfolio_optimizer_run"].run_sync(instruments="SPY, TLT", frequency="W", **kwargs))


def test_run_returns_labelled_sharpe_ratio_from_own_return_and_vol() -> None:
    payload = _run_optimizer(objective="max_ratio")
    assert payload["risk_free_rate_annual_used"] == 0.0
    assert "risk_free_rate" in payload["sharpe_ratio_definition"]
    expected = payload["expected_return_annualized"] / payload["actual_volatility"]
    assert payload["sharpe_ratio_annualized"] == pytest.approx(expected)


def test_run_sharpe_uses_explicit_risk_free_rate() -> None:
    payload = _run_optimizer(objective="max_ratio", risk_free_rate=0.02)
    assert payload["risk_free_rate_annual_used"] == 0.02
    expected = (payload["expected_return_annualized"] - 0.02) / payload["actual_volatility"]
    assert payload["sharpe_ratio_annualized"] == pytest.approx(expected)


def test_run_reports_every_requested_instrument_including_zero_weight() -> None:
    # SPY has a strongly positive drift and TLT none; a tight cap-free max_return
    # may drop TLT. Either way both names must be present in the output.
    payload = _run_optimizer(objective="max_return")
    ids = [item["security_id"] for item in payload["target_weights"]]
    assert ids == ["ticker:SPY", "ticker:TLT"]
    assert all(isinstance(item["weight"], float) for item in payload["target_weights"])
