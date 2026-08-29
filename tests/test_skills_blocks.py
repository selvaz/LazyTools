"""Instruction blocks: what the library enforces rather than asks.

An instruction that merely asks is not a constraint. So the tests are about the
refusals — an unknown id, a cycle, a closure over budget — because those are the
difference between lazy loading and a prompt that quietly grows until nothing in
it is read.
"""

from __future__ import annotations

import pytest

from lazytools.skills.blocks import BlockError, instruction_tools, load_library

LIBRARY = """# Credit analyst

## block: core/identity
kind: core
summary: Who the analyst is and what it may conclude.
---
You assess credit. You do not invent figures.

## block: process/leverage
kind: process
summary: What leverage measures and where it is silent.
requires: process/units
---
Leverage is a stock measure. It says nothing about when the debt falls due.

## block: process/units
kind: process
summary: Currency, scale and the unit a figure is expressed in.
---
A figure without its unit is not a figure.
"""


def _write(tmp_path, text: str = LIBRARY):
    path = tmp_path / "analyst.md"
    path.write_text(text, encoding="utf-8")
    return load_library(path)


# --- the catalogue is what makes the rest reachable ------------------------ #


def test_the_catalogue_lists_every_block_with_its_summary(tmp_path) -> None:
    catalogue = _write(tmp_path).catalogue()
    assert "core/identity (core): Who the analyst is" in catalogue
    assert catalogue.count("\n") == 2


def test_the_catalogue_does_not_grow_with_the_instructions_it_indexes(tmp_path) -> None:
    # The property that makes it safe in a static system prompt: its size is set
    # by how many blocks exist, not by how much they say. A toy fixture has a
    # catalogue longer than its bodies, which is why the comparison to measure
    # is this one and not their relative sizes.
    small = _write(tmp_path).catalogue()
    fat = _write(tmp_path, LIBRARY.replace("Leverage is a stock measure.",
                                           "Leverage is a stock measure. " + "Detail. " * 400))
    assert fat.catalogue() == small


# --- dependencies are resolved, not requested ------------------------------ #


def test_a_required_block_comes_along_whether_or_not_it_was_asked_for(tmp_path) -> None:
    text = _write(tmp_path).render(["process/leverage"])
    assert "process/units" in text and "not a figure" in text


def test_a_requirement_is_placed_before_what_needs_it(tmp_path) -> None:
    text = _write(tmp_path).render(["process/leverage"])
    assert text.index("process/units") < text.index("process/leverage")


def test_a_block_asked_for_twice_is_returned_once(tmp_path) -> None:
    text = _write(tmp_path).render(["process/units", "process/leverage"])
    assert text.count("## process/units") == 1


# --- refusals -------------------------------------------------------------- #


def test_an_unknown_id_is_refused_and_the_catalogue_named(tmp_path) -> None:
    # Dropping it silently would return a closure missing a piece nobody was
    # told about.
    with pytest.raises(BlockError, match="no such block"):
        _write(tmp_path).render(["process/nonsense"])


def test_a_closure_over_budget_is_refused_whole_not_truncated(tmp_path) -> None:
    library = _write(tmp_path)
    with pytest.raises(BlockError, match="Load fewer blocks"):
        library.render(["process/leverage"], budget=10)


def test_a_cycle_is_caught_when_the_library_loads_not_when_it_is_used(tmp_path) -> None:
    cyclic = LIBRARY + """
## block: process/a
kind: process
summary: A.
requires: process/b
---
A.

## block: process/b
kind: process
summary: B.
requires: process/a
---
B.
"""
    with pytest.raises(BlockError, match="cycle"):
        _write(tmp_path, cyclic)


def test_a_requirement_that_does_not_exist_fails_at_load(tmp_path) -> None:
    broken = LIBRARY.replace("requires: process/units", "requires: process/ghost")
    with pytest.raises(BlockError, match="which do not exist"):
        _write(tmp_path, broken)


def test_a_duplicate_id_fails_at_load(tmp_path) -> None:
    with pytest.raises(BlockError, match="defined twice"):
        _write(tmp_path, LIBRARY + "\n## block: core/identity\nkind: core\nsummary: x.\n---\nx\n")


def test_a_block_with_no_summary_fails_at_load(tmp_path) -> None:
    # Without one it is invisible in the catalogue, so it can never be loaded.
    with pytest.raises(BlockError, match="no summary"):
        _write(tmp_path, "## block: x\nkind: core\n---\nbody\n")


def test_a_block_with_no_body_fails_at_load(tmp_path) -> None:
    with pytest.raises(BlockError, match="no body"):
        _write(tmp_path, "## block: x\nkind: core\nsummary: s.\n---\n")


def test_a_file_with_no_blocks_says_what_it_expected(tmp_path) -> None:
    with pytest.raises(BlockError, match="expected"):
        _write(tmp_path, "just prose, no headings")


# --- the tool surface ------------------------------------------------------- #


def test_exactly_one_tool_is_exposed_however_many_blocks_exist(tmp_path) -> None:
    # A tool per block would put the whole catalogue in every request, since a
    # tool's schema is sent every turn.
    tools = instruction_tools(_write(tmp_path))
    assert len(tools) == 1 and tools[0].name == "load_instructions"


def test_the_tool_loads_a_closure_from_comma_separated_ids(tmp_path) -> None:
    tool = instruction_tools(_write(tmp_path))[0]
    text = tool.func("core/identity, process/leverage")
    assert "process/units" in text and "You assess credit" in text


def test_the_tool_answers_an_unknown_id_with_guidance_not_an_exception(tmp_path) -> None:
    tool = instruction_tools(_write(tmp_path))[0]
    assert "no such block" in tool.func("process/nonsense")


def test_the_tool_answers_an_empty_request_with_guidance(tmp_path) -> None:
    tool = instruction_tools(_write(tmp_path))[0]
    assert "at least one block id" in tool.func("  ,  ")


def test_the_tool_refuses_an_over_budget_request_in_words_the_model_can_act_on(tmp_path) -> None:
    tool = instruction_tools(_write(tmp_path), budget=10)[0]
    assert "Load fewer blocks" in tool.func("process/leverage")
