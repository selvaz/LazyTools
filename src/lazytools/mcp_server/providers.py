"""Default provider menu for the LazyTools MCP server.

The mapping below is the server's "menu": a stable id → factory for each
provider `lazytools-mcp` can serve. Each factory takes a single
``allow_write`` flag:

* ``allow_write=False`` (the default) constructs the provider in its
  **read-only** shape — ``DataHubTools()`` without ``allow_refresh``,
  ``RegimeTools(allow_write=False)`` — which never even *emit* their write
  tools.
* ``allow_write=True`` constructs the **write-enabled** shape, so the
  mutating tools (datahub refresh/register, regime fit/persist/delete) are
  emitted. This is the CLI's ``--allow-unsafe`` path; there is no MCP
  confirmation gating, so it is strictly opt-in.

Providers whose optional extra is missing still construct here — expansion
in :func:`.server.expand_tools` is what skips them, so the menu stays
declarative and import-light.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any


def _data_home() -> str:
    """Per-user directory for LazyTools-owned SQLite stores / artifacts.

    Overridable with ``LAZYTOOLS_DATA_DIR``; defaults to ``~/.lazytools``.
    This only computes the path — it never creates the directory, so merely
    listing the (read-only) provider menu never touches the filesystem. Callers
    that actually write create it under an ``allow_write`` branch.
    """
    return os.environ.get("LAZYTOOLS_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".lazytools")


#: id → factory(allow_write, *, data_source=None) returning a ToolProvider.
#: Order here is the order tools are listed to the client. Read-only by
#: default; a factory only emits write tools when called with
#: ``allow_write=True``.
PROVIDER_FACTORIES: dict[str, Callable[..., Any]] = {}


def _register(provider_id: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def deco(factory: Callable[..., Any]) -> Callable[..., Any]:
        PROVIDER_FACTORIES[provider_id] = factory
        return factory

    return deco


@_register("datahub")
def _datahub(allow_write: bool = False, *, data_source: dict[str, Any] | None = None) -> Any:
    """market-data-hub discovery / resolution / financial facts.

    ``allow_write`` enables ``allow_refresh`` (the on-demand ingestion
    writers ``datahub_ensure_*`` and ``datahub_register_listing``).
    """
    from lazytools.connectors.datahub import DataHubTools
    from lazytools.connectors.datahub.backend import MarketDataHubBackend

    db_path = (data_source or {}).get("path")
    if db_path:
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    backend = MarketDataHubBackend(db_path=db_path) if db_path else None
    return DataHubTools(backend=backend, allow_refresh=allow_write)


@_register("statistical")
def _statistical(allow_write: bool = False, *, data_source: dict[str, Any] | None = None) -> Any:
    """Volatility, correlation, outliers and regression over hub series (read-only)."""
    from lazytools.statistical_analysis import StatisticalAnalysisTools

    return StatisticalAnalysisTools(db_path=(data_source or {}).get("path"))  # no write surface


@_register("regimes")
def _regimes(allow_write: bool = False, *, data_source: dict[str, Any] | None = None) -> Any:
    """HMM / Markov-switching regimes (needs lazystats[regimes]).

    ``allow_write`` enables the fitting / persistence / deletion tools.
    """
    from lazytools.connectors.regimes import RegimeTools

    return RegimeTools(
        allow_write=allow_write,
        db_path=_regime_db_path(data_source),
        market_data_path=(data_source or {}).get("path"),
    )


def _regime_db_path(data_source: dict[str, Any] | None) -> str:
    """The one depot path used by BOTH the ``regimes`` provider and the
    ``report`` provider's ``regimes:`` figure resolver -- previously each
    computed its own default independently (this function had its own
    explicit/env/default chain, ``_regimes`` above had none at all and just
    let ``RegimeTools`` fall through to ITS OWN chain), a dormant version of
    the exact bug that made a freshly-generated regime plot resolve as "not
    found" elsewhere in this codebase. Delegates to lazystats' canonical
    resolver so there is exactly one such chain in the whole ecosystem.

    ``report`` must keep working even when lazystats isn't installed (it only
    needs a path string for figure resolution, not the regimes package itself)
    -- falls back to the same env/default chain lazystats' resolver uses when
    the import is unavailable.
    """
    explicit = (data_source or {}).get("regime_db_path")
    try:
        from lazystats.regimes import resolve_depot_path
    except ImportError:
        if explicit:
            return explicit
        env = os.environ.get("LAZYTOOLS_REGIME_DB")
        if env:
            return env
        return os.path.join(_data_home(), "regime_depot.db")
    return resolve_depot_path(explicit)


@_register("report")
def _report(allow_write: bool = False, *, data_source: dict[str, Any] | None = None) -> Any:
    """LazyReport — deterministic memo rendering ("LazyReport").

    Always emits the pure renderers (``render_memo``, ``render_memo_html``);
    figures resolve from the regime depot (``regimes:``), on-demand
    market-data-hub charts (``chart:``), or a local file (``file:``).
    ``allow_write`` additionally sandboxes a ``reports/`` directory under the
    data home and emits the persisting tools (``save_memo_html``,
    ``save_memo_markdown`` from ``ReportTools``, ``save_report`` from
    ``ReportFiles``) so a rendered report can be written to disk and then
    handed to an outbound tool (e.g. ``telegram_send_document``); dropped in
    read-only server mode by the name guard, same as the other writers.

    Returns a *list* of two providers in write mode — the same
    ``[ReportTools(artifacts=..., files=files), files]`` shape
    ``lazytools.skills`` already uses to wire an agent's report tools, so
    ``save_report`` (a distinct tool on ``ReportFiles`` itself, not merged
    into ``ReportTools``) is reachable without a name collision. See
    :func:`default_providers`, which flattens a list-returning factory.
    """
    from lazytools.report import ReportFiles, ReportTools, ecosystem_resolvers

    reports_dir = os.path.join(_data_home(), "reports")
    resolvers = ecosystem_resolvers(
        regimes_db=_regime_db_path(data_source),
        datahub_db_path=(data_source or {}).get("path"),
        file_base_dir=reports_dir,
    )
    if not allow_write:
        return ReportTools(artifacts=resolvers)
    os.makedirs(reports_dir, exist_ok=True)
    files = ReportFiles(base_dir=reports_dir)
    return [ReportTools(artifacts=resolvers, files=files), files]


@_register("web")
def _web(allow_write: bool = False, *, data_source: dict[str, Any] | None = None) -> Any:
    """LazyCrawler search / crawl / get-page as tools (needs the [web] extra).

    Smart-mode (content="smart") extraction runs an LLM via LazyBridge. The
    provider is inferred from the model string; on this deployment DeepSeek is
    the configured provider, so the crawler's default OpenAI ``gpt-4o-mini`` is
    overridden here. Override the model with ``LAZYTOOLS_WEB_MODEL``. The
    matching provider key (e.g. ``DEEPSEEK_API_KEY``) must be present in the
    server's environment or every smart-mode page fails with ``llm_error``;
    the zero-token ``*_ml`` presets need no key.
    """
    from lazycrawler import CrawlerDB, CrawlerTools, DBConfig, LLMConfig
    from lazycrawler.config import resolve_news_db_path

    from lazytools.connectors.web import WebTools

    model = os.environ.get("LAZYTOOLS_WEB_MODEL", "deepseek-v4-flash")
    # A dedicated ``news_db_path`` key -- NOT the shared ``path`` key every
    # other provider here uses for the market-data-hub db, which this is
    # unrelated to. Reusing "path" would mean a single --config JSON setting
    # it for the datahub/statistical/regimes/fin providers silently redirects
    # the crawler's cache at the wrong file too.
    # Explicit data_source override, else LazyCrawler's own canonical resolver
    # (LAZYCRAWLER_NEWS_DB env, the same one its setup_first_run.ps1 persists)
    # -- not a second, independent env-var chain. CrawlerTools itself now
    # warns (never silently) if this still resolves to nothing and it falls
    # back to ":memory:".
    db_path = (data_source or {}).get("news_db_path") or resolve_news_db_path()
    if db_path:
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    db = CrawlerDB(DBConfig(db_path=db_path)) if db_path else None
    provider = CrawlerTools(db=db, llm_cfg=LLMConfig(model=model))
    return WebTools(provider=provider)


@_register("fin")
def _fin(allow_write: bool = False, *, data_source: dict[str, Any] | None = None) -> Any:
    """LazyPortfolio hierarchical (V2) optimizer + walk-forward backtest.

    Two providers share one data backend:

    * ``PortfolioOptimizationTools`` — ``portfolio_optimizer_*``, a single flat
      node over market-data-hub returns. Stateless: no persisted store, unlike
      the removed Skfolio-direct engine this replaced. Emits all its tools
      regardless of ``allow_write``; the mutating ones (run / backtest) are
      dropped in read-only server mode by the name guard alone.
    * ``PortfolioTreeTools`` — ``portfolio_tree_*``, the full node-tree
      (parent/child hierarchies, per-node proxies, flat/forward/
      forward_backward modes), persisted through the same shared store Tree
      Studio (LazyPortfolio's local visual editor) reads and writes — a tree
      built via one is immediately visible in the other. Gates its own
      mutating tools (save/delete/estimate/backtest) at construction via
      ``allow_write``, on top of the name guard.

    Needs the ``lazyportfolio`` package; if that is missing the server skips
    this provider (each ``as_tools()`` raises and expansion is per-provider).
    """
    from lazyportfolio import MarketDataHubOptimizationBackend

    from lazytools.connectors.fin.tools import PortfolioOptimizationTools
    from lazytools.connectors.fin.tree_tools import PortfolioTreeTools

    backend = MarketDataHubOptimizationBackend(db_path=(data_source or {}).get("path"))
    return [
        PortfolioOptimizationTools(backend=backend),
        PortfolioTreeTools(
            backend=backend,
            # This provider's own single allow_write switch still maps onto
            # all three of PortfolioTreeTools' privileges together -- the
            # split (docs/node-advisor-operational-plan.md §7.2) exists so a
            # *different* caller (NodeAdvisorReadTools) can grant none of
            # them, not to change this general-purpose provider's behavior.
            allow_compute=allow_write,
            allow_persist=allow_write,
            allow_delete=allow_write,
            store_path=(data_source or {}).get("tree_store_db"),
        ),
    ]


@_register("optimizer_agent")
def _optimizer_agent(allow_write: bool = False, *, data_source: dict[str, Any] | None = None) -> Any:
    """LLM specialist agent over the ``fin`` tool surface, exposed as ONE MCP tool.

    Unlike every other provider here, this constructs a ``lazybridge.Agent`` —
    ``expand_tools`` wraps any ``_is_lazy_agent`` object into a single tool
    (``portfolio-optimizer-specialist``) taking one ``task: str`` argument;
    calling it runs the agent's own internal tool loop and returns only its
    final text. That is a real LLM call (cost, non-determinism, its own
    multi-step tool sequencing a per-name safety net can't inspect from
    outside) — qualitatively different from this server's other, deterministic
    tools. So unlike ``fin`` itself, this provider is **opt-in only**: it
    raises (skipped by :func:`default_providers`'s existing broad except,
    same as a missing optional dependency) unless ``allow_write=True``, and
    again if the configured model's API key isn't set.

    Model: ``LAZYTOOLS_OPTIMIZER_AGENT_MODEL``, default ``deepseek-v4-flash``
    (needs ``DEEPSEEK_API_KEY`` — same convention as the ``web`` provider).
    """
    if not allow_write:
        raise RuntimeError("optimizer_agent is opt-in only: pass allow_write=True (--allow-unsafe).")
    model = os.environ.get("LAZYTOOLS_OPTIMIZER_AGENT_MODEL", "deepseek-v4-flash")
    if model.startswith("deepseek") and not os.environ.get("DEEPSEEK_API_KEY"):
        raise RuntimeError("optimizer_agent needs DEEPSEEK_API_KEY for its default model; skipped.")

    from lazybridge import LLMEngine
    from lazyportfolio import MarketDataHubOptimizationBackend

    from lazytools.connectors.datahub import DataHubTools
    from lazytools.connectors.fin.optimizer_agent import (
        OPTIMIZER_SPECIALIST_SYSTEM,
        optimizer_specialist,
    )
    from lazytools.connectors.fin.tools import PortfolioOptimizationTools
    from lazytools.connectors.fin.tree_tools import PortfolioTreeTools

    backend = MarketDataHubOptimizationBackend(db_path=(data_source or {}).get("path"))
    tools: list[Any] = [
        DataHubTools(),
        PortfolioOptimizationTools(backend=backend),
        PortfolioTreeTools(
            backend=backend,
            allow_compute=True,
            allow_persist=True,
            allow_delete=True,
            store_path=(data_source or {}).get("tree_store_db"),
        ),
    ]
    engine = LLMEngine(model, system=OPTIMIZER_SPECIALIST_SYSTEM, max_tool_calls_per_turn=16)
    return optimizer_specialist(engine, tools=tools)


@_register("report_agent")
def _report_agent(allow_write: bool = False, *, data_source: dict[str, Any] | None = None) -> Any:
    """LLM specialist agent over the ``report`` tool surface, one MCP tool.

    Same opt-in gating and rationale as ``optimizer_agent``. Deliberately
    narrow tool list — render/save only (``ReportTools``/``ReportFiles``,
    same ``ecosystem_resolvers``/shared ``reports/`` directory as the
    ``report`` provider) — no direct ``datahub_*``/``statistical_*``/
    ``regime_*`` access; the caller supplies the content to structure.

    Model: ``LAZYTOOLS_REPORT_AGENT_MODEL``, default ``deepseek-v4-flash``
    (needs ``DEEPSEEK_API_KEY``).
    """
    if not allow_write:
        raise RuntimeError("report_agent is opt-in only: pass allow_write=True (--allow-unsafe).")
    model = os.environ.get("LAZYTOOLS_REPORT_AGENT_MODEL", "deepseek-v4-flash")
    if model.startswith("deepseek") and not os.environ.get("DEEPSEEK_API_KEY"):
        raise RuntimeError("report_agent needs DEEPSEEK_API_KEY for its default model; skipped.")

    from lazybridge import LLMEngine

    from lazytools.report import (
        REPORT_SPECIALIST_SYSTEM,
        ReportFiles,
        ReportTools,
        ecosystem_resolvers,
        report_specialist,
    )

    reports_dir = os.path.join(_data_home(), "reports")
    os.makedirs(reports_dir, exist_ok=True)
    resolvers = ecosystem_resolvers(
        regimes_db=_regime_db_path(data_source),
        datahub_db_path=(data_source or {}).get("path"),
        file_base_dir=reports_dir,
    )
    tools: list[Any] = [ReportTools(artifacts=resolvers, files=ReportFiles(base_dir=reports_dir))]
    engine = LLMEngine(model, system=REPORT_SPECIALIST_SYSTEM, max_tool_calls_per_turn=16)
    return report_specialist(engine, tools=tools)


@_register("stats_agents")
def _stats_agents(allow_write: bool = False, *, data_source: dict[str, Any] | None = None) -> Any:
    """Expose the statistical specialists and supervisor as MCP tools.

    Same opt-in gating and rationale as ``optimizer_agent``/``report_agent``:
    these construct real ``lazybridge.Agent`` instances (LLM calls, cost,
    non-determinism), so they must be entirely absent unless BOTH
    ``allow_write=True`` AND the configured model's API key are present —
    never just present-but-limited.

    Model: ``LAZYTOOLS_STATS_AGENT_MODEL``, default ``deepseek-v4-flash``
    (needs ``DEEPSEEK_API_KEY``).
    """
    if not allow_write:
        raise RuntimeError("stats_agents is opt-in only: pass allow_write=True (--allow-unsafe).")
    model = os.environ.get("LAZYTOOLS_STATS_AGENT_MODEL", "deepseek-v4-flash")
    if model.startswith("deepseek") and not os.environ.get("DEEPSEEK_API_KEY"):
        raise RuntimeError("stats_agents needs DEEPSEEK_API_KEY for its default model; skipped.")

    from lazytools.skills.stats_agents import (
        regime_analyst,
        regression_analyst,
        stats_supervisor,
        volatility_correlation_analyst,
    )

    specialists = [
        volatility_correlation_analyst(model=model),
        regime_analyst(model=model, allow_write=allow_write),
        regression_analyst(model=model),
    ]
    return [
        *specialists,
        stats_supervisor(
            model=model,
            specialists=specialists,
            regime_allow_write=allow_write,
        ),
    ]


@_register("telegram")
def _telegram(allow_write: bool = False, *, data_source: dict[str, Any] | None = None) -> Any:
    """Telegram send (message + document), bounded to an allow-listed chat.

    Lights up when ``TELEGRAM_BOT_TOKEN`` is set (needs the ``telegram`` extra,
    httpx). Sends are allow-listed to ``TELEGRAM_CHAT_ID``. MCP has no
    interactive confirm step, so the one-shot confirmation gate is disabled
    ONLY when an allow-list is present — a send can then reach the user's own
    chat but never an arbitrary one. With no allow-list, sends stay gated
    (and therefore blocked over MCP), while the surface remains visible.
    """
    from lazytools.connectors.telegram import TelegramClient, TelegramTools

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set; telegram connector skipped.")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    allow: list[int | str] | None = [chat] if chat else None
    # Confine document uploads to a controlled reports directory so
    # telegram_send_document can never exfiltrate arbitrary readable host files
    # (SSH keys, secrets) even when the confirmation gate is disabled. Only
    # create the directory when send is actually enabled (write mode).
    attachments = os.environ.get("LAZYTOOLS_ATTACHMENTS_DIR") or os.path.join(_data_home(), "reports")
    if allow_write:
        os.makedirs(attachments, exist_ok=True)
    client = TelegramClient.from_token(token)
    return TelegramTools(
        client,
        allowed_chat_ids=allow,
        require_confirmation=allow is None,
        attachments_dir=attachments,
    )


@_register("gmail")
def _gmail(allow_write: bool = False, *, data_source: dict[str, Any] | None = None) -> Any:
    """Gmail read / draft / send. Opt-in and non-interactive only.

    Requires ``LAZYTOOLS_GMAIL_CREDENTIALS`` (OAuth client secret) and an
    already-authorized ``LAZYTOOLS_GMAIL_TOKEN`` file — if the token file is
    absent the provider is skipped rather than triggering an interactive OAuth
    flow at server startup. Sends stay gated unless ``LAZYTOOLS_EMAIL_ALLOWLIST``
    (comma-separated recipients) is set.
    """
    creds = os.environ.get("LAZYTOOLS_GMAIL_CREDENTIALS")
    token = os.environ.get("LAZYTOOLS_GMAIL_TOKEN")
    if not (creds and token and os.path.exists(token)):
        raise RuntimeError(
            "Gmail not configured (need LAZYTOOLS_GMAIL_CREDENTIALS and an existing "
            "LAZYTOOLS_GMAIL_TOKEN file); gmail connector skipped."
        )
    scopes = ["https://www.googleapis.com/auth/gmail.modify"]
    # Guard against an interactive OAuth flow at server startup: a token file
    # can exist yet be expired without a usable refresh token, in which case
    # GmailClient.from_credentials would fall through to run_local_server and
    # block the stdio server. Pre-validate (and refresh) the cached token here,
    # non-interactively, and skip the provider if it can't be made valid.
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as exc:
        raise RuntimeError("Gmail extra not installed; gmail connector skipped.") from exc
    cached = Credentials.from_authorized_user_file(token, scopes)
    if not cached.valid:
        if cached.expired and cached.refresh_token:
            cached.refresh(Request())  # non-interactive
        else:
            raise RuntimeError(
                "Gmail token is invalid and not refreshable without an interactive "
                "flow; gmail connector skipped."
            )
    from lazytools.connectors.gmail import GmailClient, GmailTools

    # Token is now valid (or refreshable), so from_credentials takes its
    # non-interactive branch and never opens a local server.
    client = GmailClient.from_credentials(credentials_path=creds, token_path=token, scopes=scopes)
    allow_env = os.environ.get("LAZYTOOLS_EMAIL_ALLOWLIST")
    allow = [a.strip() for a in allow_env.split(",") if a.strip()] if allow_env else None
    return GmailTools(client, allowed_recipients=allow, require_confirmation=allow is None)


@_register("outlook")
def _outlook(allow_write: bool = False, *, data_source: dict[str, Any] | None = None) -> Any:
    """Outlook (local desktop, COM) read / draft / send. Opt-in.

    Enable with ``LAZYTOOLS_ENABLE_OUTLOOK=1`` (needs ``pywin32`` and a running,
    signed-in Outlook on Windows). Sends stay gated unless
    ``LAZYTOOLS_EMAIL_ALLOWLIST`` is set.
    """
    if os.environ.get("LAZYTOOLS_ENABLE_OUTLOOK") != "1":
        raise RuntimeError("Outlook connector disabled (set LAZYTOOLS_ENABLE_OUTLOOK=1 to enable).")
    from lazytools.connectors.outlook import OutlookClient, OutlookTools

    client = OutlookClient.connect()
    allow_env = os.environ.get("LAZYTOOLS_EMAIL_ALLOWLIST")
    allow = [a.strip() for a in allow_env.split(",") if a.strip()] if allow_env else None
    return OutlookTools(client, allowed_recipients=allow, require_confirmation=allow is None)


@_register("registry")
def _registry(allow_write: bool = False, *, data_source: dict[str, Any] | None = None) -> Any:
    """Ecosystem DB registry + cross-repo artifact catalog. Core, no extra needed.

    ``allow_write`` enables ``artifact_register``; ``registry_status``/
    ``artifact_search``/``artifact_get`` are always read-only regardless.
    """
    from lazytools.registry import RegistryTools

    return RegistryTools(allow_write=allow_write)


def default_providers(
    ids: list[str] | None = None,
    *,
    allow_write: bool = False,
    data_source: dict[str, Any] | None = None,
) -> list[Any]:
    """Instantiate the default providers.

    ``ids`` selects a subset (validated against :data:`PROVIDER_FACTORIES`);
    ``None`` builds them all. ``allow_write`` is threaded to every factory —
    ``False`` (default) yields read-only providers, ``True`` yields the
    write-enabled shapes (the CLI's ``--allow-unsafe`` path). ``data_source``
    is ALSO threaded to every factory (each already accepts it -- ``path``
    for market-data-hub, ``regime_db_path`` for the regime depot, etc.) --
    until this was wired up here, every factory's ``data_source`` parameter
    was silent dead code: nothing ever called ``default_providers`` with one,
    so it was always ``None`` no matter what a factory's own docstring
    implied was configurable. See ``__main__.py``'s ``--config`` flag for
    where a caller actually populates this now.

    A factory that raises at construction time (rare — most only fail later,
    at ``as_tools()``) is skipped so one missing dependency never sinks the
    server. A factory may return a single provider, or a ``list``/``tuple``
    of several (e.g. ``report``'s ``[ReportTools(...), ReportFiles(...)]`` —
    the same shape ``lazytools.skills`` uses to wire the same two providers
    for an agent) — either shape is flattened into the returned list.
    """
    selected = list(PROVIDER_FACTORIES) if ids is None else ids
    unknown = [i for i in selected if i not in PROVIDER_FACTORIES]
    if unknown:
        raise ValueError(
            f"Unknown provider id(s): {', '.join(unknown)}. Known: {', '.join(PROVIDER_FACTORIES)}"
        )

    providers: list[Any] = []
    for provider_id in selected:
        try:
            built = PROVIDER_FACTORIES[provider_id](allow_write, data_source=data_source)
        except Exception as exc:  # construction failure is non-fatal
            import logging

            logging.getLogger("lazytools.mcp_server").warning(
                "Could not construct provider %r: %s", provider_id, exc
            )
            continue
        if isinstance(built, (list, tuple)):
            providers.extend(built)
        else:
            providers.append(built)
    return providers
