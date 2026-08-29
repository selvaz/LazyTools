"""The analyst's shape: a small static prompt over a large library."""

from __future__ import annotations

from datetime import date

from lazytools.financials.normalised import Check, Element, NormalisedBase
from lazytools.skills.credit import (
    base_as_context,
    load_common_library,
    system_prompt,
)


def _base(**elements: Element) -> NormalisedBase:
    return NormalisedBase(
        issuer_name="Test Co", cik="0000000001", ontology="corporate",
        open_signals=("captive_finance",), accounting_standard="us-gaap",
        currency="USD", period_start=date(2024, 1, 1), period_end=date(2024, 12, 31),
        information_cutoff=date(2025, 3, 1), perimeter_status="unavailable",
        accession="x", elements=dict(elements))


# --- the library itself ----------------------------------------------------- #


def test_the_shipped_library_loads_and_validates() -> None:
    library = load_common_library()
    assert len(library.blocks) >= 10
    assert "core/identity" in library.blocks


def test_the_common_library_refuses_thresholds_rather_than_stating_them() -> None:
    # A lexical test cannot tell a threshold from an illustration: "3.2x is
    # moderate" appears in the identity block precisely as an example of what
    # NOT to write. What is testable is the rule itself -- every mention of a
    # threshold in the sector-neutral layer must be one that declines to supply
    # one, because levels are sector judgement and belong in the sector library.
    library = load_common_library()
    # Matched per BLOCK, not per line: the prose wraps, so a refusal and the
    # word it qualifies routinely sit on different lines.
    blocks = [b for b in library.blocks.values() if "threshold" in b.body.lower()]
    assert blocks, "the library never tells the agent what to do about levels"
    permitted = ("threshold from memory", "not been given", "would need",
                 "is a trigger", "level is yours")
    for block in blocks:
        assert any(phrase in block.body.lower() for phrase in permitted), (
            f"{block.id} mentions a threshold without declining to supply one")


def test_every_block_that_reads_the_base_requires_the_evidence_rules() -> None:
    library = load_common_library()
    for block in library.blocks.values():
        if "state" in block.body and block.kind == "process":
            assert "core/evidence" in block.requires, block.id


# --- the static prompt ------------------------------------------------------ #


def test_the_static_prompt_is_a_fraction_of_the_library() -> None:
    library = load_common_library()
    whole = sum(b.size for b in library.blocks.values())
    assert len(system_prompt(library)) < whole / 2


def test_the_static_prompt_inlines_only_the_two_core_blocks() -> None:
    # They govern every answer, so an agent that had to load them could answer
    # before it did.
    prompt = system_prompt(load_common_library())
    assert "You assess whether an issuer can service" in prompt
    assert "Leverage compares a" not in prompt


def test_the_catalogue_is_in_the_prompt_so_the_rest_is_reachable() -> None:
    prompt = system_prompt(load_common_library())
    assert "process/liquidity" in prompt and "load_instructions" in prompt


def test_with_no_sector_library_the_agent_is_told_it_has_no_thresholds() -> None:
    assert "you have no thresholds" in system_prompt(load_common_library())


def test_naming_a_sector_changes_what_the_agent_is_told() -> None:
    prompt = system_prompt(load_common_library(), sector="regulated utilities")
    assert "regulated utilities" in prompt and "state no levels" in prompt


# --- the base as the agent sees it ------------------------------------------ #


def test_a_figure_always_travels_with_its_state() -> None:
    context = base_as_context(_base(
        revenue=Element("revenue", 100.0, "verified", checks=(Check("c", True),))))
    assert '"state": "verified"' in context


def test_a_blocked_figure_travels_with_the_reason_it_is_blocked() -> None:
    context = base_as_context(_base(
        operating_da_total=Element("operating_da_total", 700.0, "unreconciled",
                                   blocked_reason="the combined concept excludes amortisation")))
    assert "excludes amortisation" in context


def test_the_open_signals_reach_the_agent() -> None:
    assert "captive_finance" in base_as_context(_base())
