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
    store.write("prices_ready", "AMZN daily from 2015")
    # each specialist reads via bb_get and produces via one typed publish
    for agent in specs.values():
        assert {"bb_get", "bb_list", "publish"} <= set(agent._tool_map)
    # stats declares prices_ready in its reads → may read it
    assert specs["stats"]._tool_map["bb_get"].run_sync(key="prices_ready") == "AMZN daily from 2015"
    # market_data reads nothing (it produces prices_ready) → the scoped tool refuses it
    assert "not permitted" in specs["market_data"]._tool_map["bb_get"].run_sync(key="prices_ready")


def test_publish_tool_writes_the_declared_handles() -> None:
    from lazybridge import Store

    store = Store()
    specs = build_specialists(engine=_StubEngine(), store=store, skills=NON_REGIME)
    # the report skill's publish exposes exactly its writes as parameters
    specs["report"]._tool_map["publish"].run_sync(report_path="/tmp/report.html")
    assert store.read("report_path") == "/tmp/report.html"


def test_blackboard_scoping_enforces_reads() -> None:
    from lazybridge import Store

    tools = {t.name: t for t in Blackboard(Store()).as_tools(readable={"a"}, writable={"b"})}
    assert "not permitted" in tools["bb_put"].run_sync(key="a", value="x")  # a is read-only here
    assert tools["bb_put"].run_sync(key="b", value="x").startswith("blackboard: wrote")
    assert "not permitted" in tools["bb_get"].run_sync(key="z")


# --- orchestrators (same specialists, three strategies) --------------------- #


def test_specialist_engine_carries_its_skill_system() -> None:
    # the model= path must wire each Skill.system into that specialist's engine —
    # otherwise the skill's know-how is declared but never delivered to the model.
    specs = build_specialists(model=ORCH_MODEL, skills=NON_REGIME)
    assert "market-data specialist" in specs["market_data"].engine.system
    assert "reporting specialist" in specs["report"].engine.system
    assert "statistics specialist" in specs["stats"].engine.system
    # the shared prefix is kept alongside the skill prompt
    assert "one at a time" in specs["report"].engine.system


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


# --------------------------------------------------------------------------- #
# AnalystConfig.hub_db must reach every tool builder that reads market data --
# audit finding: only the report builder's ecosystem_resolvers() got it, so a
# run with a custom hub_db could read macro/stats/regime data from the
# hub's own default db while the report resolved figures from cfg.hub_db.
# --------------------------------------------------------------------------- #


def test_market_data_and_financials_tools_use_configured_hub_db(tmp_path) -> None:
    pytest.importorskip("market_data_hub")
    from lazytools.skills.analyst import _financials_tools, _market_data_tools

    hub_db = str(tmp_path / "hub.duckdb")
    cfg = AnalystConfig(hub_db=hub_db, out_dir=str(tmp_path / "out"))

    (market,) = _market_data_tools(cfg)
    (financials,) = _financials_tools(cfg)
    assert market._resolve()._db_path == hub_db
    assert financials._resolve()._db_path == hub_db


def test_market_data_tools_default_backend_when_hub_db_unset(tmp_path) -> None:
    from lazytools.skills.analyst import _market_data_tools

    cfg = AnalystConfig(out_dir=str(tmp_path / "out"))
    (market,) = _market_data_tools(cfg)
    assert market._backend is None  # unresolved -- MarketDataHubBackend()'s own default, unchanged behavior


def test_stats_tools_use_configured_hub_db(tmp_path) -> None:
    from lazytools.skills.analyst import _stats_tools

    hub_db = str(tmp_path / "hub.duckdb")
    cfg = AnalystConfig(hub_db=hub_db, out_dir=str(tmp_path / "out"))
    (stats,) = _stats_tools(cfg)
    assert stats._db_path == hub_db


def test_regime_tools_receive_both_regime_db_and_hub_db(tmp_path) -> None:
    pytest.importorskip("lazystats.regimes", reason="regime skill needs lazystats[regimes]")
    from lazytools.skills.analyst import _regime_tools

    hub_db = str(tmp_path / "hub.duckdb")
    regime_db = str(tmp_path / "r.db")
    cfg = AnalystConfig(hub_db=hub_db, regime_db=regime_db, out_dir=str(tmp_path / "out"))
    (regime,) = _regime_tools(cfg)
    assert regime._db_path == regime_db
    assert regime._market_data_path == hub_db


def test_report_resolver_isolated_across_two_regime_depots_in_one_process(tmp_path) -> None:
    """The isolation hazard itself: two pipelines, two different regime
    depots, interleaved in one process -- each pipeline's report resolver
    must read its OWN depot's plot, never whichever depot happened to be
    "active" globally most recently.

    Also demonstrates why the old regimes_db=None approach was unsafe: at
    the point cfg1's report is built, the global depot has already been
    reassigned to cfg2's file by building cfg2's regime tools."""
    pytest.importorskip("lazystats.regimes", reason="regime skill needs lazystats[regimes]")
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from lazytools.skills.analyst import _regime_tools, _report_tools

    cfg1 = AnalystConfig(regime_db=str(tmp_path / "depot1.db"), out_dir=str(tmp_path / "out1"))
    cfg2 = AnalystConfig(regime_db=str(tmp_path / "depot2.db"), out_dir=str(tmp_path / "out2"))

    # Build cfg1's regime tools (init_regime_db(depot1) -> global _DB = depot1),
    # write a plot into depot1.
    (regime1,) = _regime_tools(cfg1)
    fig1, ax1 = plt.subplots(figsize=(1, 1))
    ax1.set_title("depot1")
    key1 = regime1._db().get_db().write_plot(fig1, result_key="depot1_key", series_name="S", title="depot1")
    plt.close(fig1)

    # Build cfg2's regime tools (init_regime_db(depot2) -> global _DB REASSIGNED
    # to depot2, exactly the hazard the audit flagged), write a DIFFERENT
    # figure into depot2, under a different result_key so the two plot keys
    # can never collide regardless of same-second timestamp granularity.
    (regime2,) = _regime_tools(cfg2)
    fig2, ax2 = plt.subplots(figsize=(1, 1))
    ax2.set_title("depot2")
    key2 = regime2._db().get_db().write_plot(fig2, result_key="depot2_key", series_name="S", title="depot2")
    plt.close(fig2)

    assert key1 != key2

    # At this point the global depot is depot2. Building cfg1's report tools
    # must still resolve depot1's plot, not depot2's -- this is the fix.
    report1, _files1 = _report_tools(cfg1)
    png1, _mime1 = report1._artifacts.resolve(f"regimes:{key1}")

    report2, _files2 = _report_tools(cfg2)
    png2, _mime2 = report2._artifacts.resolve(f"regimes:{key2}")

    assert png1 != png2  # different figures, proving no cross-contamination
    # depot1's report resolver must NOT be able to see depot2's key (proves
    # it's reading depot1's file specifically, not just "whatever key exists").
    with pytest.raises(KeyError):
        report1._artifacts.resolve(f"regimes:{key2}")


def test_macro_views_macro_and_market_tools_use_configured_hub_db(tmp_path) -> None:
    pytest.importorskip("lazystats.regimes", reason="regime tool needs lazystats[regimes]")
    pytest.importorskip("market_data_hub")
    from lazytools.skills.macro_views import _macro_tools, _market_tools

    hub_db = str(tmp_path / "hub.duckdb")
    regime_db = str(tmp_path / "r.db")
    cfg = AnalystConfig(hub_db=hub_db, regime_db=regime_db, out_dir=str(tmp_path / "out"))

    (macro_datahub,) = _macro_tools(cfg)
    assert macro_datahub._resolve()._db_path == hub_db

    stats, regime = _market_tools(cfg)
    assert stats._db_path == hub_db
    assert regime._market_data_path == hub_db
    assert regime._db_path == regime_db
