"""market-data-hub connector: discovery + extraction as LazyBridge tools.

Wraps market-data-hub's ``tool_*`` surface the lazytools way — a
:class:`DataHubBackend` Protocol, a default :class:`MarketDataHubBackend` that
lazily imports ``market_data_hub`` (the ``datahub`` extra), and a
:class:`DataHubTools` ``ToolProvider`` exposing ``datahub_*`` tools. The
provider and protocol import without the extra and are testable with a fake
backend.
"""

from __future__ import annotations

from lazytools.connectors.datahub.backend import DataHubBackend, MarketDataHubBackend
from lazytools.connectors.datahub.tools import DataHubTools

__all__ = [
    "DataHubBackend",
    "DataHubTools",
    "MarketDataHubBackend",
]
