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
>
> **`PortfolioTreeTools` added (this release):** the flat-node limitation
> this page otherwise describes throughout no longer applies to the tree
> surface — see [Tree configurations](#tree-configurations) below for the
> current, accurate contract. Everything else on this page is still the
> stale V1 content described above.

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

## Tree configurations

Unlike everything above, this section describes the **current** contract —
`PortfolioTreeTools` (`src/lazytools/connectors/fin/tree_tools.py`), added
this release.

`PortfolioOptimizationTools` always wraps a single flat node. `PortfolioTreeTools`
exposes the full LazyPortfolio V2 node-tree: multiple nodes, each with its own
instruments, a `proxy` ticker (how its parent sees it), an `objective` and
`constraints`, composed into `flat` / `forward` / `forward_backward` estimates.
A tree config is one JSON object:

```json
{
  "root_id": "root",
  "nodes": [
    {"id": "root", "name": "Global", "children": ["equity", "bonds"], "instruments": [], "proxy": "", "goal": {"objective": "min_risk"}, "constraints": {}},
    {"id": "equity", "name": "Equity", "children": [], "instruments": ["SPY", "VGK", "EWJ"], "proxy": "ACWI", "goal": {"objective": "min_risk"}, "constraints": {}},
    {"id": "bonds", "name": "Bonds", "children": [], "instruments": ["SHY", "IEF", "TLT"], "proxy": "AGG", "goal": {"objective": "min_risk"}, "constraints": {}}
  ],
  "data": {"start": "2018-01-01", "end": ""},
  "backtest": {
    "train_size": 104, "rebalance_frequency": "M", "estimation_frequency": "W",
    "transaction_cost_bps": 5, "forward_enabled": true, "hierarchy_mode": "proxy",
    "benchmark": {"name": "B0", "weights": {"ACWI": 0.7, "AGG": 0.3}}
  }
}
```

`children` is a flat list of *other node ids in the same `nodes` list*, not
nested objects — every node but the root needs exactly one parent and (if it
has one) a `proxy` ticker; `constraints` may be `{}` and LazyPortfolio fills in
its ~25 optional fields. `backtest.forward_enabled`/`hierarchy_mode` select the
estimate mode (`forward_enabled=false` → `flat`; `hierarchy_mode="proxy"` →
`forward`; `"synthetic_reconstructed"` → `forward_backward`) via
`lazyportfolio.v2.mode.mode_from_config` — never chosen ad hoc by a tool
argument, so a saved tree always estimates the same way wherever it runs.

**This is the same format [Tree Studio](https://github.com/selvaz/LazyPortfolio)
(LazyPortfolio's local visual editor) edits and saves** — both go through
`lazyportfolio.v2.store`, sharing one directory via the
`LAZYPORTFOLIO_TREE_MODELS_DIR` env var (set it the same way for both
processes; every `portfolio_tree_list`/`_save` response reports the resolved
directory so a mismatch is visible rather than silent). A tree built via
`portfolio_tree_save` appears in Tree Studio's saved-model list immediately;
a tree built/edited in the GUI loads via `portfolio_tree_load`/`_list`.

```python
from lazytools.connectors.fin.tree_tools import PortfolioTreeTools

tools = PortfolioTreeTools()                  # validate / list / load
tools = PortfolioTreeTools(allow_write=True)  # + save / delete / estimate / backtest
```

- `portfolio_tree_validate(config)` — never touches market data or the store;
  returns the flattened instrument universe and per-sleeve breakdown when
  valid, or a clear structural error (never raises) when not.
- `portfolio_tree_list()` / `portfolio_tree_load(name)` — read the shared store.
- `portfolio_tree_save(name, config)` — validates (same check as `_validate`)
  before writing; refuses to write anything on a validation failure.
- `portfolio_tree_delete(name)`.
- `portfolio_tree_estimate(config=None, name="", estimation_frequency="", train_size=0)` /
  `portfolio_tree_backtest(config=None, name="", estimation_frequency="", train_size=0, rebalance_frequency="", transaction_cost_bps=None)` —
  pass either an inline `config` or a saved `name` (`config` wins if both are
  given); the optional frequency/window/cost arguments override the tree's own
  `backtest` values for that call only, without mutating the saved file.
  Responses carry `terminal_weights`, per-node `local_weights`/
  `terminal_weights`/`audit`, and aggregate backtest metrics/provenance —
  never `synthetic_returns` or per-period curves.
