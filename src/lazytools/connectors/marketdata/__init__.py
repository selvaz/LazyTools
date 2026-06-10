"""Market-data connector: swappable price adapters, client, and tools.

Only the concrete :class:`StooqAdapter` needs the ``marketdata`` extra
(``httpx``); the :class:`MarketDataAdapter` protocol, :class:`MarketDataClient`,
and :class:`MarketDataTools` import without it and are fully testable with a
fake adapter. Prices are strings (Decimal-safe) throughout.
"""

from __future__ import annotations

from lazytools.connectors.marketdata.adapters import (
    DEFAULT_MAX_RESPONSE_BYTES,
    RANGE_DAYS,
    MarketDataAdapter,
    StooqAdapter,
)
from lazytools.connectors.marketdata.client import VALID_RANGES, MarketDataClient
from lazytools.connectors.marketdata.tools import MarketDataTools

__all__ = [
    "DEFAULT_MAX_RESPONSE_BYTES",
    "RANGE_DAYS",
    "VALID_RANGES",
    "MarketDataAdapter",
    "MarketDataClient",
    "MarketDataTools",
    "StooqAdapter",
]
