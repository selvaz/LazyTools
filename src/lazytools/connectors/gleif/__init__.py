"""GLEIF's Global LEI Index as bounded, live tools for an agent.

    from lazytools.connectors.gleif import GLEIFTools

    tools = GLEIFTools()
    tools.gleif_search("Apple Inc.", country="US")

The connector is stateless and read-only. See :mod:`.tools` for the LLM tool
surface and :mod:`.client` for the underlying HTTP calls.

**Terms of use.** GLEIF makes Global LEI Index data available as a free, open
public good under CC0 without restrictions on reuse. This connector sends
whatever it returns to the model provider running the agent, which is still a
transmission to that third party.
"""

from __future__ import annotations

from lazytools.connectors.gleif.client import (
    GLEIFBudgetExceeded,
    GLEIFClient,
    GLEIFError,
    LEIRecord,
)
from lazytools.connectors.gleif.tools import MAX_ROWS, GLEIFTools

__all__ = [
    "GLEIFTools",
    "GLEIFClient",
    "GLEIFError",
    "GLEIFBudgetExceeded",
    "LEIRecord",
    "MAX_ROWS",
]
