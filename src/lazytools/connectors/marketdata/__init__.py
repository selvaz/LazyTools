"""Market-data connector: swappable price adapters and CLIENT only.

The LLM-facing ``MarketDataTools`` provider was REMOVED (audit CA-03, no
compatibility window needed): agents get prices exclusively through the
hub-backed ``datahub_*`` tools. The client/adapters stay as injectable
plumbing for non-agent code (e.g. LazyFin's PriceSource protocol).

Only the concrete :class:`StooqAdapter` needs the ``marketdata`` extra
(``httpx``); the :class:`MarketDataAdapter` protocol, :class:`MarketDataClient`,
and :class:`MarketDataTools` import without it and are fully testable with a
fake adapter. Prices are strings (Decimal-safe) throughout.
"""

from __future__ import annotations

from lazytools.connectors.marketdata.adapters import (
    DEFAULT_MAX_RESPONSE_BYTES,
    RANGE_DAYS,
    UNKNOWN_CURRENCY,
    MarketDataAdapter,
    MarketDataUnavailable,
    StooqAdapter,
)
from lazytools.connectors.marketdata.client import VALID_RANGES, MarketDataClient

__all__ = [
    "DEFAULT_MAX_RESPONSE_BYTES",
    "RANGE_DAYS",
    "UNKNOWN_CURRENCY",
    "VALID_RANGES",
    "MarketDataAdapter",
    "MarketDataUnavailable",
    "MarketDataClient",
    "StooqAdapter",
]
