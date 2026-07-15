"""Default read-only provider set for the LazyTools MCP server.

The mapping below is the server's "menu": a stable id → factory for each
provider that is safe to expose over MCP with no interactive confirmation.
Every factory constructs the provider in its **read-only** configuration
(``DataHubTools()`` without ``allow_raw_series`` / ``allow_refresh``,
``RegimeTools()`` without ``allow_write``). Providers whose optional extra
is missing still construct here — expansion in :func:`.server.expand_tools`
is what skips them, so the menu stays declarative and import-light.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

#: id → zero-arg factory returning a read-only ToolProvider. Order here is
#: the order tools are listed to the client.
READ_ONLY_PROVIDERS: dict[str, Callable[[], Any]] = {}


def _register(provider_id: str) -> Callable[[Callable[[], Any]], Callable[[], Any]]:
    def deco(factory: Callable[[], Any]) -> Callable[[], Any]:
        READ_ONLY_PROVIDERS[provider_id] = factory
        return factory

    return deco


@_register("datahub")
def _datahub() -> Any:
    """market-data-hub discovery / resolution / financial-facts (read-only)."""
    from lazytools.connectors.datahub import DataHubTools

    return DataHubTools()  # no allow_raw_series, no allow_refresh


@_register("statistical")
def _statistical() -> Any:
    """Volatility, correlation, outliers and regression over hub series."""
    from lazytools.statistical_analysis import StatisticalAnalysisTools

    return StatisticalAnalysisTools()


@_register("regimes")
def _regimes() -> Any:
    """HMM / Markov-switching regime inspection (read-only; needs lazystats[regimes])."""
    from lazytools.connectors.regimes import RegimeTools

    return RegimeTools()  # allow_write defaults to False


@_register("web")
def _web() -> Any:
    """LazyCrawler search / crawl / get-page as tools (needs the [web] extra)."""
    from lazytools.connectors.web import WebTools

    return WebTools()


def default_providers(ids: list[str] | None = None) -> list[Any]:
    """Instantiate the default read-only providers.

    ``ids`` selects a subset (validated against :data:`READ_ONLY_PROVIDERS`);
    ``None`` builds them all. A factory that raises at construction time
    (rare — most only fail later, at ``as_tools()``) is skipped so one
    missing dependency never sinks the server.
    """
    selected = list(READ_ONLY_PROVIDERS) if ids is None else ids
    unknown = [i for i in selected if i not in READ_ONLY_PROVIDERS]
    if unknown:
        raise ValueError(
            f"Unknown provider id(s): {', '.join(unknown)}. "
            f"Known: {', '.join(READ_ONLY_PROVIDERS)}"
        )

    providers: list[Any] = []
    for provider_id in selected:
        try:
            providers.append(READ_ONLY_PROVIDERS[provider_id]())
        except Exception as exc:
            import logging

            logging.getLogger("lazytools.mcp_server").warning(
                "Could not construct provider %r: %s", provider_id, exc
            )
    return providers
