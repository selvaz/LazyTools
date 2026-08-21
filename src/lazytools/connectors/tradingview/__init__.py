"""TradingView's screener as bounded, live tools for an agent.

    from lazytools.connectors.tradingview import TradingViewTools

    tools = TradingViewTools()
    tools.tradingview_breadth(universe="us_cap1b")

Stateless: nothing is stored, so an answer cannot be reproduced later. Most
tools reach the endpoint on every call; ``tradingview_fields`` and all of
``tradingview_vocabulary`` except its ``enumerations`` section answer from the
local catalogue and cost nothing. See :mod:`.tools` for what the live ones
cost and :mod:`.catalog` for everything the surface accepts.

**Terms of use.** The endpoint is undocumented, the data is licensed to
TradingView by the exchanges, and this connector sends its values to whichever
model provider the agent runs on. That last step is a transmission to a third
party, whatever the intent. Deciding that your use is permitted is yours, not
this package's; see the compliance warning in the connectors guide.
"""

from __future__ import annotations

from lazytools.connectors.tradingview.catalog import (
    BREADTH_METRICS,
    BUNDLES,
    FIELDS,
    MARKETS,
    SCREENS,
    TIMEFRAMES,
    UNIVERSES,
)
from lazytools.connectors.tradingview.client import (
    ScanResult,
    ScreenerBudgetExceeded,
    ScreenerClient,
    ScreenerError,
)
from lazytools.connectors.tradingview.tools import (
    MAX_ROWS,
    MAX_SYMBOLS,
    TradingViewTools,
)

__all__ = [
    "TradingViewTools",
    "ScreenerClient",
    "ScreenerError",
    "ScreenerBudgetExceeded",
    "ScanResult",
    "FIELDS",
    "BUNDLES",
    "SCREENS",
    "BREADTH_METRICS",
    "UNIVERSES",
    "MARKETS",
    "TIMEFRAMES",
    "MAX_SYMBOLS",
    "MAX_ROWS",
]
