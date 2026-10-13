from __future__ import annotations

from datetime import date

import pytest

from lazytools.connectors.yahoo_finance import YahooFundamentalsClient, YahooFundamentalsError, compare_with_base
from lazytools.financials.normalised import Element, NormalisedBase


class _Series(dict):
    def items(self):
        return super().items()


class _Frame:
    def __init__(self, rows: dict[str, float], period: date = date(2024, 12, 31)) -> None:
        self.empty = False
        self.columns = [period]
        self._rows = rows

    def __getitem__(self, _column):
        return _Series(self._rows)


class _Ticker:
    income_stmt = _Frame({"Total Revenue": 1000.0, "Operating Income": 100.0})
    balance_sheet = _Frame({"Cash And Cash Equivalents": 50.0, "Current Debt": 25.0})
    cash_flow = _Frame({"Operating Cash Flow": 120.0})
    quarterly_income_stmt = income_stmt
    quarterly_balance_sheet = balance_sheet
    quarterly_cash_flow = cash_flow


def _base() -> NormalisedBase:
    return NormalisedBase(
        issuer_name="Example plc", cik="x", ontology="corporate", open_signals=(),
        accounting_standard="us-gaap", currency="USD", period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31), information_cutoff=date(2025, 2, 1),
        perimeter_status="matched", accession="x", elements={
            "revenue": Element("revenue", 1000.0, "reported", sources=("filing",)),
            "operating_income": Element("operating_income", 99.0, "reported", sources=("filing",)),
            "cfo": Element("cfo", 120.0, "reported", sources=("filing",)),
        },
    )


def test_fetch_reads_all_three_statement_types_without_persistence() -> None:
    client = YahooFundamentalsClient(ticker_factory=lambda symbol: _Ticker(), cache_dir="unused")
    result = client.fetch("exm")
    assert result.ticker == "EXM"
    assert {item.statement for item in result.statements} == {"income", "balance", "cash_flow"}
    assert result.statement("income", date(2024, 12, 31)).values["Total Revenue"] == 1000.0


def test_comparison_returns_findings_but_never_replaces_filing_data() -> None:
    vendor = YahooFundamentalsClient(ticker_factory=lambda symbol: _Ticker(), cache_dir="unused").fetch("EXM")
    findings = {item.metric: item for item in compare_with_base(_base(), vendor)}
    assert findings["revenue"].status == "match"
    assert findings["operating_income"].status == "match"
    assert findings["cfo"].status == "match"
    assert findings["cash_and_equivalents"].status == "missing_primary"
    assert _base().value("revenue") == 1000.0


def test_empty_vendor_statements_fail_loudly() -> None:
    class _Empty:
        income_stmt = balance_sheet = cash_flow = _Frame({})
        quarterly_income_stmt = quarterly_balance_sheet = quarterly_cash_flow = income_stmt

    with pytest.raises(YahooFundamentalsError, match="no annual statements"):
        YahooFundamentalsClient(ticker_factory=lambda symbol: _Empty()).fetch("EXM")
