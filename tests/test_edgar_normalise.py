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

from lazytools.connectors.edgar.normalise import normalise

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
    # EVERY claim to the contested line is rejected, not just the losers.
    # Keeping whichever the model emitted first settles a real ambiguity by the
    # order of a list.
    placed = [k for k in ("operating_da_total", "depreciation", "amortisation_intangibles")
              if k in base.elements and base.elements[k].usable]
    assert placed == []
