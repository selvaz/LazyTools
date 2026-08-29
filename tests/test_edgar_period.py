"""Period reading is arithmetic, so it is tested as arithmetic.

The anchor case throughout is Microsoft: fiscal year ending 30 June, so its
fiscal Q2 FY2026 ended 31 December 2025 while calendar Q2 2026 ended 30 June
2026 and was its fiscal Q4.  Its real 10-Q for that quarter (accession
0001193125-26-027207, filed 28 January 2026) reports a period of 31 December
2025 — which is what makes this the case a naive reader gets wrong.
"""

from __future__ import annotations

from datetime import date

import pytest

from lazytools.connectors.edgar.period import (
    PeriodParseError,
    interpret,
    resolve,
)

MSFT_FYE = "0630"


# --- interpret ------------------------------------------------------------ #


def test_a_quarter_is_read_both_ways_fiscal_first() -> None:
    readings = interpret("Microsoft Q2 2026")
    assert [r.basis for r in readings] == ["fiscal", "calendar"]
    assert all(r.kind == "quarter" and r.year == 2026 and r.ordinal == 2 for r in readings)


def test_year_first_ordering_is_read_the_same_way() -> None:
    assert interpret("2026 Q2")[0].ordinal == 2
    assert interpret("2026 Q2")[0].year == 2026


def test_half_year_is_read_both_ways() -> None:
    readings = interpret("LVMH H1 2025 operating profit")
    assert [r.basis for r in readings] == ["fiscal", "calendar"]
    assert readings[0].kind == "half" and readings[0].ordinal == 1


def test_nine_months_is_also_read_both_ways() -> None:
    # A non-December filer's nine months and January-September are different
    # periods and different numbers. Returning only the fiscal reading would
    # answer the second question with the first and say nothing about it.
    readings = interpret("9M 2025")
    assert [r.basis for r in readings] == ["fiscal", "calendar"]


def test_fy_prefix_reads_as_annual() -> None:
    readings = interpret("FY2026 revenue")
    assert readings[0].kind == "annual"
    assert readings[0].ordinal is None


def test_a_bare_year_falls_back_to_annual() -> None:
    readings = interpret("Apple 2024 revenue")
    assert readings[0].kind == "annual" and readings[0].year == 2024


def test_a_dated_pattern_wins_over_the_bare_year_fallback() -> None:
    # "Q3 2025" contains a bare year too; the quarter must not be lost to it.
    assert interpret("Q3 2025")[0].kind == "quarter"


def test_a_phrase_with_no_year_is_refused_not_guessed() -> None:
    with pytest.raises(PeriodParseError):
        interpret("latest quarter revenue")


def test_an_empty_phrase_is_refused() -> None:
    with pytest.raises(PeriodParseError):
        interpret("   ")


# --- resolve -------------------------------------------------------------- #


def test_microsoft_fiscal_q2_2026_ends_in_december_2025() -> None:
    fiscal = interpret("Q2 2026")[0]
    window = resolve(fiscal, fiscal_year_end=MSFT_FYE)
    assert (window.start, window.end) == (date(2025, 10, 1), date(2025, 12, 31))


def test_calendar_q2_2026_is_a_different_quarter_entirely() -> None:
    calendar = interpret("Q2 2026")[1]
    window = resolve(calendar)
    assert (window.start, window.end) == (date(2026, 4, 1), date(2026, 6, 30))


def test_the_two_readings_of_one_phrase_do_not_overlap_for_microsoft() -> None:
    fiscal, calendar = interpret("Q2 2026")
    f = resolve(fiscal, fiscal_year_end=MSFT_FYE)
    c = resolve(calendar)
    assert f.end < c.start


def test_a_fiscal_year_runs_to_its_year_end_not_from_it() -> None:
    annual = interpret("FY2026")[0]
    window = resolve(annual, fiscal_year_end=MSFT_FYE)
    assert (window.start, window.end) == (date(2025, 7, 1), date(2026, 6, 30))
    assert 360 <= window.duration_days <= 370


def test_a_december_filer_fiscal_year_matches_its_calendar_year() -> None:
    fiscal = interpret("FY2025")[0]
    window = resolve(fiscal, fiscal_year_end="1231")
    assert (window.start, window.end) == (date(2025, 1, 1), date(2025, 12, 31))


def test_half_year_for_a_december_filer_is_january_to_june() -> None:
    window = resolve(interpret("H1 2025")[0], fiscal_year_end="1231")
    assert (window.start, window.end) == (date(2025, 1, 1), date(2025, 6, 30))


def test_a_fiscal_reading_without_a_year_end_is_refused_not_defaulted() -> None:
    # Defaulting to December would produce a calendar window wearing a fiscal
    # label -- the exact confusion this module exists to prevent.
    with pytest.raises(ValueError, match="fiscal_year_end"):
        resolve(interpret("Q2 2026")[0])


def test_a_malformed_year_end_is_refused() -> None:
    with pytest.raises(ValueError, match="MMDD"):
        resolve(interpret("Q2 2026")[0], fiscal_year_end="June")


def test_an_impossible_month_day_is_refused() -> None:
    with pytest.raises(ValueError, match="real month/day"):
        resolve(interpret("Q2 2026")[0], fiscal_year_end="1345")


def test_a_year_end_on_the_31st_survives_a_short_month() -> None:
    # A filer ending on 31 March has no 31 February to step back through.
    window = resolve(interpret("FY2026")[0], fiscal_year_end="0331")
    assert window.end == date(2026, 3, 31)
    assert window.start == date(2025, 4, 1)


# --- accepts: the Q2-vs-H1 trap ------------------------------------------- #


def test_a_three_month_fact_is_accepted_by_the_quarter_window() -> None:
    window = resolve(interpret("Q2 2026")[0], fiscal_year_end=MSFT_FYE)
    assert window.accepts(date(2025, 10, 1), date(2025, 12, 31))


def test_a_six_month_fact_sharing_the_end_date_is_rejected() -> None:
    # This is the whole point.  In a Q2 10-Q the year-to-date fact and the
    # three-month fact share an end date, an accession, and a fiscal-period
    # focus.  Matching on the end date alone hands back H1 as if it were Q2.
    window = resolve(interpret("Q2 2026")[0], fiscal_year_end=MSFT_FYE)
    assert not window.accepts(date(2025, 7, 1), date(2025, 12, 31))


def test_an_instant_is_accepted_on_its_end_date_alone() -> None:
    # A balance-sheet fact has no duration to compare against.
    window = resolve(interpret("Q2 2026")[0], fiscal_year_end=MSFT_FYE)
    assert window.accepts(None, date(2025, 12, 31))


def test_a_fact_ending_in_a_different_quarter_is_rejected() -> None:
    window = resolve(interpret("Q2 2026")[0], fiscal_year_end=MSFT_FYE)
    assert not window.accepts(date(2026, 1, 1), date(2026, 3, 31))


def test_a_few_days_of_drift_is_tolerated_for_a_52_53_week_filer() -> None:
    # A 13-week quarter does not land on a month end; rejecting it would fail
    # every retailer.
    window = resolve(interpret("Q2 2026")[0], fiscal_year_end=MSFT_FYE)
    assert window.accepts(date(2025, 9, 28), date(2025, 12, 27))


# --- fiscal calendars that are hostile to naive arithmetic ---------------- #


def test_consecutive_fiscal_years_do_not_overlap_for_a_29_february_year_end() -> None:
    # Stepping 12 months back from an already-clamped 28 February lands on the
    # previous year's 29 February -- a day that would then belong to both years.
    fy24 = resolve(interpret("FY2024")[0], fiscal_year_end="0229")
    fy25 = resolve(interpret("FY2025")[0], fiscal_year_end="0229")
    assert fy24.end == date(2024, 2, 29)
    assert fy25.start == date(2024, 3, 1)
    assert fy24.end < fy25.start


def test_quarters_leave_no_day_unassigned_for_a_30_january_year_end() -> None:
    # The clamp-carrying bug: with the year opening on the 31st, a Q2 end
    # computed from Q2's own clamped start falls a day before Q3 opens.
    quarters = [
        resolve(interpret(f"Q{n} 2025")[0], fiscal_year_end="0130") for n in (1, 2, 3, 4)
    ]
    for earlier, later in zip(quarters, quarters[1:]):
        assert (later.start - earlier.end).days == 1


def test_the_four_quarters_exactly_cover_the_fiscal_year() -> None:
    year = resolve(interpret("FY2026")[0], fiscal_year_end=MSFT_FYE)
    quarters = [resolve(interpret(f"Q{n} 2026")[0], fiscal_year_end=MSFT_FYE) for n in (1, 2, 3, 4)]
    assert quarters[0].start == year.start
    assert quarters[-1].end == year.end


def test_an_impossible_day_for_the_month_is_refused_not_clamped() -> None:
    # 0231 is corrupted issuer metadata, not a typo to round into 28 February.
    with pytest.raises(ValueError, match="at most 29"):
        resolve(interpret("FY2026")[0], fiscal_year_end="0231")


def test_a_29_february_year_end_is_still_accepted() -> None:
    window = resolve(interpret("FY2024")[0], fiscal_year_end="0229")
    assert window.end == date(2024, 2, 29)


def test_a_leap_day_year_end_falls_back_in_a_common_year() -> None:
    assert resolve(interpret("FY2025")[0], fiscal_year_end="0229").end == date(2025, 2, 28)
