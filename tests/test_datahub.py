"""DataHubTools wraps the market-data-hub tool surface (fake backend, no MDH)."""

from __future__ import annotations

import json

import pytest

from lazytools.connectors.datahub import DataHubTools
from lazytools.testing import FakeDataHubBackend

EXPECTED_NAMES = {
    "datahub_list_datasets",
    "datahub_list_symbols",
    "datahub_list_sectors",
    "datahub_list_macro",
    "datahub_list_indicators",
    "datahub_list_countries",
    "datahub_describe",
    "datahub_search",
    "datahub_get_series",
    "datahub_get_returns",
    "datahub_get_coverage",
}


def _tools(backend: FakeDataHubBackend | None = None):
    provider = DataHubTools(backend or FakeDataHubBackend())
    by_name = {t.name: t for t in provider.as_tools()}
    return provider, by_name


def test_provider_is_tool_provider() -> None:
    assert DataHubTools(FakeDataHubBackend())._is_lazy_tool_provider is True


def test_as_tools_exposes_expected_names() -> None:
    _, by_name = _tools()
    assert set(by_name) == EXPECTED_NAMES


def test_each_tool_returns_backend_json() -> None:
    backend = FakeDataHubBackend()
    _, by_name = _tools(backend)

    out = json.loads(by_name["datahub_list_datasets"].run_sync())
    assert out == {"tool": "list_datasets", "args": {}, "fake": True}

    out = json.loads(by_name["datahub_search"].run_sync(query="vix"))
    assert out["tool"] == "search"
    assert out["args"] == {"query": "vix"}

    out = json.loads(by_name["datahub_list_symbols"].run_sync(asset_class="EQUITY", area="USA"))
    assert out["args"]["asset_class"] == "EQUITY"
    assert out["args"]["area"] == "USA"


def test_get_series_forwards_all_arguments() -> None:
    backend = FakeDataHubBackend()
    _, by_name = _tools(backend)
    out = json.loads(
        by_name["datahub_get_series"].run_sync(
            symbols="SPY,TLT", start="2020-01-01", domain="prices", transform="log_return", frequency="W"
        )
    )
    assert out["args"]["symbols"] == "SPY,TLT"
    assert out["args"]["transform"] == "log_return"
    assert out["args"]["frequency"] == "W"
    # And the backend recorded the call.
    assert backend.calls[-1][0] == "get_series"


def test_canned_response_override_is_passed_through_verbatim() -> None:
    payload = {"meta": {"n_rows": 3}, "data": [{"date": "2026-06-09", "SPY": "1.0"}]}
    backend = FakeDataHubBackend(responses={"get_returns": payload})
    _, by_name = _tools(backend)
    out = json.loads(by_name["datahub_get_returns"].run_sync(symbols="SPY"))
    assert out == payload


def test_default_backend_is_lazy_and_unused_without_calls() -> None:
    # Constructing with no backend must NOT import market_data_hub; only a tool
    # call would. Building the tool list is fine.
    provider = DataHubTools()
    names = {t.name for t in provider.as_tools()}
    assert names == EXPECTED_NAMES


def test_refresh_tool_hidden_by_default_and_gated() -> None:
    backend = FakeDataHubBackend()
    _, by_name = _tools(backend)
    assert "datahub_refresh_prices" not in by_name

    provider = DataHubTools(backend, allow_refresh=True)
    by_name = {t.name: t for t in provider.as_tools()}
    assert "datahub_refresh_prices" in by_name
    out = json.loads(by_name["datahub_refresh_prices"].run_sync(symbols="SPY,QQQ"))
    assert out["tool"] == "refresh_prices"
    assert out["args"] == {"symbols": "SPY,QQQ", "start": "2010-01-01"}


# --------------------------------------------------------------------------- #
# Contract tests against the real market_data_hub (skipped when not installed)
# --------------------------------------------------------------------------- #
def test_backend_protocol_matches_mdh_tool_signatures() -> None:
    """Every DataHubBackend method must mirror the matching ``tool_*`` signature
    in market_data_hub.agent_tools — this is the drift guard between the two
    repos (the Protocol is otherwise hand-maintained)."""
    import inspect

    mdh_tools = pytest.importorskip("market_data_hub.agent_tools")
    from lazytools.connectors.datahub.backend import DataHubBackend

    methods = [
        name
        for name, fn in vars(DataHubBackend).items()
        if not name.startswith("_") and callable(fn)
    ]
    assert methods, "Protocol unexpectedly empty"
    for name in methods:
        mdh_fn = getattr(mdh_tools, f"tool_{name}")
        proto_params = list(inspect.signature(getattr(DataHubBackend, name)).parameters.values())[1:]  # drop self
        mdh_params = list(inspect.signature(mdh_fn).parameters.values())
        assert [(p.name, p.default) for p in proto_params] == [
            (p.name, p.default) for p in mdh_params
        ], f"signature drift on {name!r}"


def test_real_backend_end_to_end(tmp_path, monkeypatch) -> None:
    """MarketDataHubBackend forwards to the real tool_* functions (fresh DB)."""
    pytest.importorskip("market_data_hub")
    monkeypatch.setenv("MARKET_DATA_DB", str(tmp_path / "hub.duckdb"))
    from lazytools.connectors.datahub.backend import MarketDataHubBackend

    backend = MarketDataHubBackend()
    datasets = json.loads(backend.list_datasets())
    assert isinstance(datasets, list) and datasets
    coverage = json.loads(backend.get_coverage())
    assert coverage == []  # fresh DB: no series yet, but the call round-trips
