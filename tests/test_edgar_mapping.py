"""What a model is allowed to say about a statement.

The interface is deliberately narrow: a model names a line, never a number. So
the tests here are almost all about what gets REJECTED, because that is where
the safety lives — a proposal this module accepts is one the caller will resolve
against its own parsed statement.
"""

from __future__ import annotations

from lazytools.connectors.edgar.mapping import (
    Mapping,
    elements_as_prompt,
    parse_mapping,
    propose,
    statements_as_prompt,
)
from lazytools.connectors.edgar.statements import ReportRef, parse_statement

REPORT = ReportRef(filename="R5.htm", short_name="Consolidated Statements of Operations",
                   category="Statements")

STATEMENT_HTML = (
    '<html><body><table class="report">'
    '<tr><th class="tl" colspan="1" rowspan="2"><div><strong>'
    'Consolidated Statements of Operations - USD ($) $ in Millions</strong></div></th>'
    '<th class="th">Jul. 27, 2024</th></tr>'
    '<tr><td class="pl"><a class="a" href="#" '
    "onclick=\"Show.showAR( this, 'defref_us-gaap_Revenues', window );\">Total revenue</a></td>"
    '<td class="nump">53,803<span></span></td></tr>'
    '<tr><td class="pl"><a class="a" href="#" '
    "onclick=\"Show.showAR( this, 'defref_us-gaap_OperatingIncomeLoss', window );\">"
    'OPERATING INCOME</a></td><td class="nump">12,181<span></span></td></tr>'
    '</table></body></html>')


def _statements():
    return [parse_statement(STATEMENT_HTML, report=REPORT)]


# --- what the model is shown ------------------------------------------------ #


def test_the_prompt_shows_labels_and_concepts_but_never_values() -> None:
    # A figure the model never saw is a figure it cannot anchor a wrong answer to.
    prompt = statements_as_prompt(_statements(), column=0)
    assert "Total revenue" in prompt and "OperatingIncomeLoss" in prompt
    assert "53,803" not in prompt and "53803" not in prompt


def test_the_registry_is_offered_with_its_meanings() -> None:
    # The meaning is what makes an element mappable at all.
    prompt = elements_as_prompt()
    assert "revenue:" in prompt
    assert "revenue-recognition policy" in prompt


# --- what the model may say ------------------------------------------------- #


def test_a_reference_is_accepted_and_carries_no_value() -> None:
    mapping = parse_mapping({"mapped": [
        {"element_id": "revenue", "statement": "Operations", "label": "Total revenue",
         "value": 999, "note": "consolidated"}]})
    assert mapping.refs[0].element_id == "revenue"
    assert not hasattr(mapping.refs[0], "value")


def test_an_element_outside_the_registry_is_rejected_with_its_reason() -> None:
    mapping = parse_mapping({"mapped": [
        {"element_id": "adjusted_ebitda", "label": "Adjusted EBITDA"}]})
    assert mapping.refs == ()
    assert "not an element of the base" in mapping.rejected[0]


def test_a_reference_naming_no_line_is_rejected() -> None:
    mapping = parse_mapping({"mapped": [{"element_id": "revenue", "statement": "Operations"}]})
    assert mapping.refs == () and "named no line label" in mapping.rejected[0]


def test_an_absence_is_kept_with_the_reason_given() -> None:
    mapping = parse_mapping({"absent": [
        {"element_id": "depreciation", "reason": "no cash flow statement provided"}]})
    assert mapping.absences[0].element_id == "depreciation"
    assert "cash flow" in mapping.absences[0].reason


def test_an_absence_with_no_reason_still_says_so() -> None:
    mapping = parse_mapping({"absent": [{"element_id": "depreciation"}]})
    assert mapping.absences[0].reason == "the model gave no reason"


def test_an_answer_that_is_not_an_object_yields_nothing_rather_than_raising() -> None:
    assert parse_mapping(["revenue"]) == Mapping(refs=(), absences=(), rejected=())


# --- a model that answers badly --------------------------------------------- #


def test_a_model_returning_prose_yields_an_empty_mapping_not_a_crash() -> None:
    mapping = propose(_statements(), column=0, agent=lambda task: "I could not do this.")
    assert mapping.refs == () and mapping.rejected


def test_a_model_that_raises_yields_an_empty_mapping() -> None:
    def broken(task: str) -> str:
        raise RuntimeError("no model configured")

    mapping = propose(_statements(), column=0, agent=broken)
    assert mapping.refs == ()
    assert "unusable" in mapping.rejected[0]


def test_json_wrapped_in_prose_is_still_read() -> None:
    def chatty(task: str) -> str:
        return ('Here is the mapping you asked for:\n```json\n'
                '{"mapped": [{"element_id": "revenue", "statement": "Operations", '
                '"label": "Total revenue"}], "absent": []}\n```\nHope that helps.')

    mapping = propose(_statements(), column=0, agent=chatty)
    assert [r.element_id for r in mapping.refs] == ["revenue"]


# --- a model may not claim a figure this code computes ---------------------- #


def test_a_computed_element_is_rejected_however_confidently_it_is_claimed() -> None:
    # A presented line accepted as free cash flow silently replaces the
    # calculation with a relabelled cash-flow line, and every figure downstream
    # inherits it. This happened: focf, dcf and house_ffo all came back equal to
    # CFO, and only the analyst reading the base noticed.
    mapping = parse_mapping({"mapped": [
        {"element_id": "focf", "statement": "Cash Flows", "label": "Free cash flow"}]})
    assert mapping.refs == ()
    assert "computed from other elements" in mapping.rejected[0]


def test_the_prompt_never_offers_a_computed_element() -> None:
    prompt = elements_as_prompt()
    assert "cfo:" in prompt and "focf" not in prompt and "house_ffo" not in prompt


def test_an_element_a_filer_really_does_present_is_still_offered() -> None:
    # Walmart shows one "Depreciation and amortization" line, and many issuers
    # show a total debt line. Those are presented; reconciliation checks them.
    prompt = elements_as_prompt()
    assert "operating_da_total" in prompt and "reported_financial_debt" in prompt
