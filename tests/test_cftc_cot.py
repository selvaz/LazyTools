"""The CFTC COT connector against a fake hub reader, not the real one.

``market_data_hub.reader.read_cftc_tff``/``read_cftc_legacy`` are new hub-side
functions landing in a sibling repo in parallel with this connector; the
already-installed ``market_data_hub`` package on this machine may or may not
carry them yet. These tests must not depend on that -- they install a fake
``market_data_hub.reader`` module via ``sys.modules`` before each call, so the
connector's local ``from market_data_hub.reader import read_cftc_tff`` (etc.)
picks up the fake regardless of what the real package currently has.
"""

from __future__ import annotations

import sys
import types

import pytest

pytest.importorskip("market_data_hub")

import pandas as pd  # must follow the importorskip above

from lazytools.connectors.cftc_cot import CFTCPositioningTools


def _install_fake_reader(monkeypatch, **fakes):
    import market_data_hub

    fake_reader = types.SimpleNamespace(**fakes)
    monkeypatch.setattr(market_data_hub, "reader", fake_reader, raising=False)
    monkeypatch.setitem(sys.modules, "market_data_hub.reader", fake_reader)
    return fake_reader


@pytest.fixture()
def tools() -> CFTCPositioningTools:
    return CFTCPositioningTools(db_path="hub.duckdb")


# --------------------------------------------------------------- tool surface
def test_the_tool_surface_is_exactly_the_two_readers(tools) -> None:
    names = {t.name for t in tools.as_tools()}
    assert names == {"cftc_positioning_financial", "cftc_positioning_commodities"}


# ------------------------------------------------------------------ financial
def test_financial_happy_path_returns_plain_dict_rows(monkeypatch, tools) -> None:
    df = pd.DataFrame(
        [
            {
                "report_date": "2026-08-18",
                "contract_market_name": "EURO FX",
                "dealer_long": 100,
                "dealer_short": 80,
                "asset_mgr_long": 200,
                "asset_mgr_short": 150,
                "lev_money_long": 300,
                "lev_money_short": 250,
            },
        ]
    )
    calls: list[dict] = []

    def fake_read_cftc_tff(**kwargs):
        calls.append(kwargs)
        return df

    _install_fake_reader(monkeypatch, read_cftc_tff=fake_read_cftc_tff)

    result = tools.cftc_positioning_financial("2026-08-01", "2026-08-18", "EURO FX")

    assert result["returned"] == 1
    assert isinstance(result["rows"], list)
    row = result["rows"][0]
    assert isinstance(row, dict)
    assert row["contract_market_name"] == "EURO FX"
    assert row["dealer_long"] == 100
    assert row["asset_mgr_short"] == 150
    assert row["lev_money_long"] == 300
    assert result["start"] == "2026-08-01"
    assert result["end"] == "2026-08-18"
    assert result["contract_market_name"] == "EURO FX"
    assert len(calls) == 1


def test_financial_empty_contract_filter_becomes_none(monkeypatch, tools) -> None:
    calls: list[dict] = []

    def fake_read_cftc_tff(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame()

    _install_fake_reader(monkeypatch, read_cftc_tff=fake_read_cftc_tff)

    tools.cftc_positioning_financial("2026-08-01", "2026-08-18", "")

    assert len(calls) == 1
    assert calls[0]["contract_market_name"] is None


def test_financial_passes_the_bound_db_path(monkeypatch, tools) -> None:
    calls: list[dict] = []

    def fake_read_cftc_tff(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame()

    _install_fake_reader(monkeypatch, read_cftc_tff=fake_read_cftc_tff)

    tools.cftc_positioning_financial("2026-08-01", "2026-08-18")

    assert calls[0]["db_path"] == "hub.duckdb"


def test_financial_rejects_empty_start_before_any_import(monkeypatch, tools) -> None:
    calls: list[dict] = []

    def fake_read_cftc_tff(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame()

    _install_fake_reader(monkeypatch, read_cftc_tff=fake_read_cftc_tff)

    with pytest.raises(ValueError):
        tools.cftc_positioning_financial("", "2026-08-18")

    assert calls == []


def test_financial_rejects_empty_end_before_any_import(monkeypatch, tools) -> None:
    calls: list[dict] = []

    def fake_read_cftc_tff(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame()

    _install_fake_reader(monkeypatch, read_cftc_tff=fake_read_cftc_tff)

    with pytest.raises(ValueError):
        tools.cftc_positioning_financial("2026-08-01", "")

    assert calls == []


def test_financial_rejects_blank_start(monkeypatch, tools) -> None:
    def fake_read_cftc_tff(**kwargs):
        raise AssertionError("should not be reached")

    _install_fake_reader(monkeypatch, read_cftc_tff=fake_read_cftc_tff)

    with pytest.raises(ValueError):
        tools.cftc_positioning_financial("   ", "2026-08-18")


# ----------------------------------------------------------------- commodities
def test_commodities_happy_path_returns_plain_dict_rows(monkeypatch, tools) -> None:
    df = pd.DataFrame(
        [
            {
                "report_date": "2026-08-18",
                "contract_market_name": "GOLD",
                "comm_long": 400,
                "comm_short": 350,
                "noncomm_long": 500,
                "noncomm_short": 450,
            },
        ]
    )
    calls: list[dict] = []

    def fake_read_cftc_legacy(**kwargs):
        calls.append(kwargs)
        return df

    _install_fake_reader(monkeypatch, read_cftc_legacy=fake_read_cftc_legacy)

    result = tools.cftc_positioning_commodities("2026-08-01", "2026-08-18", "GOLD")

    assert result["returned"] == 1
    row = result["rows"][0]
    assert isinstance(row, dict)
    assert row["contract_market_name"] == "GOLD"
    assert row["comm_long"] == 400
    assert row["noncomm_short"] == 450
    assert result["contract_market_name"] == "GOLD"
    assert len(calls) == 1


def test_commodities_empty_contract_filter_becomes_none(monkeypatch, tools) -> None:
    calls: list[dict] = []

    def fake_read_cftc_legacy(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame()

    _install_fake_reader(monkeypatch, read_cftc_legacy=fake_read_cftc_legacy)

    tools.cftc_positioning_commodities("2026-08-01", "2026-08-18", "")

    assert len(calls) == 1
    assert calls[0]["contract_market_name"] is None


def test_commodities_passes_the_bound_db_path(monkeypatch, tools) -> None:
    calls: list[dict] = []

    def fake_read_cftc_legacy(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame()

    _install_fake_reader(monkeypatch, read_cftc_legacy=fake_read_cftc_legacy)

    tools.cftc_positioning_commodities("2026-08-01", "2026-08-18")

    assert calls[0]["db_path"] == "hub.duckdb"


def test_commodities_rejects_empty_start_before_any_import(monkeypatch, tools) -> None:
    calls: list[dict] = []

    def fake_read_cftc_legacy(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame()

    _install_fake_reader(monkeypatch, read_cftc_legacy=fake_read_cftc_legacy)

    with pytest.raises(ValueError):
        tools.cftc_positioning_commodities("", "2026-08-18")

    assert calls == []


def test_commodities_rejects_empty_end_before_any_import(monkeypatch, tools) -> None:
    calls: list[dict] = []

    def fake_read_cftc_legacy(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame()

    _install_fake_reader(monkeypatch, read_cftc_legacy=fake_read_cftc_legacy)

    with pytest.raises(ValueError):
        tools.cftc_positioning_commodities("2026-08-01", "")

    assert calls == []


# --------------------------------------------------------- docstring surfacing
# LazyBridge's SIGNATURE-mode schema builder only exposes the FIRST PARAGRAPH
# of a docstring (up to the first blank line) as the tool's LLM-visible
# description. These assert the financial-vs-commodities distinction actually
# lands there, so a regression that pushes it below the first blank line is
# caught here rather than trusted to code review.
def test_financial_description_states_the_financial_vs_commodities_split() -> None:
    from lazybridge import Tool

    definition = Tool.wrap(
        CFTCPositioningTools().cftc_positioning_financial,
        name="cftc_positioning_financial",
    ).definition()

    assert "\n\n" not in definition.description
    assert "financial" in definition.description.lower()
    assert "cftc_positioning_commodities" in definition.description
    assert "dealer" in definition.description.lower()


def test_commodities_description_states_the_financial_vs_commodities_split() -> None:
    from lazybridge import Tool

    definition = Tool.wrap(
        CFTCPositioningTools().cftc_positioning_commodities,
        name="cftc_positioning_commodities",
    ).definition()

    assert "\n\n" not in definition.description
    assert "commodity" in definition.description.lower()
    assert "cftc_positioning_financial" in definition.description
    assert "commercial" in definition.description.lower()


def test_as_tools_descriptions_match_direct_wrap() -> None:
    provider = CFTCPositioningTools()
    tools_by_name = {t.name: t.definition() for t in provider.as_tools()}

    assert (
        "financial"
        in tools_by_name["cftc_positioning_financial"].description.lower()
    )
    assert (
        "commodity"
        in tools_by_name["cftc_positioning_commodities"].description.lower()
    )
