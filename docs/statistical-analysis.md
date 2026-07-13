# Statistical analysis

`lazytools.statistical_analysis` gives a LazyBridge agent read-only tools for
analysing historical hub series (by default as **log returns**):

- `statistical_return_volatility` — sample standard deviation and (for return
  series) annualised volatility per instrument;
- `statistical_return_correlation` — pairwise Pearson-correlation matrix and
  the number of shared observations for every pair;
- `statistical_return_outliers` — dates where the absolute z-score of a value
  is at least a threshold (default `2`);
- `statistical_regression_ols` — univariate/multivariate OLS via statsmodels
  (robust `HC0`–`HC3` or Newey-West `HAC` standard errors, confidence
  intervals, R², F, AIC/BIC, Durbin-Watson, residual diagnostics);
- `statistical_regression_ridge` / `statistical_regression_lasso` — regularised
  fits via scikit-learn; `alpha` empty selects it by cross-validation, Lasso
  also reports the surviving (non-zero) regressors. At most 10 regressors.
  These need `lazystats[regression]` installed.

### Series specs and transforms

Every instrument argument accepts `'<id>[|<transform>]'` specs, comma-separated:
`ticker:SPY, ticker:AAPL|level, macro:FEDFUNDS|diff, factor:FF5_daily/Mkt-RF`.
Transforms are the hub's own (`level`, `log_return`, `pct_change`, `diff`);
when omitted the default is per-domain: tickers `log_return`, Fama-French
factors `level` (they are stored as decimal returns — weekly/monthly requests
compound them correctly instead of taking the last print) and macro series
`diff` (log/pct are undefined on series that cross zero). Mixed-domain panels
are outer-joined on dates; pairwise statistics and regression alignment use
the shared dates downstream. A regression dependent variable can therefore be
any hub series — a ticker return, a price level (`ticker:AAPL|level`) or a
macro series.

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
G="git+https://github.com/selvaz/LazyTools.git"
pip install "lazytoolkit @ $G"
pip install "market-data-hub @ git+https://github.com/selvaz/market-data-hub.git"
```

### Quickstart scripts

The minimal, zero-boilerplate shape — one tool provider, one natural-language
question, no bootstrap scaffolding:

- `examples/statistical_analysis_quickstart.py` — volatility, correlation and
  outliers over a ticker pair.
- `examples/regression_quickstart.py` — OLS/Ridge/Lasso across domains (a
  ticker return regressed on another ticker, a Fama-French factor and a
  macro level in the same call).

Both read `LB_MODEL` (default `claude-haiku-4-5`) and need a matching
provider API key (e.g. `ANTHROPIC_API_KEY`) plus market-data-hub reachable
(`MARKET_DATA_DB` optional). Run directly:

```bash
python examples/statistical_analysis_quickstart.py
python examples/regression_quickstart.py
```

`agent(...)` never raises — a failed call comes back as an error envelope, so
both scripts check `result.ok` before trusting `result.text()` and print
`result.error` otherwise (missing key, missing package, etc.).

For a real end-to-end DeepSeek tool-call smoke test from Spyder that also
*asserts* the tools were actually called (rather than just printing the
answer), run `examples/run_statistical_analysis_deepseek.py` from the
repository checkout. It defaults to the low-cost `deepseek-v4-flash` model,
reads the API key from `DEEPSEEK_API_KEY` or the workspace `deepseek.env`,
and asserts that the agent actually invoked each of the three statistical
tools.

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
statistics surface supports `ticker:`, `factor:` and `macro:` instruments
(`crypto:` and `macro_panel:` are rejected with a clear error). All outputs
are `lazydatacore.AnalysisResult` JSON with source/provenance set to
`market-data-hub`.

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
z-score and direction. `max_results` defaults to 100 and has a hard cap of 250;
the result preserves the true `total_outliers` and sets `truncated: true` when
the selected output limit is reached.
