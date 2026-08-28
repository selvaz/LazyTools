"""Polymarket's public market data as bounded, live tools for an agent.

    from lazytools.connectors.polymarket import PolymarketTools

    tools = PolymarketTools()
    tools.polymarket_list_markets(tag_id=745)  # numeric tag id, e.g. NBA

Stateless: nothing is stored, so an answer cannot be reproduced later.
Read-only by construction -- placing or cancelling an order needs a
wallet-signed request this connector does not carry. See :mod:`.tools` for
the tool surface and :mod:`.client` for the underlying Gamma/CLOB calls.

**Terms of use.** Gamma and CLOB market data are public and need no API key,
but they are still Polymarket's data, rate-limited per IP, and this
connector sends whatever it returns to the model provider the agent runs on
-- a transmission to a third party, whatever the intent. Deciding that your
use is permitted is yours, not this package's.
"""

from __future__ import annotations

from lazytools.connectors.polymarket.client import (
    CLOB_URL,
    GAMMA_URL,
    Market,
    PolymarketBudgetExceeded,
    PolymarketClient,
    PolymarketError,
)
from lazytools.connectors.polymarket.tools import MAX_ROWS, PolymarketTools

__all__ = [
    "PolymarketTools",
    "PolymarketClient",
    "PolymarketError",
    "PolymarketBudgetExceeded",
    "Market",
    "GAMMA_URL",
    "CLOB_URL",
    "MAX_ROWS",
]
