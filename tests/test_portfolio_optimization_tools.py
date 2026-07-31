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

    def load_returns(self, instruments, *, start="", end="", frequency="D"):
        self.calls.append({"instruments": instruments, "start": start, "end": end, "frequency": frequency})
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


def test_run_publishes_to_operations_catalog(monkeypatch) -> None:
    """Covers the actual wiring in tools.py, not just publish() in isolation."""
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

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "lazytools.operations.portfolio.publish",
        lambda task_name, **kwargs: calls.append({"task_name": task_name, **kwargs}),
    )

    tools["portfolio_optimizer_run"].run_sync(instruments="SPY, TLT", objective="min_risk", frequency="W")

    assert len(calls) == 1
    assert calls[0]["task_name"] == "portfolio_optimizer_run"
    assert calls[0]["parameters"]["instruments"] == ["ticker:SPY", "ticker:TLT"]
    assert calls[0]["result"]["status"] == "optimal"
