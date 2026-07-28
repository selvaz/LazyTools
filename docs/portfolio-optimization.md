# Portfolio optimization

> **Stale content below (2026-07-28):** `PortfolioOptimizationTools` was ported
> to LazyPortfolio's hierarchical (V2) engine — `lazyfin.optimization`,
> `OptimizationStore`, `ModelPortfolio`, `SkfolioOptimizer` and the 7 named
> methods described below no longer exist. The current tool surface is
> `portfolio_optimizer_list_objectives` / `_run` / `_backtest` over
> `lazyportfolio`'s objectives (`min_risk`, `max_return`, `max_ratio`,
> `max_utility`, `hrp`) with no persisted store. See
> `src/lazytools/connectors/fin/tools.py`'s `PortfolioOptimizationTools`
> docstring for the current contract; this page and
> `portfolio-optimization-methodology.md` need a full rewrite to match.

`PortfolioOptimizationTools` is the LLM-facing, auditable surface over the
Skfolio-backed LazyFin optimizer. It is for decision support: it returns target
weights and diagnostics, never submits trades.

The full mathematical and scientific specification of the supported policies is
in [Portfolio optimization methodology](portfolio-optimization-methodology.md).
This page documents the LLM tool boundary and operational usage.

## Data boundary

The agent supplies comma-separated ticker symbols (`SPY,TLT`) or canonical
instruments (`ticker:SPY,ticker:TLT`), a date window, a method and constraints.
Canonical daily **simple returns** are loaded privately from `market-data-hub`
by LazyFin. The tool's `frequency` parameter selects the fitting grid
(`D`/`W`/`M`/`Q`), not the data extraction grid. Tool results are deliberately
limited to weights, risk metrics, costs, tracking error, bounded provenance and
persistent ids; prices, return rows and covariance matrices never enter LLM
context.

## Setup

Install LazyFin's optional quantitative dependency and configure a durable
audit database:

```bash
pip install "lazyfin[optimizer]"
```

```python
from lazybridge import Agent
from lazyfin.optimization import OptimizationStore
from lazytools.connectors.fin import PortfolioOptimizationTools

optimizer_tools = PortfolioOptimizationTools(
    OptimizationStore("lazyfin_optimizer.sqlite")
)
agent = Agent("claude-opus-4-8", tools=[optimizer_tools])
```

## Policies and constraints

- `min_variance_shrinkage`, `min_cvar`, `max_sharpe_shrinkage` and
  `max_utility_shrinkage` use Skfolio `MeanRisk`; they support bounds, groups,
  linear constraints, transaction costs, hard turnover and tracking-error
  limits. `max_utility_shrinkage` also accepts a positive `risk_aversion`.
- `risk_budget_cvar` uses `RiskBudgeting`; it supports bounds, groups, linear
  constraints and transaction costs.
- `hrp_cvar` uses `HierarchicalRiskParity`; it supports bounds and transaction
  costs. Its CVaR confidence is Skfolio's fixed 95%.
- `max_return_benchmark_vol` uses `MeanRisk` to maximise expected return under
  a volatility cap dynamically measured from the declared `benchmark_id` in
  each fitting window. It needs a benchmark; it does not use future or realised
  OOS benchmark volatility as its target.

The provider rejects an unsupported method/constraint combination rather than
altering the generated weights after the solve.

The V1 LLM surface exposes a uniform `max_weight`; individual `min_weights`
remain zero by design (long-only is the natural default). Per-asset minimum
weights are available to programmatic callers through `OptimizationSpec` and
will be surfaced to the agent only with a dedicated portfolio-policy interface.

## Benchmarks and backtests

Create a versioned model portfolio, for example a 70/30 ACWI/aggregate-bond
allocation, before referencing it by `benchmark_id`. It is always available as
a performance comparator; `MeanRisk` policies can also enforce an optional
tracking-error budget against it.

`portfolio_optimizer_backtest` fits Skfolio on the requested return frequency
and values the resulting holding periods on daily simple returns. Its
`train_size` is measured in fitting-return observations; its
`rebalance_frequency` (`D`/`W`/`M`/`Q`) independently controls when weights are
renewed. A fit ending at a rebalance endpoint is applied beginning with the
following daily observation, so there is no look-ahead or artificial extra
execution delay. OOS NAV, costs, drawdown, CAGR and annualised realised metrics
therefore use daily data and factor 252, even when fitting is weekly or monthly.

Tool results distinguish `annualized_mean` from geometric `cagr`; the latter
comes from the compounded daily OOS wealth curve. The store records benchmark
versions, specifications, weights
at each out-of-sample rebalance, aggregate metrics and data provenance—but not
historical observations.

Pass a `chart_filename` only when the provider has been built with an
`artifacts_dir`. It writes a PNG of strategy versus benchmark cumulative
out-of-sample return and returns only a sandboxed `file:` artifact reference;
no observations are returned to the LLM. Pair that reference with
`ReportTools(..., files=ReportFiles(...))` and `save_memo_html` for a
self-contained HTML report with the chart embedded.

## Live DeepSeek smoke test

`examples/run_portfolio_optimization_deepseek.py` performs a live agentic
smoke test: DeepSeek receives only the DataHub, optimizer, report and Telegram
providers plus a portfolio-research objective. It decides the tool sequence
and the appropriate policy comparison for a SPY/GLD/TLT/BCI universe against a
70/30 SPY/TLT benchmark, using a three-year rolling window and quarterly
rebalancing. It saves a self-contained HTML report with OOS chart(s) and uses
the existing Telegram tool provider to send both outputs. The runner verifies
only completion artifacts (backtest, persisted HTML, message and attachment),
not a prescribed chain of tool calls. It reads `DEEPSEEK_API_KEY`, `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID` from the environment (with the established workspace
`deepseek.env` fallback for local development). Telegram receives only a short
status message; the detailed Markdown report is attached as a document.
