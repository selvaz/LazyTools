"""StooqAdapter (httpx MockTransport, no network) + MarketDataClient/Tools."""

from __future__ import annotations

import json

import httpx
import pytest

from lazytools.connectors.marketdata import (
    MarketDataClient,
    MarketDataTools,
    MarketDataUnavailable,
    StooqAdapter,
)
from lazytools.safety import UrlBlocked
from lazytools.testing import FakeMarketDataAdapter

QUOTE_CSV = "Symbol,Date,Time,Open,High,Low,Close,Volume\naapl.us,2026-06-09,22:00:04,204.60,206.30,203.70,203.92,50342000\n"

QUOTE_CSV_ND = "Symbol,Date,Time,Open,High,Low,Close,Volume\nnope.us,N/D,N/D,N/D,N/D,N/D,N/D,N/D\n"

HISTORY_CSV = (
    "Date,Open,High,Low,Close,Volume\n"
    "2020-06-09,80.00,82.00,79.50,81.25,90000000\n"
    "2025-06-09,190.10,193.00,189.20,192.40,61000000\n"
    "2026-03-10,210.00,212.50,208.80,211.30,55000000\n"
    "not-a-date,1,2,3,4,5\n"  # malformed date — skipped
    "2026-06-08,203.20,205.00,202.40,,45120000\n"  # missing close — skipped
    "2026-06-09,204.60,206.30,203.70,203.92,50342000\n"
)


def make_adapter(body: str, **kwargs) -> tuple[StooqAdapter, list[str]]:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text=body)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return StooqAdapter(http=http, **kwargs), requested


# --- StooqAdapter.quote --------------------------------------------------- #


def test_quote_parses_csv_and_maps_us_symbol() -> None:
    adapter, requested = make_adapter(QUOTE_CSV)
    quote = adapter.quote("AAPL")
    assert quote == {"price": "203.92", "currency": "USD", "as_of": "2026-06-09", "source": "stooq"}
    assert "s=aapl.us" in requested[0]


def test_quote_keeps_explicit_market_suffix() -> None:
    adapter, requested = make_adapter(QUOTE_CSV)
    adapter.quote("SAP.DE")
    assert "s=sap.de" in requested[0]


def test_quote_unknown_symbol_raises() -> None:
    adapter, _ = make_adapter(QUOTE_CSV_ND)
    with pytest.raises(ValueError, match="no quote"):
        adapter.quote("NOPE")


def test_quote_non_numeric_price_raises() -> None:
    bad = "Symbol,Date,Time,Open,High,Low,Close,Volume\naapl.us,2026-06-09,22:00,1,1,1,oops,1\n"
    adapter, _ = make_adapter(bad)
    with pytest.raises(ValueError, match="non-numeric"):
        adapter.quote("AAPL")


def test_unmapped_suffix_currency_is_unknown_not_usd() -> None:
    # A ticker on a market we can't map must NOT be silently labelled USD —
    # fail closed instead so downstream typed-money construction rejects it.
    adapter, _ = make_adapter(QUOTE_CSV)
    assert adapter.quote("foo.za")["currency"] == "UNKNOWN"  # unmapped suffix
    assert adapter.quote("nesn.ch")["currency"] == "CHF"  # newly mapped
    assert adapter.quote("shop.ca")["currency"] == "CAD"  # newly mapped


def test_rate_limited_html_body_raises_unavailable_not_unknown_symbol() -> None:
    # stooq answers HTTP 200 with a throttle message when rate-limiting.
    adapter, _ = make_adapter("Exceeded the daily hits limit\n")
    with pytest.raises(MarketDataUnavailable, match="non-CSV"):
        adapter.quote("AAPL")


def test_empty_ticker_rejected() -> None:
    adapter, _ = make_adapter(QUOTE_CSV)
    with pytest.raises(ValueError, match="non-empty"):
        adapter.quote("  ")


# --- StooqAdapter.history -------------------------------------------------- #


def test_history_skips_malformed_rows_and_sorts() -> None:
    adapter, _ = make_adapter(HISTORY_CSV)
    rows = adapter.history("AAPL", range_="5y")
    # 2020 row is outside 5y of the latest row; the two malformed rows are skipped.
    assert [r["date"] for r in rows] == ["2025-06-09", "2026-03-10", "2026-06-09"]
    # All values stay strings (Decimal-safe):
    assert rows[-1] == {
        "date": "2026-06-09",
        "open": "204.60",
        "high": "206.30",
        "low": "203.70",
        "close": "203.92",
        "volume": "50342000",
    }


def test_history_range_filtering_is_anchored_to_latest_row() -> None:
    adapter, _ = make_adapter(HISTORY_CSV)
    assert [r["date"] for r in adapter.history("AAPL", range_="1m")] == ["2026-06-09"]
    assert [r["date"] for r in adapter.history("AAPL", range_="6m")] == ["2026-03-10", "2026-06-09"]
    assert [r["date"] for r in adapter.history("AAPL", range_="1y")] == [
        "2025-06-09",
        "2026-03-10",
        "2026-06-09",
    ]


def test_history_invalid_range_raises() -> None:
    adapter, _ = make_adapter(HISTORY_CSV)
    with pytest.raises(ValueError, match="invalid range_"):
        adapter.history("AAPL", range_="2w")


def test_history_empty_body_returns_empty_list() -> None:
    adapter, _ = make_adapter("Date,Open,High,Low,Close,Volume\n")
    assert adapter.history("AAPL", range_="1y") == []


def test_history_drops_internally_inconsistent_ohlc() -> None:
    # high < low is impossible — the row is dropped, not surfaced as data.
    body = (
        "Date,Open,High,Low,Close,Volume\n"
        "2026-06-08,100,90,110,105,1000\n"  # high(90) < low(110) -> drop
        "2026-06-09,204.60,206.30,203.70,203.92,50342000\n"
    )
    adapter, _ = make_adapter(body)
    assert [r["date"] for r in adapter.history("AAPL", range_="1m")] == ["2026-06-09"]


def test_history_negative_price_row_dropped() -> None:
    body = (
        "Date,Open,High,Low,Close,Volume\n"
        "2026-06-08,-1,5,-2,3,1000\n"  # negative prices -> drop
        "2026-06-09,204.60,206.30,203.70,203.92,50342000\n"
    )
    adapter, _ = make_adapter(body)
    assert [r["date"] for r in adapter.history("AAPL", range_="1m")] == ["2026-06-09"]


def test_nan_placeholders_are_dropped_not_raised() -> None:
    # Decimal("NaN") parses but `NaN > 0` raises — a non-finite placeholder must
    # be treated as a bad value (row dropped / quote rejected), never abort the run.
    hist = (
        "Date,Open,High,Low,Close,Volume\n"
        "2026-06-08,NaN,206.30,203.70,203.92,Infinity\n"  # non-finite OHLC/volume -> drop
        "2026-06-09,204.60,206.30,203.70,203.92,50342000\n"
    )
    adapter, _ = make_adapter(hist)
    assert [r["date"] for r in adapter.history("AAPL", range_="1m")] == ["2026-06-09"]

    quote_nan = "Symbol,Date,Time,Open,High,Low,Close,Volume\naapl.us,2026-06-09,22:00,1,1,1,NaN,1\n"
    adapter2, _ = make_adapter(quote_nan)
    with pytest.raises(ValueError, match="non-numeric"):
        adapter2.quote("AAPL")


def test_history_unknown_volume_is_blank_not_fabricated_zero() -> None:
    body = (
        "Date,Open,High,Low,Close,Volume\n"
        "2026-06-09,204.60,206.30,203.70,203.92,N/D\n"  # OHLC valid, volume unknown
    )
    adapter, _ = make_adapter(body)
    rows = adapter.history("AAPL", range_="1m")
    assert rows[0]["volume"] == ""  # blank, never "0"


# --- caps & redirects ------------------------------------------------------ #


def test_size_cap_enforced() -> None:
    adapter, _ = make_adapter("x" * 1000, max_response_bytes=64)
    with pytest.raises(RuntimeError, match="max_response_bytes"):
        adapter.quote("AAPL")


def test_redirect_off_stooq_hosts_is_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://10.0.0.1/internal"})

    adapter = StooqAdapter(http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(UrlBlocked):
        adapter.quote("AAPL")


# --- MarketDataClient ------------------------------------------------------ #


def test_prices_get_contract_with_fake_adapter() -> None:
    fake = FakeMarketDataAdapter()
    client = MarketDataClient(fake)
    result = client.prices_get("aapl")
    assert result == {
        "ticker": "AAPL",
        "price": "203.92",
        "currency": "USD",
        "as_of": "2026-06-09",
        "source": "stooq",
    }
    assert all(isinstance(v, str) for v in result.values())  # Decimal-safe strings
    assert fake.quote_calls == ["aapl"]


def test_prices_history_passes_range_to_adapter() -> None:
    fake = FakeMarketDataAdapter()
    client = MarketDataClient(fake)
    rows = client.prices_history("AAPL", range_="3m")
    assert len(rows) == 3
    assert set(rows[0]) == {"date", "open", "high", "low", "close", "volume"}
    assert fake.history_calls == [("AAPL", "3m")]


def test_prices_history_validates_range() -> None:
    client = MarketDataClient(FakeMarketDataAdapter())
    with pytest.raises(ValueError, match="invalid range_"):
        client.prices_history("AAPL", range_="max")


def test_client_rejects_empty_ticker() -> None:
    client = MarketDataClient(FakeMarketDataAdapter())
    with pytest.raises(ValueError, match="non-empty"):
        client.prices_get("")


# --- MarketDataTools (ToolProvider) ----------------------------------------- #


def test_provider_is_tool_provider() -> None:
    tools = MarketDataTools(MarketDataClient(FakeMarketDataAdapter()))
    assert tools._is_lazy_tool_provider is True


def test_as_tools_exposes_expected_names() -> None:
    tools = MarketDataTools(MarketDataClient(FakeMarketDataAdapter()))
    assert {t.name for t in tools.as_tools()} == {"prices_get", "prices_history"}


def test_tools_run_sync_against_fake() -> None:
    provider = MarketDataTools(MarketDataClient(FakeMarketDataAdapter()))
    tools = {t.name: t for t in provider.as_tools()}

    quote = json.loads(tools["prices_get"].run_sync(ticker="AAPL"))
    assert quote["ticker"] == "AAPL"
    assert quote["price"] == "203.92"

    rows = json.loads(tools["prices_history"].run_sync(ticker="AAPL", range_="1y"))
    assert [r["date"] for r in rows] == ["2026-06-05", "2026-06-08", "2026-06-09"]
