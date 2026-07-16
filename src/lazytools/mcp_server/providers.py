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


class _LazyOptimizationStore:
    """Defer ``OptimizationStore`` creation (and its SQLite file) until first
    use.

    ``OptimizationStore.__init__`` opens/creates its file eagerly, so building
    it just to list the provider menu would mutate the filesystem even on a
    read-only server. ``SkfolioOptimizer(store)`` and
    ``PortfolioOptimizationTools`` only stash the reference at construction, so
    this proxy lets the optimizer's read tools stay available while no file is
    touched until a tool that actually reads/writes the store is called.
    """

    def __init__(self, factory: Callable[[], Any]) -> None:
        self.__dict__["_factory"] = factory
        self.__dict__["_real"] = None

    def _resolve(self) -> Any:
        if self.__dict__["_real"] is None:
            self.__dict__["_real"] = self.__dict__["_factory"]()
        return self.__dict__["_real"]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolve(), name)

#: id → factory(allow_write) returning a ToolProvider. Order here is the
#: order tools are listed to the client. Read-only by default; a factory
#: only emits write tools when called with ``allow_write=True``.
PROVIDER_FACTORIES: dict[str, Callable[[bool], Any]] = {}


def _register(provider_id: str) -> Callable[[Callable[[bool], Any]], Callable[[bool], Any]]:
    def deco(factory: Callable[[bool], Any]) -> Callable[[bool], Any]:
        PROVIDER_FACTORIES[provider_id] = factory
        return factory

    return deco


@_register("datahub")
def _datahub(allow_write: bool = False) -> Any:
    """market-data-hub discovery / resolution / financial facts.

    ``allow_write`` enables ``allow_refresh`` (the on-demand ingestion
    writers ``datahub_ensure_*`` and ``datahub_register_listing``).
    """
    from lazytools.connectors.datahub import DataHubTools

    return DataHubTools(allow_refresh=allow_write)


@_register("statistical")
def _statistical(allow_write: bool = False) -> Any:
    """Volatility, correlation, outliers and regression over hub series (read-only)."""
    from lazytools.statistical_analysis import StatisticalAnalysisTools

    return StatisticalAnalysisTools()  # no write surface


@_register("regimes")
def _regimes(allow_write: bool = False) -> Any:
    """HMM / Markov-switching regimes (needs lazystats[regimes]).

    ``allow_write`` enables the fitting / persistence / deletion tools.
    """
    from lazytools.connectors.regimes import RegimeTools

    return RegimeTools(allow_write=allow_write)


@_register("web")
def _web(allow_write: bool = False) -> Any:
    """LazyCrawler search / crawl / get-page as tools (needs the [web] extra)."""
    from lazytools.connectors.web import WebTools

    return WebTools()  # read-only surface only


@_register("fin")
def _fin(allow_write: bool = False) -> Any:
    """LazyFin Skfolio-backed portfolio optimizer + walk-forward backtest.

    Exposes ``portfolio_optimizer_*`` over market-data-hub returns. Needs
    ``lazyfin`` with the optimizer extra (skfolio); if that is missing the
    server skips this provider (its ``as_tools()`` raises and expansion is
    per-provider). Runs and benchmarks persist to a private SQLite *audit*
    store — never market observations. The mutating tools (run / backtest /
    create_benchmark) are dropped in read-only server mode by the name guard.
    """
    from lazyfin.optimization import OptimizationStore

    from lazytools.connectors.fin.tools import PortfolioOptimizationTools

    # The store's file is created lazily (see _LazyOptimizationStore), so the
    # read tools stay available in read-only mode without any provider
    # construction touching disk — the file appears only when a store-backed
    # tool is actually used. Chart artifacts (a write path) are enabled only in
    # write mode.
    base = _data_home()
    store_path = os.environ.get("LAZYTOOLS_OPTIMIZER_STORE") or os.path.join(base, "optimizer_store.db")

    def _make_store() -> Any:
        os.makedirs(os.path.dirname(store_path) or ".", exist_ok=True)
        return OptimizationStore(store_path)

    artifacts: str | None = None
    if allow_write:
        artifacts = os.environ.get("LAZYTOOLS_OPTIMIZER_ARTIFACTS") or os.path.join(base, "optimizer_artifacts")
        os.makedirs(artifacts, exist_ok=True)
    return PortfolioOptimizationTools(_LazyOptimizationStore(_make_store), artifacts_dir=artifacts)


@_register("telegram")
def _telegram(allow_write: bool = False) -> Any:
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
def _gmail(allow_write: bool = False) -> Any:
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
def _outlook(allow_write: bool = False) -> Any:
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


def default_providers(ids: list[str] | None = None, *, allow_write: bool = False) -> list[Any]:
    """Instantiate the default providers.

    ``ids`` selects a subset (validated against :data:`PROVIDER_FACTORIES`);
    ``None`` builds them all. ``allow_write`` is threaded to every factory —
    ``False`` (default) yields read-only providers, ``True`` yields the
    write-enabled shapes (the CLI's ``--allow-unsafe`` path). A factory that
    raises at construction time (rare — most only fail later, at
    ``as_tools()``) is skipped so one missing dependency never sinks the
    server.
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
            providers.append(PROVIDER_FACTORIES[provider_id](allow_write))
        except Exception as exc:  # construction failure is non-fatal
            import logging

            logging.getLogger("lazytools.mcp_server").warning(
                "Could not construct provider %r: %s", provider_id, exc
            )
    return providers
