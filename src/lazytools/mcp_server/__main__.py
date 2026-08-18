"""CLI entry point: ``python -m lazytools.mcp_server`` / ``lazytools-mcp``.

Serves the read-only LazyTools providers over stdio. Positional args (or the
``LAZYTOOLS_MCP_PROVIDERS`` env var, comma-separated) select a subset of
provider ids; with none given, all read-only providers are served.

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
import json
import logging
import os

from lazytools import __version__
from lazytools.mcp_server.providers import PROVIDER_FACTORIES, default_providers
from lazytools.mcp_server.server import build_server, serve_http, serve_stdio


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="lazytools-mcp",
        description="Serve LazyTools' read-only tool providers over the Model Context Protocol (stdio).",
    )
    parser.add_argument(
        "providers",
        nargs="*",
        help=f"Provider ids to serve (default: all). Known: {', '.join(PROVIDER_FACTORIES)}.",
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
        default=os.environ.get("LAZYTOOLS_MCP_LOG_LEVEL", "INFO"),
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
        default=os.environ.get("LAZYTOOLS_MCP_HOST", "127.0.0.1"),
        help="Interface to bind with --http (default: 127.0.0.1). Loopback by default "
        "deliberately: these tools read, and with --allow-unsafe write, local production "
        "databases.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("LAZYTOOLS_MCP_PORT", "8787")),
        help="Port to bind with --http (default: 8787).",
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("LAZYTOOLS_MCP_CONFIG"),
        help="Path to a JSON file populating each provider factory's data_source dict "
        "(e.g. {\"path\": \"...\", \"regime_db_path\": \"...\", \"news_db_path\": \"...\", "
        "\"tree_store_db\": \"...\"}). "
        "Without this, every provider falls back to its own individual env-var/default "
        "resolution (MARKET_DATA_DB, LAZYTOOLS_REGIME_DB, LAZYCRAWLER_NEWS_DB, ...) -- this "
        "flag is for overriding several at once from one file instead of setting each env "
        "var separately. Also settable via LAZYTOOLS_MCP_CONFIG.",
    )
    return parser.parse_args(argv)


def _load_data_source(config_path: str | None) -> dict[str, object] | None:
    if not config_path:
        return None
    with open(config_path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"--config {config_path!r} must contain a JSON object, got {type(data).__name__}")
    return data


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    # Logs go to stderr — stdout is the JSON-RPC channel and must stay clean.
    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s %(name)s: %(message)s")

    ids: list[str] | None = args.providers or None
    if ids is None:
        env = os.environ.get("LAZYTOOLS_MCP_PROVIDERS", "").strip()
        if env:
            ids = [part.strip() for part in env.split(",") if part.strip()]

    data_source = _load_data_source(args.config)
    providers = default_providers(ids, allow_write=args.allow_unsafe, data_source=data_source)
    server = build_server(
        providers,
        name="lazytools",
        version=__version__,
        read_only=not args.allow_unsafe,
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
            "model turn per call)."
        ),
    )
    if args.http:
        serve_http(server, host=args.host, port=args.port)
    else:
        asyncio.run(serve_stdio(server))


if __name__ == "__main__":  # pragma: no cover
    main()
