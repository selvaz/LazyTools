"""The retriever's instruction library, and the line it does not cross."""

from __future__ import annotations

import pytest

from lazytools.connectors.edgar.mapping import _system
from lazytools.skills.blocks import BlockError
from lazytools.skills.retriever import (
    MAPPING_BLOCKS,
    load_retriever_library,
    mapping_instructions,
    system_prompt,
)


def test_the_library_loads_and_validates() -> None:
    # load_library validates as it composes: duplicate ids, a `requires` naming
    # a block nobody ships, and cycles all raise here.
    library = load_retriever_library()
    assert "core/identity" in library.blocks
    assert "route/jurisdiction" in library.blocks


def test_the_catalogue_is_a_small_fraction_of_the_library() -> None:
    library = load_retriever_library()
    whole = sum(b.size for b in library.blocks.values())
    assert len(library.catalogue()) < whole / 4


def test_the_static_prompt_carries_the_catalogue_not_the_source_blocks() -> None:
    prompt = system_prompt()
    assert "source/sec/traps" in prompt          # listed
    assert "DebtLongtermAndShorttermCombined" not in prompt   # not loaded


def test_the_one_rule_is_inlined_rather_than_loadable() -> None:
    # An agent that had to load "never state a figure" could emit one before it
    # did. It goes in the static prompt or it is not a guarantee.
    assert "You never state a figure" in system_prompt()


def test_the_mapping_call_carries_the_traps_it_needs() -> None:
    # The mapping call is one turn with no tools: it cannot fetch a block, so
    # what governs it has to be in front of it.
    text = mapping_instructions()
    assert "AmortizationOfIntangibleAssets" in text     # Cisco, entity-wide vs true
    assert "per share data" in text                     # per-row scale
    assert "no debt maturity schedule" in text          # NVIDIA's lease ladder


def test_the_mapping_call_does_NOT_carry_what_is_already_settled() -> None:
    # Routing and multi-period are decided before a mapping runs; shipping them
    # into every call is prompt nobody reads.
    text = mapping_instructions()
    assert "route/jurisdiction" not in MAPPING_BLOCKS
    assert "restatement" not in text.lower()


def test_the_traps_reach_the_live_mapping_prompt() -> None:
    # The connector built its prompt from a string literal for a while. This is
    # the test that the library is wired in rather than merely shipped.
    assert "AmortizationOfIntangibleAssets" in _system()
    assert "labelling, not analysing" in _system()


def test_asking_for_a_block_that_does_not_exist_says_so() -> None:
    library = load_retriever_library()
    with pytest.raises(BlockError):
        library.render(["source/ir/discovery"])
