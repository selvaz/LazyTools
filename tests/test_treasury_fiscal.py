"""Contract tests for the Treasury Fiscal Data connector.

There is no HTTP transport to stub here -- ``treasury_fiscal.py`` does a
LOCAL import inside each tool method (``from market_data_hub.reader import
read_treasury_...``), not at module import time, because the hub owns the
actual fetch/store and this connector only translates its reader API into an
LLM-callable shape. The interception point is therefore ``sys.modules``, not
an injected client.

This matters right now for a more concrete reason: the hub-side
``read_treasury_*`` functions are landing in ``market_data_hub/reader.py`` in
a parallel, out-of-worktree change. The REAL ``market_data_hub`` package is
importable here (editable install of the sibling repo) and its ``reader``
submodule is a real module too -- it just doesn't have the treasury readers
yet. Verified empirically before writing these tests: ``import
market_data_hub`` succeeds, and ``market_data_hub.reader`` already exists as
a module object. So the fake only needs to *replace* ``reader``, not
fabricate the parent package.
"""

from __future__ import annotations

import sys
import types

import pandas as pd
import pytest

from lazytools.connectors.treasury_fiscal import TreasuryFiscalTools

TREASURY_TOOL_NAMES = {
    "treasury_cash_balance",
    "treasury_debt",
    "treasury_auctions",
}


def _install_fake_reader(monkeypatch, **fakes):
    """Inject a fake ``market_data_hub.reader`` module.

    ``fakes`` maps reader function names to callables, e.g.
    ``read_treasury_cash_balance=lambda **kw: some_dataframe``. Only install
    what a given test needs.

    Both patches matter: ``market_data_hub.reader`` (the attribute) is what a
    plain ``import market_data_hub.reader`` would rebind against, and
    ``sys.modules["market_data_hub.reader"]`` is what ``from
    market_data_hub.reader import name`` actually resolves through once
    ``market_data_hub.reader`` is already a known module -- Python looks the
    submodule up in ``sys.modules`` first and only falls back to the
    filesystem if it is missing there.
    """
    import market_data_hub  # the real package -- confirmed importable in this env

    fake_reader = types.SimpleNamespace(**fakes)
    monkeypatch.setattr(market_data_hub, "reader", fake_reader, raising=False)
    monkeypatch.setitem(sys.modules, "market_data_hub.reader", fake_reader)
    return fake_reader


# --------------------------------------------------------------------------- #
# Fixtures: fake hub frames, shaped like the documented columns
# --------------------------------------------------------------------------- #
def _cash_balance_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "record_date": "2026-08-01",
                "account_type": "Treasury General Account (TGA) Opening Balance",
                "open_today_bal": "500000.00",
                "close_today_bal": "510000.00",
                "open_month_bal": "480000.00",
                "open_fiscal_year_bal": "450000.00",
                "source": "fiscaldata",
            },
        ]
    )


def _empty_cash_balance_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "record_date",
            "account_type",
            "open_today_bal",
            "close_today_bal",
            "open_month_bal",
            "open_fiscal_year_bal",
            "source",
        ]
    )


def _debt_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "record_date": "2026-08-01",
                "debt_held_public": "27500000000000.00",
                "intragov_holdings": "7100000000000.00",
                "total_public_debt_outstanding": "34600000000000.00",
                "source": "fiscaldata",
            },
        ]
    )


def _auctions_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "auction_date": "2026-08-05",
                "security_type": "Bill",
                "security_term": "13-Week",
                "cusip": "912796XX1",
                "high_yield": "5.10",
                "bid_to_cover_ratio": "2.85",
                "source": "fiscaldata",
            },
        ]
    )


# --------------------------------------------------------------------------- #
# The mounted surface
# --------------------------------------------------------------------------- #
def test_tool_surface_is_exactly_expected() -> None:
    provider = TreasuryFiscalTools()
    assert {t.name for t in provider.as_tools()} == TREASURY_TOOL_NAMES


# --------------------------------------------------------------------------- #
# treasury_cash_balance
# --------------------------------------------------------------------------- #
def test_cash_balance_happy_path_returns_plain_dict_rows(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_read_treasury_cash_balance(**kwargs):
        calls.append(kwargs)
        return _cash_balance_frame()

    _install_fake_reader(
        monkeypatch, read_treasury_cash_balance=fake_read_treasury_cash_balance
    )

    provider = TreasuryFiscalTools()
    out = provider.treasury_cash_balance(
        "2026-08-01",
        "2026-08-01",
        account_type="Treasury General Account (TGA) Opening Balance",
    )

    assert out["start"] == "2026-08-01"
    assert out["end"] == "2026-08-01"
    assert out["account_type"] == "Treasury General Account (TGA) Opening Balance"
    assert out["returned"] == 1

    rows = out["rows"]
    assert isinstance(rows, list)
    assert not isinstance(rows, pd.DataFrame)
    assert isinstance(rows[0], dict)
    assert rows[0] == {
        "record_date": "2026-08-01",
        "account_type": "Treasury General Account (TGA) Opening Balance",
        "open_today_bal": "500000.00",
        "close_today_bal": "510000.00",
        "open_month_bal": "480000.00",
        "open_fiscal_year_bal": "450000.00",
        "source": "fiscaldata",
    }

    assert calls == [
        {
            "start": "2026-08-01",
            "end": "2026-08-01",
            "account_type": "Treasury General Account (TGA) Opening Balance",
            "db_path": None,
        }
    ]


def test_cash_balance_empty_account_type_passes_none_not_empty_string(
    monkeypatch,
) -> None:
    calls: list[dict] = []

    def fake_read_treasury_cash_balance(**kwargs):
        calls.append(kwargs)
        return _empty_cash_balance_frame()

    _install_fake_reader(
        monkeypatch, read_treasury_cash_balance=fake_read_treasury_cash_balance
    )

    provider = TreasuryFiscalTools()
    out = provider.treasury_cash_balance("2026-08-01", "2026-08-31")

    assert calls[0]["account_type"] is None
    assert out["account_type"] is None
    assert out["rows"] == []
    assert out["returned"] == 0


def test_cash_balance_forwards_configured_db_path(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_read_treasury_cash_balance(**kwargs):
        calls.append(kwargs)
        return _empty_cash_balance_frame()

    _install_fake_reader(
        monkeypatch, read_treasury_cash_balance=fake_read_treasury_cash_balance
    )

    provider = TreasuryFiscalTools(db_path="C:/data/hub.duckdb")
    provider.treasury_cash_balance("2026-08-01", "2026-08-31")

    assert calls[0]["db_path"] == "C:/data/hub.duckdb"


def test_cash_balance_requires_start() -> None:
    provider = TreasuryFiscalTools()
    with pytest.raises(ValueError, match="start is required"):
        provider.treasury_cash_balance("", "2026-08-31")


def test_cash_balance_requires_end() -> None:
    provider = TreasuryFiscalTools()
    with pytest.raises(ValueError, match="end is required"):
        provider.treasury_cash_balance("2026-08-01", "")


# --------------------------------------------------------------------------- #
# treasury_debt
# --------------------------------------------------------------------------- #
def test_debt_happy_path_returns_plain_dict_rows(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_read_treasury_debt(**kwargs):
        calls.append(kwargs)
        return _debt_frame()

    _install_fake_reader(monkeypatch, read_treasury_debt=fake_read_treasury_debt)

    provider = TreasuryFiscalTools()
    out = provider.treasury_debt("2026-08-01", "2026-08-01")

    assert out["start"] == "2026-08-01"
    assert out["end"] == "2026-08-01"
    assert out["returned"] == 1

    rows = out["rows"]
    assert isinstance(rows, list)
    assert not isinstance(rows, pd.DataFrame)
    assert rows[0] == {
        "record_date": "2026-08-01",
        "debt_held_public": "27500000000000.00",
        "intragov_holdings": "7100000000000.00",
        "total_public_debt_outstanding": "34600000000000.00",
        "source": "fiscaldata",
    }
    assert calls == [
        {"start": "2026-08-01", "end": "2026-08-01", "db_path": None}
    ]


def test_debt_requires_start() -> None:
    provider = TreasuryFiscalTools()
    with pytest.raises(ValueError, match="start is required"):
        provider.treasury_debt("", "2026-08-31")


def test_debt_requires_end() -> None:
    provider = TreasuryFiscalTools()
    with pytest.raises(ValueError, match="end is required"):
        provider.treasury_debt("2026-08-01", "")


# --------------------------------------------------------------------------- #
# treasury_auctions
# --------------------------------------------------------------------------- #
def test_auctions_happy_path_returns_plain_dict_rows(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_read_treasury_auctions(**kwargs):
        calls.append(kwargs)
        return _auctions_frame()

    _install_fake_reader(
        monkeypatch, read_treasury_auctions=fake_read_treasury_auctions
    )

    provider = TreasuryFiscalTools()
    out = provider.treasury_auctions(
        "2026-08-01", "2026-08-31", security_type="Bill"
    )

    assert out["start"] == "2026-08-01"
    assert out["end"] == "2026-08-31"
    assert out["security_type"] == "Bill"
    assert out["returned"] == 1

    rows = out["rows"]
    assert isinstance(rows, list)
    assert not isinstance(rows, pd.DataFrame)
    assert rows[0] == {
        "auction_date": "2026-08-05",
        "security_type": "Bill",
        "security_term": "13-Week",
        "cusip": "912796XX1",
        "high_yield": "5.10",
        "bid_to_cover_ratio": "2.85",
        "source": "fiscaldata",
    }
    assert calls == [
        {
            "start": "2026-08-01",
            "end": "2026-08-31",
            "security_type": "Bill",
            "db_path": None,
        }
    ]


def test_auctions_empty_security_type_passes_none_not_empty_string(
    monkeypatch,
) -> None:
    calls: list[dict] = []

    def fake_read_treasury_auctions(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame(
            columns=[
                "auction_date",
                "security_type",
                "security_term",
                "cusip",
                "high_yield",
                "bid_to_cover_ratio",
                "source",
            ]
        )

    _install_fake_reader(
        monkeypatch, read_treasury_auctions=fake_read_treasury_auctions
    )

    provider = TreasuryFiscalTools()
    out = provider.treasury_auctions("2026-08-01", "2026-08-31")

    assert calls[0]["security_type"] is None
    assert out["security_type"] is None
    assert out["rows"] == []
    assert out["returned"] == 0


def test_auctions_requires_start() -> None:
    provider = TreasuryFiscalTools()
    with pytest.raises(ValueError, match="start is required"):
        provider.treasury_auctions("", "2026-08-31")


def test_auctions_requires_end() -> None:
    provider = TreasuryFiscalTools()
    with pytest.raises(ValueError, match="end is required"):
        provider.treasury_auctions("2026-08-01", "")
