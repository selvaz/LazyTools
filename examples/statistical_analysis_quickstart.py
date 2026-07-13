"""Minimal LazyBridge agent using the statistical_analysis tools.

The canonical, zero-boilerplate shape: build an Agent with one tool
provider, ask it a question in plain language, print the answer. The tool
descriptions and per-parameter docstrings (see
``lazytools/statistical_analysis/tools.py``) are what let the model figure
out instrument specs, transforms and frequencies on its own — nothing here
tells it how to call the tools.

Requires:
    - an LLM provider API key matching MODEL below (e.g. ANTHROPIC_API_KEY);
    - market-data-hub reachable (optional MARKET_DATA_DB env var; the hub's
      default path is used otherwise).

Run:
    python examples/statistical_analysis_quickstart.py
"""

import os

from lazybridge import Agent

from lazytools.statistical_analysis import StatisticalAnalysisTools

MODEL = os.getenv("LB_MODEL", "claude-haiku-4-5")

agent = Agent(MODEL, tools=[StatisticalAnalysisTools()])

result = agent(
    "Compare SPY and TLT: what is their weekly volatility and correlation "
    "over 2023-2024? Then flag any weekly return outliers for SPY beyond "
    "2 standard deviations."
)
if result.ok:
    print(result.text())
else:
    # agent(...) never raises: a failed call comes back as an error
    # envelope, so a real script must check .ok before trusting .text().
    print(f"agent call failed: {result.error}")
