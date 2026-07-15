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

from collections.abc import Callable
from typing import Any

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
