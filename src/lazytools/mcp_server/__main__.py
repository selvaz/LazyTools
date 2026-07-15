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
from lazytools.mcp_server.providers import READ_ONLY_PROVIDERS, default_providers
from lazytools.mcp_server.server import build_server, serve_stdio


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="lazytools-mcp",
        description="Serve LazyTools' read-only tool providers over the Model Context Protocol (stdio).",
    )
    parser.add_argument(
        "providers",
        nargs="*",
        help=f"Provider ids to serve (default: all). Known: {', '.join(READ_ONLY_PROVIDERS)}.",
    )
    parser.add_argument(
        "--allow-unsafe",
        action="store_true",
        help="Disable the read-only guard and expose mutating tools too. "
        "Only use with your own confirmation/allow-list gating in place.",
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

    providers = default_providers(ids)
    server = build_server(
        providers,
        name="lazytools",
        version=__version__,
        read_only=not args.allow_unsafe,
        instructions=(
            "LazyTools ecosystem tools: market-data-hub discovery/financials (datahub_*), "
            "statistical analysis (statistical_*), regime detection (regime_*), and web "
            "search/crawl. All read-only unless started with --allow-unsafe."
        ),
    )
    asyncio.run(serve_stdio(server))


if __name__ == "__main__":  # pragma: no cover
    main()
