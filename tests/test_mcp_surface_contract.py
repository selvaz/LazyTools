"""Contract test on the MOUNTED MCP tool surface.

Package-level tests each assert one connector in isolation, but the failure
mode this repo actually hit (audit follow-up) was *integration* drift: a
connector written and green in isolation yet never mounted in the server menu,
or mounted but emitting a tool set that silently drifted from its dependency.
Those gaps are invisible until someone drives the real ``lazytools-mcp``
surface by hand.

This test pins that surface so CI fails the moment it moves:

  1. the provider menu (``PROVIDER_FACTORIES``) is exactly the expected id set —
     catches a connector being unmounted (or a new one added silently);
  2. each in-repo connector emits exactly its documented tool names — catches
     per-connector drift (a tool renamed/removed by a dependency bump);
  3. the read-only mounted surface never leaks a mutating tool, and write mode
     is a strict superset — catches a writer escaping the name guard.

Comms connectors are exercised with the in-repo fake services so the contract
holds without live credentials; optional-extra connectors are importorskip-ed.
"""

from __future__ import annotations

import pytest

from lazytools.mcp_server.providers import PROVIDER_FACTORIES, default_providers
from lazytools.mcp_server.server import UNSAFE_TOOL_PATTERNS, expand_tools

# --------------------------------------------------------------------------- #
# 1. The provider menu
# --------------------------------------------------------------------------- #

EXPECTED_PROVIDER_IDS = {
    "datahub",
    "statistical",
    "econ_calendar",
    "earnings_calendar",
    "tradingview",
    "polymarket",
    "gleif",
    "calendar_agent",
    "regimes",
    "report",
    "web",
    "fin",
    "optimizer_agent",
    "report_agent",
    "stats_agents",
    "code_review",
    "claude_review",
    "code_write",
    "telegram",
    "gmail",
    "outlook",
    "registry",
}


def test_provider_menu_ids_are_exactly_expected() -> None:
    """The server's advertised provider menu must not drift.

    Adding a connector without registering it here (the exact bug that left the
    optimizer + messaging connectors unreachable) fails this test; so does
    unmounting one. Import-free on purpose — it asserts registration, not
    construction, so it runs even when no optional extra is installed.
    """
    assert set(PROVIDER_FACTORIES) == EXPECTED_PROVIDER_IDS


# --------------------------------------------------------------------------- #
# 2. Per-connector tool-name contracts (deterministic: fakes + importorskip)
# --------------------------------------------------------------------------- #

DATAHUB_READ = {
    "datahub_list_datasets",
    "datahub_list_symbols",
    "datahub_list_sectors",
    "datahub_list_macro",
    "datahub_list_indicators",
    "datahub_list_countries",
    "datahub_describe",
    "datahub_search",
    "datahub_get_coverage",
    "datahub_resolve_instrument",
    "datahub_get_price_summary",
    "datahub_get_financials_coverage",
    "datahub_get_financial_facts",
    "datahub_get_statement",
    "datahub_get_job_status",
    "datahub_get_ingestion_health",
    "datahub_calendar_vocabulary",
    "datahub_calendar_series",
}
DATAHUB_WRITE = {
    "datahub_register_listing",
    "datahub_ensure_price_history",
    "datahub_ensure_financials",
}

STATISTICAL_TOOLS = {
    "statistical_return_volatility",
    "statistical_return_correlation",
    "statistical_return_outliers",
    "statistical_regression_ols",
    "statistical_regression_ridge",
    "statistical_regression_lasso",
}

# PortfolioOptimizationTools emits all three regardless of mode; the
# read/write split is applied by the server name guard (see section 3), not
# the connector. PortfolioTreeTools, its "fin" sibling, gates its own write
# tools at construction (see test_fin_provider_contract below).
FIN_READ = {"portfolio_optimizer_list_objectives"}
FIN_WRITE = {"portfolio_optimizer_run", "portfolio_optimizer_backtest"}
PORTFOLIO_TREE_READ = {"portfolio_tree_validate", "portfolio_tree_list", "portfolio_tree_load"}
PORTFOLIO_TREE_WRITE = {
    "portfolio_tree_save",
    "portfolio_tree_delete",
    "portfolio_tree_estimate",
    "portfolio_tree_backtest",
}

EARNINGS_TOOLS = {
    "earnings_vocabulary",
    "earnings_week",
    "earnings_for_day",
    "earnings_aggregate",
    "earnings_event",
}

TRADINGVIEW_TOOLS = {
    "tradingview_vocabulary",
    "tradingview_fields",
    "tradingview_resolve",
    "tradingview_quote",
    "tradingview_screen",
    "tradingview_breadth",
}

POLYMARKET_TOOLS = {
    "polymarket_list_markets",
    "polymarket_get_market",
    "polymarket_order_book",
    "polymarket_price",
    "polymarket_midpoint",
}

GLEIF_TOOLS = {
    "gleif_search",
    "gleif_get_record",
    "gleif_parents",
    "gleif_children",
    "gleif_fuzzy_search",
}

REPORT_READ = {"render_memo", "render_memo_html"}
REPORT_WRITE = {"save_memo_html", "save_memo_markdown", "save_report"}

TELEGRAM_TOOLS = {"telegram_send_message", "telegram_send_document"}
GMAIL_TOOLS = {"gmail_list_emails", "gmail_get_email", "gmail_create_draft", "gmail_send"}
OUTLOOK_TOOLS = {"outlook_list_emails", "outlook_get_email", "outlook_create_draft", "outlook_send"}


def _names(provider) -> set[str]:
    return {t.name for t in provider.as_tools()}


def _names_multi(providers) -> set[str]:
    out: set[str] = set()
    for p in providers:
        out |= _names(p)
    return out


def test_datahub_read_write_contract() -> None:
    from lazytools.connectors.datahub import DataHubTools
    from lazytools.testing import FakeDataHubBackend

    assert _names(DataHubTools(FakeDataHubBackend(), allow_refresh=False)) == DATAHUB_READ
    assert _names(DataHubTools(FakeDataHubBackend(), allow_refresh=True)) == DATAHUB_READ | DATAHUB_WRITE


def test_statistical_contract() -> None:
    from lazytools.statistical_analysis import StatisticalAnalysisTools

    assert _names(StatisticalAnalysisTools()) == STATISTICAL_TOOLS


def test_earnings_calendar_contract() -> None:
    """Read-only: the calendar is written by its ingestion job, never by an agent."""
    from lazytools.connectors.earnings_calendar import EarningsCalendarTools

    assert _names(EarningsCalendarTools()) == EARNINGS_TOOLS


def test_tradingview_contract() -> None:
    """Read-only: the endpoint has no write surface and nothing is persisted.

    Constructed with a client that has no transport, so building the tool list
    reaches no network -- the contract is about the surface, not the service.
    """
    from lazytools.connectors.tradingview import ScreenerClient, TradingViewTools

    provider = TradingViewTools(client=ScreenerClient(transport=object()))
    assert _names(provider) == TRADINGVIEW_TOOLS


def test_polymarket_contract() -> None:
    """Read-only: no write surface at all, order placement needs a wallet.

    Constructed with a client that has no transport, so building the tool
    list reaches no network -- the contract is about the surface, not the
    service.
    """
    from lazytools.connectors.polymarket import PolymarketClient, PolymarketTools

    provider = PolymarketTools(client=PolymarketClient(transport=object()))
    assert _names(provider) == POLYMARKET_TOOLS


def test_gleif_contract() -> None:
    """Read-only: this is a reference lookup, there is nothing to write.

    Constructed with a client that has no transport, so building the tool
    list reaches no network -- the contract is about the surface, not the
    service.
    """
    from lazytools.connectors.gleif import GLEIFClient, GLEIFTools

    provider = GLEIFTools(client=GLEIFClient(transport=object()))
    assert _names(provider) == GLEIF_TOOLS


def test_fin_provider_contract(monkeypatch) -> None:
    # "fin"'s factory returns a *list* ([PortfolioOptimizationTools,
    # PortfolioTreeTools]) — go through default_providers so the
    # list-flattening it relies on is exercised too, not just the individual
    # provider classes. PortfolioOptimizationTools doesn't gate at
    # construction (see the note above FIN_READ/FIN_WRITE), so its write
    # tools only disappear in read-only mode via the server name guard —
    # exercised here through expand_tools, not default_providers alone.
    from lazytools.mcp_server.server import expand_tools

    pytest.importorskip("lazyfin")
    pytest.importorskip("lazyportfolio")
    monkeypatch.delenv("LAZYPORTFOLIO_TREE_MODELS_DIR", raising=False)

    ro_providers = default_providers(["fin"], allow_write=False)
    wr_providers = default_providers(["fin"], allow_write=True)
    assert set(expand_tools(ro_providers, read_only=True)) == FIN_READ | PORTFOLIO_TREE_READ
    assert (
        set(expand_tools(wr_providers, read_only=False))
        == FIN_READ | FIN_WRITE | PORTFOLIO_TREE_READ | PORTFOLIO_TREE_WRITE
    )


def test_report_contract(tmp_path, monkeypatch) -> None:
    # report's factory returns a *list* ([ReportTools, ReportFiles]) in write
    # mode — go through default_providers so the list-flattening it relies on
    # is exercised too, not just the individual provider classes.
    monkeypatch.setenv("LAZYTOOLS_DATA_DIR", str(tmp_path))
    assert _names_multi(default_providers(["report"], allow_write=False)) == REPORT_READ
    assert _names_multi(default_providers(["report"], allow_write=True)) == REPORT_READ | REPORT_WRITE


def test_specialist_agents_are_opt_in_only(tmp_path, monkeypatch) -> None:
    # Unlike every other provider, these construct a real lazybridge.Agent —
    # they must be entirely absent unless BOTH allow_write=True AND the
    # configured model's API key are present, never just present-but-limited.
    # stats_agents is included here because it once had no gating at all (an
    # audit finding): it must behave identically to optimizer_agent/report_agent.
    pytest.importorskip("lazybridge")
    pytest.importorskip("lazyfin")
    pytest.importorskip("lazyportfolio")
    monkeypatch.setenv("LAZYTOOLS_DATA_DIR", str(tmp_path))
    ids = ["optimizer_agent", "report_agent", "stats_agents"]

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert default_providers(ids, allow_write=False) == []
    assert default_providers(ids, allow_write=True) == []  # key still missing

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-a-real-secret")
    assert default_providers(ids, allow_write=False) == []  # allow_write still required

    providers = default_providers(ids, allow_write=True)
    # stats_agents' factory returns a *list* (3 specialists + 1 supervisor) —
    # flatten the same way default_providers/expand_tools do downstream.
    flat = []
    for p in providers:
        flat.extend(p if isinstance(p, list) else [p])
    names = {getattr(p, "name", None) for p in flat}
    assert names == {
        "portfolio-optimizer-specialist",
        "report-specialist",
        "volatility-correlation-analyst",
        "regime-analyst",
        "regression-analyst",
        "stats-supervisor",
    }
    assert all(getattr(p, "_is_lazy_agent", False) for p in flat)


def test_stats_agents_absent_without_write_or_credential(tmp_path, monkeypatch) -> None:
    """Narrower regression for the specific audit finding: stats_agents used
    to construct its agents unconditionally, regardless of allow_write or
    DEEPSEEK_API_KEY -- assert the raw factory itself raises, not just that
    default_providers' try/except happens to swallow it."""
    pytest.importorskip("lazybridge")
    monkeypatch.setenv("LAZYTOOLS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    from lazytools.mcp_server.providers import PROVIDER_FACTORIES

    factory = PROVIDER_FACTORIES["stats_agents"]
    with pytest.raises(RuntimeError, match="opt-in"):
        factory(allow_write=False)

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-a-real-secret")
    with pytest.raises(RuntimeError, match="opt-in"):
        factory(allow_write=False)


def test_comms_connectors_contract() -> None:
    from lazytools.connectors.gmail import GmailTools
    from lazytools.connectors.outlook import OutlookTools
    from lazytools.connectors.telegram import TelegramTools
    from lazytools.testing.fake_clients import (
        FakeGmailService,
        FakeOutlookService,
        FakeTelegramService,
    )

    tg = TelegramTools(FakeTelegramService())
    gm = GmailTools(FakeGmailService())
    ol = OutlookTools(FakeOutlookService())
    assert _names(tg) == TELEGRAM_TOOLS
    assert _names(gm) == GMAIL_TOOLS
    assert _names(ol) == OUTLOOK_TOOLS
    # Sends must stay behind the one-shot confirmation gate by default, so an
    # MCP prompt can never fire one unattended.
    assert tg.require_confirmation and gm.require_confirmation and ol.require_confirmation


# --------------------------------------------------------------------------- #
# 3. Read/write split on the actual mounted surface
# --------------------------------------------------------------------------- #


def _clear_comms_env(monkeypatch) -> None:
    # Make the env-gated connectors skip deterministically, independent of the
    # host machine, so the mounted surface is reproducible. Includes
    # DEEPSEEK_API_KEY so optimizer_agent/report_agent skip here too — their
    # own opt-in gating is exercised deterministically (key present AND
    # absent) by test_specialist_agents_are_opt_in_only instead.
    for var in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "LAZYTOOLS_GMAIL_CREDENTIALS",
        "LAZYTOOLS_GMAIL_TOKEN",
        "LAZYTOOLS_ENABLE_OUTLOOK",
        "LAZYTOOLS_EMAIL_ALLOWLIST",
        "DEEPSEEK_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def _mounted(monkeypatch, *, allow_write: bool) -> set[str]:
    _clear_comms_env(monkeypatch)
    providers = default_providers(None, allow_write=allow_write)
    return set(expand_tools(providers, read_only=not allow_write))


# Substrings that must never appear in a read-only-served tool name: these are
# the mutating/ side-effecting verbs. A read tool matching one is a leak.
_DANGER_SUBSTRINGS = (
    "_ensure_",
    "_register",
    "_send",
    "_delete",
    "_fit",
    "_init_db",
    "optimizer_run",
    "optimizer_backtest",
    "optimizer_create",
    "generate_plots",
    "save_",
    "tree_estimate",
    "tree_backtest",
    "-specialist",
)


def test_readonly_mounted_surface_has_no_mutating_tools(monkeypatch) -> None:
    ro = _mounted(monkeypatch, allow_write=False)
    leaked = [n for n in ro if any(s in n for s in _DANGER_SUBSTRINGS)]
    assert leaked == [], f"mutating tools leaked into read-only surface: {leaked}"


def test_write_surface_is_a_strict_superset(monkeypatch) -> None:
    ro = _mounted(monkeypatch, allow_write=False)
    wr = _mounted(monkeypatch, allow_write=True)
    assert ro <= wr
    # Write mode must actually expose writers, not just re-serve the read set.
    assert wr - ro, "write mode exposed no additional (mutating) tools"


def test_unsafe_patterns_cover_the_optimizer_and_depot_writers() -> None:
    # Guards against a future edit dropping a pattern that currently keeps a
    # mutating optimizer/regime tool out of read-only mode.
    def unsafe(name: str) -> bool:
        return any(p in name for p in UNSAFE_TOOL_PATTERNS)

    for mutating in (
        "portfolio_optimizer_run",
        "portfolio_optimizer_backtest",
        "portfolio_optimizer_create_benchmark",
        "regime_init_db",
        "telegram_send_message",
        "save_memo_html",
        "save_memo_markdown",
        "save_report",
        "portfolio_tree_save",
        "portfolio_tree_delete",
        "portfolio_tree_estimate",
        "portfolio_tree_backtest",
        "portfolio-optimizer-specialist",
        "report-specialist",
    ):
        assert unsafe(mutating), f"{mutating} no longer matches an unsafe pattern"
    for read in (
        "portfolio_optimizer_list_methods",
        "portfolio_optimizer_get_run",
        "portfolio_optimizer_get_backtest",
        "render_memo",
        "render_memo_html",
        "portfolio_tree_validate",
        "portfolio_tree_list",
        "portfolio_tree_load",
    ):
        assert not unsafe(read), f"{read} wrongly matches an unsafe pattern"
