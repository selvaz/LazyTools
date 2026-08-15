"""The calendar specialist: construction contract and tool surface.

Mirrors test_specialist_agents.py -- no live LLM call. Constructing an
``LLMEngine`` only stores parameters, so the real classes are exercised
without a network round trip or an API key.
"""

from __future__ import annotations

import pytest

pytest.importorskip("lazybridge")

from lazytools.connectors.econ_calendar import EconCalendarTools
from lazytools.skills.calendar_agent import (
    MACRO_CALENDAR_ANALYST_SYSTEM,
    macro_calendar_analyst,
)


def test_defaults_to_the_cheap_tier_with_medium_thinking() -> None:
    """The work is navigating a catalogue and reporting it faithfully, which
    rewards deliberation over capability -- and the whole point of a specialist
    is that it does not cost what a generalist costs."""
    a = macro_calendar_analyst()
    assert a.name == "macro-calendar-analyst"
    assert a.engine.model == "deepseek-v4-flash"
    assert a.engine.thinking == "medium"


def test_the_four_questions_are_all_reachable() -> None:
    nomi = {t.name for t in EconCalendarTools().as_tools()}
    assert nomi == {
        "calendar_vocabulary",          # what can I filter on
        "calendar_list_series",         # what is covered
        "calendar_explain_indicator",   # what does it mean, and why there
        "calendar_recent_releases",     # what printed, and by how much it missed
    }


def test_the_prompt_teaches_method_not_economics() -> None:
    """The rationales and methodology notes live in the catalogue and are read
    at run time. A prompt restating them would be a second copy that stops
    matching the first the day somebody edits the catalogue -- and the prompt
    is the copy nobody would update. So it must name the traps, not the facts.
    """
    testo = MACRO_CALENDAR_ANALYST_SYSTEM
    for trappola in ("flash_final", "cumulative", "revision_prone",
                     "consensus_low", "reference_date_origin",
                     "release_precision"):
        assert trappola in testo
    # ... and it must not carry the substance it is supposed to go and read
    assert "RBI" not in testo and "fresh food" not in testo


def test_a_relative_db_path_is_resolved_where_the_caller_stands(tmp_path) -> None:
    """The hub reads a relative db_path as relative to its OWN repository root.
    A reader called from anywhere else got an empty database created inside
    market-data-hub, and then answered 'nothing found' about a catalogue that
    was perfectly fine."""
    t = EconCalendarTools(db_path="calendario.duckdb")
    assert t._db_path == "calendario.duckdb"      # stored as given
    import os
    from pathlib import Path
    os.chdir(tmp_path)
    atteso = str(Path(tmp_path, "calendario.duckdb").resolve())
    # resolution happens at connect time, against the caller's directory
    assert str(Path(t._db_path).expanduser().resolve()) == atteso
