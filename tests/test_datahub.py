"""DataHubTools wraps the market-data-hub tool surface (fake backend, no MDH)."""

from __future__ import annotations

import json

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
