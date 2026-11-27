"""Authoritative presentation metadata for the MCP provider catalog.

The registry in :mod:`lazytools.mcp_server.providers` owns construction; this
module owns the stable, UI-facing classification of those same providers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict

from lazytools.connectors.web.cache import web_cache_requested
from lazytools.mcp_server.server import is_unsafe_tool_name

Access = Literal["read", "write"]


class NamedTool(Protocol):
    """The small part of ``lazybridge.Tool`` needed by :func:`tool_metadata`."""

    name: str


@dataclass(frozen=True, slots=True)
class ProviderCatalogEntry:
    """Display and provenance metadata for one registered provider."""

    group: str
    group_order: int
    provider_label: str
    origin_repo: str
    description: str


class ToolMetadata(TypedDict):
    """Metadata attached to an individual inventory tool."""

    group: str
    group_order: int
    provider_label: str
    origin_repo: str
    access: Access


def _entry(group: str, order: int, label: str, origin: str, description: str) -> ProviderCatalogEntry:
    return ProviderCatalogEntry(group, order, label, origin, description)


# Keep this adjacent in purpose to PROVIDER_FACTORIES: coverage is enforced by
# tests so adding a provider to the construction registry requires cataloguing it.
PROVIDER_GROUPS: dict[str, ProviderCatalogEntry] = {
    "codemap": _entry("Coding agents", 6, "Code Map", "LazyTools", "Rank candidate files and map repositories with RepoMap."),
    "datahub": _entry("Market data", 0, "Data Hub", "market-data-hub", "Discover and resolve market data and financial facts."),
    "tradingview": _entry("Market data", 0, "TradingView", "LazyTools", "Screen live markets and inspect breadth and fund snapshots."),
    "cftc_cot": _entry("Market data", 0, "CFTC COT", "market-data-hub", "Inspect CFTC Commitments of Traders positioning."),
    "treasury_fiscal": _entry("Market data", 0, "Treasury Fiscal", "market-data-hub", "Query U.S. Treasury fiscal data."),
    "alfred": _entry("Market data", 0, "ALFRED", "LazyTools", "Query point-in-time FRED vintages."),
    "gleif": _entry("Market data", 0, "GLEIF", "LazyTools", "Look up legal entities and LEIs."),
    "econ_calendar": _entry("Calendars and macro", 1, "Economic Calendar", "market-data-hub", "Inspect economic release dates and values."),
    "earnings_calendar": _entry("Calendars and macro", 1, "Earnings Calendar", "market-data-hub", "Inspect corporate earnings dates."),
    "calendar_agent": _entry("Calendars and macro", 1, "Calendar Agent", "LazyTools", "Ask the macro-calendar specialist to analyse releases."),
    "polymarket": _entry("Prediction markets", 2, "Polymarket", "LazyTools", "Inspect Polymarket markets and order books."),
    "manifold": _entry("Prediction markets", 2, "Manifold", "LazyTools", "Inspect Manifold crowd forecasts."),
    "statistical": _entry("Statistics and regimes", 3, "Statistical Analysis", "LazyStats", "Analyse volatility, correlation, outliers, and regressions."),
    "regimes": _entry("Statistics and regimes", 3, "Regimes", "LazyStats", "Fit and inspect market-regime models."),
    "stats_agents": _entry("Statistics and regimes", 3, "Statistics Agents", "LazyTools", "Run statistical specialist and supervisor agents."),
    "fin": _entry("Portfolio", 4, "Portfolio", "LazyPortfolio", "Optimise and backtest flat or hierarchical portfolios."),
    "optimizer_agent": _entry("Portfolio", 4, "Optimizer Agent", "LazyTools", "Ask the portfolio-optimisation specialist."),
    "web": _entry("Research and reports", 5, "Web Research", "LazyCrawler", "Search, crawl, and retrieve research pages."),
    "report": _entry("Research and reports", 5, "Reports", "LazyTools", "Render and persist deterministic research memos."),
    "report_agent": _entry("Research and reports", 5, "Report Agent", "LazyTools", "Ask the report specialist to structure a memo."),
    "code_bridge": _entry("Coding agents", 6, "Code Bridge", "LazyTools", "Submit, monitor, and retrieve coding-agent jobs."),
    "code_write": _entry("Coding agents", 6, "Code Write", "LazyTools", "Run Claude Code or Codex with repository write access."),
    "code_review": _entry("Coding agents", 6, "Codex Review", "LazyTools", "Review code and answer design questions with Codex."),
    "claude_review": _entry("Coding agents", 6, "Claude Review", "LazyTools", "Review code and answer design questions with Claude Code."),
    "code_sessions": _entry("Coding agents", 6, "Code Sessions", "LazyTools", "Inspect and curate named coding sessions."),
    "fleet": _entry("Coding agents", 6, "Fleet", "LazyTools", "Read jobs, approval tickets, routes, autonomy, models, and capabilities."),
    "telegram": _entry("Messaging", 7, "Telegram", "LazyTools", "Send allow-listed Telegram messages and documents."),
    "gmail": _entry("Messaging", 7, "Gmail", "LazyTools", "Read, draft, and send Gmail messages."),
    "outlook": _entry("Messaging", 7, "Outlook", "LazyTools", "Read, draft, and send Outlook messages."),
    "projects": _entry("Operations", 8, "Projects", "LazyTools", "Inspect and maintain the shared project registry."),
    "registry": _entry("Operations", 8, "Registry", "LazyTools", "Inspect ecosystem databases and artifacts."),
    "knowledge": _entry("Operations", 8, "Knowledge", "LazyTools", "Search and record the lessons shared with the dispatcher."),
    "pulse": _entry("Operations", 8, "LazyCEO backlog and approvals", "LazyCEO", "Inspect LazyCEO backlog items and approval tickets."),
    "studio": _entry("Operations", 8, "Studio", "LazyStudio", "Discover and invoke agents published by LazyStudio."),
}


# These factories emit no tools at all without allow_write. Consequently every
# tool they can contribute is write-access inventory, even where the underlying
# operation is a model review or another non-filesystem side effect.
_WRITE_ONLY_PROVIDERS = frozenset(
    {
        "calendar_agent",
        "claude_review",
        "code_bridge",
        "code_review",
        "code_sessions",
        "code_write",
        "optimizer_agent",
        "report_agent",
        "stats_agents",
    }
)
_STUDIO_READ_TOOLS = frozenset({"studio_run_status", "studio_status"})
# Regime writers whose names deliberately do not match the coarse server name
# guard. RegimeTools is the authoritative gate and only emits these in its
# allow_write shape (the server module documents this limitation explicitly).
_GATED_WRITE_TOOLS = {
    "projects": frozenset(
        {
            "projects_add_note",
            "projects_create",
            "projects_open_contract",
            "projects_promote",
            "projects_review_plan",
            "projects_revise_plan",
            "projects_schedule_task",
            "projects_set_deadline",
            "projects_set_owner",
        }
    ),
    "regimes": frozenset(
        {
            "regime_apply_params",
            "regime_generate_plots",
            "regime_load_from_datahub",
        }
    )
}

# These optimizer calls only compute from market data and return their result;
# they have no persisted store. They remain in the server's conservative unsafe
# patterns as expensive computations, but catalog access describes state writes.
_READ_ONLY_PATTERN_EXCEPTIONS = frozenset(
    {"portfolio_optimizer_run", "portfolio_optimizer_backtest"}
)


def tool_metadata(
    provider_id: str, tool: str | NamedTool, *, web_cache_db: str | None = None
) -> ToolMetadata:
    """Return catalog metadata and effective access for ``tool``.

    Access follows the server's existing write knowledge: providers that only
    exist with ``allow_write`` are write-only, while mixed providers use the
    same unsafe-name guard as the read-only MCP surface.

    Raises:
        KeyError: if ``provider_id`` is not catalogued.
        TypeError: if ``tool`` is neither a tool name nor an object with one.
    """
    entry = PROVIDER_GROUPS[provider_id]
    name = tool if isinstance(tool, str) else getattr(tool, "name", None)
    if not isinstance(name, str):
        raise TypeError("tool must be a name or an object with a string 'name' attribute")

    write = (
        provider_id in _WRITE_ONLY_PROVIDERS
        or name in _GATED_WRITE_TOOLS.get(provider_id, ())
        or is_unsafe_tool_name(name)
    )
    if name in _READ_ONLY_PATTERN_EXCEPTIONS:
        write = False
    # With interactive isolation enabled, web_* fetches only populate a
    # disposable local page cache. Unset, they retain LazyCrawler's production
    # write path and must not be advertised as read-only.
    if provider_id == "web" and name.startswith("web_"):
        write = not web_cache_requested(web_cache_db)
    if provider_id == "studio" and name not in _STUDIO_READ_TOOLS:
        write = True
    return {
        "group": entry.group,
        "group_order": entry.group_order,
        "provider_label": entry.provider_label,
        "origin_repo": entry.origin_repo,
        "access": "write" if write else "read",
    }
