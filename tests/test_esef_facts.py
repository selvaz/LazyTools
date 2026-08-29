"""Reading an ESEF filing's xBRL-JSON, and the conventions that shift a year."""

from __future__ import annotations

from datetime import date

import pytest

from lazytools.connectors.esef.facts import (
    extension_namespaces,
    is_dimensioned,
    parse_facts,
)
from lazytools.financials.facts import FactParseError


def _document(*entries: dict) -> dict:
    return {"documentInfo": {"namespaces": {"ifrs-full": "u", "iso4217": "u", "tot": "u"}},
            "facts": {f"tag_{i}": e for i, e in enumerate(entries)}}


def _numeric(concept: str, period: str, value: str, **extra: str) -> dict:
    return {"value": value,
            "dimensions": {"concept": concept, "entity": "scheme:X",
                           "period": period, "unit": "iso4217:USD", **extra}}


# --- the exclusive endpoint, which is the whole point ------------------------ #


def test_a_duration_ending_on_new_years_day_is_the_year_before() -> None:
    result = parse_facts(_document(_numeric(
        "ifrs-full:Revenue", "2024-01-01T00:00:00/2025-01-01T00:00:00", "214550000000.0")),
        filing_id="f")
    fact = result.facts[0]
    assert fact.start == date(2024, 1, 1)
    assert fact.end == date(2024, 12, 31)


def test_an_INSTANT_is_exclusive_too_and_this_is_the_expensive_one() -> None:
    # TotalEnergies' year-end cash of $25.8bn is stamped 2025-01-01. Read
    # literally it lands in the wrong year, and every year-on-year comparison
    # shifts by one -- with no error anywhere.
    result = parse_facts(_document(_numeric(
        "ifrs-full:CashAndCashEquivalents", "2025-01-01T00:00:00", "25844000000.0")),
        filing_id="f")
    fact = result.facts[0]
    assert fact.start is None
    assert fact.end == date(2024, 12, 31)


# --- what is and is not a figure --------------------------------------------- #


def test_text_facts_are_skipped_rather_than_reported_as_losses() -> None:
    # A legal form and an explanation of a name change are not figures that
    # went missing.
    result = parse_facts(_document(
        {"value": "societe europeenne",
         "dimensions": {"concept": "ifrs-full:LegalFormOfEntity", "entity": "scheme:X",
                        "period": "2024-01-01T00:00:00/2025-01-01T00:00:00",
                        "language": "fr"}},
        _numeric("ifrs-full:Revenue", "2024-01-01T00:00:00/2025-01-01T00:00:00", "1.0")),
        filing_id="f")
    assert len(result.facts) == 1 and not result.dropped


def test_a_numeric_fact_that_cannot_be_read_is_reported_not_silently_dropped() -> None:
    result = parse_facts(_document(
        _numeric("ifrs-full:Revenue", "2024-01-01T00:00:00/2025-01-01T00:00:00", "1.0"),
        _numeric("ifrs-full:Other", "not-a-period", "2.0")),
        filing_id="f")
    assert len(result.facts) == 1 and len(result.dropped) == 1


def test_a_document_whose_shape_changed_raises_rather_than_returning_nothing() -> None:
    # "The company reported nothing" and "we can no longer parse this" are
    # different findings, and only the first is about the company.
    with pytest.raises(FactParseError, match="shape has probably changed"):
        parse_facts(_document(_numeric("ifrs-full:Revenue", "bad", "1.0")), filing_id="f")


def test_no_facts_object_at_all_raises() -> None:
    with pytest.raises(FactParseError, match="no facts object"):
        parse_facts({"documentInfo": {}}, filing_id="f")


# --- slices, and whose concepts these are ------------------------------------ #


def test_a_fact_carrying_an_axis_is_a_slice_of_its_concept() -> None:
    sliced = {"concept": "ifrs-full:Equity", "entity": "scheme:X", "unit": "iso4217:USD",
              "period": "2025-01-01T00:00:00",
              "ifrs-full:ComponentsOfEquityAxis": "ifrs-full:IssuedCapitalMember"}
    whole = {k: v for k, v in sliced.items() if "Axis" not in k}
    assert is_dimensioned(sliced)
    assert not is_dimensioned(whole)


def test_the_language_of_a_fact_does_not_make_it_a_slice() -> None:
    # A French-language label is an identity dimension, not an axis. Treating it
    # as one would exclude a whole filing's figures.
    assert not is_dimensioned({"concept": "c", "entity": "e", "period": "p",
                               "unit": "u", "language": "fr"})


def test_the_issuers_own_namespaces_are_named() -> None:
    # TotalEnergies tags under `tot:`. An extension concept means what the
    # issuer decided it means, which is worth knowing before comparing it.
    assert extension_namespaces(_document()) == ("tot",)


def test_the_currency_prefix_is_stripped_but_other_units_are_not() -> None:
    result = parse_facts(_document(
        _numeric("ifrs-full:Revenue", "2024-01-01T00:00:00/2025-01-01T00:00:00", "1.0"),
        {"value": "2.0", "dimensions": {"concept": "ifrs-full:Shares", "entity": "e",
                                        "period": "2025-01-01T00:00:00", "unit": "xbrli:shares"}}),
        filing_id="f")
    assert {f.unit for f in result.facts} == {"USD", "xbrli:shares"}


def test_a_fact_carries_no_filing_date_because_ESEF_publishes_none() -> None:
    # The repository dates a report by the period it covers and by when it was
    # ingested. Neither is a filing date, and inventing one would make a
    # version policy's ordering look decided.
    result = parse_facts(_document(_numeric(
        "ifrs-full:Revenue", "2024-01-01T00:00:00/2025-01-01T00:00:00", "1.0")),
        filing_id="f")
    assert result.facts[0].filed is None
    assert result.facts[0].accession == "f"
