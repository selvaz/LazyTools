"""ALFRED — FRED's point-in-time view — as bounded, live tools for an agent.

    from lazytools.connectors.alfred import ALFREDTools

    tools = ALFREDTools()
    tools.alfred_vintage("CPIAUCSL", as_of="2020-04-10")

The connector is stateless and read-only: it asks FRED at call time and
stores nothing. See :mod:`.tools` for the LLM tool surface and :mod:`.client`
for the HTTP calls.

**Why it exists.** Asking FRED what CPI was in March 2020 returns the number
as revised since — which nobody could have known in March 2020. Pinning the
vintage returns what was actually being published on a chosen date, which is
the only version a walk-forward backtest is entitled to use.

**Credential.** Needs a free FRED API key in ``FRED_API_KEY``, the same
variable market-data-hub already resolves. Resolved lazily, so importing and
constructing never require it — only calling does.
"""

from __future__ import annotations

from lazytools.connectors.alfred.client import (
    ALFREDBudgetExceeded,
    ALFREDClient,
    ALFREDError,
    Observation,
)
from lazytools.connectors.alfred.tools import MAX_OBSERVATIONS, ALFREDTools

__all__ = [
    "ALFREDTools",
    "ALFREDClient",
    "ALFREDError",
    "ALFREDBudgetExceeded",
    "Observation",
    "MAX_OBSERVATIONS",
]
