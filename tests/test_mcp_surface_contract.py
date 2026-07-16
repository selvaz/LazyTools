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
    "regimes",
    "web",
    "fin",
    "telegram",
    "gmail",
    "outlook",
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

# The optimizer connector emits all seven regardless of mode; the read/write
# split is applied by the server name guard (see section 3), not the connector.
FIN_TOOLS = {
    "portfolio_optimizer_list_methods",
    "portfolio_optimizer_create_benchmark",
    "portfolio_optimizer_list_benchmarks",
    "portfolio_optimizer_run",
    "portfolio_optimizer_get_run",
    "portfolio_optimizer_get_backtest",
    "portfolio_optimizer_backtest",
}

TELEGRAM_TOOLS = {"telegram_send_message", "telegram_send_document"}
GMAIL_TOOLS = {"gmail_list_emails", "gmail_get_email", "gmail_create_draft", "gmail_send"}
OUTLOOK_TOOLS = {"outlook_list_emails", "outlook_get_email", "outlook_create_draft", "outlook_send"}


def _names(provider) -> set[str]:
    return {t.name for t in provider.as_tools()}


def test_datahub_read_write_contract() -> None:
    from lazytools.connectors.datahub import DataHubTools
    from lazytools.testing import FakeDataHubBackend

    assert _names(DataHubTools(FakeDataHubBackend(), allow_refresh=False)) == DATAHUB_READ
    assert _names(DataHubTools(FakeDataHubBackend(), allow_refresh=True)) == DATAHUB_READ | DATAHUB_WRITE


def test_statistical_contract() -> None:
    from lazytools.statistical_analysis import StatisticalAnalysisTools

    assert _names(StatisticalAnalysisTools()) == STATISTICAL_TOOLS


def test_fin_optimizer_contract(tmp_path) -> None:
    pytest.importorskip("lazyfin.optimization", reason="fin connector needs lazyfin[optimizer]")
    pytest.importorskip("skfolio")
    from lazyfin.optimization import OptimizationStore

    from lazytools.connectors.fin import PortfolioOptimizationTools

    store = OptimizationStore(str(tmp_path / "opt.db"))
    assert _names(PortfolioOptimizationTools(store)) == FIN_TOOLS


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
    # host machine, so the mounted surface is reproducible.
    for var in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "LAZYTOOLS_GMAIL_CREDENTIALS",
        "LAZYTOOLS_GMAIL_TOKEN",
        "LAZYTOOLS_ENABLE_OUTLOOK",
        "LAZYTOOLS_EMAIL_ALLOWLIST",
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
    ):
        assert unsafe(mutating), f"{mutating} no longer matches an unsafe pattern"
    for read in ("portfolio_optimizer_list_methods", "portfolio_optimizer_get_run", "portfolio_optimizer_get_backtest"):
        assert not unsafe(read), f"{read} wrongly matches an unsafe pattern"
