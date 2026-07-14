# Portfolio optimization methodology

This note specifies the mathematics and empirical protocol behind the policies
exposed by `PortfolioOptimizationTools`. It describes the implementation, not a
recommendation to invest. The optimizer is an auditable decision-support
component: a model chooses a method and constraints, while returns remain in
`market-data-hub` and numerical fitting remains in LazyFin/Skfolio.

## Scope and notation

Let \(r_t\in\mathbb{R}^{n}\) be the vector of asset returns at date \(t\),
\(t=1,\ldots,T\), and let \(w\in\mathbb{R}^{n}\) be portfolio weights. The
portfolio return is

\[
r_{p,t}=w^\top r_t - c^\top\lvert w-w^-\rvert,
\]

where \(w^-\) is the pre-trade allocation and \(c\) is the vector of
per-unit-turnover transaction-cost assumptions. A cost in bps is converted to a
decimal, e.g. 10 bps is \(0.001\). This is a model assumption, not a record of
broker fills, spreads, market impact or taxes.

The default feasible set for the LLM surface is long-only and fully invested:

\[
\mathcal W=\{w: \sum_i w_i=1-cash,\; 0\leq w_i\leq \bar w_i\}.
\]

`min_cash_weight` changes the investment budget to \(1-cash\). Group and
linear constraints can further restrict \(\mathcal W\). For a benchmark
\(w_b\), the optional active-risk constraint is

\[
\operatorname{TE}(w,w_b)=
\sqrt{(w-w_b)^\top\Sigma(w-w_b)}\leq\tau_{TE},
\]

and the optional turnover constraint is
\(\lVert w-w^-\rVert_1\leq\tau_{TO}\).

## Statistical inputs

### Returns and moments

The data backend resamples price history to the requested frequency and creates
returns internally. The LLM never receives the return matrix, covariance matrix
or individual observations. The empirical prior estimates expected returns
\(\hat\mu\) and covariance from the fitting window.

For the `MeanRisk` policies, LazyFin uses Skfolio's `EmpiricalPrior` with
`ShrunkCovariance(shrinkage=0.1)`. With sample covariance \(S\) and
\(\bar s=\operatorname{tr}(S)/n\), the covariance used by the optimizer is

\[
\hat\Sigma=(1-0.1)S+0.1\,\bar s I.
\]

The shrinkage improves conditioning when the asset count is material relative
to the observation count. It reduces estimation variance, at the cost of bias
toward a spherical covariance target. It is a fixed 10% shrinkage, not a
data-adaptive Ledoit--Wolf estimate.

### Tail risk

For loss \(L_t=-r_{p,t}\), CVaR at confidence \(\beta\) is the mean loss in
the tail beyond VaR. Its sample convex representation is

\[
\operatorname{CVaR}_\beta(w)=
\min_a\left[a+\frac{1}{(1-\beta)T}
\sum_{t=1}^T\max(L_t-a,0)\right].
\]

The exposed default is \(\beta=0.95\). CVaR is scenario-based and captures
loss severity beyond a quantile, but its tail estimate is necessarily noisy in
short rolling windows.

## Supported policies

| Tool method | Skfolio estimator | Mathematical target | Best use | Principal sensitivity |
| --- | --- | --- | --- | --- |
| `min_variance_shrinkage` | `MeanRisk` | minimize variance | stable, diversified universe | covariance estimate and concentration at bounds |
| `min_cvar` | `MeanRisk` | minimize 95% CVaR | drawdown/tail-risk focus | number and representativeness of tail observations |
| `max_sharpe_shrinkage` | `MeanRisk` | maximize return/risk ratio | strong expected-return conviction | expected returns; can be unstable |
| `max_utility_shrinkage` | `MeanRisk` | maximize mean minus risk penalty | explicit risk-aversion trade-off | calibration of risk aversion |
| `risk_budget_cvar` | `RiskBudgeting` | equalize CVaR risk contributions | diversified tail-risk allocation | nonlinear risk attribution and tail sample |
| `hrp_cvar` | `HierarchicalRiskParity` | cluster then recursively allocate tail risk | correlated/ill-conditioned assets | clustering distance and tree stability |

### Minimum variance with shrinkage

`min_variance_shrinkage` solves, subject to the active constraints,

\[
\min_{w\in\mathcal W}\quad w^\top\hat\Sigma w
+ c^\top\lvert w-w^-\rvert.
\]

It does not require a return forecast and is therefore often more stable than
mean-variance portfolios. It can still become concentrated in assets with low
estimated variance or strong correlations. The current implementation uses
variance, rather than standard deviation, as the optimized risk measure.

### Minimum CVaR

`min_cvar` replaces variance with the empirical CVaR expression above:

\[
\min_{w\in\mathcal W}\quad
\operatorname{CVaR}_{0.95}(w)+c^\top\lvert w-w^-\rvert.
\]

It is appropriate when avoiding severe losses matters more than symmetric
dispersion. It is not a guarantee against future losses: a three-year weekly
window contains only about eight observations in the worst 5% tail.

### Maximum Sharpe ratio with shrinkage

`max_sharpe_shrinkage` solves the constrained fractional program

\[
\max_{w\in\mathcal W}\quad
\frac{w^\top\hat\mu-r_f}{\sqrt{w^\top\hat\Sigma w}}.
\]

The engine uses `risk_free_rate=0` unless a future API exposes it. Constraints,
costs and covariance shrinkage help, but cannot remove the well-known
instability caused by noisy expected-return estimates. This policy should be
compared out of sample rather than selected from a single in-sample ratio.

### Maximum mean--risk utility

`max_utility_shrinkage` solves

\[
\max_{w\in\mathcal W}\quad
w^\top\hat\mu-\lambda\sqrt{w^\top\hat\Sigma w}
-c^\top\lvert w-w^-\rvert,
\]

where \(\lambda>0\) is `risk_aversion`. In the current policy it uses
variance as the selected risk measure; Skfolio applies the same
mean-minus-\(\lambda\) times risk form. Larger \(\lambda\) favors lower-risk
allocations. Its numeric scale depends on return frequency, so \(\lambda\)
must be calibrated consistently with the data frequency.

### CVaR risk budgeting

For a positively homogeneous risk measure \(\rho(w)\), marginal risk
contribution is \(\partial\rho/\partial w_i\), and component risk
contribution is

\[
RC_i=w_i\frac{\partial\rho(w)}{\partial w_i}.
\]

`risk_budget_cvar` uses Skfolio `RiskBudgeting` with CVaR and its default equal
risk budget. Conceptually it searches for weights whose component CVaR
contributions are equal (or match a specified budget in lower-level Skfolio).
LazyFin V1 does not expose custom risk-budget vectors to the LLM.

This differs from equal capital weights: an asset with higher tail risk receives
less capital to contribute comparable tail risk. The problem is nonlinear and
depends on scenario tails, so bounds and diagnostics matter.

### Hierarchical risk parity with CVaR

`hrp_cvar` applies Skfolio hierarchical risk parity. Conceptually it:

1. converts inter-asset dependence into a distance and builds a hierarchical
   clustering tree;
2. quasi-diagonalizes the assets so related assets are adjacent;
3. recursively splits the tree and assigns more capital to the lower-risk
   branch, using CVaR as the risk measure.

HRP avoids direct inversion of a covariance matrix and is often robust when
correlations are noisy. It is not a convex `MeanRisk` program and, in this V1
surface, does not accept group or arbitrary linear constraints. Skfolio fixes
the CVaR confidence for this exposed policy at 95%.

## Constraint support and costs

`MeanRisk` policies support bounds, group constraints, linear constraints,
transaction costs, a benchmark tracking-error budget and a turnover cap.
`RiskBudgeting` supports bounds, groups, linear constraints and transaction
costs. `HierarchicalRiskParity` supports only bounds and transaction costs in
this surface. Unsupported combinations are rejected before solving; weights are
never post-processed to appear compliant.

The live research task should state transaction costs explicitly, for example
`transaction_cost_bps=10.0`. A 10-bps assumption means 10 bps applied to each
unit of modelled turnover; it is not “10 bps per year”. Reported performance is
only comparable when the same cost convention, rebalancing rule and universe
are used for every strategy and benchmark.

## Walk-forward evaluation

The backtest is Skfolio `WalkForward` plus `cross_val_predict`. For rolling
window length \(L\), out-of-sample block length \(H\), and purge \(P\), fold
\(k\) fits on returns available before time \(t_k\), estimates \(w_k\), then
evaluates on the following out-of-sample block. No return row is emitted to the
LLM.

For the current weekly/monthly convention:

\[
L=156\text{ weeks},\qquad H=4\text{ weeks},\qquad P=0.
\]

Thus the weights estimated at a rebalance date apply to the immediately
following weekly return and are held for four weeks. `P=0` is not a look-ahead
violation: it means there is no artificial extra delay between the end of the
information set and the first out-of-sample observation. A nonzero purge is a
separate anti-leakage protocol choice.

The persistent store saves only the specification, versions, OOS weights,
aggregate metrics, reason codes and bounded data provenance. It does not save
the raw historical return series.

## Metric interpretation and current caveat

`annualized_return` currently maps to Skfolio's `annualized_mean`; it is an
annualized arithmetic mean, **not CAGR**. CAGR requires compounded portfolio
returns, \((\prod_t(1+r_{p,t}))^{A/T}-1\), and must not be substituted by the
arithmetic annualized mean in a report.

Also, Skfolio `Portfolio` defaults to `annualized_factor=252`. The current
adapter does not yet set a frequency-specific factor. Therefore, for weekly
backtests, annualized return, volatility and annualized Sharpe should not be
treated as correctly annualized until the adapter passes \(A=52\) (and 12 for
monthly, 252 for daily). Raw period Sharpe and maximum drawdown remain useful
within a fixed-frequency comparison, but labels must make the frequency clear.

This caveat applies to the live reports already generated: their ranking within
the same weekly protocol can be informative, but any annualized labels or
claims expressed as CAGR need frequency-aware recomputation before investment
use.

## Scientific use and model risk

- Compare a small, predeclared policy set under the same walk-forward protocol;
  do not pick the winner after repeatedly changing universe, dates and
  constraints without accounting for selection bias.
- Treat max-Sharpe and utility results as especially sensitive to mean estimates.
  Prefer stability of weights, turnover, drawdown and performance across folds
  over a single high ratio.
- Include plausible transaction costs, a static benchmark and an equal-weight
  comparator. A strategy that wins only before costs is not a robust result.
- Test multiple regimes and stress periods. Regime references are represented
  in the contracts for the next extension, but regime-conditioned optimization
  is not yet exposed as a V1 LLM tool.
- A successful numerical solve is not a trade approval. Execution, liquidity,
  taxes, leverage and mandate checks remain separate controls.

## References

- Markowitz, H. (1952), *Portfolio Selection*.
- Rockafellar, R. T. and Uryasev, S. (2000), *Optimization of Conditional Value-at-Risk*.
- Ledoit, O. and Wolf, M. (2004), *A Well-Conditioned Estimator for Large-Dimensional Covariance Matrices*.
- Maillard, S., Roncalli, T. and Teïletche, J. (2010), *The Properties of Equally Weighted Risk Contribution Portfolios*.
- López de Prado, M. (2016), *Building Diversified Portfolios that Outperform Out of Sample*.
- [Skfolio documentation](https://skfolio.org/) for estimator-level implementation details and solver behavior.
