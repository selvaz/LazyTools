"""CLI entry point: ``python -m lazytools.mcp_server`` / ``lazytools-mcp``.

Serves the read-only LazyTools providers over stdio. Positional args (or the
``LAZYTOOLS_MCP_PROVIDERS`` env var, comma-separated) select a subset of
provider ids; with none given, all read-only providers are served.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

from lazytools import __version__
from lazytools.mcp_server.providers import PROVIDER_FACTORIES, default_providers
from lazytools.mcp_server.server import build_server, serve_stdio


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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    # Logs go to stderr — stdout is the JSON-RPC channel and must stay clean.
    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s %(name)s: %(message)s")

    ids: list[str] | None = args.providers or None
    if ids is None:
        env = os.environ.get("LAZYTOOLS_MCP_PROVIDERS", "").strip()
        if env:
            ids = [part.strip() for part in env.split(",") if part.strip()]

    providers = default_providers(ids, allow_write=args.allow_unsafe)
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
            "credentials/opt-in env vars are set; sends are allow-listed."
        ),
    )
    asyncio.run(serve_stdio(server))


if __name__ == "__main__":  # pragma: no cover
    main()
