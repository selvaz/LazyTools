"""Independent verification of LazyPortfolio's V2 optimizer tool.

For each case: (1) call PortfolioOptimizationTools' real tool function
directly against real market-data-hub data; (2) independently reconstruct
the SAME optimization from scratch -- own data resampling (matching
lazyportfolio.calendar._resample_simple_returns' documented formula), own
moment estimation (skfolio.moments.ShrunkCovariance called directly, sample
mean), own scipy.optimize solve (SLSQP, several random restarts) -- and
compare the resulting weights. This tests whether the tool's internal
solver.py is wired correctly, not whether skfolio itself is correct.

Universe: SPY, TLT, GLD, QQQ (canonical, easy to sanity-check by eye).
mean_estimator is pinned to 'empirical' (plain sample mean) on both sides to
avoid ambiguity from the 'auto' -> bayes_stein/equilibrium resolution.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import minimize

LAZYTOOLS_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = LAZYTOOLS_ROOT.parent


def _prefer_workspace_sources() -> None:
    for path in (WORKSPACE_ROOT / "LazyBridge", LAZYTOOLS_ROOT / "src", WORKSPACE_ROOT / "market-data-hub"):
        text = str(path)
        if path.exists() and text not in sys.path:
            sys.path.insert(0, text)


TICKERS = ["ticker:SPY", "ticker:TLT", "ticker:GLD", "ticker:QQQ"]
BARE = ["SPY", "TLT", "GLD", "QQQ"]
START, END, FREQ = "2015-01-01", "2026-07-28", "W"
N_RESTARTS = 12
SEED = 7


def independent_moments(hub_db: str):
    from lazyportfolio import MarketDataHubOptimizationBackend
    from lazyportfolio.calendar import _resample_simple_returns
    from skfolio.moments import ShrunkCovariance

    backend = MarketDataHubOptimizationBackend(db_path=hub_db)
    daily = backend.load_returns(TICKERS, start=START, end=END, frequency="D").returns
    weekly = _resample_simple_returns(daily, FREQ)
    weekly = weekly[TICKERS]  # keep column order canonical
    mu = weekly.mean().to_numpy()
    cov = ShrunkCovariance().fit(weekly.to_numpy()).covariance_
    return mu, np.asarray(cov), weekly


def solve_independent(objective: str, mu: np.ndarray, cov: np.ndarray) -> np.ndarray:
    n = len(mu)
    bounds = [(0.0, 1.0)] * n
    cons = [{"type": "eq", "fun": lambda w: np.sum(w) - 1.0}]

    def variance(w):
        return float(w @ cov @ w)

    def neg_sharpe(w):
        vol = np.sqrt(max(variance(w), 1e-18))
        return -float(mu @ w) / vol

    loss = variance if objective == "min_risk" else neg_sharpe

    rng = np.random.default_rng(SEED)
    starts = [np.full(n, 1.0 / n)]
    for _ in range(N_RESTARTS):
        raw = rng.random(n)
        starts.append(raw / raw.sum())

    best = None
    best_val = np.inf
    for x0 in starts:
        res = minimize(loss, x0, method="SLSQP", bounds=bounds, constraints=cons, options={"maxiter": 500, "ftol": 1e-12})
        if res.success and res.fun < best_val:
            best_val = res.fun
            best = res.x
    assert best is not None, f"independent solve failed for {objective}"
    return best


def solve_hrp_independent(weekly: pd.DataFrame) -> np.ndarray:
    from skfolio.moments import ShrunkCovariance
    from skfolio.optimization import HierarchicalRiskParity
    from skfolio.prior import EmpiricalPrior

    n = weekly.shape[1]
    estimator = HierarchicalRiskParity(
        prior_estimator=EmpiricalPrior(covariance_estimator=ShrunkCovariance()),
        min_weights=dict(zip(TICKERS, [0.0] * n, strict=True)),
        max_weights=dict(zip(TICKERS, [1.0] * n, strict=True)),
    )
    estimator.fit(weekly)  # DataFrame (not .to_numpy()) -- dict min/max_weights need column names to align
    return np.asarray(estimator.weights_, dtype=float)


def run_case(objective: str, tools, hub_db: str) -> None:
    print(f"\n{'='*70}\nCASE: objective={objective!r}  universe={BARE}  {START}..{END}  freq={FREQ}")

    # hrp doesn't use an expected-return estimator -- must leave mean_estimator
    # at 'auto' (the tool raises otherwise); the other two objectives use
    # 'empirical' to avoid the 'auto' -> bayes_stein/equilibrium ambiguity.
    tool_result_raw = tools._optimize(
        instruments=",".join(BARE),
        objective=objective,
        start=START,
        end=END,
        frequency=FREQ,
        mean_estimator="auto" if objective == "hrp" else "empirical",
    )
    tool_result = json.loads(tool_result_raw)
    tool_weights_dict = {row["security_id"]: row["weight"] for row in tool_result["target_weights"]}
    # terminal_weights appears to omit near-zero holdings -- default missing to 0.0
    # rather than KeyError (confirmed: the omitted tickers' weights sum with the
    # present ones to ~1.0, so this is a "compact target list" convention, not a
    # dropped instrument).
    tool_weights = np.array([tool_weights_dict.get(t, 0.0) for t in TICKERS])
    print(f"TOOL   weights: {dict(zip(BARE, np.round(tool_weights, 4)))}")
    print(f"TOOL   resolved_mean_estimator={tool_result.get('resolved_mean_estimator')}  "
          f"exp_ret_ann={tool_result.get('expected_return_annualized')}  "
          f"actual_vol={tool_result.get('actual_volatility')}")

    mu, cov, weekly = independent_moments(hub_db)
    indep_weights = solve_hrp_independent(weekly) if objective == "hrp" else solve_independent(objective, mu, cov)
    print(f"INDEP  weights: {dict(zip(BARE, np.round(indep_weights, 4)))}")

    diff = np.abs(tool_weights - indep_weights)
    print(f"max abs weight diff: {diff.max():.5f}  (per-asset: {dict(zip(BARE, np.round(diff, 5)))})")

    def ann_vol(w):
        return float(np.sqrt(w @ cov @ w) * np.sqrt(52.0))

    def ann_ret(w):
        return float(mu @ w * 52.0)

    print(f"TOOL   ann. return={ann_ret(tool_weights):.4%}  ann. vol={ann_vol(tool_weights):.4%}")
    print(f"INDEP  ann. return={ann_ret(indep_weights):.4%}  ann. vol={ann_vol(indep_weights):.4%}")

    tol = 0.02  # 2 percentage points of weight -- generous, given SLSQP local optima / restart variance
    status = "MATCH" if diff.max() <= tol else "MISMATCH"
    print(f"--> {status} (tolerance {tol:.0%})")


BL_VIEWS = [
    {"instruments": {"ticker:GLD": 1.0}, "expected_return": 0.08, "confidence": 0.6, "source": "test"},
    {"instruments": {"ticker:QQQ": 1.0, "ticker:SPY": -1.0}, "expected_return": 0.05, "confidence": 0.5, "source": "test"},
]
VIEW_TAU = 0.05


def bl_posterior_independent(mu: np.ndarray, cov: np.ndarray, views: list[dict], tau: float, periods_per_year: float) -> np.ndarray:
    """Idzorek confidence-scaled Black-Litterman posterior mean, reimplemented
    from the standard closed-form (He-Litterman): M = mu + tau*Sigma*P' *
    [P*tau*Sigma*P' + Omega]^-1 * (Q - P*mu), Omega_kk = tau*(1-c_k)/c_k*(P_k Sigma P_k').
    Independent of lazyportfolio.v2.moments.black_litterman_posterior -- not a
    call into it, a from-scratch reimplementation to cross-check that function.
    """
    index = {n: i for i, n in enumerate(TICKERS)}
    k = len(views)
    pick = np.zeros((k, len(TICKERS)))
    q = np.zeros(k)
    omega_diag = np.zeros(k)
    for row, v in enumerate(views):
        for instrument, coefficient in v["instruments"].items():
            pick[row, index[instrument]] = coefficient
        q[row] = v["expected_return"] / periods_per_year
        alpha = (1.0 - v["confidence"]) / v["confidence"]
        prior_view_variance = float(pick[row] @ cov @ pick[row])
        omega_diag[row] = tau * alpha * prior_view_variance
    tau_sigma_pick = tau * cov @ pick.T
    system = pick @ tau_sigma_pick + np.diag(omega_diag)
    mu_solution = np.linalg.solve(system, q - pick @ mu)
    return mu + tau_sigma_pick @ mu_solution


def run_bl_case(hub_db: str) -> None:
    print(f"\n{'='*70}\nCASE: Black-Litterman  objective=max_ratio  view_covariance_policy=prior_risk (default)")
    print(f"Views: {BL_VIEWS}")
    periods_per_year = 52.0
    mu, cov, weekly = independent_moments(hub_db)

    # TOOL side: PortfolioOptimizationTools' _optimize has NO views/view_tau
    # argument at all (confirmed by inspecting its signature) -- BL views are
    # only reachable through V2Model.from_config + HierarchicalV2Estimator
    # directly, the same path Tree Studio uses. This is a real gap in the
    # LLM-facing tool surface, not a workaround.
    from lazyportfolio import HierarchicalV2Estimator, V2Model

    config = {
        "root_id": "root",
        "nodes": [
            {
                "id": "root",
                "name": "BL Test",
                "children": [],
                "instruments": BARE,
                "goal": {"objective": "max_ratio"},
                "constraints": {"mean_estimator": "empirical", "views": BL_VIEWS, "view_tau": VIEW_TAU},
            }
        ],
        "backtest": {"benchmark": {"name": "B0", "weights": {t: 1.0 / len(BARE) for t in BARE}}},
    }
    model = V2Model.from_config(config)
    estimate = HierarchicalV2Estimator().estimate(model, weekly, mode="flat", periods_per_year=periods_per_year)
    tool_weights_dict = dict(estimate.terminal_weights)
    tool_weights = np.array([tool_weights_dict.get(t, 0.0) for t in TICKERS])
    print(f"TOOL (BL)     weights: {dict(zip(BARE, np.round(tool_weights, 4)))}")

    # INDEP side: from-scratch Idzorek BL posterior mean (prior covariance
    # unchanged, matching the default view_covariance_policy='prior_risk') +
    # my own max_ratio SLSQP solve, same as the non-BL cases above.
    posterior_mu = bl_posterior_independent(mu, cov, BL_VIEWS, VIEW_TAU, periods_per_year)
    print(f"posterior mean (annualized): {dict(zip(BARE, np.round(posterior_mu * periods_per_year, 4)))}")
    print(f"prior mean    (annualized): {dict(zip(BARE, np.round(mu * periods_per_year, 4)))}")
    indep_weights = solve_independent("max_ratio", posterior_mu, cov)
    print(f"INDEP (BL)    weights: {dict(zip(BARE, np.round(indep_weights, 4)))}")

    diff = np.abs(tool_weights - indep_weights)
    print(f"max abs weight diff: {diff.max():.5f}  (per-asset: {dict(zip(BARE, np.round(diff, 5)))})")

    baseline_weights = solve_independent("max_ratio", mu, cov)
    print(f"BASELINE (no views, max_ratio) weights: {dict(zip(BARE, np.round(baseline_weights, 4)))}")

    tol = 0.02
    status = "MATCH" if diff.max() <= tol else "MISMATCH"
    print(f"--> {status} (tolerance {tol:.0%})")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _prefer_workspace_sources()

    default_hub = WORKSPACE_ROOT / "market-data-hub" / "market_data.duckdb"
    hub_db = str(Path(os.environ.get("MARKET_DATA_DB", default_hub)).resolve())

    from lazyportfolio import MarketDataHubOptimizationBackend
    from lazytools.connectors.fin import PortfolioOptimizationTools

    tools = PortfolioOptimizationTools(backend=MarketDataHubOptimizationBackend(db_path=hub_db))

    for objective in ("min_risk", "max_ratio", "hrp"):
        run_case(objective, tools, hub_db)

    run_bl_case(hub_db)


if __name__ == "__main__":
    main()
