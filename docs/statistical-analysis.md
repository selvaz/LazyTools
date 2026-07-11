# Statistical analysis

`lazytools.statistical_analysis` gives a LazyBridge agent three read-only tools
for analysing historical **log returns**:

- `statistical_return_volatility` — sample standard deviation and annualised
  volatility per instrument;
- `statistical_return_correlation` — pairwise Pearson-correlation matrix and
  the number of shared observations for every pair;
- `statistical_return_outliers` — dates where the absolute z-score of a return
  is at least a threshold (default `2`).

The tools have one data path: they read the full return matrix through
`market_data_hub.extract.extract_returns`. They do not call the Stooq connector,
accept user-provided price vectors, download data, or write to DuckDB. This is
important: the generic `datahub_get_returns` agent tool limits its JSON response
to protect model context; using that truncated response for a statistic would
silently make a long-window calculation wrong.

The complete matrix remains in the tool process. Tool results contain only
aggregate metrics and, for the outlier tool, the flagged rows; metadata is
allow-listed so a raw-return payload cannot leak into an LLM context through a
future backend change.

## Install and add to an agent

`market-data-hub` is the central private data dependency and must be installed
from its approved Git source:

```bash
pip install lazytoolkit
pip install 'market-data-hub @ git+https://github.com/selvaz/market-data-hub.git'
```

For a real end-to-end DeepSeek tool-call smoke test from Spyder, run
[`examples/run_statistical_analysis_deepseek.py`](../examples/run_statistical_analysis_deepseek.py).
It defaults to the low-cost `deepseek-v4-flash` model, reads the API key from
`DEEPSEEK_API_KEY` or the workspace `deepseek.env`, and asserts that the agent
actually invoked each of the three statistical tools.

```python
from lazybridge import Agent
from lazytools.connectors.datahub import DataHubTools
from lazytools.statistical_analysis import StatisticalAnalysisTools

agent = Agent(
    "claude-opus-4-8",
    tools=[DataHubTools(), StatisticalAnalysisTools()],
)
```

Use canonical `lazydatacore` identities such as `ticker:SPY,ticker:TLT`.
Bare symbols remain a convenience input and are canonicalised by the hub. The
current return-statistics surface intentionally supports `ticker:` instruments:
these map to the shared `prices_daily` warehouse and use the hub's
`extract_returns` semantics. All outputs are `lazydatacore.AnalysisResult` JSON
with source/provenance set to `market-data-hub`.

## Parameters and interpretation

Every tool receives `instruments`, optional inclusive `start`/`end` dates and
`frequency` (`D`, `W`, `M` or `Q`; default `D`). Returns are computed by the hub
after resampling levels, so weekly/monthly returns compound correctly.

Volatility reports both the standard deviation at the requested return frequency
and annualised volatility using `sqrt(252)`, `sqrt(52)`, `sqrt(12)` or `sqrt(4)`.
Correlation is pairwise: each coefficient only uses dates for which both
instruments have a return, and its accompanying `pairwise_observations` matrix
makes that sample size explicit.

Outlier z-scores are cross-sectional in time **within each instrument's selected
period**, not rolling: `z = (return - period_mean) / sample_standard_deviation`.
An observation is flagged when `abs(z) >= threshold`; `threshold=2.0` is the
default. The result gives the date, canonical instrument, log return, signed
z-score and direction. `max_results` defaults to 1000 and, if reached, the
result preserves the true `total_outliers` and sets `truncated: true`.
