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


def test_registered_on_the_mcp_server() -> None:
    """Importable from Python was only half of it: without a provider entry the
    tools are invisible to any agent outside this process, which is most of
    the point of putting them in LazyTools."""
    from lazytools.mcp_server.providers import PROVIDER_FACTORIES

    assert "econ_calendar" in PROVIDER_FACTORIES
    assert "calendar_agent" in PROVIDER_FACTORIES

    lette = PROVIDER_FACTORIES["econ_calendar"]()
    assert {t.name for t in lette.as_tools()} == {
        "calendar_vocabulary", "calendar_list_series",
        "calendar_explain_indicator", "calendar_recent_releases",
    }


def test_the_agent_provider_is_opt_in() -> None:
    """Running an LLM is the side effect that needs gating, not any risk to the
    calendar -- which these tools cannot write to at all."""
    from lazytools.mcp_server.providers import PROVIDER_FACTORIES

    with pytest.raises(RuntimeError, match="opt-in only"):
        PROVIDER_FACTORIES["calendar_agent"]()


# --- the translation layer, which is this class's whole job ------------------
class _FintoCatalogo:
    """Stands in for market_data_hub so the argument translation is testable.

    The hub is git-installed with no extra and is absent from CI, so the tool
    bodies would otherwise be exercised only by a live LLM run. What they
    actually *do* -- turn LLM-shaped arguments into the hub's Python ones -- is
    exactly what a fake can check, and is the reason the class re-declares
    signatures at all.
    """

    def __init__(self) -> None:
        self.chiamate: list[dict] = []
        self.sql: list = []
        self.riga: tuple | None = None
        self.righe: list = []

    def available_series(self, con, **kw):
        self.chiamate.append(kw)
        return [{"indicator_key": "us_cpi_yy"}]

    def catalogue_vocabulary(self, con):
        return {"category": {"Inflation": 29}}


@pytest.fixture()
def finto(monkeypatch):
    """Mount a fake market_data_hub for the duration of one test."""
    import sys
    import types

    catalogo = _FintoCatalogo()

    mod_cat = types.ModuleType("market_data_hub.econ_calendar.catalog")
    mod_cat.available_series = catalogo.available_series
    mod_cat.catalogue_vocabulary = catalogo.catalogue_vocabulary

    class _Con:
        def __init__(self) -> None:
            self.sql: list = []

        def execute(self, q, p=None):
            self.sql.append((q, p))
            catalogo.sql.append((q, p))
            return self

        def fetchone(self):
            return catalogo.riga

        def fetchall(self):
            return catalogo.righe

        def close(self) -> None:
            pass

    mod_conn = types.ModuleType("market_data_hub.db.connection")
    mod_conn.get_conn = lambda path=None, read_only=False: _Con()

    for nome, mod in (
        ("market_data_hub", types.ModuleType("market_data_hub")),
        ("market_data_hub.db", types.ModuleType("market_data_hub.db")),
        ("market_data_hub.db.connection", mod_conn),
        ("market_data_hub.econ_calendar",
         types.ModuleType("market_data_hub.econ_calendar")),
        ("market_data_hub.econ_calendar.catalog", mod_cat),
    ):
        monkeypatch.setitem(sys.modules, nome, mod)
    sys.modules["market_data_hub.db"].connection = mod_conn
    return catalogo


def test_empty_strings_become_absent_not_empty_filters(finto) -> None:
    """A model omitting an argument and a model passing '' are the same thing,
    and neither means 'match the empty string'."""
    EconCalendarTools().calendar_list_series(country="USA")
    kw = finto.chiamate[-1]
    assert kw["country"] == "USA"
    assert kw["category"] is None and kw["criticality"] is None
    assert kw["tags"] is None


def test_a_comma_string_becomes_a_list_of_tags(finto) -> None:
    """The hub wants Optional[Iterable[str]]; a tool schema expresses that
    badly, so tags cross the boundary as 'a,b'."""
    EconCalendarTools().calendar_list_series(tags="flash_final, revision_prone")
    assert finto.chiamate[-1]["tags"] == ["flash_final", "revision_prone"]


def test_vocabulary_is_forwarded_unchanged(finto) -> None:
    assert EconCalendarTools().calendar_vocabulary() == {"category": {"Inflation": 29}}


def test_an_unknown_key_says_how_to_find_the_right_one(finto) -> None:
    """A bare 'not found' leaves a model guessing; the error names the tool
    that lists the keys."""
    finto.riga = None
    esito = EconCalendarTools().calendar_explain_indicator("us_nonexistent")
    assert "no indicator" in esito["error"]
    assert "calendar_list_series" in esito["error"]


def test_explain_returns_the_catalogue_s_own_words(finto) -> None:
    finto.riga = (
        "in_cpi_yy", "CPI y/y", "IN", "IND", "Inflation", "hard", None,
        "policy_input", "M", "T1", "coincident", "MoSPI",
        "Headline consumer inflation.", "Fixed-weight basket.",
        "The RBI's formal target since 2016.", None,
    )
    d = EconCalendarTools().calendar_explain_indicator("IN_CPI_YY")
    assert d["rationale"].startswith("The RBI's formal target")
    assert d["tags"] == "policy_input" and d["criticality"] == "T1"
    # the key is matched case-insensitively, so a model need not know the casing
    assert "lower(indicator_key) = lower(?)" in finto.sql[-1][0]


def test_releases_filter_by_country_and_tier(finto) -> None:
    finto.righe = [(
        "us_cpi_yy", "CPI y/y", "US", "T1", "Inflation", "2026-08-12 12:30",
        "minute", "Jul", None, "source", "3.4%", "2.7%", "3.5%", 0.7,
        False, 2.7, 2.7, 3, True, None,
    )]
    r = EconCalendarTools().calendar_recent_releases(
        "2026-08-01", to_day="2026-08-31", country="usa", criticality="T1")
    assert r[0]["indicator_key"] == "us_cpi_yy" and r[0]["surprise"] == 0.7
    q, p = finto.sql[-1]
    assert p == ["2026-08-01", "2026-08-31", "usa", "T1"]
    # the surprise is withheld in SQL where the sources disagreed, not here
    assert "NOT e.consensus_disputed" in q
