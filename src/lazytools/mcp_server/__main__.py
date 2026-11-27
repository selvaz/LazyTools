"""CLI entry points: ``lazytools-mcp`` and ``lazytools-orchestration-mcp``.

``python -m lazytools.mcp_server`` is the module form of ``lazytools-mcp``.
Both serve read-only LazyTools providers over stdio through one launcher.
Positional args (or the ``LAZYTOOLS_MCP_PROVIDERS`` env var, comma-separated)
select a subset of provider ids; with none given, ``lazytools-mcp`` serves all
read-only providers and ``lazytools-orchestration-mcp`` those in
``ORCHESTRATION_PROVIDERS`` (the only ids it accepts).

``--config <path.json>`` (or ``LAZYTOOLS_MCP_CONFIG``) points at a JSON file
that becomes every provider factory's ``data_source`` dict -- e.g. ``{"path":
"...", "regime_db_path": "...", "news_db_path": "...", "tree_store_db":
"..."}``. Without it, each provider falls back to its own individual env var
(``MARKET_DATA_DB``, ``LAZYTOOLS_REGIME_DB``, ``LAZYCRAWLER_NEWS_DB``, ...);
this is only for overriding several at once from one file.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import os
from dataclasses import dataclass

from lazytools import __version__
from lazytools.mcp_server._code_scope import configured_task_resolver
from lazytools.mcp_server.providers import ORCHESTRATION_PROVIDERS, PROVIDER_FACTORIES, default_providers
from lazytools.mcp_server.server import build_server, expand_tools, serve_http, serve_stdio, visible_tools


@dataclass(frozen=True)
class _ServerSpec:
    """What differs between the console scripts that share :func:`_launch`."""

    prog: str
    name: str
    description: str
    instructions: str
    #: Default and only accepted provider ids; ``None`` means every provider.
    providers: tuple[str, ...] | None = None
    #: Send new approval tickets as MCP notifications (with ``--allow-unsafe``).
    notify_approvals: bool = False


_DATA_SERVER = _ServerSpec(
    prog="lazytools-mcp",
    name="lazytools",
    description="Serve LazyTools' read-only tool providers over the Model Context Protocol (stdio).",
    instructions=(
        "LazyTools ecosystem tools: market-data-hub discovery/financials (datahub_*), "
        "statistical analysis (statistical_*), regime detection (regime_*), Skfolio "
        "portfolio optimization/backtest (portfolio_optimizer_*), deterministic memo/report "
        "rendering (render_memo*, save_memo_*, save_report — figures embed from regime "
        "plots, on-demand hub charts, or local files), web search/crawl, and guarded "
        "messaging (telegram_*/gmail_*/outlook_* when configured). Read-only unless "
        "started with --allow-unsafe. Messaging/email connectors light up only when their "
        "credentials/opt-in env vars are set; sends are allow-listed. With --allow-unsafe "
        "and a local Codex login, codex_code_review(task, repo_path, diff_ref, paths) "
        "delegates a read-only code review of a local repository to Codex (minutes, one "
        "model turn per call). With --allow-unsafe, codex_write(task, cwd, session_name) "
        "delegates a coding task to Codex WITH write access (workspace-write sandbox), "
        "confined to the same root. A governed code task requires a typed approval "
        "for each write. The code tools take optional per-call model, "
        "effort and session_name (a durable, renamable conversation alias; see "
        "code_sessions_list)."
    ),
)

_ORCHESTRATION_SERVER = _ServerSpec(
    prog="lazytools-orchestration-mcp",
    name="lazytools-orchestration",
    description="Serve LazyTools' orchestration tool providers over the Model Context Protocol (stdio).",
    instructions=(
        "LazyTools orchestration tools: project registry (projects_*), code-bridge fleet, "
        "routing and model views (bridge_*, capabilities_search), pull-request polling "
        "(pr_watch_poll), repository maps (codemap_*) and recorded lessons (lessons_search). "
        "Read-only unless started with --allow-unsafe, which adds coding jobs (code_bridge_*), "
        "pull-request work (pr_navigate, bridge_publish, bridge_apply), ticket decisions and "
        "job cancellation (bridge_approve, bridge_reject, bridge_cancel), read-only Codex and "
        "Claude Code review and questions (codex_*, claude_*; each needs its local CLI), named "
        "sessions (code_sessions_*) and lesson recording (lessons_record)."
    ),
    providers=ORCHESTRATION_PROVIDERS,
    notify_approvals=True,
)

#: Providers that ``lazytools-mcp`` keeps for one release only.
_MOVING_PROVIDERS = frozenset(
    {"code_bridge", "code_review", "claude_review", "code_sessions", "codemap", "fleet", "pr"}
)
_DEPRECATION_NOTE = "coding tools move to lazytools-orchestration-mcp; they stay here for one release"

_SCIENTIFIC_PROVIDER_MODULES: dict[str, tuple[str, ...]] = {
    "datahub": ("market_data_hub.agent_tools",),
    "statistical": (
        "market_data_hub.extract",
        "market_data_hub.reader",
        "market_data_hub.lazydatacore",
    ),
    "econ_calendar": ("market_data_hub.db.connection", "market_data_hub.econ_calendar.catalog"),
    "earnings_calendar": ("market_data_hub.db.connection", "market_data_hub.earnings_calendar"),
    "cftc_cot": ("market_data_hub.reader",),
    "treasury_fiscal": ("market_data_hub.reader",),
    "report": ("market_data_hub.extract",),
}


def _preimport_scientific_modules(provider_ids: list[str] | None) -> None:
    """Load scientific dependencies before stdio dispatch starts worker threads.

    Some data providers deliberately import pandas and hub readers only at the
    first tool call.  Importing those modules from the dispatch worker can
    deadlock with the main event-loop thread, so warm the applicable import
    graph here while startup still runs synchronously.  Every import remains
    optional: an unavailable provider is handled by the existing provider
    construction/expansion guards rather than making the server fail startup.
    """
    selected = provider_ids if provider_ids is not None else list(PROVIDER_FACTORIES)
    provider_modules = [
        module
        for provider_id in selected
        for module in _SCIENTIFIC_PROVIDER_MODULES.get(provider_id, ())
    ]
    if not provider_modules:
        return
    modules = ["numpy", "pandas"]
    modules.extend(provider_modules)
    for module in dict.fromkeys(modules):
        try:
            importlib.import_module(module)
        except Exception:  # same isolation as default_providers(): a broken optional dep must not stop other providers
            logging.getLogger("lazytools.mcp_server").debug(
                "Optional startup pre-import unavailable: %s", module, exc_info=True
            )


def _env(name: str, default: str | None = None) -> str | None:
    """Read ``name``; an unset OR blank value means "use the default".

    Installers and launchers blank variables they do not set; a blank
    ``LAZYTOOLS_MCP_HOST`` must not widen the bind to every interface, and a
    blank ``LAZYTOOLS_MCP_PORT`` must not crash ``--help``.
    """
    value = os.environ.get(name, "").strip()
    return value or default


def _build_parser(spec: _ServerSpec) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=spec.prog, description=spec.description)
    parser.add_argument(
        "providers",
        nargs="*",
        help=f"Provider ids to serve (default: all). Known: {', '.join(spec.providers or PROVIDER_FACTORIES)}.",
    )
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="Print the names of the tools these flags would serve, one per line, and exit.",
    )
    parser.add_argument(
        "--allow-unsafe",
        action="store_true",
        help="Construct providers in write-enabled mode AND disable the read-only "
        "name guard, exposing mutating tools (datahub refresh/register, regime "
        "fit/persist/delete). No MCP confirmation gating is applied — use only "
        "when you accept full responsibility for the writes.",
    )
    parser.add_argument(
        "--log-level",
        default=_env("LAZYTOOLS_MCP_LOG_LEVEL", "INFO"),
        help="Logging level for stderr diagnostics (default: INFO).",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve over Streamable HTTP instead of stdio. The practical difference is "
        "whose process it is: a stdio server is spawned by the client, so its tool list "
        "is fixed for that client's lifetime and picking up a changed tool means "
        "restarting the editor. Over HTTP the process is its own, and a client that "
        "reconnects re-reads the tool list -- so a tool change costs a restart of this "
        "server rather than of the editor. Configure it in .mcp.json with "
        '{\"type\": \"http\", \"url\": \"http://127.0.0.1:8787/mcp\"} (a url without a '
        "type is read as a stdio entry and skipped).",
    )
    parser.add_argument(
        "--host",
        default=_env("LAZYTOOLS_MCP_HOST", "127.0.0.1"),
        help="Interface to bind with --http (default: 127.0.0.1). Loopback by default "
        "deliberately: these tools read, and with --allow-unsafe write, local production "
        "databases.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(_env("LAZYTOOLS_MCP_PORT") or "8787"),
        help="Port to bind with --http (default: 8787).",
    )
    parser.add_argument(
        "--config",
        default=_env("LAZYTOOLS_MCP_CONFIG"),
        help="Path to a JSON file populating each provider factory's data_source dict "
        "(e.g. {\"path\": \"...\", \"regime_db_path\": \"...\", \"news_db_path\": \"...\", "
        "\"web_cache_db\": \"...\" (opt-in interactive web cache; see LAZYTOOLS_WEB_CACHE_DB), "
        "\"tree_store_db\": \"...\"}). "
        "Without this, every provider falls back to its own individual env-var/default "
        "resolution (MARKET_DATA_DB, LAZYTOOLS_REGIME_DB, LAZYCRAWLER_NEWS_DB, ...) -- this "
        "flag is for overriding several at once from one file instead of setting each env "
        "var separately. Also settable via LAZYTOOLS_MCP_CONFIG.",
    )
    return parser


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return _build_parser(_DATA_SERVER).parse_args(argv)


def _load_data_source(config_path: str | None) -> dict[str, object] | None:
    if not config_path:
        return None
    with open(config_path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"--config {config_path!r} must contain a JSON object, got {type(data).__name__}")
    return data


def _launch(spec: _ServerSpec, argv: list[str] | None = None) -> None:
    parser = _build_parser(spec)
    args = parser.parse_args(argv)

    # Logs go to stderr — stdout is the JSON-RPC channel and must stay clean.
    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s %(name)s: %(message)s")

    ids: list[str] | None = args.providers or None
    if ids is None:
        env = os.environ.get("LAZYTOOLS_MCP_PROVIDERS", "").strip()
        if env:
            ids = [part.strip() for part in env.split(",") if part.strip()]
    if spec.providers is not None:
        refused = [i for i in ids or () if i not in spec.providers]
        if refused:
            parser.error(
                f"provider id(s) not served by {spec.prog}: {', '.join(refused)}. "
                f"Allowed: {', '.join(spec.providers)}"
            )
        ids = ids or list(spec.providers)

    data_source = _load_data_source(args.config)
    if args.http and (data_source or {}).get("projects_task_principal") is not None:
        raise SystemExit("projects_task_principal requires a dedicated stdio host for one authenticated principal")
    if not args.list_tools:
        _preimport_scientific_modules(ids)
    providers = default_providers(ids, allow_write=args.allow_unsafe, data_source=data_source)
    if spec.providers is None and any(
        getattr(p, "_lazytools_mcp_provider_id", None) in _MOVING_PROVIDERS for p in providers
    ):
        logging.getLogger("lazytools.mcp_server").warning(_DEPRECATION_NOTE)
    if args.list_tools:
        # No request exists here: resolve a configured code task the stdio way,
        # which is what an authenticated client of either transport is served.
        tools = visible_tools(
            expand_tools(providers, read_only=not args.allow_unsafe),
            configured_task_resolver(data_source or {}, http=False),
        )
        for tool_name in sorted(tools):
            print(tool_name)
        return
    server = build_server(
        providers,
        name=spec.name,
        version=__version__,
        read_only=not args.allow_unsafe,
        code_task_resolver=configured_task_resolver(data_source or {}, http=args.http),
        instructions=spec.instructions,
        notify_approvals=spec.notify_approvals,
    )
    if args.http:
        serve_http(server, host=args.host, port=args.port)
    else:
        asyncio.run(serve_stdio(server))


def main(argv: list[str] | None = None) -> None:
    """``lazytools-mcp``: every provider, data and (for one release) coding."""
    _launch(_DATA_SERVER, argv)


def orchestration_main(argv: list[str] | None = None) -> None:
    """``lazytools-orchestration-mcp``: only ``ORCHESTRATION_PROVIDERS``."""
    _launch(_ORCHESTRATION_SERVER, argv)


if __name__ == "__main__":  # pragma: no cover
    main()
