"""LazyBridge tool providers for the finance domain — two different sources.

``PortfolioTools``, ``RiskTools``, ``OptimizerTools`` and ``ScoringTools`` wrap
deterministic LazyFin functions with ``Tool.wrap`` so an
``Agent(tools=[PortfolioTools(ledger)])`` can call them. The computation stays
in lazyfin; only the LLM-facing wrapping lives here (plan v3.1, Fase 5 — the
same classes in ``lazyfin.kernel``/``lazyfin.scoring`` are deprecated shims).
``OptimizerTools`` in particular wraps ``lazyfin.kernel.optimize_target_weights``
— a simple score-ranked greedy weight solver, the one LazyFin optimizer that
was *not* part of the hierarchical-engine extraction below (see LazyFin's own
README: "unrelated and unaffected").

``PortfolioOptimizationTools`` (this file) and ``PortfolioTreeTools``
(``tree_tools.py``) are the odd ones out: they wrap LazyPortfolio's
hierarchical (V2) engine — a separate package with **no** LazyFin dependency
of its own — lazily imported inside each class's own ``__init__``, never at
this module's top level. Do not confuse the two "optimizer" surfaces: if
you're looking for the hierarchical/tree engine, it is in ``lazyportfolio``,
never in ``lazyfin.kernel`` or ``lazyfin.optimization`` (the latter no longer
exists — see ``docs/portfolio-optimization.md``'s stale-content banner).

``ResolveTools`` was REMOVED (audit CA-03, no compatibility window needed):
it fetched raw EDGAR company facts directly through its injected client,
bypassing market-data-hub. Agents resolve and read financials through the
hub-backed ``datahub_*`` tools; ``ScoringTools``' ``get_facts`` callable can
be fed from those.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from lazybridge import Tool

try:
    from lazyfin.kernel import (
        Mandate,
        OptimizationConstraints,
        PortfolioLedger,
        compute_concentration,
        compute_drift,
        compute_exposure,
        optimize_target_weights,
        run_risk_checks,
    )
    from lazyfin.model import (
        FinancialFact,
        Money,
        OptimizationRun,
        Portfolio,
        RiskReport,
        SecurityScore,
    )
    from lazyfin.scoring import score_security
except ImportError as exc:  # pragma: no cover - clear hint over bare failure
    raise ImportError(
        "lazytools.connectors.fin requires the lazyfin package: "
        "pip install 'lazyfin @ git+https://github.com/selvaz/LazyFin.git'"
    ) from exc

if TYPE_CHECKING:
    from datetime import datetime

    from lazyportfolio import OptimizationDataBackend

__all__ = [
    "PortfolioTools",
    "RiskTools",
    "OptimizerTools",
    "PortfolioOptimizationTools",
    "ScoringTools",
]


class PortfolioTools:
    """LazyBridge tool provider over the portfolio engine.

    Wraps the pure kernel functions (plus ``load_portfolio`` bound to a
    :class:`lazyfin.kernel.PortfolioLedger`) so an
    ``Agent(tools=[PortfolioTools(ledger)])`` can read state and compute
    exposure / concentration / drift. All tools are read-only and
    deterministic.
    """

    _is_lazy_tool_provider = True

    def __init__(self, ledger: PortfolioLedger) -> None:
        self._ledger = ledger

    def _load_portfolio(self, portfolio_id: str) -> Portfolio:
        return self._ledger.get(portfolio_id)

    def as_tools(self) -> list[Tool]:
        return [
            Tool.wrap(
                self._load_portfolio,
                name="load_portfolio",
                description="Load the latest immutable snapshot of a portfolio by id.",
            ),
            Tool.wrap(
                compute_exposure,
                name="compute_exposure",
                description=(
                    "Compute portfolio weights by asset class, sector, country and "
                    "currency (cash included) from an immutable snapshot."
                ),
            ),
            Tool.wrap(
                compute_concentration,
                name="compute_concentration",
                description=(
                    "Compute issuer concentration (descending weights, top-N shares, "
                    "HHI) and per-dimension concentration from a snapshot."
                ),
            ),
            Tool.wrap(
                compute_drift,
                name="compute_drift",
                description=(
                    "Compute actual-vs-target drift of a snapshot against an "
                    "AllocationView (security / asset_class / sector / country / currency)."
                ),
            ),
        ]


class RiskTools:
    """LazyBridge tool provider exposing the deterministic risk manager."""

    _is_lazy_tool_provider = True

    def __init__(self, mandate: Mandate, *, adv: dict[str, Money] | None = None) -> None:
        self._mandate = mandate
        self._adv = adv

    def _run_risk_checks(self, snapshot: Portfolio) -> RiskReport:
        return run_risk_checks(snapshot, self._mandate, adv=self._adv)

    def as_tools(self) -> list[Tool]:
        return [
            Tool.wrap(
                self._run_risk_checks,
                name="run_risk_checks",
                description=(
                    "Run the deterministic risk mandate (concentration, cash bounds, "
                    "drift, liquidity, compliance, stress) against a portfolio "
                    "snapshot; hard failures require human approval."
                ),
            )
        ]


class OptimizerTools:
    """LazyBridge tool provider over the deterministic optimizer."""

    _is_lazy_tool_provider = True

    def __init__(self, constraints: OptimizationConstraints) -> None:
        self._constraints = constraints

    def _optimize_target_weights(self, scores: list[SecurityScore], as_of: datetime) -> OptimizationRun:
        return optimize_target_weights(scores, self._constraints, as_of=as_of)

    def as_tools(self) -> list[Tool]:
        return [
            Tool.wrap(
                self._optimize_target_weights,
                name="optimize_target_weights",
                description=(
                    "Deterministically allocate target portfolio weights from "
                    "security scores under position/bucket/cash constraints; "
                    "returns an OptimizationRun with per-decision reason codes."
                ),
            )
        ]


class PortfolioOptimizationTools:
    """Safe LLM surface over LazyPortfolio's hierarchical (V2) optimizer.

    Historical returns are loaded internally by the injected data backend. The
    model receives only instrument identifiers, a flat set of constraints and
    compact diagnostics; neither tool argument nor result can carry raw
    observations. This wraps a single flat node — LazyPortfolio's full
    node-tree configuration (parent/child hierarchies, per-node proxies) is
    only exposed through Tree Studio / ``V2Model.from_config`` directly, not
    through this LLM-facing surface.
    """

    _is_lazy_tool_provider = True

    def __init__(
        self,
        *,
        backend: OptimizationDataBackend | None = None,
    ) -> None:
        try:
            from lazyportfolio import (
                HierarchicalV2Backtester,
                HierarchicalV2Estimator,
                MarketDataHubOptimizationBackend,
                V2Model,
            )
            from lazyportfolio.calendar import _annualization_factor, _resample_simple_returns
        except ImportError as exc:  # pragma: no cover - optional dependency boundary
            raise ImportError(
                "PortfolioOptimizationTools requires the lazyportfolio package: "
                "pip install 'lazyportfolio @ git+https://github.com/selvaz/LazyPortfolio.git'"
            ) from exc
        self._backend = backend
        self._market_data_backend = MarketDataHubOptimizationBackend
        self._v2_model = V2Model
        self._estimator = HierarchicalV2Estimator()
        self._backtester = HierarchicalV2Backtester()
        self._annualization_factor = _annualization_factor
        self._resample_simple_returns = _resample_simple_returns

    def _resolve_backend(self) -> OptimizationDataBackend:
        if self._backend is None:
            self._backend = self._market_data_backend()
        return self._backend

    def as_tools(self) -> list[Tool]:
        return [
            Tool.wrap(
                self._list_objectives,
                name="portfolio_optimizer_list_objectives",
                description="List the hierarchical (V2) optimizer's objectives and constraint fields.",
            ),
            Tool.wrap(
                self._optimize,
                name="portfolio_optimizer_run",
                description=(
                    "Estimate target weights with LazyPortfolio's V2 optimizer over canonical "
                    "daily market-data-hub simple returns, resampled to `frequency`. objective MUST "
                    "be one of: min_risk (default), max_return, max_ratio, max_utility, hrp — call "
                    "portfolio_optimizer_list_objectives for the authoritative list. Never pass "
                    "prices or returns: use comma-separated tickers (SPY,TLT) or canonical IDs "
                    "(ticker:SPY,ticker:TLT), a date range, objective and constraints only. "
                    "benchmark_weights (e.g. {'ticker:SPY': 0.7, 'ticker:TLT': 0.3}) is required "
                    "when mean_estimator='equilibrium' or objective='max_utility' with 'auto'; it "
                    "defaults to an equal-weight benchmark otherwise. This tool has NO Black-"
                    "Litterman view support -- for views, use portfolio_tree_estimate/_backtest "
                    "instead (a single node with no children is a valid flat portfolio there too)."
                ),
            ),
            Tool.wrap(
                self._backtest,
                name="portfolio_optimizer_backtest",
                description=(
                    "Run a causal walk-forward backtest of the V2 optimizer using daily "
                    "simple-return OOS valuation. Same objective/constraint vocabulary as "
                    "portfolio_optimizer_run; see portfolio_optimizer_list_objectives. frequency "
                    "selects the fitting-return grid (D|W|M|Q); rebalance_frequency selects when "
                    "weights are renewed (D|W|M|Q). Returns only aggregate metrics and provenance, "
                    "never return observations. Instruments accept SPY,TLT or ticker:SPY,ticker:TLT."
                ),
            ),
        ]

    def _list_objectives(self) -> str:
        return _json(
            {
                "objectives": ["min_risk", "max_return", "max_ratio", "max_utility", "hrp"],
                "mean_estimators": ["auto", "equilibrium", "empirical", "bayes_stein", "james_stein", "bodnar_okhrin"],
                "constraints": [
                    "min_weight",
                    "max_weight",
                    "cash_enabled",
                    "max_leverage",
                    "mean_estimator",
                    "risk_aversion",
                    "risk_free_rate",
                    "benchmark_weights",
                ],
                "engine": "lazyportfolio (hierarchical V2, skfolio-backed moment estimation)",
            }
        )

    def _build_model(
        self,
        universe: list[str],
        *,
        objective: str,
        min_weight: float,
        max_weight: float,
        cash_enabled: bool,
        max_leverage: float,
        mean_estimator: str,
        risk_aversion: float | None,
        risk_free_rate: float | None,
        benchmark_weights: dict[str, float] | None,
    ) -> Any:
        equal_weight = 1.0 / len(universe)
        config = {
            "root_id": "root",
            "nodes": [
                {
                    "id": "root",
                    "name": "root",
                    "children": [],
                    "instruments": universe,
                    "goal": {"objective": objective},
                    "constraints": {
                        "min_weights": {instrument: min_weight for instrument in universe} if min_weight else {},
                        "max_weights": {instrument: max_weight for instrument in universe} if max_weight < 1.0 else {},
                        "mean_estimator": mean_estimator,
                        "cash_enabled": cash_enabled,
                        "max_leverage": max_leverage,
                        "risk_aversion": risk_aversion,
                        "risk_free_rate": risk_free_rate,
                    },
                }
            ],
            "backtest": {
                "benchmark": {
                    "name": "B0",
                    "weights": benchmark_weights
                    or {instrument: equal_weight for instrument in universe},
                }
            },
        }
        return self._v2_model.from_config(config)

    def _optimize(
        self,
        instruments: str,
        objective: str = "min_risk",
        start: str = "",
        end: str = "",
        frequency: str = "W",
        min_weight: float = 0.0,
        max_weight: float = 1.0,
        cash_enabled: bool = False,
        max_leverage: float = 1.0,
        mean_estimator: str = "auto",
        risk_aversion: float | None = None,
        risk_free_rate: float | None = None,
        benchmark_weights: dict[str, float] | None = None,
    ) -> str:
        universe = _canonical_ticker_instruments(instruments)
        model = self._build_model(
            universe,
            objective=objective,
            min_weight=min_weight,
            max_weight=max_weight,
            cash_enabled=cash_enabled,
            max_leverage=max_leverage,
            mean_estimator=mean_estimator,
            risk_aversion=risk_aversion,
            risk_free_rate=risk_free_rate,
            benchmark_weights=benchmark_weights,
        )
        dataset = self._resolve_backend().load_returns(universe, start=start, end=end, frequency="D")
        estimation = self._resample_simple_returns(dataset.returns, frequency)
        estimate = self._estimator.estimate(
            model,
            estimation,
            mode="flat",
            periods_per_year=self._annualization_factor(frequency),
        )
        result = next(iter(estimate.node_results.values()))
        payload = {
            "status": "optimal",
            "objective": objective,
            "target_weights": [
                {"security_id": instrument, "weight": weight}
                for instrument, weight in estimate.terminal_weights.items()
            ],
            "expected_return_annualized": result.audit.expected_return_annualized,
            "actual_volatility": result.audit.actual_volatility,
            "resolved_mean_estimator": result.audit.resolved_mean_estimator,
            "solver_message": result.audit.solver_message,
            "provenance": {
                "source": dataset.metadata.get("source"),
                "n_rows": dataset.metadata.get("n_rows"),
                "frequency": frequency,
            },
        }
        from lazytools.operations.portfolio import publish
        publish("portfolio_optimizer_run", parameters={
            "instruments": universe, "objective": objective, "start": start, "end": end,
            "frequency": frequency, "min_weight": min_weight, "max_weight": max_weight,
            "cash_enabled": cash_enabled, "max_leverage": max_leverage,
            "mean_estimator": mean_estimator, "risk_aversion": risk_aversion,
            "risk_free_rate": risk_free_rate, "benchmark_weights": benchmark_weights,
        }, result=payload)
        return _json(payload)

    def _backtest(
        self,
        instruments: str,
        objective: str = "min_risk",
        start: str = "",
        end: str = "",
        frequency: str = "W",
        min_weight: float = 0.0,
        max_weight: float = 1.0,
        cash_enabled: bool = False,
        max_leverage: float = 1.0,
        mean_estimator: str = "auto",
        risk_aversion: float | None = None,
        risk_free_rate: float | None = None,
        benchmark_weights: dict[str, float] | None = None,
        train_size: int = 104,
        rebalance_frequency: str = "M",
        transaction_cost_bps: float = 0.0,
    ) -> str:
        universe = _canonical_ticker_instruments(instruments)
        model = self._build_model(
            universe,
            objective=objective,
            min_weight=min_weight,
            max_weight=max_weight,
            cash_enabled=cash_enabled,
            max_leverage=max_leverage,
            mean_estimator=mean_estimator,
            risk_aversion=risk_aversion,
            risk_free_rate=risk_free_rate,
            benchmark_weights=benchmark_weights,
        )
        dataset = self._resolve_backend().load_returns(universe, start=start, end=end, frequency="D")
        report = self._backtester.run(
            model,
            dataset.returns,
            mode="flat",
            train_size=train_size,
            estimation_frequency=frequency,
            rebalance_frequency=rebalance_frequency,
            transaction_cost_bps=transaction_cost_bps,
        )
        payload = {
            "status": "optimal",
            "objective": objective,
            "n_folds": len(report.folds),
            "metrics": report.metrics.get("FINAL"),
            "benchmark_metrics": report.metrics.get("B0"),
            "transaction_cost_paid": report.transaction_cost_paid.get("FINAL"),
            "provenance": {
                "source": dataset.metadata.get("source"),
                "n_rows": dataset.metadata.get("n_rows"),
                "estimation_frequency": frequency,
                "rebalance_frequency": rebalance_frequency,
            },
        }
        from lazytools.operations.portfolio import publish
        publish("portfolio_optimizer_backtest", parameters={
            "instruments": universe, "objective": objective, "start": start, "end": end,
            "frequency": frequency, "min_weight": min_weight, "max_weight": max_weight,
            "cash_enabled": cash_enabled, "max_leverage": max_leverage,
            "mean_estimator": mean_estimator, "risk_aversion": risk_aversion,
            "risk_free_rate": risk_free_rate, "benchmark_weights": benchmark_weights,
            "train_size": train_size, "rebalance_frequency": rebalance_frequency,
            "transaction_cost_bps": transaction_cost_bps,
        }, result=payload)
        return _json(payload)


class ScoringTools:
    """LazyBridge tool provider binding the scoring engine to a facts source.

    ``get_facts`` is any callable returning normalised facts for a canonical
    security id (typically ``ResolveTools._get_financial_facts`` or a cached
    store lookup); ``get_market_cap`` optionally supplies the market cap for
    the valuation leg.
    """

    _is_lazy_tool_provider = True

    def __init__(
        self,
        get_facts: Callable[[str], list[FinancialFact]],
        *,
        get_market_cap: Callable[[str], Money | None] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._get_facts = get_facts
        self._get_market_cap = get_market_cap
        self._now = now

    def _score_security(self, security_id: str) -> SecurityScore:
        facts = self._get_facts(security_id)
        market_cap = self._get_market_cap(security_id) if self._get_market_cap else None
        as_of = self._now() if self._now else datetime.now(tz=UTC)
        return score_security(security_id, facts, as_of=as_of, market_cap=market_cap)

    def as_tools(self) -> list[Tool]:
        return [
            Tool.wrap(
                self._score_security,
                name="score_security",
                description=(
                    "Deterministically score a security (valuation, quality, balance "
                    "sheet, cash flow, momentum, minus penalties) from its normalised "
                    "SEC fundamentals. Reproducible: same facts, same score."
                ),
            )
        ]


def _json(payload: object) -> str:
    return json.dumps(payload, default=str, sort_keys=True)


def _canonical_ticker_instruments(instruments: str) -> list[str]:
    requested = [item.strip() for item in instruments.split(",") if item.strip()]
    return [item if ":" in item else f"ticker:{item}" for item in requested]
