"""Fact selection, anchored on a real filing that punishes the naive query.

The fixture below is not invented. It is Microsoft's own
``RevenueFromContractWithCustomerExcludingAssessedTax`` as served by
``data.sec.gov/api/xbrl/companyconcept/CIK0000789019/...`` on 2026-08-28, cut to
the observations that end 2025-12-31.

Two of them do. They share a concept, an accession (0001193125-26-027207), a
form, a filing date, a fiscal year, a fiscal period focus, and an end date.
They differ only in ``start`` — and in value, by $77.7 billion:

    start 2025-07-01  (184 days, H1)  158,946,000,000
    start 2025-10-01  ( 92 days, Q2)   81,273,000,000

Any selector keying on ``fy``/``fp``/``end`` returns the first and calls it the
quarter. That is the bug these tests exist to hold shut.
"""

from __future__ import annotations

from datetime import date

import pytest

from lazytools.connectors.edgar.facts import (
    facts_from_company_facts,
    facts_from_concept,
    parse_concept,
)
from lazytools.financials.facts import AmbiguousFactError, Fact, FactParseError, pick, select
from lazytools.financials.period import interpret, resolve

MSFT_FYE = "0630"
MSFT_ACCN = "0001193125-26-027207"

MSFT_REVENUE_CONCEPT = {
    "cik": 789019,
    "taxonomy": "us-gaap",
    "tag": "RevenueFromContractWithCustomerExcludingAssessedTax",
    "units": {
        "USD": [
            {  # H1 FY2026 — year to date
                "start": "2025-07-01", "end": "2025-12-31", "val": 158946000000,
                "accn": MSFT_ACCN, "fy": 2026, "fp": "Q2", "form": "10-Q", "filed": "2026-01-28",
            },
            {  # Q2 FY2026 — the three months actually asked for
                "start": "2025-10-01", "end": "2025-12-31", "val": 81273000000,
                "accn": MSFT_ACCN, "fy": 2026, "fp": "Q2", "form": "10-Q", "filed": "2026-01-28",
            },
            {  # the prior-year quarter, a comparative in the same filing
                "start": "2024-10-01", "end": "2024-12-31", "val": 69632000000,
                "accn": MSFT_ACCN, "fy": 2026, "fp": "Q2", "form": "10-Q", "filed": "2026-01-28",
            },
        ]
    },
}


def _q2_window():
    return resolve(interpret("Q2 2026")[0], fiscal_year_end=MSFT_FYE)


def _facts():
    return facts_from_concept(MSFT_REVENUE_CONCEPT)


# --- parsing -------------------------------------------------------------- #


def test_every_observation_becomes_a_fact_with_its_unit() -> None:
    facts = _facts()
    assert len(facts) == 3
    assert {f.unit for f in facts} == {"USD"}
    assert {f.concept for f in facts} == {"RevenueFromContractWithCustomerExcludingAssessedTax"}


def test_durations_are_computed_from_the_reported_endpoints() -> None:
    by_start = {f.start: f.duration_days for f in _facts()}
    assert by_start[date(2025, 7, 1)] == 184
    assert by_start[date(2025, 10, 1)] == 92


def _one_bad_row(row: dict) -> dict:
    return {"tag": "X", "taxonomy": "us-gaap", "units": {"USD": [row]}}


def test_a_row_with_no_end_date_is_dropped() -> None:
    payload = _one_bad_row(
        {"start": "2025-01-01", "val": 1, "accn": "a", "form": "10-K", "filed": "2025-02-01"}
    )
    assert facts_from_concept(payload, strict=False) == []


def test_a_non_numeric_value_is_dropped() -> None:
    payload = _one_bad_row(
        {"end": "2025-01-01", "val": "n/a", "accn": "a", "form": "10-K", "filed": "2025-02-01"}
    )
    assert facts_from_concept(payload, strict=False) == []


def test_a_boolean_is_not_accepted_as_a_number() -> None:
    # bool is an int subclass in Python; a True silently becoming 1.0 would be a
    # fact that reads as a real reported value.
    payload = _one_bad_row(
        {"end": "2025-01-01", "val": True, "accn": "a", "form": "10-K", "filed": "2025-02-01"}
    )
    assert facts_from_concept(payload, strict=False) == []


def test_a_wholly_unreadable_payload_raises_instead_of_reading_as_no_data() -> None:
    # "the company never reported this" and "we can no longer parse the format"
    # are different answers, and only the first is a finding about the company.
    payload = _one_bad_row(
        {"end": "2025-01-01", "val": "n/a", "accn": "a", "form": "10-K", "filed": "2025-02-01"}
    )
    with pytest.raises(FactParseError) as excinfo:
        facts_from_concept(payload)
    assert excinfo.value.seen == 1


def test_a_genuinely_empty_concept_is_not_a_parse_failure() -> None:
    assert facts_from_concept({"tag": "X", "taxonomy": "us-gaap", "units": {}}) == []


def test_losing_one_row_among_good_ones_is_reported_not_absorbed() -> None:
    # The dangerous partial loss: if the unreadable row is the restatement, the
    # surviving original is returned as the current figure and nothing says so.
    payload = {"tag": "X", "taxonomy": "us-gaap", "units": {"USD": [
        {"start": "2025-10-01", "end": "2025-12-31", "val": 100, "accn": "a",
         "form": "10-Q", "filed": "2026-01-28"},
        {"start": "2025-10-01", "end": "2025-12-31", "val": 95, "accn": "b",
         "form": "10-Q", "filed": "not-a-date"},
    ]}}
    with pytest.raises(FactParseError) as excinfo:
        facts_from_concept(payload)
    assert excinfo.value.seen == 2
    assert [r["val"] for r in excinfo.value.dropped] == [95]


def test_taking_the_rest_is_available_but_must_be_asked_for() -> None:
    payload = {"tag": "X", "taxonomy": "us-gaap", "units": {"USD": [
        {"start": "2025-10-01", "end": "2025-12-31", "val": 100, "accn": "a",
         "form": "10-Q", "filed": "2026-01-28"},
        {"end": "2025-12-31", "val": "n/a", "accn": "b", "form": "10-Q", "filed": "2026-01-28"},
    ]}}
    assert len(facts_from_concept(payload, strict=False)) == 1


def test_parse_concept_reports_both_halves_without_raising() -> None:
    payload = {"tag": "X", "taxonomy": "us-gaap", "units": {"USD": [
        {"start": "2025-10-01", "end": "2025-12-31", "val": 100, "accn": "a",
         "form": "10-Q", "filed": "2026-01-28"},
        {"end": "2025-12-31", "val": "n/a", "accn": "b", "form": "10-Q", "filed": "2026-01-28"},
    ]}}
    result = parse_concept(payload)
    assert len(result.facts) == 1 and len(result.dropped) == 1 and result.seen == 2


@pytest.mark.parametrize("bad_start", ["not-a-date", None, ""])
def test_a_present_but_unusable_start_is_never_promoted_to_an_instant(bad_start) -> None:
    # null and "" are as dangerous as garbage: only an ABSENT key means instant,
    # and accepts() waves instants through on the end date alone.
    payload = _one_bad_row(
        {"start": bad_start, "end": "2025-12-31", "val": 158946000000,
         "accn": "a", "form": "10-Q", "filed": "2026-01-28"}
    )
    assert facts_from_concept(payload, strict=False) == []


def test_an_unreadable_start_is_dropped_not_promoted_to_an_instant() -> None:
    # The dangerous shape: accepts() waves instants through on the end date
    # alone, so a duration row whose start failed to parse would slip into a
    # quarter it merely shares an end date with.
    payload = _one_bad_row(
        {"start": "not-a-date", "end": "2025-12-31", "val": 158946000000,
         "accn": "a", "form": "10-Q", "filed": "2026-01-28"}
    )
    assert facts_from_concept(payload, strict=False) == []


def test_a_real_instant_has_no_start_key_at_all() -> None:
    payload = _one_bad_row(
        {"end": "2025-12-31", "val": 500, "accn": "a", "form": "10-Q", "filed": "2026-01-28"}
    )
    facts = facts_from_concept(payload)
    assert len(facts) == 1 and facts[0].start is None


def test_reading_one_concept_out_of_full_companyfacts_gives_the_same_facts() -> None:
    companyfacts = {"facts": {"us-gaap": {
        "RevenueFromContractWithCustomerExcludingAssessedTax": {
            "units": MSFT_REVENUE_CONCEPT["units"]
        }
    }}}
    from_full = facts_from_company_facts(
        companyfacts, taxonomy="us-gaap",
        tag="RevenueFromContractWithCustomerExcludingAssessedTax",
    )
    assert [f.value for f in from_full] == [f.value for f in _facts()]


def test_an_absent_concept_returns_empty_rather_than_raising() -> None:
    assert facts_from_company_facts({"facts": {}}, taxonomy="us-gaap", tag="Nope") == []


# --- the Q2-vs-H1 regression ---------------------------------------------- #


def test_the_quarter_query_returns_the_quarter_not_the_half_year() -> None:
    survivors = select(_facts(), _q2_window(), unit="USD")
    assert len(survivors) == 1
    assert survivors[0].value == 81273000000
    assert survivors[0].start == date(2025, 10, 1)


def test_the_half_year_fact_is_reachable_by_asking_for_the_half_year() -> None:
    window = resolve(interpret("H1 2026")[0], fiscal_year_end=MSFT_FYE)
    survivors = select(_facts(), window, unit="USD")
    assert [f.value for f in survivors] == [158946000000]


def test_fy_and_fp_do_not_separate_the_two_facts() -> None:
    # Documents the reason the selector cannot use them: all three observations,
    # including a prior-YEAR comparative, carry fy=2026 fp=Q2.
    assert {(f.fy, f.fp) for f in _facts()} == {(2026, "Q2")}


def test_the_prior_year_comparative_does_not_answer_this_year_question() -> None:
    survivors = select(_facts(), _q2_window(), unit="USD")
    assert all(f.end.year == 2025 for f in survivors)


# --- filters -------------------------------------------------------------- #


def test_a_unit_filter_keeps_only_that_unit() -> None:
    payload = {"tag": "X", "taxonomy": "us-gaap", "units": {
        "USD": [{"start": "2025-10-01", "end": "2025-12-31", "val": 1, "accn": "a",
                 "form": "10-Q", "filed": "2026-01-28"}],
        "EUR": [{"start": "2025-10-01", "end": "2025-12-31", "val": 2, "accn": "a",
                 "form": "10-Q", "filed": "2026-01-28"}],
    }}
    window = _q2_window()
    assert [f.value for f in select(facts_from_concept(payload), window, unit="EUR")] == [2.0]


def test_no_unit_filter_keeps_every_unit_visibly() -> None:
    payload = {"tag": "X", "taxonomy": "us-gaap", "units": {
        "USD": [{"start": "2025-10-01", "end": "2025-12-31", "val": 1, "accn": "a",
                 "form": "10-Q", "filed": "2026-01-28"}],
        "EUR": [{"start": "2025-10-01", "end": "2025-12-31", "val": 2, "accn": "a",
                 "form": "10-Q", "filed": "2026-01-28"}],
    }}
    assert len(select(facts_from_concept(payload), _q2_window())) == 2


def test_a_form_filter_does_not_silently_admit_the_amended_twin() -> None:
    rows = [
        {"start": "2025-10-01", "end": "2025-12-31", "val": 1, "accn": "a",
         "form": "10-Q", "filed": "2026-01-28"},
        {"start": "2025-10-01", "end": "2025-12-31", "val": 2, "accn": "b",
         "form": "10-Q/A", "filed": "2026-03-01"},
    ]
    facts = facts_from_concept({"tag": "X", "taxonomy": "us-gaap", "units": {"USD": rows}})
    assert len(select(facts, _q2_window(), unit="USD", forms=("10-Q",))) == 1
    assert len(select(facts, _q2_window(), unit="USD", forms=("10-Q", "10-Q/A"))) == 2


def test_an_amended_form_is_flagged_but_not_acted_on() -> None:
    fact = Fact(concept="X", taxonomy="us-gaap", unit="USD", value=1.0,
                start=date(2025, 10, 1), end=date(2025, 12, 31),
                accession="b", form="10-Q/A", filed=date(2026, 3, 1))
    assert fact.is_amendment


# --- version policy ------------------------------------------------------- #


def _restated():
    rows = [
        {"start": "2025-10-01", "end": "2025-12-31", "val": 100, "accn": "orig",
         "form": "10-Q", "filed": "2026-01-28"},
        {"start": "2025-10-01", "end": "2025-12-31", "val": 95, "accn": "later",
         "form": "10-Q", "filed": "2026-10-28"},
    ]
    return facts_from_concept({"tag": "X", "taxonomy": "us-gaap", "units": {"USD": rows}})


def test_latest_takes_the_restated_value() -> None:
    chosen, displaced = pick(_restated(), policy="latest")
    assert chosen.value == 95.0
    assert [f.value for f in displaced] == [100.0]


def test_original_takes_what_was_reported_at_the_time() -> None:
    chosen, displaced = pick(_restated(), policy="original")
    assert chosen.value == 100.0
    assert [f.value for f in displaced] == [95.0]


def test_as_of_reproduces_what_a_reader_would_have_seen_then() -> None:
    chosen, _ = pick(_restated(), policy="as_of", as_of=date(2026, 6, 30))
    assert chosen.value == 100.0


def test_as_of_before_every_filing_is_an_error_not_an_empty_answer() -> None:
    with pytest.raises(ValueError, match="on or before"):
        pick(_restated(), policy="as_of", as_of=date(2020, 1, 1))


def test_as_of_without_a_date_is_refused() -> None:
    with pytest.raises(ValueError, match="requires an as_of"):
        pick(_restated(), policy="as_of")


def test_picking_from_nothing_is_an_error() -> None:
    with pytest.raises(ValueError, match="at least one"):
        pick([], policy="latest")


def test_two_different_values_filed_the_same_day_raise_rather_than_coin_flip() -> None:
    rows = [
        {"start": "2025-10-01", "end": "2025-12-31", "val": 100, "accn": "a",
         "form": "10-Q", "filed": "2026-01-28"},
        {"start": "2025-10-01", "end": "2025-12-31", "val": 90, "accn": "b",
         "form": "10-Q", "filed": "2026-01-28"},
    ]
    facts = facts_from_concept({"tag": "X", "taxonomy": "us-gaap", "units": {"USD": rows}})
    with pytest.raises(AmbiguousFactError) as excinfo:
        pick(facts, policy="latest")
    assert len(excinfo.value.candidates) == 2


def test_the_same_value_filed_twice_is_a_duplicate_not_a_conflict() -> None:
    rows = [
        {"start": "2025-10-01", "end": "2025-12-31", "val": 100, "accn": "a",
         "form": "10-Q", "filed": "2026-01-28"},
        {"start": "2025-10-01", "end": "2025-12-31", "val": 100, "accn": "b",
         "form": "10-Q", "filed": "2026-01-28"},
    ]
    facts = facts_from_concept({"tag": "X", "taxonomy": "us-gaap", "units": {"USD": rows}})
    chosen, displaced = pick(facts, policy="latest")
    assert chosen.value == 100.0 and len(displaced) == 1


# --- policy validation ---------------------------------------------------- #


def test_an_unknown_policy_is_refused_rather_than_falling_through_to_latest() -> None:
    # Without this, the typo "orginal" takes the latest branch and hands back a
    # restated figure to a caller who explicitly asked for the original one.
    with pytest.raises(ValueError, match="unknown version policy"):
        pick(_restated(), policy="orginal")  # type: ignore[arg-type]


# --- durations that are neither the quarter nor the half year ------------- #


def test_a_two_month_fact_is_not_accepted_as_a_quarter() -> None:
    # Rejected by the endpoint check: the start is 30 days from the window's.
    window = resolve(interpret("Q2 2026")[1])  # calendar Q2: 2026-04-01..06-30
    payload = {"tag": "X", "taxonomy": "us-gaap", "units": {"USD": [
        {"start": "2026-05-01", "end": "2026-06-30", "val": 42, "accn": "a",
         "form": "10-Q", "filed": "2026-07-28"},
    ]}}
    assert select(facts_from_concept(payload), window, unit="USD") == []


def test_a_short_period_inside_both_endpoint_tolerances_is_still_rejected() -> None:
    # The case that isolates the DURATION check. Calendar Q2 2026 is
    # 2026-04-01..06-30 (91 days). This fact starts 15 days late and ends 15 days
    # early -- each endpoint sits exactly ON the tolerance limit, so both endpoint
    # checks pass -- yet it covers 61 days, a period 30 days shorter than the one
    # asked for. Remove the duration check and this test goes green wrongly.
    window = resolve(interpret("Q2 2026")[1])
    payload = {"tag": "X", "taxonomy": "us-gaap", "units": {"USD": [
        {"start": "2026-04-16", "end": "2026-06-15", "val": 42, "accn": "a",
         "form": "10-Q", "filed": "2026-07-28"},
    ]}}
    fact = facts_from_concept(payload)[0]
    assert abs((fact.start - window.start).days) <= window.tolerance_days
    assert abs((fact.end - window.end).days) <= window.tolerance_days
    assert select([fact], window, unit="USD") == []
