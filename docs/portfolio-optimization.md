# Portfolio optimization

LazyTools 0.10.1 exposes LazyPortfolio's hierarchical V2 engine through two
providers. Computation belongs to `lazyportfolio`; tools return compact
weights, diagnostics and provenance. They do not submit trades. The old
LazyFin `optimization` module, `OptimizationStore`, `ModelPortfolio` and
seven-method V1 tool menu are no longer the contract.

| Surface | Tools | Scope |
| --- | --- | --- |
| `PortfolioOptimizationTools` | `portfolio_optimizer_list_objectives`, `portfolio_optimizer_run`, `portfolio_optimizer_backtest` | One flat node built from instrument identifiers and constraints |
| `PortfolioTreeTools` | `portfolio_tree_validate`, `portfolio_tree_list`, `portfolio_tree_load` | Full node-tree configuration and shared saved models |
| Tree privileges | `portfolio_tree_save`, `portfolio_tree_delete`, `portfolio_tree_estimate`, `portfolio_tree_backtest` | Explicitly enabled persistence, deletion and computation |

These connectors require separately installed `lazyfin` and `lazyportfolio`;
there is no LazyTools `fin` pip extra. Use the ecosystem compatibility manifest
and `requirements/verified.txt` for a co-installable set. That manifest's older
LazyTools pin is an ecosystem CI baseline, distinct from the current 0.10.1
release described here. This docs refresh does not promote a new ecosystem set.

## Flat-node tools

```python
from lazytools.connectors.fin import PortfolioOptimizationTools

flat = PortfolioOptimizationTools()
print([tool.name for tool in flat.as_tools()])
```

`portfolio_optimizer_run` accepts comma-separated `instruments` (bare tickers
become `ticker:` identifiers), `objective`, `start`, `end`, `frequency`,
`min_weight`, `max_weight`, `cash_enabled`, `max_leverage`, `mean_estimator`,
`risk_aversion`, `risk_free_rate` and `benchmark_weights`.
Defaults are `objective="min_risk"`, `frequency="W"`, weight bounds 0/1,
no cash, leverage 1 and `mean_estimator="auto"`.

Call `portfolio_optimizer_list_objectives` to inspect the current vocabulary:
`min_risk`, `max_return`, `max_ratio`, `max_utility`, `hrp`. Mean estimator
choices are `auto`, `equilibrium`, `empirical`, `bayes_stein`, `james_stein`,
`bodnar_okhrin`. The wrapper constructs one flat V2 node with USD reference
currency; it does not expose every V2 engine setting.

`portfolio_optimizer_backtest` takes the same arguments plus `train_size=104`,
`rebalance_frequency="M"`, `transaction_cost_bps=0.0`. It returns fold count,
FINAL/B0 aggregate metrics, total transaction cost and bounded provenance;
no raw observations or period-by-period curve enters the model context.
There is no persistent flat optimization store, run-id lifecycle or
`portfolio_optimizer_list_methods` tool in this version.

Canonical daily simple returns load through `MarketDataHubOptimizationBackend`
or an injected `OptimizationDataBackend`. The estimation grid is resampled
from daily data. See [methodology and data contract](portfolio-optimization-methodology.md).

## Tree configurations

`PortfolioTreeTools` exposes complete V2 configuration: a root, nodes,
parent/child structure, per-node instruments, proxies, goals, constraints,
reference currency, data range and backtest settings. It shares saved JSON
models with LazyPortfolio's Tree Studio through `lazyportfolio` store functions
and `LAZYPORTFOLIO_TREE_DB` (or explicit `store_path`). The caller chooses a
saved `name` or inline `config`; inline config wins when both are supplied.

```python
from lazytools.connectors.fin.tree_tools import PortfolioTreeTools

tools = PortfolioTreeTools()  # validate/list/load
print([tool.name for tool in tools.as_tools()])
tools = PortfolioTreeTools(allow_compute=True, allow_persist=True, allow_delete=True)
print([tool.name for tool in tools.as_tools()])
```

There is no `allow_write` argument on this constructor. Privileges are separate:
`allow_compute` adds estimate/backtest, `allow_persist` adds save, `allow_delete`
adds delete. The MCP `fin` provider maps its write-enabled selection to all
three; programmatic callers can grant compute without persistence or deletion.

Validation calls `V2Model.from_config` and reports errors; save validates before
writing. Estimate/backtest derive flat/forward/forward_backward mode from the
configuration through `mode_from_config`, rather than letting the LLM invent a
mode. Per-call overrides are `estimation_frequency` and `train_size`; backtest
also allows `rebalance_frequency` and `transaction_cost_bps`.
The wrapper resolves tree currency and complete instrument requirements before
loading returns. It returns per-node local/terminal weights and compact audits
or aggregate backtest metrics, not raw input series.

## MCP and errors

The `fin` provider serves the flat tools and read-only tree tools; opt-in
`--allow-unsafe` adds tree computation/persistence/deletion. Missing optional
packages cause the provider to be skipped with diagnostics. Constructor import
errors explain the separately installed dependency. Bad configurations or
unavailable data are failures to resolve, not a signal to silently substitute
an obsolete method or fabricate returns.

Regression coverage lives in `test_portfolio_optimization_tools.py` and
`test_fin_tree_tools.py`, using injected backends. These tests establish the
API/data boundary; they are not investment-performance benchmarks.
