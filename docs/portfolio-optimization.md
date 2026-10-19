# Portfolio optimization

`PortfolioOptimizationTools` is the LLM-facing, auditable surface over
LazyPortfolio's hierarchical V2 engine. It is for decision support: it returns
target weights and diagnostics, never submits trades. Five policy objectives
remain available: `min_risk`, `max_return`, `max_ratio`, `max_utility`, `hrp`.
The extraction from LazyFin changed the implementation and API, not the need
to document their mathematics and evaluation protocol.

The mathematical specification of the supported objectives is in
[Portfolio optimization methodology](portfolio-optimization-methodology.md).
This page documents the LLM tool boundary and operational usage.
The flat provider and [tree provider](#tree-configurations) use the same
engine; the tree surface additionally exposes hierarchy and per-node settings.

## Data boundary

The agent supplies comma-separated ticker symbols (`SPY,TLT`) or canonical
instruments (`ticker:SPY,ticker:TLT`), a date window, an `objective` and constraints.
Canonical daily **simple returns** are loaded privately from `market-data-hub`
by `MarketDataHubOptimizationBackend`, or an injected `OptimizationDataBackend`.
The tool's `frequency` parameter selects the fitting grid (`D`/`W`/`M`/`Q`),
not the data extraction grid. The flat wrapper declares USD reference currency;
the full tree config declares its own currency.

Tool results are deliberately limited to weights, diagnostics, aggregate
backtest metrics and bounded provenance. Prices, return rows and covariance
matrices never enter LLM context through these tools. Flat results have no
persistent run id: keep the request and result in the caller's audit record.

## Setup

The flat connector imports `lazyfin` for its sibling finance providers and
`lazyportfolio` for the V2 engine. The tree module imports only `lazyportfolio`
lazily. Both packages are separate installations; there is no LazyTools `fin`
extra and no current `lazyfin[optimizer]` setup for this engine. Consult
`ecosystem/compatibility.toml` and `requirements/verified.txt` for tested pins.
That file's older LazyTools pin is an ecosystem CI baseline, distinct from
LazyTools 0.10.4; this docs change does not update the ecosystem combination.

```python
from lazytools.connectors.fin import PortfolioOptimizationTools

flat = PortfolioOptimizationTools()
print([tool.name for tool in flat.as_tools()])
```

For an agent, pass this provider in `Agent(..., tools=[flat])`. The constructor
accepts only keyword `backend=`; it no longer accepts `OptimizationStore`.
The current menu is `portfolio_optimizer_list_objectives`,
`portfolio_optimizer_run`, `portfolio_optimizer_backtest`.

## Policies and constraints

Call `portfolio_optimizer_list_objectives` for the authoritative vocabulary.
Names are `objective=`, not the former `method=`:

| Objective | Implemented target | Important limitation |
| --- | --- | --- |
| `min_risk` | Minimize covariance-implied variance | Sensitive to covariance and bounds; does not need a mean forecast |
| `max_return` | Maximize expected excess return | Flat surface supplies bounds, not a benchmark-volatility cap |
| `max_ratio` | Maximize expected excess return / standard deviation | Fractional objective; no global-optimality promise |
| `max_utility` | Maximize excess mean minus `risk_aversion / 2` times variance | Uses variance, not standard deviation or CVaR |
| `hrp` | Hierarchical risk parity with Skfolio's default variance measure | Bounds supported; cash/leverage and non-`auto` mean estimators refused |

The flat run/backtest arguments are `instruments`, `objective`, `start`, `end`,
`frequency`, `min_weight`, `max_weight`, `cash_enabled`, `max_leverage`,
`mean_estimator`, `risk_aversion`, `risk_free_rate`, `benchmark_weights`.
Defaults: `min_risk`, weekly fitting, uniform minimum 0 and maximum 1,
cash disabled, leverage 1, mean estimator `auto`. Uniform per-asset minimum
weights are now supported through `min_weight`; they are not fixed to zero.

Mean estimators are `auto`, `equilibrium`, `empirical`, `bayes_stein`,
`james_stein`, `bodnar_okhrin`. The wrapper always constructs B0 weights,
using supplied canonical instrument weights or an equal-weight fallback.
The engine resolves `auto` from objective, reference availability and financing;
inspect `resolved_mean_estimator` in the run result. In the verified engine,
utility/financing `auto` uses Bayes-Stein rather than reconstructing the benchmark.
Explicit equilibrium requires a complete fully invested reference and rejects
active cash financing. Tool descriptions can be more conservative about asking
for explicit benchmark weights; supply them when that reference is meaningful.

The full tree surface exposes per-asset bounds, volatility targets/caps,
tracking error, covariance choice, borrowing spread and Black-Litterman views.
Those are tree `constraints`, not extra flat-tool keywords. HRP rejects unsupported
combinations; validation/solve errors must remain visible. No post-solve patch
should make a refused configuration look compliant.

### What changed from the seven-method interface

| Former name or API | Current location or status |
| --- | --- |
| `min_variance_shrinkage` | Use `min_risk`; default covariance still uses fixed shrinkage |
| `max_sharpe_shrinkage` | Use `max_ratio`; `risk_free_rate` is now exposed |
| `max_utility_shrinkage` | Use `max_utility`; quadratic utility has factor one half |
| `hrp_cvar` | Use `hrp` only for variance-based HRP; this is a changed risk measure |
| `min_cvar`, `risk_budget_cvar` | No equivalent V2 objective in this tool menu; CVaR/RiskBudgeting remain lower-level Skfolio capabilities |
| `max_return_benchmark_vol` | Tree `max_return` with an explicit benchmark-referenced volatility cap; flat `max_return` alone is not equivalent |
| `OptimizationStore`, `ModelPortfolio`, `benchmark_id`, run-id lifecycle | Removed from the flat wrapper; B0 uses `benchmark_weights`, saved tree configs use the LazyPortfolio store |
| `chart_filename`, `artifacts_dir` | Not flat optimizer arguments; use a separate report/chart pipeline |

The deterministic score-ranked `OptimizerTools` / `optimize_target_weights`
still lives in `lazyfin.kernel`. It is a separate optimizer and was unaffected
by the hierarchical-engine extraction.

## Benchmarks and backtests

Declare a static comparator with `benchmark_weights`, for example
`{"ticker:SPY": 0.7, "ticker:TLT": 0.3}`. There is no preliminary model-portfolio
creation or `benchmark_id`. The B0 comparator does not impose a risk budget
unless a tree configuration explicitly declares a benchmark risk constraint.

`portfolio_optimizer_backtest` fits the V2 estimator on the requested return
frequency and values the resulting holding periods on daily simple returns.
Its `train_size` (default 104) counts fitting observations;
`rebalance_frequency` (default `M`) independently controls renewed weights.
A fit ending at a rebalance endpoint applies to following daily observations,
not the training window. Annualized OOS metrics use daily factor 252, even
when fitting is weekly or monthly. See the methodology for compounding,
drifting weights and the cost convention.

The backtest takes all run arguments plus `train_size`, `rebalance_frequency`
and `transaction_cost_bps` (default 0). Pass costs deliberately: zero is a
configuration default, not an estimate of real execution cost.
The result contains `n_folds`, FINAL `metrics`, B0 `benchmark_metrics`,
`transaction_cost_paid` and bounded source/frequency provenance. It does not
persist specifications, weights at each fold or raw observations in a V1 store.

Here is a complete flat call; run it only with a configured data backend:

```python
import asyncio
import json
from lazytools.connectors.fin import PortfolioOptimizationTools

async def evaluate():
    flat = PortfolioOptimizationTools()
    menu = {tool.name: tool for tool in flat.as_tools()}
    vocabulary = json.loads(await menu["portfolio_optimizer_list_objectives"].run())
    assert "max_utility" in vocabulary["objectives"]
    result = await menu["portfolio_optimizer_backtest"].run(
        instruments="SPY,TLT", objective="min_risk",
        start="2018-01-01", end="2026-01-01", frequency="W",
        max_weight=0.7, train_size=104, rebalance_frequency="M",
        transaction_cost_bps=10.0,
        benchmark_weights={"ticker:SPY": 0.7, "ticker:TLT": 0.3},
    )
    print(json.loads(result))

asyncio.run(evaluate())
```

Retain the exact inputs and compact outputs for reproduction. A self-contained
HTML report can still use `ReportTools` / `ReportFiles` and `save_memo_html`,
but a chart needs a separately authorized source; the optimizer returns no
OOS curve or sandboxed `file:` chart reference. Do not infer a missing curve
from aggregate metrics.

## Live DeepSeek smoke test

`examples/run_portfolio_optimization_deepseek.py` remains the live agentic
smoke-test runner. It now imports `MarketDataHubOptimizationBackend` from
LazyPortfolio and the current flat provider. DeepSeek chooses a sequence of
DataHub, optimizer, report and Telegram tools for a SPY/GLD/TLT/BCI study against
a 70/30 SPY/TLT comparator. The task specifies a roughly three-year rolling
window, quarterly rebalancing and capped long-only weights.

The runner checks completion artifacts, not a predetermined chain or numerical
performance. It uses report `chart:` schemas for instrument price/return charts,
not the removed optimizer `chart_filename` interface. It reads `DEEPSEEK_API_KEY`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` and a configured market-data-hub database,
with its documented local key-file fallback. This sends a Telegram summary and
HTML attachment: it is an explicitly invoked live example, not part of docs
verification. The docs tests substitute engines/data and send no messages.

## Tree configurations

`PortfolioTreeTools` (`src/lazytools/connectors/fin/tree_tools.py`) exposes
the current full-tree contract alongside the flat tools above.

`PortfolioOptimizationTools` always wraps a single flat node. `PortfolioTreeTools`
exposes the full LazyPortfolio V2 node-tree: multiple nodes, each with its own
instruments, a `proxy` ticker (how its parent sees it), an `objective` and
`constraints`, composed into `flat` / `forward` / `forward_backward` estimates.
A tree config is one JSON object:

```json
{
  "root_id": "root",
  "currency": "USD",
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
`lazyportfolio.v2.store`, sharing one SQLite database via the
`LAZYPORTFOLIO_TREE_DB` env var (set it the same way for both
processes; every `portfolio_tree_list`/`_save` response reports the resolved
database path so a mismatch is visible rather than silent). A tree built via
`portfolio_tree_save` appears in Tree Studio's saved-model list immediately;
a tree built/edited in the GUI loads via `portfolio_tree_load`/`_list`.

```python
from lazytools.connectors.fin.tree_tools import PortfolioTreeTools

tools = PortfolioTreeTools()                  # validate / list / load
tools = PortfolioTreeTools(allow_compute=True, allow_persist=True, allow_delete=True)
print([tool.name for tool in tools.as_tools()])
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


### Tree privileges and errors

`allow_compute` adds estimate/backtest, `allow_persist` adds save, and
`allow_delete` adds delete. There is no constructor `allow_write` parameter.
Compute can be granted without persistence or deletion. The MCP `fin` provider
maps its write-enabled selection (`--allow-unsafe`) to all three privileges;
read-only tree tools are emitted otherwise. Flat estimation/backtests are already
in the provider menu: this flag is not a universal compute-cost gate.

`store_path` overrides `LAZYPORTFOLIO_TREE_DB`. Inline `config` wins over `name`.
Save validates before writing, and frequency/window/cost overrides affect only
that call. `portfolio_tree_validate` verifies structure without fetching data;
it cannot prove coverage, feasibility or forecast quality. Tree views are
typed per-node settings; a single-node tree is sufficient for views on a flat
universe. The flat `portfolio_optimizer_*` calls have no view argument.

Missing optional dependencies, missing data, invalid structure and infeasible
settings are errors to resolve. They are not reasons to invent returns or
silently fall back to a removed policy. Regression coverage in
`test_portfolio_optimization_tools.py` and `test_fin_tree_tools.py` verifies this
API/data boundary, not profitability.
