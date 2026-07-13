"""StatisticalAnalysisTools: deterministic calculations over a fake hub reader."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("lazystats", reason="statistical_analysis now delegates "
                    "its math to lazystats.core (plan v3.1 Fase 6)")

from lazytools.statistical_analysis import ReturnDataset, StatisticalAnalysisTools


class _FakeHubBackend:
    def __init__(self, dataset: ReturnDataset) -> None:
        self.dataset = dataset
        self.calls: list[dict[str, str]] = []

    def load_returns(
        self,
        instruments: str,
        *,
        start: str = "",
        end: str = "",
        frequency: str = "D",
    ) -> ReturnDataset:
        self.calls.append(
            {"instruments": instruments, "start": start, "end": end, "frequency": frequency}
        )
        return self.dataset


@pytest.fixture
def dataset() -> ReturnDataset:
    return ReturnDataset(
        instruments=["ticker:SPY", "ticker:TLT"],
        rows=[
            {"date": "2024-01-02", "ticker:SPY": 0.01, "ticker:TLT": 0.02},
            {"date": "2024-01-03", "ticker:SPY": 0.02, "ticker:TLT": 0.04},
            {"date": "2024-01-04", "ticker:SPY": 0.03, "ticker:TLT": 0.06},
            {"date": "2024-01-05", "ticker:SPY": None, "ticker:TLT": 0.08},
        ],
        metadata={"source": "market-data-hub", "date_start": "2024-01-02", "date_end": "2024-01-05"},
    )


def _tools(dataset: ReturnDataset):
    backend = _FakeHubBackend(dataset)
    by_name = {tool.name: tool for tool in StatisticalAnalysisTools(backend).as_tools()}
    return backend, by_name


def test_provider_exposes_the_read_only_statistics_tools(dataset: ReturnDataset) -> None:
    provider = StatisticalAnalysisTools(_FakeHubBackend(dataset))
    assert provider._is_lazy_tool_provider is True
    assert {tool.name for tool in provider.as_tools()} == {
        "statistical_return_volatility",
        "statistical_return_correlation",
        "statistical_return_outliers",
        "statistical_regression_ols",
        "statistical_regression_ridge",
        "statistical_regression_lasso",
    }


def test_volatility_is_annualized_from_hub_returns_and_forwards_window(dataset: ReturnDataset) -> None:
    backend, tools = _tools(dataset)
    result = json.loads(
        tools["statistical_return_volatility"].run_sync(
            instruments="ticker:SPY,ticker:TLT", start="2024-01-01", end="2024-01-31", frequency="W"
        )
    )

    assert backend.calls == [
        {"instruments": "ticker:SPY,ticker:TLT", "start": "2024-01-01", "end": "2024-01-31", "frequency": "W"}
    ]
    payload = result["payload"]
    assert payload["periods_per_year"] == 52
    assert payload["volatility"]["ticker:SPY"]["observations"] == 3
    assert payload["volatility"]["ticker:SPY"]["annualized_volatility"] == pytest.approx(0.0721110255)
    assert payload["data"]["source"] == "market-data-hub"


def test_correlation_uses_only_dates_with_both_returns(dataset: ReturnDataset) -> None:
    _, tools = _tools(dataset)
    result = json.loads(tools["statistical_return_correlation"].run_sync(instruments="ticker:SPY,ticker:TLT"))

    payload = result["payload"]
    assert payload["pairwise_observations"]["ticker:SPY"]["ticker:TLT"] == 3
    assert payload["correlation"]["ticker:SPY"]["ticker:TLT"] == pytest.approx(1.0)
    assert payload["correlation"]["ticker:SPY"]["ticker:SPY"] == pytest.approx(1.0)


def test_return_outliers_apply_absolute_zscore_threshold() -> None:
    dataset = ReturnDataset(
        instruments=["ticker:SPY"],
        rows=[
            {"date": f"2024-01-{day:02d}", "ticker:SPY": 0.0}
            for day in range(2, 7)
        ] + [{"date": "2024-01-07", "ticker:SPY": 0.10}],
        metadata={"source": "market-data-hub"},
    )
    _, tools = _tools(dataset)
    result = json.loads(tools["statistical_return_outliers"].run_sync(instruments="ticker:SPY"))

    payload = result["payload"]
    assert payload["threshold"] == 2.0
    assert payload["total_outliers"] == 1
    assert payload["outliers"] == [
        {
            "date": "2024-01-07",
            "instrument": "ticker:SPY",
            "log_return": 0.1,
            "z_score": pytest.approx(2.0412414523),
            "direction": "positive",
        }
    ]


def test_outliers_reject_invalid_threshold_and_result_limit(dataset: ReturnDataset) -> None:
    _, tools = _tools(dataset)
    with pytest.raises(ValueError, match="threshold"):
        tools["statistical_return_outliers"].run_sync(instruments="ticker:SPY", threshold=0)
    with pytest.raises(ValueError, match="max_results"):
        tools["statistical_return_outliers"].run_sync(instruments="ticker:SPY", max_results=0)


def test_outlier_results_are_hard_capped_for_llm_context_safety() -> None:
    dataset = ReturnDataset(
        instruments=["ticker:SPY"],
        rows=[
            {"date": f"2024-day-{index:03d}", "ticker:SPY": float(index)}
            for index in range(300)
        ],
        metadata={"source": "market-data-hub"},
    )
    _, tools = _tools(dataset)
    result = json.loads(
        tools["statistical_return_outliers"].run_sync(
            instruments="ticker:SPY", threshold=0.0001, max_results=10_000
        )
    )

    payload = result["payload"]
    assert payload["total_outliers"] == 300
    assert payload["returned_outliers"] == 250
    assert payload["truncated"] is True


def test_tool_results_never_include_raw_return_rows() -> None:
    dataset = ReturnDataset(
        instruments=["ticker:SPY"],
        rows=[{"date": "2024-01-02", "ticker:SPY": 0.01}, {"date": "2024-01-03", "ticker:SPY": 0.02}],
        metadata={
            "source": "market-data-hub",
            "n_rows": 2,
            # Simulates an accidental future backend addition: it must not leak
            # through a statistics result to the LLM.
            "raw_return_rows": [{"date": "2024-01-02", "ticker:SPY": 0.01}],
        },
    )
    _, tools = _tools(dataset)
    result = json.loads(tools["statistical_return_volatility"].run_sync(instruments="ticker:SPY"))

    assert result["payload"]["data"] == {"source": "market-data-hub", "n_rows": 2}
    assert "raw_return_rows" not in json.dumps(result)


def test_missing_lazystats_raises_clear_import_error(monkeypatch, dataset):
    """Without lazystats installed, the tool must fail with an install hint,
    not a bare ModuleNotFoundError."""
    import builtins

    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name == "lazystats" or name.startswith("lazystats."):
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    _, tools = _tools(dataset)
    with pytest.raises(ImportError, match="lazystats"):
        tools["statistical_return_volatility"].run_sync(instruments="ticker:SPY")
