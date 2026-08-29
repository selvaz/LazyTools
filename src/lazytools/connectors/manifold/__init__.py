"""Manifold Markets' public market data as bounded, live tools for an agent.

    from lazytools.connectors.manifold import ManifoldTools

    tools = ManifoldTools()
    tools.manifold_search_markets("artificial intelligence", limit=10)

Stateless: nothing is stored, so an answer cannot be reproduced later.
Read-only by construction: this connector exposes no bet-placement surface.
See :mod:`.tools` for the tool surface and :mod:`.client` for the underlying
public REST calls.

Manifold markets are mostly play-money markets. Their probabilities reflect
crowd forecasting on a platform where most markets have no real-money stakes,
which is a materially different signal from Polymarket's real-money prices and
should be considered when comparing the two.

**Terms of use.** Manifold market data are public and need no API key, but they
are still Manifold Markets' data, subject to its service policies, and this
connector sends whatever it returns to the model provider the agent runs on --
a transmission to a third party, whatever the intent. Deciding that your use
is permitted is yours, not this package's.
"""

from __future__ import annotations

from lazytools.connectors.manifold.client import (
    ManifoldBudgetExceeded,
    ManifoldClient,
    ManifoldError,
    Market,
)
from lazytools.connectors.manifold.tools import MAX_ROWS, ManifoldTools

__all__ = [
    "ManifoldTools",
    "ManifoldClient",
    "ManifoldError",
    "ManifoldBudgetExceeded",
    "Market",
    "MAX_ROWS",
]
