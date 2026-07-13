"""Minimal LazyBridge agent using the regression tools (OLS / Ridge / Lasso).

Same shape as ``statistical_analysis_quickstart.py``: one tool provider, one
natural-language question. This scenario also shows that the dependent
variable and regressors can span domains — a ticker return, a Fama-French
factor and a macro series in the same regression — because the agent only
ever sees instrument specs, never raw data.

Requires:
    - an LLM provider API key matching MODEL below (e.g. ANTHROPIC_API_KEY);
    - lazystats[regression] installed;
    - market-data-hub reachable (optional MARKET_DATA_DB env var).

Run:
    python examples/regression_quickstart.py
"""

import os

from lazybridge import Agent

from lazytools.statistical_analysis import StatisticalAnalysisTools

MODEL = os.getenv("LB_MODEL", "claude-haiku-4-5")

agent = Agent(MODEL, tools=[StatisticalAnalysisTools()])

result = agent(
    "Run an OLS regression of SPY weekly returns on TLT and the Fama-French "
    "market factor (FF5_daily/Mkt-RF) since 2020, with Newey-West standard "
    "errors. Then run a Lasso with the same regressors plus FEDFUNDS (as a "
    "level, not a return) and tell me which regressors survive."
)
if result.ok:
    print(result.text())
else:
    # agent(...) never raises: a failed call comes back as an error
    # envelope, so a real script must check .ok before trusting .text().
    print(f"agent call failed: {result.error}")
