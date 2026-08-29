"""The handover contract between the two agents.

Its job is to make an unusable figure impossible to consume by accident. Every
test is a way a number could otherwise slip through: a key that does not match
its element, a state contradicted by its own check, a claim with no evidence
behind it, a lower bound read as a figure.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from lazytools.financials.normalised import (
    ELEMENTS,
    Check,
    Contribution,
    Element,
    NormalisedBase,
)

PASSED = (Check("debt components", True, "balanced"),)


def _base(**elements: Element) -> NormalisedBase:
    return NormalisedBase(
        issuer_name="CISCO SYSTEMS, INC.", cik="0000858877", ontology="corporate",
        open_signals=("captive_finance",), accounting_standard="us-gaap",
        currency="USD", period_start=date(2023, 7, 30), period_end=date(2024, 7, 27),
        information_cutoff=date(2024, 9, 5), perimeter_status="reported_but_mismatched",
        accession="0000858877-24-000017", elements=dict(elements),
    )


# --- the key and the figure must be the same element ----------------------- #


def test_an_element_filed_under_the_wrong_key_is_refused() -> None:
    # Otherwise value("revenue") returns cash flow, and the meaning serialised
    # under "revenue" is cash flow's.
    with pytest.raises(ValueError, match="carries id"):
        NormalisedBase(
            issuer_name="x", cik="x", ontology="corporate", open_signals=(),
            accounting_standard="us-gaap", currency="USD", period_start=None,
            period_end=date(2024, 1, 1), information_cutoff=date(2024, 1, 1),
            perimeter_status="matched", accession="x",
            elements={"revenue": Element("cfo", 10.0, "reported", sources=("R8.htm",))},
        )


def test_the_handover_cannot_be_edited_after_it_was_validated() -> None:
    base = _base(revenue=Element("revenue", 1.0, "reported", sources=("R5.htm",)))
    with pytest.raises(TypeError):
        base.elements["cfo"] = Element("cfo", 2.0, "reported", sources=("R8.htm",))  # type: ignore[index]


# --- a state is a claim, and every claim is paid for ----------------------- #


def test_a_failed_check_forbids_a_usable_state() -> None:
    with pytest.raises(ValueError, match="usable and contradicted"):
        Element("revenue", 10.0, "verified",
                checks=(Check("components", False, "does not reconcile"),))


def test_verified_needs_a_check_that_passed() -> None:
    with pytest.raises(ValueError, match="check that passed"):
        Element("revenue", 10.0, "verified")


def test_reported_needs_a_source() -> None:
    with pytest.raises(ValueError, match="needs a source"):
        Element("revenue", 10.0, "reported")


def test_derived_needs_its_formula_or_its_inputs() -> None:
    with pytest.raises(ValueError, match="formula or the inputs"):
        Element("house_operating_ebitda", 10.0, "derived")
    assert Element(
        "house_operating_ebitda", 10.0, "derived",
        contributions=(Contribution("operating_income", 8.0),
                       Contribution("operating_da_total", 2.0)),
    ).usable


def test_a_blocking_state_needs_a_reason() -> None:
    with pytest.raises(ValueError, match="blocked_reason"):
        Element("revenue", None, "unavailable")


def test_a_state_asserting_no_figure_may_not_carry_one() -> None:
    with pytest.raises(ValueError, match="asserts no figure"):
        Element("operating_lease_total", 100.0, "not_applicable")


def test_a_misspelled_state_is_refused_rather_than_silently_neither() -> None:
    # A Literal is not enforced at runtime: "verifed" would be neither usable
    # nor blocking, and would vanish from blocked().
    with pytest.raises(ValueError, match="is not a state"):
        Element("revenue", 10.0, "verifed")  # type: ignore[arg-type]


def test_an_element_outside_the_registry_is_refused() -> None:
    with pytest.raises(ValueError, match="not an element"):
        Element("ebitda_adjusted_by_management", 1.0, "reported", sources=("x",))


# --- an unusable figure cannot be consumed by accident --------------------- #


def test_a_figure_that_failed_its_check_is_not_returned_as_a_value() -> None:
    base = _base(operating_da_total=Element(
        "operating_da_total", 698_000_000.0, "unreconciled",
        checks=(Check("scope", False, "equals the operating-expense part"),),
        blocked_reason="cost of sales holds another 955m"))
    assert base.value("operating_da_total") is None
    assert base.get("operating_da_total").value == 698_000_000.0


def test_a_lower_bound_needs_a_deliberate_accessor() -> None:
    base = _base(operating_lease_total=Element(
        "operating_lease_total", 17_437_000_000.0, "lower_bound",
        blocked_reason="only the non-current component resolved"))
    assert base.value("operating_lease_total") is None
    assert base.lower_bound("operating_lease_total") == 17_437_000_000.0


def test_a_usable_figure_is_a_lower_bound_too() -> None:
    base = _base(revenue=Element("revenue", 5.0, "verified", checks=PASSED))
    assert base.lower_bound("revenue") == 5.0


def test_an_unavailable_figure_is_not_a_lower_bound() -> None:
    base = _base(revenue=Element("revenue", None, "unavailable",
                                 blocked_reason="no route resolved"))
    assert base.lower_bound("revenue") is None


# --- what is blocked, and what was never attempted ------------------------- #


def test_blocked_elements_are_listed_with_their_reasons() -> None:
    base = _base(
        revenue=Element("revenue", 1.0, "verified", checks=PASSED),
        cfo=Element("cfo", None, "unavailable", blocked_reason="no route resolved"),
    )
    assert [e.id for e in base.blocked()] == ["cfo"]
    assert base.blocked()[0].blocked_reason


def test_never_attempted_is_distinct_from_looked_for_and_not_found() -> None:
    base = _base(revenue=Element("revenue", 1.0, "verified", checks=PASSED))
    assert "cfo" in base.missing() and base.blocked() == ()


# --- the base carries meaning, and no ratios ------------------------------- #


def test_every_meaning_adds_information_the_element_name_does_not() -> None:
    # Length is the wrong test: "Principal due in months 13-24" is short and is
    # a better definition than a paragraph of rhetoric. What matters is whether
    # a reader learns something the identifier did not already tell them.
    for key, spec in ELEMENTS.items():
        from_name = {p.lower() for p in key.split("_")}
        added = {w.strip(".,:;()-").lower() for w in spec.meaning.split()} - from_name
        substantive = {w for w in added if len(w) > 3}
        assert spec.meaning.endswith("."), f"{key}: meaning is not a sentence"
        assert len(substantive) >= 4, f"{key}: meaning only restates the name"


def test_the_base_carries_no_ratio_shaped_element() -> None:
    banned = ("ratio", "leverage", "coverage", "margin", "_to_", "yield", "per_")
    assert not [k for k in ELEMENTS if any(b in k for b in banned)]


def test_the_meaning_travels_with_the_value_on_the_wire() -> None:
    base = _base(house_operating_ebitda=Element(
        "house_operating_ebitda", 14_389_000_000.0, "derived",
        route="operating_income + operating_da_total"))
    wire = base.to_dict()["elements"]["house_operating_ebitda"]
    assert wire["route"] == "operating_income + operating_da_total"
    assert "house convention" in wire["meaning"]


# --- the wire form ---------------------------------------------------------- #


def test_the_payload_is_versioned_so_a_consumer_can_refuse_it() -> None:
    assert _base().to_dict()["schema_version"] >= 1


def test_the_base_serialises_to_json_with_its_checks_and_contributions() -> None:
    base = _base(house_operating_ebitda=Element(
        "house_operating_ebitda", 14_389_000_000.0, "derived",
        contributions=(Contribution("operating_income", 12_181_000_000.0),
                       Contribution("operating_da_total", 2_208_000_000.0, "one complete route")),
        checks=(Check("route completeness", True),)))
    wire = json.loads(json.dumps(base.to_dict()))["elements"]["house_operating_ebitda"]
    assert wire["contributions"][1]["reason"] == "one complete route"
    assert wire["checks"][0]["passed"] is True


def test_the_envelope_carries_the_facts_that_make_it_reproducible() -> None:
    wire = _base().to_dict()
    assert wire["information_cutoff"] == "2024-09-05"
    assert wire["perimeter_status"] == "reported_but_mismatched"
    assert wire["open_signals"] == ["captive_finance"]
