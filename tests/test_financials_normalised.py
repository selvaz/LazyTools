"""The handover contract between the two agents.

Its job is to make an unusable figure impossible to consume by accident. Every
test here is about a way a number could otherwise slip through: a blocking state
with no explanation, a lower bound read as a value, a check that failed being
summed anyway.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from lazytools.financials.normalised import (
    BLOCKING_STATES,
    ELEMENTS,
    Element,
    NormalisedBase,
)


def _base(**elements: Element) -> NormalisedBase:
    return NormalisedBase(
        issuer_name="CISCO SYSTEMS, INC.", cik="0000858877", ontology="corporate",
        open_signals=("captive_finance",), accounting_standard="us-gaap",
        currency="USD", period_start=date(2023, 7, 30), period_end=date(2024, 7, 27),
        information_cutoff=date(2024, 9, 5), perimeter_status="reported_but_mismatched",
        accession="0000858877-24-000017", elements=dict(elements),
    )


# --- an unusable figure cannot be consumed by accident --------------------- #


def test_a_blocked_element_is_not_returned_as_a_value() -> None:
    base = _base(operating_da_total=Element(
        "operating_da_total", 698_000_000, "unreconciled",
        blocked_reason="the entity-wide fact equals the operating-expense part while "
                       "cost of sales holds another 955m"))
    assert base.value("operating_da_total") is None
    assert base.get("operating_da_total").value == 698_000_000


def test_a_lower_bound_is_not_returned_as_a_value() -> None:
    base = _base(operating_lease_total=Element(
        "operating_lease_total", 17_437_000_000, "lower_bound",
        blocked_reason="only the non-current component resolved"))
    assert base.value("operating_lease_total") is None


def test_a_usable_element_is_returned() -> None:
    base = _base(revenue=Element("revenue", 53_803_000_000, "verified"))
    assert base.value("revenue") == 53_803_000_000


def test_every_blocking_state_needs_a_reason() -> None:
    for state in sorted(BLOCKING_STATES):
        with pytest.raises(ValueError, match="blocked_reason"):
            Element("revenue", None, state)  # type: ignore[arg-type]


def test_claiming_a_value_and_supplying_none_is_refused() -> None:
    with pytest.raises(ValueError, match="claims a value"):
        Element("revenue", None, "verified")


def test_an_element_outside_the_base_is_refused() -> None:
    # The base is a closed contract: a sector overlay adds its own layer rather
    # than inventing elements inside this one.
    with pytest.raises(ValueError, match="not an element"):
        Element("ebitda_adjusted_by_management", 1.0, "reported")


# --- what is blocked, and what was never attempted ------------------------- #


def test_blocked_elements_are_listed_with_their_reasons() -> None:
    base = _base(
        revenue=Element("revenue", 1.0, "verified"),
        operating_da_total=Element("operating_da_total", None, "unavailable",
                                   blocked_reason="no complete D&A route resolved"),
    )
    blocked = base.blocked()
    assert [e.id for e in blocked] == ["operating_da_total"]
    assert blocked[0].blocked_reason


def test_never_attempted_is_distinct_from_looked_for_and_not_found() -> None:
    base = _base(revenue=Element("revenue", 1.0, "verified"))
    assert "operating_da_total" in base.missing()
    assert base.blocked() == ()


# --- the base carries meaning, not just numbers ---------------------------- #


def test_every_element_of_the_base_states_what_it_means() -> None:
    assert all(spec.meaning.strip() for spec in ELEMENTS.values())


def test_the_meaning_travels_with_the_value_on_the_wire() -> None:
    base = _base(house_operating_ebitda=Element(
        "house_operating_ebitda", 14_389_000_000, "derived",
        route="operating_income + operating_da_total"))
    wire = base.to_dict()["elements"]["house_operating_ebitda"]
    assert wire["route"] == "operating_income + operating_da_total"
    assert "house convention" in wire["meaning"]


def test_the_base_carries_no_ratios() -> None:
    # Leverage and coverage need thresholds, thresholds are sector judgements,
    # and the sector overlay consumes this layer rather than replacing it.
    assert not any(k for k in ELEMENTS if "ratio" in k or "leverage" in k or "coverage" in k)


# --- the wire form ---------------------------------------------------------- #


def test_the_base_serialises_to_json() -> None:
    base = _base(revenue=Element("revenue", 53_803_000_000.0, "verified",
                                 route="us-gaap:RevenueFromContractWithCustomer...",
                                 sources=("R5.htm",), checks=("segments: balanced",)))
    text = json.dumps(base.to_dict())
    assert "0000858877-24-000017" in text
    assert json.loads(text)["elements"]["revenue"]["checks"] == ["segments: balanced"]


def test_the_envelope_carries_the_facts_that_make_it_reproducible() -> None:
    wire = _base().to_dict()
    assert wire["information_cutoff"] == "2024-09-05"
    assert wire["perimeter_status"] == "reported_but_mismatched"
    assert wire["open_signals"] == ["captive_finance"]
