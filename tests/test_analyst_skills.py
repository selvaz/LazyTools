"""Analyst skills: contracts, blackboard, specialists, and the 3 orchestrators.

These build the objects without any live LLM call (a stub engine for the
specialists; orchestrator engines construct but are never run). The regime
skill needs lazystats, so the full-five build is guarded with importorskip.
"""

from __future__ import annotations

import pytest

from lazytools.skills import (
    REPORT,
    SKILLS,
    STATS,
    AnalystConfig,
    Blackboard,
    blackboard_orchestrator,
    build_specialists,
    plan_orchestrator,
    replan_orchestrator,
    roster,
)

ORCH_MODEL = "deepseek-v4-flash"  # constructs an engine; never invoked here
NON_REGIME = tuple(s for s in SKILLS if s.name != "regime")


class _StubEngine:
    async def run(self, *a, **k):  # pragma: no cover - never invoked
        raise NotImplementedError


# --- contract --------------------------------------------------------------- #


def test_skill_description_is_a_contract() -> None:
    d = STATS.description()
    assert STATS.summary in d
    assert "Reads from the shared blackboard: prices_ready" in d
    assert "Writes to the shared blackboard: vola_outlier" in d


def test_report_reads_all_upstream_handles() -> None:
    # the report skill's contract must name every handle it consumes
    assert set(REPORT.reads) == {"balance_sheet", "vola_outlier", "regime_summary", "regime_plot_key"}
    assert REPORT.writes == ("report_path",)


def test_handles_form_a_consistent_producer_consumer_graph() -> None:
    # every handle a skill reads must be written by some skill (no dangling inputs)
    produced = {w for s in SKILLS for w in s.writes}
    consumed = {r for s in SKILLS for r in s.reads}
    assert consumed <= produced, f"unproduced handles: {consumed - produced}"


def test_roster_lists_every_skill() -> None:
    r = roster()
    for s in SKILLS:
        assert s.name in r and s.summary in r


# --- blackboard ------------------------------------------------------------- #


def test_blackboard_roundtrip() -> None:
    from lazybridge import Store

    tools = {t.name: t for t in Blackboard(Store()).as_tools()}
    assert set(tools) == {"bb_put", "bb_get", "bb_list"}
    tools["bb_put"].run_sync(key="regime_plot_key", value="hmm__series_with_regimes__AMZN__x")
    assert tools["bb_get"].run_sync(key="regime_plot_key") == "hmm__series_with_regimes__AMZN__x"
    assert tools["bb_get"].run_sync(key="missing") == ""
    assert "regime_plot_key" in tools["bb_list"].run_sync()


# --- specialists ------------------------------------------------------------ #


def test_build_specialists_non_regime() -> None:
    from lazybridge import Agent, Store

    specs = build_specialists(engine=_StubEngine(), store=Store(), skills=NON_REGIME)
    assert set(specs) == {"market_data", "financials", "stats", "report"}
    for name, agent in specs.items():
        assert isinstance(agent, Agent)
        assert agent.name == name


def test_specialists_share_one_blackboard_store() -> None:
    from lazybridge import Store

    store = Store()
    specs = build_specialists(engine=_StubEngine(), store=store, skills=NON_REGIME)
    # a value written to the shared store is visible to every specialist's blackboard
    store.write("prices_ready", "AMZN daily from 2015")
    for agent in specs.values():
        assert {"bb_put", "bb_get", "bb_list"} <= set(agent._tool_map)
        assert agent._tool_map["bb_get"].run_sync(key="prices_ready") == "AMZN daily from 2015"


# --- orchestrators (same specialists, three strategies) --------------------- #


def test_three_orchestrators_build_over_the_same_specialists() -> None:
    from lazybridge import Agent, Store

    specs = build_specialists(engine=_StubEngine(), store=Store(), skills=NON_REGIME)

    plan = plan_orchestrator(specs, ticker="AMZN")
    board = blackboard_orchestrator(specs, model=ORCH_MODEL)
    replan = replan_orchestrator(specs, planner_model=ORCH_MODEL)

    for orch in (plan, board, replan):
        assert isinstance(orch, Agent)


# --- full five (needs lazystats for the regime skill) ----------------------- #


def test_build_all_five_specialists(tmp_path) -> None:
    pytest.importorskip("lazystats.regimes", reason="regime skill needs lazystats[regimes]")
    from lazybridge import Agent, Store

    cfg = AnalystConfig(regime_db=str(tmp_path / "r.db"), out_dir=str(tmp_path / "out"))
    specs = build_specialists(engine=_StubEngine(), store=Store(), cfg=cfg)
    assert set(specs) == {"market_data", "financials", "stats", "regime", "report"}
    assert all(isinstance(a, Agent) for a in specs.values())
