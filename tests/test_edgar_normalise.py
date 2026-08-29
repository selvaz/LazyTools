"""Producing a normalised base from a filing's own statements.

The division under test: a model says WHICH line, code reads the VALUE. So the
tests that matter are the ones where the model is wrong — it names a line that
does not exist, names two, or supplies a figure. None of those can put a number
into the base.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from lazytools.financials.normalised import Element
from lazytools.connectors.edgar.normalise import (
    _DEBT_SCOPED,
    _LEASE_TABLE,
    _combine,
    _settle,
    normalise,
)

M = 1_000_000

_SUMMARY = (
    "<FilingSummary>"
    "<Report><HtmlFileName>R5.htm</HtmlFileName>"
    "<ShortName>Consolidated Statements of Operations</ShortName>"
    "<MenuCategory>Statements</MenuCategory></Report>"
    "<Report><HtmlFileName>R8.htm</HtmlFileName>"
    "<ShortName>Consolidated Statements of Cash Flows</ShortName>"
    "<MenuCategory>Statements</MenuCategory></Report>"
    "</FilingSummary>")


def _line(concept: str, label: str, value: str) -> str:
    return ('<tr><td class="pl"><a class="a" href="#" '
            f"onclick=\"Show.showAR( this, 'defref_us-gaap_{concept}', window );\">{label}</a></td>"
            f'<td class="nump">{value}<span></span></td></tr>')


def _table(title: str, rows: str, header: str = "Dec. 31, 2024") -> str:
    return ('<html><body><table class="report">'
            f'<tr><th class="tl" colspan="1" rowspan="2"><div><strong>{title}'
            f'</strong></div></th><th class="th">{header}</th></tr>{rows}'
            "</table></body></html>")


OPERATIONS = _table(
    "Consolidated Statements of Operations - USD ($) $ in Millions",
    _line("Revenues", "Total revenue", "53,803")
    + _line("OperatingIncomeLoss", "OPERATING INCOME", "12,181")
    + _line("DebtCurrent", "Short-term debt", "11,341"))

CASHFLOW = _table(
    "Consolidated Statements of Cash Flows - USD ($) $ in Millions",
    _line("DepreciationAmortizationAndAccretionNet", "Depreciation and amortization", "2,208")
    + _line("NetCashProvidedByUsedInOperatingActivities", "Net cash from operations", "10,880")
    + _line("PaymentsToAcquirePropertyPlantAndEquipment", "Purchases of property", "670"))


class _Stub:
    """An EdgarService serving one filing with two rendered statements."""

    def __init__(self, sic: str = "3576") -> None:
        self.sic = sic

    def resolve_company(self, query, *, limit=10):
        return [{"cik": "0000858877", "ticker": "TST", "title": "Test Co"}]

    def issuer_profile(self, cik):
        return {"cik": cik, "name": "Test Co", "tickers": ["TST"],
                "fiscal_year_end": "1231", "sic": self.sic, "sic_description": "Test"}

    def list_filings(self, cik, *, form=None, limit=20, include_history=False):
        if form not in (None, "10-K"):
            return []
        return [{"accession_no": "0000858877-25-000111", "form": "10-K",
                 "filed_at": "2025-02-01", "report_date": "2024-12-31", "items": [],
                 "primary_document": "x.htm", "url": "u"}]

    def get_filing_document(self, cik, accession_no, filename, *, raw=False):
        if filename.endswith(".xml"):
            return {"content": _SUMMARY}
        return {"content": OPERATIONS if filename == "R5.htm" else CASHFLOW}

    def get_filing(self, cik, accession_no, *, primary_document=None):
        return {}

    def list_filing_documents(self, cik, accession_no):
        return []

    def company_concept(self, cik, taxonomy, tag):
        raise KeyError(tag)

    def company_facts(self, cik):
        return {}

    def fiscal_year_end(self, cik):
        return "1231"


def _agent(mapped: list[dict], absent: list[dict] | None = None):
    payload = json.dumps({"mapped": mapped, "absent": absent or []})
    return lambda task: payload


def _run(mapped, absent=None, **kw):
    return normalise(_Stub(**kw), company="TST", period="FY2024",
                     as_of=date(2025, 3, 1), agent=_agent(mapped, absent))


GOOD = [
    {"element_id": "revenue", "statement": "Operations", "label": "Total revenue"},
    {"element_id": "operating_income", "statement": "Operations", "label": "OPERATING INCOME"},
    {"element_id": "operating_da_total", "statement": "Cash Flows",
     "label": "Depreciation and amortization"},
    {"element_id": "cfo", "statement": "Cash Flows", "label": "Net cash from operations"},
    {"element_id": "capex_ppe", "statement": "Cash Flows", "label": "Purchases of property"},
]


# --- the model names a line; the code reads the value ---------------------- #


def test_a_mapped_line_takes_its_value_from_the_parsed_statement() -> None:
    base = _run(GOOD)
    assert base.value("revenue") == 53_803 * M
    assert "Total revenue" in base.elements["revenue"].route


def test_the_statement_scale_is_applied_not_the_rendered_digits() -> None:
    # The table shows 53,803 and means millions.
    assert _run(GOOD).value("revenue") == 53_803 * M


def test_a_value_supplied_by_the_model_is_ignored_entirely() -> None:
    # The interface cannot express a model-supplied figure, so an attempt to
    # smuggle one changes nothing.
    lying = [dict(GOOD[0], value=999), *GOOD[1:]]
    assert _run(lying).value("revenue") == 53_803 * M


def test_a_line_the_model_invented_produces_no_element() -> None:
    invented = [{"element_id": "revenue", "statement": "Operations",
                 "label": "Adjusted revenue excluding items"}]
    base = _run(invented)
    assert base.get("revenue") is None


def test_an_ambiguous_label_is_dropped_rather_than_resolved_by_luck() -> None:
    class _Ambiguous(_Stub):
        def get_filing_document(self, cik, accession_no, filename, *, raw=False):
            if filename.endswith(".xml"):
                return {"content": _SUMMARY}
            if filename == "R5.htm":
                return {"content": _table(
                    "Consolidated Statements of Operations - USD ($) $ in Millions",
                    _line("Revenues", "Total revenue", "53,803")
                    + _line("Revenues", "Total revenue", "39,253"))}
            return {"content": CASHFLOW}

    base = normalise(_Ambiguous(), company="TST", period="FY2024", as_of=date(2025, 3, 1),
                     agent=_agent([GOOD[0]]))
    assert base.get("revenue") is None


# --- derivations, which the model has no part in --------------------------- #


def test_ebitda_is_derived_from_the_mapped_lines() -> None:
    base = _run(GOOD)
    assert base.value("house_operating_ebitda") == (12_181 + 2_208) * M
    assert base.elements["house_operating_ebitda"].state == "derived"


def test_free_cash_flow_is_derived_and_records_its_inputs() -> None:
    base = _run(GOOD)
    assert base.value("focf") == (10_880 - 670) * M
    assert {c.source for c in base.elements["focf"].contributions} == {"cfo", "house_capex"}


def test_a_missing_input_blocks_everything_built_on_it() -> None:
    without_da = [g for g in GOOD if g["element_id"] != "operating_da_total"]
    base = _run(without_da)
    assert base.value("house_operating_ebitda") is None
    assert "operating_da_total" in base.elements["house_operating_ebitda"].blocked_reason


def test_a_total_built_from_partial_components_is_a_floor() -> None:
    partial = [*GOOD, {"element_id": "short_term_borrowings", "statement": "Operations",
                       "label": "Short-term debt"}]
    base = _run(partial)
    debt = base.elements["reported_financial_debt"]
    assert debt.state == "lower_bound"
    assert base.value("reported_financial_debt") is None
    assert base.lower_bound("reported_financial_debt") == 11_341 * M


# --- what the filing does not say ------------------------------------------ #


def test_an_absence_the_model_reported_becomes_a_blocked_element() -> None:
    base = _run(GOOD, absent=[{"element_id": "depreciation",
                               "reason": "not broken out on any statement"}])
    depreciation = base.elements["depreciation"]
    assert depreciation.state == "unavailable"
    assert "not broken out" in depreciation.blocked_reason


def test_a_period_no_rendered_column_covers_yields_no_figures() -> None:
    base = normalise(_Stub(), company="TST", period="FY2019", as_of=date(2025, 3, 1),
                     agent=_agent(GOOD))
    assert base.elements == {}


# --- the envelope ----------------------------------------------------------- #


def test_the_ontology_gate_rides_along_without_stopping_the_run() -> None:
    base = _run(GOOD, sic="6021")
    assert base.ontology == "bank" and base.value("revenue") == 53_803 * M


def test_the_base_is_reproducible_from_its_own_envelope() -> None:
    base = _run(GOOD)
    assert base.information_cutoff == date(2025, 3, 1)
    assert base.accession == "0000858877-25-000111"


def test_an_unresolvable_company_raises_rather_than_returning_an_empty_base() -> None:
    stub = _Stub()
    stub.resolve_company = lambda q, *, limit=10: []  # type: ignore[assignment]
    with pytest.raises(ValueError, match="matched no EDGAR filer"):
        normalise(stub, company="nope", period="FY2024")


# --- the mapping cache ------------------------------------------------------ #


def test_a_cached_mapping_means_the_model_is_not_asked_again() -> None:
    from lazytools.connectors.edgar.mapping_store import MappingStore

    calls = []

    def counting(task: str) -> str:
        calls.append(task)
        return json.dumps({"mapped": GOOD, "absent": []})

    store = MappingStore()
    first = normalise(_Stub(), company="TST", period="FY2024", as_of=date(2025, 3, 1),
                      agent=counting, store=store)
    second = normalise(_Stub(), company="TST", period="FY2024", as_of=date(2025, 3, 1),
                       agent=counting, store=store)
    assert len(calls) == 1
    assert first.value("revenue") == second.value("revenue") == 53_803 * M


def test_two_runs_over_one_filing_agree_when_the_mapping_is_cached() -> None:
    # The failure this exists for: two live runs over Cisco's FY2024 filing
    # placed different elements, so the same question answered differently.
    from lazytools.connectors.edgar.mapping_store import MappingStore

    answers = iter([json.dumps({"mapped": GOOD, "absent": []}),
                    json.dumps({"mapped": GOOD[:1], "absent": []})])
    store = MappingStore()
    kw = dict(company="TST", period="FY2024", as_of=date(2025, 3, 1), store=store)
    first = normalise(_Stub(), agent=lambda t: next(answers), **kw)
    second = normalise(_Stub(), agent=lambda t: next(answers), **kw)
    assert set(first.elements) == set(second.elements)


def test_a_model_that_placed_nothing_is_not_cached_as_an_answer() -> None:
    # Caching a failed run would make its failure permanent.
    from lazytools.connectors.edgar.mapping_store import MappingStore

    store = MappingStore()
    normalise(_Stub(), company="TST", period="FY2024", as_of=date(2025, 3, 1),
              agent=lambda t: "the model gave up", store=store)
    assert len(store) == 0


def test_one_presented_line_cannot_become_two_different_elements() -> None:
    # A model mapped "Depreciation and amortization" to BOTH depreciation and
    # amortisation of intangibles, so the same figure was admitted twice and
    # only failed later, at its own reconciliation.
    doubled = [*GOOD,
               {"element_id": "depreciation", "statement": "Cash Flows",
                "label": "Depreciation and amortization"},
               {"element_id": "amortisation_intangibles", "statement": "Cash Flows",
                "label": "Depreciation and amortization"}]
    base = _run(doubled)
    # The figure is admitted ONCE, as the whole. The registry says the other two
    # claimants are parts of it, so a combined line is the total and the parts
    # are not separable from it — settled from the registry rather than from
    # whichever claim the model emitted first.
    placed = [k for k in ("operating_da_total", "depreciation", "amortisation_intangibles")
              if k in base.elements and base.elements[k].usable]
    assert placed == ["operating_da_total"]


# --- a total contested by its own components -------------------------------- #


def test_a_total_wins_the_line_its_own_components_also_claimed() -> None:
    # Walmart shows ONE combined "Depreciation and amortization" line. The model
    # claimed it as both the total and as depreciation; dropping both left the
    # issuer with no D&A at all, and EBITDA and FFO fell with it. The registry
    # already says which of the two is the whole, so this is not a coin flip.
    assert _settle(["operating_da_total", "depreciation"]) == {"depreciation"}


def test_two_unrelated_elements_on_one_line_both_go() -> None:
    assert _settle(["revenue", "cfo"]) == {"revenue", "cfo"}


def test_a_total_contested_by_something_that_is_not_its_component_still_goes() -> None:
    # "Total debt" claimed as both reported_financial_debt and cash is a real
    # conflict; being a total does not entitle it to win one.
    assert _settle(["reported_financial_debt", "cash"]) == {"reported_financial_debt", "cash"}


def test_two_totals_on_one_line_are_a_conflict_like_any_other() -> None:
    assert _settle(["operating_da_total", "reported_financial_debt"]) == {
        "operating_da_total", "reported_financial_debt"}


def test_an_uncontested_claim_is_left_alone() -> None:
    assert _settle(["revenue"]) == set()


# --- scope, not just resolution ---------------------------------------------- #


def test_a_debt_maturity_is_not_read_off_a_lease_table() -> None:
    # NVIDIA files no debt maturity schedule at all, so the model took the
    # lease commitment schedule: the years line up, the labels look right, and
    # the result was a $2.1bn maturity ladder for an issuer with $8.5bn of
    # debt. Every figure was real, correctly scaled and from the filing. Only
    # the scope was wrong, which is why nothing downstream could catch it.
    assert "debt_maturity_y1" in _DEBT_SCOPED
    assert _LEASE_TABLE.search("Leases - Schedule of Future Minimum Lease Payments")
    assert not _LEASE_TABLE.search("Debt - Schedule of Debt")


def test_a_combined_capex_line_survives_being_claimed_by_two_kinds_of_capex() -> None:
    # NVIDIA presents one "Purchases related to property and equipment and
    # intangible assets" line. Claimed as both, both were dropped, and free
    # cash flow went with them. The parts sum into house_capex with equal
    # weight, so awarding it to one leaves the total identical.
    assert _settle(["capex_ppe", "capex_intangibles"]) == {"capex_intangibles"}
    assert _settle(["capex_intangibles", "capex_ppe"]) == {"capex_intangibles"}


def test_two_parts_of_DIFFERENT_wholes_are_still_a_conflict() -> None:
    # The equal-weight rule must not become a general licence to keep one of
    # any two claimants.
    assert _settle(["capex_ppe", "depreciation"]) == {"capex_ppe", "depreciation"}


def test_a_missing_addend_makes_a_sum_a_floor_rather_than_nothing() -> None:
    # NVIDIA has no finance leases at all, and requiring them made its adjusted
    # debt unavailable: the analysis lost a figure because a real company had
    # none of something.
    elements = {
        "reported_financial_debt": Element("reported_financial_debt", 8463.0, "reported",
                                           sources=("Debt schedule",)),
        "operating_lease_total": Element("operating_lease_total", 1807.0, "derived",
                                         route="current + noncurrent"),
    }
    _combine(elements, "house_adjusted_debt",
             {"reported_financial_debt": 1, "finance_lease_total": 1,
              "operating_lease_total": 1},
             floor_when_missing=("finance_lease_total", "operating_lease_total"))
    result = elements["house_adjusted_debt"]
    assert result.value == 10270.0
    assert result.state == "lower_bound"
    assert "finance_lease_total" in result.blocked_reason


def test_a_complete_sum_is_not_downgraded_to_a_floor() -> None:
    elements = {
        "reported_financial_debt": Element("reported_financial_debt", 8463.0, "reported",
                                           sources=("Debt schedule",)),
        "operating_lease_total": Element("operating_lease_total", 1807.0, "derived",
                                         route="current + noncurrent"),
        "finance_lease_total": Element("finance_lease_total", 500.0, "derived",
                                       route="current + noncurrent"),
    }
    _combine(elements, "house_adjusted_debt",
             {"reported_financial_debt": 1, "finance_lease_total": 1,
              "operating_lease_total": 1},
             floor_when_missing=("finance_lease_total", "operating_lease_total"))
    assert elements["house_adjusted_debt"].state == "derived"
