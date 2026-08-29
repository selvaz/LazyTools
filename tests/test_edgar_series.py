"""Several periods of one issuer, and the disagreements between filings."""

from __future__ import annotations

from datetime import date

from lazytools.connectors.edgar.series import (
    RESTATEMENT_TOLERANCE,
    Restatement,
    Series,
    _disagreements,
)
from lazytools.financials.normalised import Element, NormalisedBase


def _base(accession: str, end: date, **values: float | None) -> NormalisedBase:
    elements = {}
    for key, value in values.items():
        elements[key] = (
            Element(key, None, "unavailable", blocked_reason="not found")
            if value is None else
            Element(key, value, "reported", sources=(f"{accession} / Income",)))
    return NormalisedBase(
        issuer_name="Test Co", cik="0000000001", ontology="corporate",
        open_signals=(), accounting_standard="us-gaap", currency="USD",
        period_start=date(end.year - 1, end.month, 1), period_end=end,
        information_cutoff=date(2025, 6, 1), perimeter_status="unavailable",
        accession=accession, elements=elements)


# --- disagreement between two filings about one period ---------------------- #


def test_two_filings_disagreeing_on_a_period_is_reported_not_resolved() -> None:
    # A restatement is a finding about the issuer. Silently preferring one
    # filing would delete it.
    found = _disagreements(
        earlier=_base("acc-2023", date(2023, 1, 31), revenue=611_289_000_000.0),
        later=_base("acc-2025", date(2023, 1, 31), revenue=615_000_000_000.0))
    assert len(found) == 1
    assert found[0].first_accession == "acc-2023"
    assert found[0].later_accession == "acc-2025"
    assert found[0].change == 3_711_000_000.0


def test_agreement_is_not_reported() -> None:
    assert _disagreements(
        earlier=_base("a", date(2023, 1, 31), revenue=611_289_000_000.0),
        later=_base("b", date(2023, 1, 31), revenue=611_289_000_000.0)) == []


def test_a_rounding_difference_is_not_a_restatement() -> None:
    # Presented statements are rounded to the table's unit, so an exact test
    # would report the rounding as news.
    value = 611_289_000_000.0
    nudge = value * RESTATEMENT_TOLERANCE / 2
    assert _disagreements(
        earlier=_base("a", date(2023, 1, 31), revenue=value),
        later=_base("b", date(2023, 1, 31), revenue=value + nudge)) == []


def test_a_difference_just_beyond_the_tolerance_IS_a_restatement() -> None:
    value = 611_289_000_000.0
    nudge = value * RESTATEMENT_TOLERANCE * 2
    assert len(_disagreements(
        earlier=_base("a", date(2023, 1, 31), revenue=value),
        later=_base("b", date(2023, 1, 31), revenue=value + nudge))) == 1


def test_an_element_one_filing_could_not_place_is_not_a_restatement() -> None:
    # An absence in one filing says nothing about the other's figure, and
    # reporting it would bury the real restatements.
    assert _disagreements(
        earlier=_base("a", date(2023, 1, 31), revenue=None),
        later=_base("b", date(2023, 1, 31), revenue=611_289_000_000.0)) == []


# --- the series itself ------------------------------------------------------- #


def _series(*periods: NormalisedBase, accessions: tuple[str, ...]) -> Series:
    return Series(issuer_name="Test Co", cik="0000000001", periods=periods,
                  restatements=(), accessions=accessions)


def test_a_series_reads_one_element_across_periods_oldest_first() -> None:
    series = _series(
        _base("a", date(2023, 1, 31), revenue=611.0),
        _base("a", date(2024, 1, 31), revenue=648.0),
        _base("a", date(2025, 1, 31), revenue=681.0),
        accessions=("a",))
    assert [v for _, v in series.value("revenue")] == [611.0, 648.0, 681.0]
    assert [d.year for d, _ in series.value("revenue")] == [2023, 2024, 2025]


def test_a_period_that_could_not_establish_a_figure_reads_None_not_zero() -> None:
    # Interpolating or zero-filling would turn a gap into a trend.
    series = _series(
        _base("a", date(2023, 1, 31), revenue=None),
        _base("a", date(2024, 1, 31), revenue=648.0),
        accessions=("a",))
    assert [v for _, v in series.value("revenue")] == [None, 648.0]


def test_a_series_says_whether_it_is_on_one_basis() -> None:
    # Three years from one filing were presented together and restated by the
    # issuer where its own accounting changed. Six years from three filings
    # were not, and a reader has to be able to tell which they have.
    one = _series(_base("a", date(2025, 1, 31), revenue=681.0), accessions=("a",))
    many = _series(_base("a", date(2025, 1, 31), revenue=681.0), accessions=("a", "b"))
    assert one.single_basis and not many.single_basis


def test_a_restatement_prints_both_readings_with_their_filings() -> None:
    text = str(Restatement(
        element_id="revenue", period_end=date(2023, 1, 31),
        first_reported=611_289_000_000.0, first_accession="acc-2023",
        later_reported=615_000_000_000.0, later_accession="acc-2025"))
    assert "611,289,000,000 in acc-2023" in text
    assert "615,000,000,000 in acc-2025" in text


# --- the walk itself, which the helper tests do not exercise ----------------- #

_SUMMARY = ("<FilingSummary><MyReports>"
            "<Report><HtmlFileName>R2.htm</HtmlFileName>"
            "<ShortName>Consolidated Statements of Income</ShortName>"
            "<MenuCategory>Statements</MenuCategory></Report>"
            "</MyReports></FilingSummary>")


def _income(*, columns: tuple[str, ...], revenue: tuple[float, ...]) -> str:
    heads = "".join(f'<th class="th">{c}</th>' for c in columns)
    cells = "".join(f'<td class="nump">{v:,.0f}</td>' for v in revenue)
    return ('<table class="report">'
            '<tr><th class="tl" colspan="1" rowspan="2"><div><strong>'
            f'Consolidated Statements of Income - USD ($) $ in Millions</strong></div></th>{heads}</tr>'
            '<tr><td class="pl"><a class="a" href="#" '
            "onclick=\"Show.showAR( this, 'defref_us-gaap_Revenues', window );\">"
            f'Total revenues</a></td>{cells}</tr></table>')


class _TwoFilings:
    """Two annual filings that overlap on one year and disagree about it."""

    FILINGS = {
        "acc-2025": {"accession_no": "acc-2025", "form": "10-K", "filed_at": "2025-03-01",
                     "report_date": "2024-12-31", "items": [], "primary_document": "x.htm",
                     "url": "u"},
        "acc-2024": {"accession_no": "acc-2024", "form": "10-K", "filed_at": "2024-03-01",
                     "report_date": "2023-12-31", "items": [], "primary_document": "x.htm",
                     "url": "u"},
    }
    PAGES = {
        # The 2025 filing restates 2023 revenue upward from 500 to 505.
        "acc-2025": _income(columns=("Dec. 31, 2024", "Dec. 31, 2023"), revenue=(600.0, 505.0)),
        "acc-2024": _income(columns=("Dec. 31, 2023", "Dec. 31, 2022"), revenue=(500.0, 400.0)),
    }

    def resolve_company(self, query, *, limit=10):
        return [{"cik": "0000858877", "ticker": "TST", "title": "Test Co"}]

    def issuer_profile(self, cik):
        return {"cik": cik, "name": "Test Co", "tickers": ["TST"],
                "fiscal_year_end": "1231", "sic": "3576", "sic_description": "Test"}

    def list_filings(self, cik, *, form=None, limit=20, include_history=False):
        return list(self.FILINGS.values()) if form in (None, "10-K") else []

    def get_filing_document(self, cik, accession_no, filename, *, raw=False):
        return {"content": _SUMMARY if filename.endswith(".xml")
                else self.PAGES[accession_no]}

    def get_filing(self, cik, accession_no, *, primary_document=None):
        return {"content": ""}

    def list_filing_documents(self, cik, accession_no):
        return []

    def company_concept(self, cik, concept, *, taxonomy="us-gaap"):
        return None


def _agent(task: str) -> str:
    return ('{"mapped": [{"element_id": "revenue", "statement": "Income", '
            '"label": "Total revenues"}], "absent": []}')


def test_a_series_reaches_back_through_filings_for_the_years_it_needs() -> None:
    from lazytools.connectors.edgar.series import normalise_series

    series = normalise_series(_TwoFilings(), company="TST", years=3,
                              as_of=date(2025, 6, 1), agent=_agent)
    assert [d.year for d, _ in series.value("revenue")] == [2022, 2023, 2024]
    assert len(series.accessions) == 2
    assert not series.single_basis


def test_the_newest_filings_reading_of_a_shared_year_is_the_one_kept() -> None:
    from lazytools.connectors.edgar.series import normalise_series

    series = normalise_series(_TwoFilings(), company="TST", years=3,
                              as_of=date(2025, 6, 1), agent=_agent)
    by_year = dict(series.value("revenue"))
    assert by_year[date(2023, 12, 31)] == 505_000_000.0     # the 2025 filing's figure


def test_the_overlap_actually_produces_the_restatement() -> None:
    # The comparison is only real if the walk lands on a filing that shares a
    # period with what is already held. Jumping a whole filing's span never
    # does, which made this feature dead code until it was measured.
    from lazytools.connectors.edgar.series import normalise_series

    series = normalise_series(_TwoFilings(), company="TST", years=3,
                              as_of=date(2025, 6, 1), agent=_agent)
    assert len(series.restatements) == 1
    found = series.restatements[0]
    assert found.element_id == "revenue"
    assert found.period_end == date(2023, 12, 31)
    assert found.first_reported == 500_000_000.0 and found.first_accession == "acc-2024"
    assert found.later_reported == 505_000_000.0 and found.later_accession == "acc-2025"


def test_asking_for_more_years_than_exist_returns_what_there_is() -> None:
    from lazytools.connectors.edgar.series import normalise_series

    series = normalise_series(_TwoFilings(), company="TST", years=10,
                              as_of=date(2025, 6, 1), agent=_agent)
    assert len(series.periods) == 3
