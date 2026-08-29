"""The analyst's shape: a small static prompt over a large library."""

from __future__ import annotations

from datetime import date

import pytest

from lazytools.financials.normalised import Check, Element, NormalisedBase
from lazytools.skills.credit import (
    SECTOR_LIBRARIES,
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


# --- the sector libraries ---------------------------------------------------- #


def test_every_shipped_sector_library_composes_with_the_common_one() -> None:
    # load_library validates as it composes: duplicate block ids, a `requires`
    # naming a block nobody ships, and dependency cycles all raise here. So
    # loading each one IS the structural test.
    for sector in SECTOR_LIBRARIES:
        library = load_common_library(sector)
        assert "core/identity" in library.blocks, sector
        assert len(library.blocks) > len(load_common_library().blocks), sector


def test_an_unknown_sector_refuses_rather_than_silently_using_the_common_layer() -> None:
    # Falling back would answer a sector question with sector-neutral
    # instructions and say nothing about having done so.
    with pytest.raises(KeyError, match="no library for sector"):
        load_common_library("shipbuilding")


def test_every_sector_library_says_when_NOT_to_use_itself() -> None:
    # A sector library applied to the wrong issuer is worse than none: it
    # supplies confident instructions for a business that does not work that
    # way. Each one carries its own exclusions.
    for sector in SECTOR_LIBRARIES:
        library = load_common_library(sector)
        applies = library.blocks[f"sector/{sector}/applies"]
        assert "do **not** use" in applies.body.lower(), sector


def test_no_sector_library_ships_a_threshold_table() -> None:
    # The load-bearing finding behind this whole design: 4,405 pages of rating
    # agency methodology filed with the SEC contain the framework and no ratio
    # calibration at all. The levels are not available from a free, citable
    # source, so a library that shipped them would be shipping remembered
    # numbers dressed as standards.
    for sector in SECTOR_LIBRARIES:
        library = load_common_library(sector)
        levels = library.blocks[f"sector/{sector}/levels"]
        # Whitespace is collapsed before matching because the prose wraps: the
        # retail block writes "your own\n  judgement", and a per-line match
        # would report a missing commitment that is plainly there. This has
        # now caught the same test out three times.
        body = " ".join(levels.body.lower().split())
        assert "no threshold table" in body, sector
        assert "your own judgement" in body, sector


def test_a_sector_library_reaches_the_agent_through_the_catalogue() -> None:
    # The sector blocks are loadable, not loaded: they must be listed in the
    # catalogue the static prompt carries, but their bodies must not be in it.
    library = load_common_library("utilities")
    prompt = system_prompt(library, sector="utilities")
    assert "sector/utilities/rate_base" in prompt
    assert "regulatory asset base" not in prompt


# --- the shape of a finished note -------------------------------------------- #


def test_the_note_shape_is_reachable_and_the_agent_is_told_to_load_it() -> None:
    # A shape block nobody loads shapes nothing. The identity block is inlined
    # in every prompt, so the instruction to fetch this one has to live there.
    library = load_common_library()
    assert "output/scheda" in library.blocks
    identity = " ".join(library.blocks["core/identity"].body.split())
    assert "load `output/scheda`" in identity


def test_the_note_shape_refuses_to_emit_a_rating_symbol() -> None:
    # The whole point of borrowing the agencies' section structure without
    # their conclusion: 4,405 pages of methodology filed with the SEC contain
    # no mapping from a ratio to a rating category, so a symbol here would be
    # borrowed authority with nothing behind it.
    library = load_common_library()
    body = " ".join(library.blocks["output/scheda"].body.lower().split())
    assert "does not include is a rating or an outlook" in body
    assert "borrowed authority" in body


def test_the_note_shape_keeps_liquidity_as_its_own_section() -> None:
    # Liquidity is the section a summary-shaped note drops first, and it is the
    # one that decides whether the issuer survives the year.
    body = load_common_library().blocks["output/scheda"].body.lower()
    assert "liquidity and debt structure" in body
    assert "its own section, always" in " ".join(body.split())
