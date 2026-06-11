"""Opt-in live smoke tests against the real SEC EDGAR and stooq endpoints.

Skipped by default (CI and sandboxes have no egress to these hosts). Run
explicitly from a network-open machine with::

    LAZYTOOLS_LIVE=1 EDGAR_USER_AGENT="you you@example.com" pytest tests/test_live_smoke.py -v

Keep these minimal and polite: one resolve, one facts fetch, one quote.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("LAZYTOOLS_LIVE"),
    reason="live smoke tests are opt-in: set LAZYTOOLS_LIVE=1",
)


def _user_agent() -> str:
    ua = os.environ.get("EDGAR_USER_AGENT", "")
    if not ua:
        pytest.skip("set EDGAR_USER_AGENT='name email' (SEC fair-access policy)")
    return ua


def test_edgar_resolve_and_company_facts_live() -> None:
    from lazytools.connectors.edgar import EdgarClient

    client = EdgarClient(user_agent=_user_agent())
    matches = client.resolve_company("AAPL", limit=3)
    assert matches and matches[0]["ticker"] == "AAPL"
    assert matches[0]["cik"] == "0000320193"

    facts = client.company_facts(matches[0]["cik"])
    assert facts["entityName"].lower().startswith("apple")
    assert "us-gaap" in facts["facts"]


def test_edgar_list_and_get_filing_live() -> None:
    from lazytools.connectors.edgar import EdgarClient

    client = EdgarClient(user_agent=_user_agent())
    filings = client.list_filings("0000320193", form="10-K", limit=1)
    assert filings and filings[0]["form"] == "10-K"
    doc = client.get_filing("0000320193", filings[0]["accession_no"])
    assert doc["content_is_untrusted"] is True
    assert len(doc["content"]) > 1_000  # real prose, not an error page


def test_stooq_quote_and_history_live() -> None:
    from lazytools.connectors.marketdata import MarketDataClient
    from lazytools.connectors.marketdata.adapters import StooqAdapter

    client = MarketDataClient(StooqAdapter())
    quote = client.prices_get("AAPL")
    assert quote["ticker"] == "AAPL" and float(quote["price"]) > 0

    history = client.prices_history("AAPL", range_="1m")
    assert len(history) >= 15  # ~ a month of trading days
    assert all(float(row["close"]) > 0 for row in history)
