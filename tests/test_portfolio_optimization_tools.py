"""Tool schema tests for the bounded Skfolio portfolio optimization surface."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("lazyfin", reason="fin connector requires lazyfin")

from lazyfin.optimization import OptimizationDataset, OptimizationStore

from lazytools.connectors.fin import PortfolioOptimizationTools


def test_portfolio_optimizer_provider_exposes_no_raw_data_tool(tmp_path) -> None:
    provider = PortfolioOptimizationTools(OptimizationStore(str(tmp_path / "optimizer.sqlite")))
    assert {tool.name for tool in provider.as_tools()} == {
        "portfolio_optimizer_list_methods",
        "portfolio_optimizer_create_benchmark",
        "portfolio_optimizer_list_benchmarks",
        "portfolio_optimizer_run",
        "portfolio_optimizer_get_run",
        "portfolio_optimizer_get_backtest",
        "portfolio_optimizer_backtest",
    }


def test_benchmark_tool_persists_a_declared_model_portfolio(tmp_path) -> None:
    provider = PortfolioOptimizationTools(OptimizationStore(str(tmp_path / "optimizer.sqlite")))
    tools = {tool.name: tool for tool in provider.as_tools()}
    created = tools["portfolio_optimizer_create_benchmark"].run_sync(
        benchmark_id="balanced", name="70/30", weights={"ticker:ACWI": 0.7, "ticker:AGG": 0.3}
    )
    assert "ticker:ACWI" in created
    listed = tools["portfolio_optimizer_list_benchmarks"].run_sync()
    assert "balanced" in listed


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


def test_run_accepts_bare_tickers_and_exercises_group_constraints(tmp_path) -> None:
    pd = pytest.importorskip("pandas")
    pytest.importorskip("skfolio")
    frame = pd.DataFrame(
        {
            "ticker:SPY": [0.001 * ((index % 7) - 3) for index in range(60)],
            "ticker:TLT": [0.0005 * ((index % 5) - 2) for index in range(60)],
        }
    )
    backend = _FakeOptimizationBackend(frame)
    provider = PortfolioOptimizationTools(OptimizationStore(str(tmp_path / "optimizer.sqlite")), backend=backend)
    tools = {tool.name: tool for tool in provider.as_tools()}

    payload = json.loads(
        tools["portfolio_optimizer_run"].run_sync(
            instruments="SPY, TLT",
            groups={"SPY": ["equity"], "TLT": ["bond"]},
            linear_constraints=["equity <= 0.80"],
        )
    )
    assert backend.calls[0]["instruments"] == ["ticker:SPY", "ticker:TLT"]
    assert payload["status"] == "optimal"
    assert "raw_rows" not in json.dumps(payload)
    weights = {item["security_id"]: item["weight"] for item in payload["target_weights"]}
    assert float(weights["ticker:SPY"]) <= 0.8 + 1e-8


def test_backtest_runs_through_connector_without_return_rows(tmp_path) -> None:
    pd = pytest.importorskip("pandas")
    pytest.importorskip("skfolio")
    frame = pd.DataFrame(
        {
            "ticker:SPY": [0.001 * ((index % 7) - 3) for index in range(80)],
            "ticker:TLT": [0.0005 * ((index % 5) - 2) for index in range(80)],
        }
    )
    backend = _FakeOptimizationBackend(frame)
    provider = PortfolioOptimizationTools(
        OptimizationStore(str(tmp_path / "optimizer.sqlite")), backend=backend, artifacts_dir=tmp_path
    )
    tools = {tool.name: tool for tool in provider.as_tools()}

    payload = json.loads(
        tools["portfolio_optimizer_backtest"].run_sync(
            instruments="SPY,TLT", train_size=20, test_size=5, chart_filename="walk-forward.png"
        )
    )
    assert payload["status"] == "optimal"
    assert payload["n_folds"] > 0
    assert "raw_rows" not in json.dumps(payload)
    assert payload["chart"]["ref"].startswith("file:")
    assert (tmp_path / "walk-forward.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    reread = json.loads(tools["portfolio_optimizer_get_backtest"].run_sync(backtest_id=payload["id"]))
    assert reread["status"] == "optimal"
    assert "raw_rows" not in json.dumps(reread)
