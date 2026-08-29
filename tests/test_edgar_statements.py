"""Reading rendered statements, against the render that motivated the module.

The fixtures are cut from Cisco's FY2024 10-K as EDGAR renders it. That filing
is the reason this module exists: its entity-wide XBRL fact for
``AmortizationOfIntangibleAssets`` is $698m, which is only the operating-expense
slice. A further $955m sits in cost of sales, and the $1,653m total exists only
as a dimensioned fact the entity-wide API never returns. The rendered note
carries all four figures with the label that says which is which.
"""

from __future__ import annotations

import pytest

from lazytools.connectors.edgar.statements import (
    ReportRef,
    list_reports,
    parse_statement,
)

REPORT = ReportRef(filename="R72.htm", short_name="Amortization schedule", category="Details")


def _row(concept: str, label: str, *values: str, strong: bool = False) -> str:
    text = f"<strong>{label}</strong>" if strong else label
    cells = "".join(f'<td class="nump">{v}<span></span></td>' if v else
                    '<td class="text">&#160;<span></span></td>' for v in values)
    return (f'<tr><td class="pl" valign="top"><a class="a" href="javascript:void(0);" '
            f"onclick=\"Show.showAR( this, 'defref_{concept}', window );\">{text}</a></td>"
            f"{cells}</tr>")


def _table(title: str, rows: str, columns: str = '<th class="th">FY2024</th>') -> str:
    return ("<html><body>"
            f'<table class="report"><tr><th class="tl" colspan="1" rowspan="2">'
            f"<div><strong>{title}</strong></div></th>{columns}</tr>{rows}</table>"
            "</body></html>")


# The four ways one concept appears in Cisco's amortisation note.
CISCO_NOTE = _table(
    "Goodwill and Purchased Intangible Assets - Schedule of Amortization (Details) - USD ($) $ in Millions",
    _row("us-gaap_AcquiredFiniteLivedIntangibleAssetsLineItems", "Intangible Asset [Line Items]", "")
    + _row("us-gaap_AmortizationOfIntangibleAssets", "Amortization of purchased intangible assets", "698")
    + _row("us-gaap_IncomeStatementLocationAxis", "Cost of sales", "")
    + _row("us-gaap_AcquiredFiniteLivedIntangibleAssetsLineItems", "Intangible Asset [Line Items]", "")
    + _row("us-gaap_AmortizationOfIntangibleAssets", "Amortization of purchased intangible assets", "955")
    + _row("us-gaap_IncomeStatementLocationAxis", "Operating expenses", "")
    + _row("us-gaap_AcquiredFiniteLivedIntangibleAssetsLineItems", "Intangible Asset [Line Items]", "")
    + _row("us-gaap_AmortizationOfIntangibleAssets", "Amortization of purchased intangible assets", "698")
    + _row("us-gaap_IncomeStatementLocationAxis", "Total", "")
    + _row("us-gaap_AcquiredFiniteLivedIntangibleAssetsLineItems", "Intangible Asset [Line Items]", "")
    + _row("us-gaap_AmortizationOfIntangibleAssets", "Amortization of purchased intangible assets", "1,653"),
)


def _note():
    return parse_statement(CISCO_NOTE, report=REPORT)


# --- the case the module exists for --------------------------------------- #


def test_each_appearance_carries_the_label_that_says_which_slice_it_is() -> None:
    by_section = {ln.section: ln.values[0] for ln in _note().lines
                  if ln.tag == "AmortizationOfIntangibleAssets" and ln.values[0] is not None}
    assert by_section["Cost of sales"] == 955_000_000
    assert by_section["Operating expenses"] == 698_000_000
    assert by_section["Total"] == 1_653_000_000
    # The undimensioned row is exactly the figure the entity-wide API serves,
    # and it is NOT the total. That is the whole bug in one assertion.
    assert by_section[None] == 698_000_000


def test_structural_rows_never_become_the_section_label() -> None:
    # "[Line Items]" sits between the member name and its figures; letting it
    # win makes every dimensioned row carry the same meaningless label.
    assert "Intangible Asset [Line Items]" not in {ln.section for ln in _note().lines}


def test_one_concept_appearing_four_times_is_returned_four_times() -> None:
    assert len(_note().by_tag("AmortizationOfIntangibleAssets")) == 4


# --- scale ----------------------------------------------------------------- #


def test_the_rendered_scale_is_applied_so_values_match_xbrl_facts() -> None:
    # The table says 1,653; the fact is 1,653,000,000. Returning the former
    # beside the latter is a factor-of-a-million error that looks like data.
    total = next(ln for ln in _note().lines if ln.section == "Total")
    assert total.values[0] == 1_653_000_000


def test_a_money_scale_is_read_past_a_share_scale() -> None:
    html = _table("Statements of Operations - USD ($) shares in Millions, $ in Thousands",
                  _row("us-gaap_Revenues", "Revenue", "1,000"))
    assert parse_statement(html, report=REPORT).lines[0].values[0] == 1_000_000


def test_an_unreadable_scale_withholds_the_values_rather_than_guessing() -> None:
    html = _table("Some Schedule (Details)", _row("us-gaap_Revenues", "Revenue", "1,000"))
    assert parse_statement(html, report=REPORT).lines[0].values == (None,)


def test_a_money_column_with_no_stated_scale_is_taken_as_units() -> None:
    html = _table("Balance Sheet - USD ($)", _row("us-gaap_Assets", "Total assets", "1,234"))
    assert parse_statement(html, report=REPORT).lines[0].values[0] == 1_234


# --- values ---------------------------------------------------------------- #


def test_parentheses_are_read_as_negative() -> None:
    html = _table("Cash Flows - USD ($) $ in Millions",
                  _row("us-gaap_PaymentsToAcquirePropertyPlantAndEquipment", "Capex", "(670)"))
    assert parse_statement(html, report=REPORT).lines[0].values[0] == -670_000_000


def test_a_blank_cell_is_none_and_not_zero() -> None:
    html = _table("X - USD ($) $ in Millions", _row("us-gaap_Revenues", "Revenue", ""))
    assert parse_statement(html, report=REPORT).lines[0].values[0] is None


def test_a_dash_is_read_as_absent_not_as_a_number() -> None:
    html = _table("X - USD ($) $ in Millions", _row("us-gaap_Revenues", "Revenue", "&#8212;"))
    assert parse_statement(html, report=REPORT).lines[0].values[0] is None


# --- concepts -------------------------------------------------------------- #


def test_the_tag_is_split_from_its_taxonomy_prefix() -> None:
    assert _note().by_tag("AmortizationOfIntangibleAssets")[0].concept.startswith("us-gaap_")


# --- the report index ------------------------------------------------------ #


class _Stub:
    def __init__(self, summary: str) -> None:
        self.summary = summary

    def get_filing_document(self, cik, accession_no, filename, *, raw=False):
        return {"content": self.summary}


SUMMARY = """<FilingSummary>
<MyReports>
<Report><HtmlFileName>R3.htm</HtmlFileName><ShortName>Consolidated Balance Sheets</ShortName>
<MenuCategory>Statements</MenuCategory></Report>
<Report><HtmlFileName>R4.htm</HtmlFileName><ShortName>Consolidated Balance Sheets (Parenthetical)</ShortName>
<MenuCategory>Statements</MenuCategory></Report>
<Report><HtmlFileName>R72.htm</HtmlFileName><ShortName>Amortization (Details)</ShortName>
<MenuCategory>Details</MenuCategory></Report>
</MyReports></FilingSummary>"""


def test_the_report_index_is_read_from_the_filings_own_summary() -> None:
    reports = list_reports(_Stub(SUMMARY), "0000858877", "x")
    assert [r.filename for r in reports] == ["R3.htm", "R4.htm", "R72.htm"]


def test_a_parenthetical_is_not_a_primary_statement() -> None:
    reports = list_reports(_Stub(SUMMARY), "0000858877", "x")
    assert [r.short_name for r in reports if r.is_primary_statement] == ["Consolidated Balance Sheets"]


def test_a_filing_with_no_summary_says_so_instead_of_returning_nothing() -> None:
    # Filings older than EDGAR's renderer have no rendered statements at all.
    # An empty list would read as "this filing has no balance sheet".
    with pytest.raises(ValueError, match="no readable"):
        list_reports(_Stub(""), "0000858877", "x")


def test_a_summary_that_parses_to_no_reports_is_an_error_not_an_absence() -> None:
    with pytest.raises(ValueError, match="listed no reports"):
        list_reports(_Stub("<FilingSummary></FilingSummary>"), "0000858877", "x")


# --- one table, several units: the EPS trap -------------------------------- #


def test_a_per_share_row_is_not_multiplied_by_the_money_scale() -> None:
    # "$ in Millions, except per share data" means exactly what it says. Scaling
    # an EPS of 0.47 by a million gives 470,000 -- a number that looks like data.
    html = _table("Statements of Operations - USD ($) $ in Millions, except per share data",
                  _row("us-gaap_NetIncomeLoss", "Net income", "10,320")
                  + _row("us-gaap_EarningsPerShareDiluted", "Diluted earnings per share", "0.47"))
    lines = parse_statement(html, report=REPORT).lines
    assert lines[0].values[0] == 10_320_000_000
    assert lines[1].values[0] == 0.47


def test_a_share_count_takes_the_share_scale_not_the_money_scale() -> None:
    html = _table("Statements of Operations - USD ($) shares in Thousands, $ in Millions",
                  _row("us-gaap_NetIncomeLoss", "Net income", "10,320")
                  + _row("us-gaap_WeightedAverageNumberOfSharesOutstandingBasic",
                         "Weighted average shares", "4,000"))
    lines = parse_statement(html, report=REPORT).lines
    assert lines[0].values[0] == 10_320_000_000
    assert lines[1].values[0] == 4_000_000


def test_a_share_count_with_no_stated_share_scale_is_withheld() -> None:
    html = _table("Statements of Operations - USD ($) $ in Millions",
                  _row("us-gaap_WeightedAverageNumberOfSharesOutstandingBasic",
                       "Weighted average shares", "4,000"))
    assert parse_statement(html, report=REPORT).lines[0].values[0] is None


def test_a_bare_scale_survives_an_except_per_share_clause() -> None:
    # Rejecting any header that mentions shares withheld ordinary money rows.
    html = _table("Balance Sheets In Thousands, except per share data",
                  _row("us-gaap_Assets", "Total assets", "1,234"))
    assert parse_statement(html, report=REPORT).lines[0].values[0] == 1_234_000


# --- multi-row headers ------------------------------------------------------ #


def test_the_period_labels_come_from_the_last_header_row_not_the_spanning_one() -> None:
    # Cisco renders "12 Months Ended" above three dates. Taking the first header
    # row leaves one column label standing over three columns of values.
    html = ("<html><body><table class=\"report\">"
            "<tr><th class=\"tl\" colspan=\"1\" rowspan=\"2\"><div><strong>"
            "Statements of Operations - USD ($) $ in Millions</strong></div></th>"
            "<th class=\"th\" colspan=\"3\">12 Months Ended</th></tr>"
            "<tr><th class=\"th\">Jul. 26, 2025</th><th class=\"th\">Jul. 27, 2024</th>"
            "<th class=\"th\">Jul. 29, 2023</th></tr>"
            + _row("us-gaap_Revenues", "Total revenue", "56,654", "53,803", "56,998")
            + "</table></body></html>")
    result = parse_statement(html, report=REPORT)
    assert len(result.columns) == 3
    assert result.columns[0].startswith("Jul. 26")
    assert result.lines[0].values == (56_654_000_000, 53_803_000_000, 56_998_000_000)
