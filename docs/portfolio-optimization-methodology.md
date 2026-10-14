# Portfolio optimization methodology

This note specifies the mathematics and empirical protocol behind the five
objectives exposed by `PortfolioOptimizationTools`. Returns remain inside
the data backend and numerical fitting remains in LazyPortfolio V2. The LLM
chooses identifiers, objectives and constraints and receives compact results.
It does not compute weights from narrated observations or submit trades.

The policy discussion below is restored from the earlier guide and corrected
against `src/lazytools/connectors/fin/tools.py` and LazyPortfolio's
`v2/solver.py`, `moments.py`, `backtest.py`, `contracts.py` and `calendar.py`.
The engine check includes revision `3490f565` from `requirements/verified.txt`
and the current checkout. The wrapper has a flat node and a richer
[tree-tools surface](portfolio-optimization.md#tree-configurations).
Full hierarchy/view/financing detail lives in
[LazyPortfolio's V2 methodology](https://github.com/selvaz/LazyPortfolio/blob/main/docs/hierarchical-v2.md).

## Scope and notation

Let \(r_t\in\mathbb{R}^{n}\) be simple asset returns and let
\(w\in\mathbb{R}^{n}\) be portfolio weights. Before a rebalance cost,
the portfolio return is \(r_{p,t}=w^\top r_t\). Define periodic expected
returns \(\hat\mu\), covariance \(\hat\Sigma\), and periodic excess mean
\(\hat\mu_e=\hat\mu-r_f^{(p)}\mathbf1\), with annual risk-free input
converted by \(r_f^{(p)}=r_f/A_f\).

The default feasible set for the flat LLM surface is long-only and fully invested:

\[
\mathcal W=\{w: \sum_i w_i=1,\quad l_i\leq w_i\leq u_i\}.
\]

Uniform \(l_i\), \(u_i\) come from `min_weight`, `max_weight`; defaults
are 0 and 1. The tree surface supports per-asset bounds. With cash/financing,
risky weights and a local financing weight sum to one; the borrowing limit
implies \(1-\text{max_leverage}\leq w_{cash}\leq1\). This is not the
removed V1 `min_cash_weight` interface. HRP refuses financing.

A transaction cost in bps is converted to a decimal, e.g. 10 bps is
\(0.001\). The backtest charges that rate on modeled absolute weight changes
at each rebalance. Cost is an OOS ledger deduction, not an additional
transaction-cost penalty in the flat estimator's objective. It is a model
assumption, not a record of broker fills, spreads, market impact or taxes.

Full tree constraints can reference benchmark/proxy volatility or tracking
error. Feasibility uses the training sample's realized portfolio standard
deviation; objective risk uses shrunk covariance. Thus, for annual cap \(v\),

\[
\sqrt{A_f}\,\operatorname{std}_{sample}(Rw)\leq v,
\qquad
\operatorname{TE}=\sqrt{A_f}\,\operatorname{std}_{sample}(Rw-r_b).
\]

This corrects the former claim that every volatility/TE constraint used the
same shrunk quadratic form as the objective. These constraints are not extra
keywords on the flat wrapper. Exact volatility targets are equality
constraints; caps are upper bounds, and must not be confused with each other.

## Statistical inputs

### Returns and moments

The data backend creates canonical daily simple returns from adjusted prices.
For a requested fitting frequency, it compounds those daily returns rather than
passing log returns to the optimizer:

\[
r_k^{(f)}=\prod_{t\in k}(1+r_t^{(D)})-1.
\]

The LLM never receives the return matrix, covariance matrix or individual
observations. The empirical prior estimates expected returns \(\hat\mu\) and
covariance from the fitting window.

For the flat objectives, V2 uses Skfolio moment estimators with default
`covariance_estimator="shrunk_fixed"`, implemented by `ShrunkCovariance()`
(default shrinkage 0.1 in the verified Skfolio 0.20.1). With sample covariance \(S\) and
\(\bar s=\operatorname{tr}(S)/n\), the covariance used by the optimizer is

\[
\hat\Sigma=(1-0.1)S+0.1\,\bar s I.
\]

The shrinkage improves conditioning when the asset count is material relative
to the observation count. It reduces estimation variance, at the cost of bias
toward a spherical covariance target. It is a fixed 10% shrinkage, not a
data-adaptive Ledoit--Wolf estimate.

### Expected returns and reference reconstruction

`mean_estimator` accepts `auto`, `equilibrium`, `empirical`, `bayes_stein`,
`james_stein`, `bodnar_okhrin`. Empirical means use `EmpiricalMu`; the three
shrinkage means use `ShrunkMu` with the corresponding method. Equilibrium
uses `EquilibriumMu` with a complete fully invested reference allocation.
Conceptually its excess mean is \(\hat\mu_e=\lambda\hat\Sigma w_b\).
That identity explains why reconstructing the benchmark with utility is not
an independent forecast.

In the checked engine, `auto` normally selects equilibrium when a full
reference exists and Bayes-Stein otherwise. Utility and active financing
explicitly resolve `auto` to Bayes-Stein. The flat wrapper constructs an
equal-weight B0 if none is supplied, so defaults are not reference-free.
Read `resolved_mean_estimator` rather than assuming empirical means. Explicit
equilibrium is rejected for active financing. Tree constraints additionally
allow covariance choice and typed Black-Litterman views; the flat API does not.

### Tail risk: what is no longer a policy

The old `min_cvar`, `risk_budget_cvar` and `hrp_cvar` contracts are not current
V2 objective names. V2 does not expose a selectable CVaR risk measure through
these tools. CVaR and RiskBudgeting remain available in lower-level Skfolio;
using them directly is a separate integration, not an alias for `min_risk`
or `hrp`. The Rockafellar-Uryasev reference below records that distinction.
The previous numerical result for `max_return_benchmark_vol` is removed because
its V1 run/configuration is not reproducible through this current wrapper.

## Supported policies

| Objective | Implementation | Mathematical target | Principal sensitivity |
| --- | --- | --- | --- |
| `min_risk` | V2 local optimizer | minimize \(w^\top\hat\Sigma w\) | covariance estimate and concentration at bounds |
| `max_return` | V2 local optimizer | maximize \(w^\top\hat\mu_e\) | forecast error and binding bounds |
| `max_ratio` | V2 local optimizer | maximize excess mean / standard deviation | expected returns and nonconvex search |
| `max_utility` | V2 local optimizer | maximize excess mean minus half risk-aversion times variance | mean estimate and risk-aversion calibration |
| `hrp` | Skfolio `HierarchicalRiskParity` | cluster then recursively allocate variance risk | clustering distance and tree stability |

The first four use V2's audited local solver, not the old named `MeanRisk`
policies. Equivalent convex fast paths may be selected by the installed
engine, with audited fallback. The formulas here describe economic targets,
not a promise that every nonconvex case has a globally optimal solution.

### Minimum variance with shrinkage: `min_risk`

Without an explicit tree volatility target, `min_risk` solves

\[
\min_{w\in\mathcal W}\quad w^\top\hat\Sigma w.
\]

It does not require a return forecast and is therefore often less sensitive
to mean-estimation error than return-seeking portfolios. It can still become
concentrated in assets with low estimated variance or strong correlations.
The optimized measure is variance; the run result's `actual_volatility` is
annualized standard deviation. Neither is CVaR or a drawdown guarantee.

### Maximum return: `max_return`

The current objective is

\[
\max_{w\in\mathcal W}\quad w^\top\hat\mu_e.
\]

For a fully invested portfolio, subtracting one common risk-free rate does
not change the maximizing weights. Cash/borrowing changes the feasible
exposures and economics. Bounds alone can drive the solution toward assets
with the highest estimated means; the flat tool does not automatically apply
a benchmark-volatility cap.

For the former benchmark-volatility policy, use a tree node with
`goal.objective="max_return"`, `constraints.max_volatility_reference="benchmark"`
and a declared B0 benchmark. The cap is resolved from that benchmark's
training-window series. `volatility_reference="benchmark"` instead configures
an exact target unless a cap mode is explicitly selected. No future OOS
benchmark volatility is used as the target. Inspect the per-node audit for
resolved references and feasibility.

### Maximum Sharpe-type ratio: `max_ratio`

The constrained fractional program is

\[
\max_{w\in\mathcal W}\quad
\frac{w^\top\hat\mu-r_f^{(p)}}{\sqrt{w^\top\hat\Sigma w}}
\]

for the fully invested case. More generally the numerator is
\(w^\top\hat\mu_e\). The engine protects the denominator from zero.
`risk_free_rate` is exposed now, with node/root resolution and a zero hard
default; the older statement that a future API must expose it is obsolete.

Covariance shrinkage and bounds cannot remove instability caused by noisy
expected returns. Compare out of sample rather than choosing a single fitted
ratio. The fractional objective and an exact volatility equality are
nonconvex; deterministic multi-start improves reproducibility, not proof of
global optimality.

### Maximum mean-variance utility: `max_utility`

The actual implemented target is

\[
\max_{w\in\mathcal W}\quad
w^\top\hat\mu_e-\frac{\lambda}{2}w^\top\hat\Sigma w,
\qquad\lambda>0.
\]

This corrects the old displayed standard-deviation penalty. `risk_aversion`
is \(\lambda\); it has a positive finite validation rule and resolves
from node/root with hard default 1. Larger values favor lower variance.
Calibration must use the same return grid and mean-estimation convention.
An OOS transaction-cost deduction is separate from this fitting objective.

### Hierarchical risk parity: `hrp`

The engine applies Skfolio hierarchical risk parity. Conceptually it:

1. converts inter-asset dependence into a distance and builds a clustering tree;
2. orders related assets together;
3. recursively splits the tree, allocating more capital to the lower-risk branch.

For branch risks \(V_L,V_R\), a conceptual split allocates
\(\alpha_L=V_R/(V_L+V_R)\), \(\alpha_R=1-\alpha_L\); the actual
implementation also respects declared bounds. HRP avoids direct inversion
of the covariance matrix. It remains sensitive to dependence and tree
stability and is not a convex utility optimization.

`_solve_hrp` constructs `HierarchicalRiskParity` without overriding
`risk_measure`; in the verified Skfolio 0.20.1 its default is **variance**.
The prior uses `EmpiricalPrior(covariance_estimator=ShrunkCovariance())`.
This is not the removed CVaR-HRP policy. Cash/leverage, a non-`auto` mean
estimator, views, volatility targets/caps and tracking-error limits are
rejected for HRP in the checked engine. Bounds and the resulting weights are
audited; they are not repaired after an invalid solve.

## Constraint support and costs

The flat tool exposes uniform weight bounds, optional cash and leverage,
mean estimator, risk aversion, risk-free rate and B0 weights. It has no
group/linear constraint, hard turnover, tracking-error or fitting-cost
argument. Richer supported V2 settings belong to tree `constraints`; this
does not mean every old V1 constraint has a V2 replacement. Read the current
tree schema rather than passing former `OptimizationSpec` fields unchanged.

The backtest should state costs explicitly, for example
`transaction_cost_bps=10.0`. A 10-bps assumption applies per unit of modeled
absolute weight turnover; it is not "10 bps per year". The V2 ledger computes
the changes relative to its drifted holdings, deducts the rebalance cost from
the first following holding return, and compounds subsequent returns. This
keeps cost accounting out of the optimizer's mathematical target.

With cash enabled, positive local cash earns the effective risk-free rate;
borrowing pays that rate plus tree `borrow_spread_bps / 10000`. The flat
wrapper does not expose borrowing spread, so it uses the engine default.
Lending and borrowing regimes are evaluated separately. Hierarchical
terminal exposures multiply through parent allocations, including local
financing; child cash must not also be counted as root cash.

Performance is comparable only with the same cost convention, rebalancing
rule, calendar, universe and data cutoff for every strategy and comparator.

## Walk-forward evaluation

`train_size` counts fitting-frequency observations and `rebalance_frequency`
independently controls the rebalance calendar. V2 fits weights using only
training returns ending at the signal endpoint and values them on subsequent
**daily** observations until the next endpoint. The first holding return is
after the signal; there is no use of training returns as OOS performance.
No return row is emitted to the LLM.

Defaults are 104 training periods, weekly fitting, monthly rebalancing and
zero cost. Period completion and frequency resampling are code-defined in
`calendar.py`. A data coverage or infeasible fold error is not a license to
fill gaps with invented observations or silently narrow the universe.

The internal `V2BacktestReport` contains folds and curves. The LazyTools
wrapper projects it to fold count, FINAL/B0 aggregate metrics, paid cost and
bounded provenance. It does not persist V1 run records or expose curves.
Save the exact arguments and result in the caller's audit record; saved tree
JSON lives in the LazyPortfolio store shared with Tree Studio.

## Metric interpretation

OOS performance uses a compounded daily V2 ledger: CAGR, drawdown, costs
and annualized return/volatility/Sharpe use factor 252. Fitting
moments use the factor matching their data grid: daily 252, weekly 52,
monthly 12 and quarterly 4.

V2 reports `annualized_excess_return`, the mean of daily returns minus
`risk_free_rate / 252`, multiplied by 252; the old `annualized_mean` field
is not returned by this engine. It is distinct from `cagr`, calculated
from the compounded, costed OOS series:
\((\prod_t(1+r_{p,t}))^{252/T}-1\). A cumulative-return chart would use
\((W_t-1)\times100\); the flat wrapper supplies no curve to chart.

The other aggregate metric names are `annualized_volatility`,
`annualized_sharpe`, `annualized_sortino`, `risk_free_rate`, `max_drawdown`
and `n_obs`. Sharpe uses daily excess mean divided by sample standard
deviation times \(\sqrt{252}\); Sortino uses negative daily excess returns
for downside deviation. `max_drawdown` is the minimum wealth/preceding-peak
ratio minus one, so it is nonpositive. These are realized OOS metrics,
distinct from the run tool's fitted `expected_return_annualized` and
`actual_volatility`. The backtest wrapper has no CVaR aggregate field.

## Scientific use and model risk

- Compare a small, predeclared policy set under the same walk-forward protocol;
  do not pick the winner after repeatedly changing universe, dates and
  constraints without accounting for selection bias.
- Treat max-Sharpe and utility results as especially sensitive to mean estimates.
  Prefer stability of weights, turnover, drawdown and performance across folds
  over a single high ratio.
- Include plausible transaction costs, a static benchmark and an equal-weight
  comparator. A strategy that wins only before costs is not a robust result.
- Test multiple regimes and stress periods. Risk-free/funding settings belong to the
  current tree engine; the flat surface has no regime-selection argument.
- A successful numerical solve is not a trade approval. Execution, liquidity,
  taxes, leverage and mandate checks remain separate controls.

## References

- Markowitz, H. (1952), *Portfolio Selection*.
- Rockafellar, R. T. and Uryasev, S. (2000), *Optimization of Conditional Value-at-Risk* (background for the removed CVaR policies, not a V2 objective).
- Ledoit, O. and Wolf, M. (2004), *A Well-Conditioned Estimator for Large-Dimensional Covariance Matrices*.
- Maillard, S., Roncalli, T. and Teïletche, J. (2010), *The Properties of Equally Weighted Risk Contribution Portfolios* (risk-allocation background; no equal-CVaR-budget tool is claimed).
- López de Prado, M. (2016), *Building Diversified Portfolios that Outperform Out of Sample*.
- [Skfolio documentation](https://skfolio.org/) for estimator-level implementation details and solver behavior.


The implementation checks above use the verified dependency version, not an
assumption that every rolling documentation default stays identical. For
estimator definitions see Skfolio's [HRP reference](https://skfolio.org/generated/skfolio.optimization.HierarchicalRiskParity.html)
and [fixed covariance shrinkage reference](https://skfolio.org/generated/skfolio.moments.ShrunkCovariance.html).
The dependency source and per-node audits determine behavior in a deployment.
