# Portfolio optimization methodology and data contract

This page describes the **LazyTools 0.10.1 wrapper boundary**. The numerical
implementation is LazyPortfolio's hierarchical V2 engine. Its complete
mathematical methodology belongs in that package's
[hierarchical V2 documentation](https://github.com/selvaz/LazyPortfolio/blob/main/docs/hierarchical-v2.md).
The removed LazyFin V1 seven-method optimizer is not the current implementation.

## Inputs, computation and evidence

The LLM supplies identifiers, goals and constraints. Code resolves the data,
computes weights and returns bounded results for the LLM to read. The
[tool guide](portfolio-optimization.md) names the actual arguments and tools.
Raw return observations are not accepted as tool arguments and are not exposed
in tool results.

`PortfolioOptimizationTools` wraps one flat V2 node. It requests canonical
**daily simple returns** from an `OptimizationDataBackend`, with the default
backend reading market-data-hub. It then resamples to the requested estimation
frequency (`W` by default). Reference currency is USD on the flat surface;
the full tree configuration supplies its own reference currency.

| Goal | Current objective identifier |
| --- | --- |
| Minimize risk | `min_risk` |
| Maximize expected return | `max_return` |
| Optimize return/risk ratio | `max_ratio` |
| Utility tradeoff | `max_utility` |
| Hierarchical risk allocation | `hrp` |

These identifiers select implemented engine goals; they are not a measured
ranking of strategies. `portfolio_optimizer_list_objectives` is the
machine-readable vocabulary. Mean-estimator inputs include `auto`,
`equilibrium`, `empirical`, `bayes_stein`, `james_stein`, `bodnar_okhrin`.
Risk aversion, risk-free rate, benchmark weights, leverage, cash and weight
bounds are explicit parameters rather than narrative instructions.

## Estimation and backtest boundary

Flat estimation builds a V2 model, loads returns internally and calls
`HierarchicalV2Estimator`. The wrapper returns target weights and diagnostics
with source, row count and frequency provenance. It does not persist a
`ModelPortfolio` or expose a V1 `OptimizationStore`.

Flat backtests call `HierarchicalV2Backtester` with `mode="flat"`, a training
window, estimation/rebalance frequencies and transaction cost basis points.
Defaults are 104 training periods, weekly estimation, monthly rebalance and
zero transaction cost. The response contains FINAL/B0 aggregate metrics, fold
count, total transaction cost and data provenance. It omits raw observations
and time-series curves. The caller must deliberately select realistic inputs;
a default zero cost is configuration, not an empirical trading-cost estimate.

Full-tree estimate/backtest use `V2Model.from_config` and `mode_from_config`.
Flat, forward and forward_backward behavior comes from the model configuration,
including proxies, hierarchy and backtest settings. The tool wrapper does not
create a different mathematical interpretation of a Tree Studio model.
Per-node local/terminal weights and solver audits explain the resulting tree.

## Reproduction and limits

Retain the exact model/config, instrument universe, date range, currency,
data source and version, estimator settings, frequencies, training window and
cost assumptions with any output. For saved trees, the JSON is shared with
Tree Studio through the LazyPortfolio store; computation and persistence are
independent privileges in `PortfolioTreeTools`.

LazyTools' fake-backend tests verify that the wrappers request the intended
canonical data and expose compact outputs. They do not establish profitability,
forecast quality, robustness of an allocation or a comparative benchmark.
Interpret numerical results with their recorded inputs and diagnostics;
provider and dataset errors must remain visible rather than becoming invented
observations. No trading or publication gate is supplied by these tools.
