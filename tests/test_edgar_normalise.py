"""Producing a normalised base, against the shapes real filings have.

The anchor is Cisco FY2024. Its combined D&A concept resolves to $700m while
amortisation of intangibles separately resolves to $698m, so the combined figure
cannot contain both and using it understates D&A by two thirds. The first cut of
this producer marked it "verified": with depreciation unserved, reconciliation
came back "incomplete", and every non-conflict outcome was being treated as
confirmation.
"""

from __future__ import annotations

from datetime import date

import pytest

from lazytools.connectors.edgar.normalise import normalise

M = 1_000_000


class _Stub:
    """An EdgarService whose every concept the test sets, with no network."""

    def __init__(self, concepts: dict[str, float], sic: str = "3576") -> None:
        self.concepts, self.sic = concepts, sic

    def resolve_company(self, query, *, limit=10):
        return [{"cik": "0000858877", "ticker": "TST", "title": "Test Co"}]

    def issuer_profile(self, cik):
        return {"cik": cik, "name": "Test Co", "tickers": ["TST"],
                "fiscal_year_end": "1231", "sic": self.sic, "sic_description": "Test"}

    def list_filings(self, cik, *, form=None, limit=20, include_history=False):
        return [{"accession_no": "0000858877-24-000017", "form": "10-K",
                 "filed_at": "2024-09-05", "report_date": "2024-12-31", "items": [],
                 "primary_document": "x.htm", "url": "u"}]

    def get_filing_document(self, cik, accession_no, filename, *, raw=False):
        return {"content": "<FilingSummary></FilingSummary>"}

    def company_concept(self, cik, taxonomy, tag):
        if tag not in self.concepts:
            raise KeyError(tag)
        return {"taxonomy": taxonomy, "tag": tag, "units": {"USD": [{
            "start": "2024-01-01", "end": "2024-12-31", "val": self.concepts[tag],
            "accn": "0000858877-24-000017", "form": "10-K", "filed": "2024-09-05"}]}}

    def get_filing(self, cik, accession_no, *, primary_document=None):
        return {}

    def list_filing_documents(self, cik, accession_no):
        return []

    def company_facts(self, cik):
        return {}

    def fiscal_year_end(self, cik):
        return "1231"


BASE_CONCEPTS = {
    "Revenues": 53_803 * M,
    "OperatingIncomeLoss": 12_181 * M,
    "NetCashProvidedByUsedInOperatingActivities": 10_880 * M,
    "PaymentsToAcquirePropertyPlantAndEquipment": 670 * M,
    "PaymentsOfDividends": 6_384 * M,
    "PaymentsForRepurchaseOfCommonStock": 5_787 * M,
    "InterestPaidNet": 583 * M,
    "IncomeTaxesPaidNet": 7_426 * M,
}


def _run(concepts: dict[str, float], **kw):
    return normalise(_Stub({**BASE_CONCEPTS, **concepts}, **kw),
                     company="TST", period="FY2024", as_of=date(2024, 9, 5))


# --- the Cisco case: a combined tag that is not one ------------------------ #


def test_a_combined_da_that_barely_exceeds_one_component_is_refused() -> None:
    base = _run({"DepreciationDepletionAndAmortization": 700 * M,
                 "AmortizationOfIntangibleAssets": 698 * M})
    da = base.elements["operating_da_total"]
    assert da.state == "unreconciled"
    assert base.value("operating_da_total") is None
    assert "does not contain the other component" in da.checks[0].detail


def test_everything_built_on_that_figure_falls_with_it() -> None:
    base = _run({"DepreciationDepletionAndAmortization": 700 * M,
                 "AmortizationOfIntangibleAssets": 698 * M})
    assert base.value("house_operating_ebitda") is None
    assert base.value("house_ffo") is None
    assert "operating_da_total" in base.elements["house_operating_ebitda"].blocked_reason


def test_a_combined_da_that_clears_its_component_is_usable_but_only_reported() -> None:
    # One source and nothing to check it against is not verification.
    base = _run({"DepreciationDepletionAndAmortization": 2_847 * M,
                 "AmortizationOfIntangibleAssets": 200 * M})
    assert base.elements["operating_da_total"].state == "reported"
    assert base.value("house_operating_ebitda") == (12_181 + 2_847) * M


def test_a_combined_da_confirmed_by_both_components_is_verified() -> None:
    base = _run({"DepreciationDepletionAndAmortization": 1_000 * M,
                 "Depreciation": 700 * M, "AmortizationOfIntangibleAssets": 300 * M})
    da = base.elements["operating_da_total"]
    assert da.state == "verified" and da.checks[0].passed


def test_components_that_contradict_the_combined_tag_win() -> None:
    # They are named for what they are; the combined one is not.
    base = _run({"DepreciationDepletionAndAmortization": 700 * M,
                 "Depreciation": 700 * M, "AmortizationOfIntangibleAssets": 698 * M})
    da = base.elements["operating_da_total"]
    assert da.state == "derived" and da.value == 1_398 * M
    # The rejection is recorded on the figure that replaced it: a failed check
    # cannot sit on a usable element, and the derived value is the remedy for
    # that failure rather than a victim of it.
    assert "the combined concept was rejected" in da.checks[0].detail
    assert "presented as a total" in da.checks[0].detail


def test_no_da_route_at_all_blocks_rather_than_guessing() -> None:
    base = _run({})
    assert base.elements["operating_da_total"].state == "unavailable"
    assert base.value("house_operating_ebitda") is None


# --- debt ------------------------------------------------------------------ #


DEBT = {"ShortTermBorrowings": 3_068 * M, "LongTermDebtCurrent": 2_598 * M,
        "LongTermDebtNoncurrent": 33_401 * M}


def test_debt_is_built_from_disjoint_components() -> None:
    base = _run(DEBT)
    debt = base.elements["reported_financial_debt"]
    assert debt.state == "derived" and debt.value == 39_067 * M
    assert len(debt.contributions) == 3


def test_a_missing_debt_component_makes_the_total_a_floor() -> None:
    base = _run({k: v for k, v in DEBT.items() if k != "ShortTermBorrowings"})
    debt = base.elements["reported_financial_debt"]
    assert debt.state == "lower_bound"
    assert base.value("reported_financial_debt") is None
    assert base.lower_bound("reported_financial_debt") == 35_999 * M


def test_adjusted_debt_needs_every_lease_component() -> None:
    base = _run({**DEBT, "FinanceLeaseLiability": 6_723 * M})
    assert base.elements["house_adjusted_debt"].state == "unavailable"
    assert "lease" in base.elements["house_adjusted_debt"].blocked_reason


def test_adjusted_debt_names_the_lease_convention_it_used() -> None:
    base = _run({**DEBT, "FinanceLeaseLiability": 6_723 * M,
                 "OperatingLeaseLiability": 14_324 * M})
    adjusted = base.elements["house_adjusted_debt"]
    assert adjusted.value == 60_114 * M
    assert "leases capitalised" in adjusted.route


# --- cash: the P&G shape --------------------------------------------------- #


def test_a_cash_concept_that_includes_restrictions_is_not_available_cash() -> None:
    # P&G serves only the combined tag. Calling it cash overstates what could
    # repay debt, so it blocks until the restriction is disclosed.
    base = _run({"CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents": 9_556 * M})
    available = base.elements["readily_available_cash"]
    assert available.state == "unreconciled"
    assert base.value("readily_available_cash") is None
    assert not available.checks[0].passed


def test_the_restriction_being_disclosed_unblocks_it() -> None:
    base = _run({"CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents": 9_556 * M,
                 "RestrictedCashAndCashEquivalents": 56 * M})
    assert base.value("readily_available_cash") == 9_500 * M


def test_a_plain_cash_concept_needs_no_subtraction() -> None:
    base = _run({"CashAndCashEquivalentsAtCarryingValue": 7_508 * M})
    assert base.value("readily_available_cash") == 7_508 * M


# --- flows ------------------------------------------------------------------ #


def test_discretionary_cash_flow_deducts_buybacks_as_well_as_dividends() -> None:
    base = _run({})
    # 10,880 CFO - 670 capex = 10,210 FOCF; less 6,384 dividends and 5,787 buybacks.
    assert base.value("focf") == 10_210 * M
    assert base.value("dcf") == (10_210 - 6_384 - 5_787) * M


def test_capex_includes_intangibles_when_they_are_disclosed() -> None:
    base = _run({"PaymentsToAcquireIntangibleAssets": 100 * M})
    assert base.value("house_capex") == 770 * M
    assert "capex_intangibles" in base.elements["house_capex"].route


# --- the gate rides along --------------------------------------------------- #


def test_the_ontology_is_carried_on_the_result() -> None:
    assert _run({}, sic="6021").ontology == "bank"


def test_an_unresolvable_company_raises_rather_than_returning_an_empty_base() -> None:
    stub = _Stub({})
    stub.resolve_company = lambda q, *, limit=10: []  # type: ignore[assignment]
    with pytest.raises(ValueError, match="matched no EDGAR filer"):
        normalise(stub, company="nope", period="FY2024")


# --- the rendered-statement fallback ---------------------------------------- #


_CF_STATEMENT = (
    '<html><body><table class="report">'
    '<tr><th class="tl" colspan="1" rowspan="2"><div><strong>'
    'Consolidated Statements of Cash Flows - USD ($) $ in Millions</strong></div></th>'
    '<th class="th" colspan="1">12 Months Ended</th></tr>'
    '<tr><th class="th">Dec. 31, 2024</th></tr>'
    '<tr><td class="pl"><a class="a" href="javascript:void(0);" '
    "onclick=\"Show.showAR( this, 'defref_us-gaap_SomeFilerSpecificTag', window );\">"
    'Depreciation and amortization</a></td>'
    '<td class="nump">12,973<span></span></td></tr>'
    '</table></body></html>')

_SUMMARY = ('<FilingSummary><Report><HtmlFileName>R8.htm</HtmlFileName>'
            '<ShortName>Consolidated Statements of Cash Flows</ShortName>'
            '<MenuCategory>Statements</MenuCategory></Report></FilingSummary>')


class _StubWithStatements(_Stub):
    """No D&A concept at all, but the figure is on the face of a statement."""

    def get_filing_document(self, cik, accession_no, filename, *, raw=False):
        return {"content": _SUMMARY if filename.endswith(".xml") else _CF_STATEMENT}


def test_da_absent_from_every_concept_is_read_off_the_rendered_statement() -> None:
    # This is what an analyst does first and the fact APIs cannot do at all.
    base = normalise(_StubWithStatements(BASE_CONCEPTS), company="TST", period="FY2024",
                     as_of=date(2024, 9, 5))
    da = base.elements["operating_da_total"]
    assert da.value == 12_973 * M
    assert da.state == "reported"
    assert "Cash Flows" in da.route and "Depreciation and amortization" in da.route


def test_the_statement_figure_carries_the_scale_the_table_declared() -> None:
    # The table says 12,973 and means millions. Reading it unscaled beside an
    # XBRL fact is a factor-of-a-million error that looks like data.
    base = normalise(_StubWithStatements(BASE_CONCEPTS), company="TST", period="FY2024",
                     as_of=date(2024, 9, 5))
    assert base.value("house_operating_ebitda") == (12_181 + 12_973) * M


def test_a_statement_column_for_another_period_is_not_used() -> None:
    class _WrongPeriod(_StubWithStatements):
        def get_filing_document(self, cik, accession_no, filename, *, raw=False):
            content = (_SUMMARY if filename.endswith(".xml")
                       else _CF_STATEMENT.replace("Dec. 31, 2024", "Dec. 31, 2019"))
            return {"content": content}

    base = normalise(_WrongPeriod(BASE_CONCEPTS), company="TST", period="FY2024",
                     as_of=date(2024, 9, 5))
    assert base.elements["operating_da_total"].state == "unavailable"
