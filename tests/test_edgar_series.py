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
