"""LazyBridge tool providers over LazyFin's pure kernel (moved from lazyfin).

Each provider wraps deterministic LazyFin functions with ``Tool.wrap`` so an
``Agent(tools=[PortfolioTools(ledger)])`` can call them. The computation stays
in lazyfin; only the LLM-facing wrapping lives here (plan v3.1, Fase 5 — the
same classes in ``lazyfin.kernel``/``lazyfin.scoring`` are deprecated shims).

``ResolveTools`` was REMOVED (audit CA-03, no compatibility window needed):
it fetched raw EDGAR company facts directly through its injected client,
bypassing market-data-hub. Agents resolve and read financials through the
hub-backed ``datahub_*`` tools; ``ScoringTools``' ``get_facts`` callable can
be fed from those.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

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

    from lazyfin.optimization import (
        ModelPortfolio,
        OptimizationDataBackend,
        OptimizationSpec,
        OptimizationStore,
        SkfolioOptimizer,
    )

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
    """Safe LLM surface over the Skfolio-backed LazyFin optimizer.

    Historical returns are loaded internally by the injected data backend. The
    model receives only instrument identifiers, constraints and compact
    diagnostics; neither tool argument nor result can carry raw observations.
    """

    _is_lazy_tool_provider = True

    def __init__(
        self,
        store: OptimizationStore,
        *,
        backend: OptimizationDataBackend | None = None,
        optimizer: SkfolioOptimizer | None = None,
        artifacts_dir: str | Path | None = None,
    ) -> None:
        try:
            from lazyfin.optimization import (
                BacktestSpec,
                MarketDataHubOptimizationBackend,
                ModelPortfolio,
                OptimizationMethod,
                OptimizationSpec,
                SkfolioOptimizer,
            )
        except ImportError as exc:  # pragma: no cover - version/extra boundary
            raise ImportError(
                "PortfolioOptimizationTools requires LazyFin with the optimizer extra: pip install 'lazyfin[optimizer]'"
            ) from exc
        self._store = store
        self._backend = backend
        self._backtest_spec = BacktestSpec
        self._market_data_backend = MarketDataHubOptimizationBackend
        self._model_portfolio = ModelPortfolio
        self._optimization_method = OptimizationMethod
        self._optimization_spec = OptimizationSpec
        self._optimizer = optimizer or SkfolioOptimizer(store)
        self._artifacts_dir = Path(artifacts_dir).expanduser().resolve() if artifacts_dir else None

    def _resolve_backend(self) -> OptimizationDataBackend:
        if self._backend is None:
            self._backend = self._market_data_backend()
        return self._backend

    def as_tools(self) -> list[Tool]:
        return [
            Tool.wrap(
                self._list_methods,
                name="portfolio_optimizer_list_methods",
                description="List Skfolio portfolio methods and their supported LazyFin constraints.",
            ),
            Tool.wrap(
                self._create_model_portfolio,
                name="portfolio_optimizer_create_benchmark",
                description=(
                    "Persist a versioned long-only model benchmark from canonical instrument weights. "
                    "Example weights: {'ticker:ACWI': 0.7, 'ticker:AGG': 0.3}."
                ),
            ),
            Tool.wrap(
                self._list_benchmarks,
                name="portfolio_optimizer_list_benchmarks",
                description="List persisted model benchmark metadata and weights.",
            ),
            Tool.wrap(
                self._optimize,
                name="portfolio_optimizer_run",
                description=(
                    "Run a Skfolio portfolio optimization over canonical daily market-data-hub "
                    "simple returns. method MUST be one of exactly these 7 (there is no plain "
                    "'max_sharpe'/'min_variance' — the mean-based ones carry the '_shrinkage' "
                    "suffix): min_variance_shrinkage (default), max_sharpe_shrinkage, "
                    "max_utility_shrinkage, min_cvar, hrp_cvar, risk_budget_cvar, "
                    "max_return_benchmark_vol; call portfolio_optimizer_list_methods for the "
                    "authoritative list and each method's supported constraints. frequency selects "
                    "the fitting-return frequency (D|W|M|Q), not the data source. Never pass "
                    "prices or returns: use comma-separated tickers (SPY,TLT) or canonical IDs "
                    "(ticker:SPY,ticker:TLT), a date range, method and constraints only. Groups map "
                    "canonical IDs to labels, e.g. {'ticker:SPY': ['equity']}."
                ),
            ),
            Tool.wrap(
                self._get_run,
                name="portfolio_optimizer_get_run",
                description="Read a bounded persisted point-in-time optimization run by id, not a backtest id.",
            ),
            Tool.wrap(
                self._get_backtest,
                name="portfolio_optimizer_get_backtest",
                description=(
                    "Read bounded aggregate metrics for a persisted walk-forward backtest by its backtest: id. "
                    "Use this for a backtest id; it never returns return observations."
                ),
            ),
            Tool.wrap(
                self._backtest,
                name="portfolio_optimizer_backtest",
                description=(
                    "Run a Skfolio walk-forward backtest using daily simple-return OOS valuation. "
                    "method MUST be one of the same 7 as portfolio_optimizer_run: "
                    "min_variance_shrinkage (default), max_sharpe_shrinkage, max_utility_shrinkage, "
                    "min_cvar, hrp_cvar, risk_budget_cvar, max_return_benchmark_vol (no plain "
                    "'max_sharpe'); see portfolio_optimizer_list_methods. "
                    "frequency selects the fitting-return grid (D|W|M|Q); rebalance_frequency "
                    "selects when weights are renewed (D|W|M|Q). Returns only aggregate metrics "
                    "and provenance, never return observations. Instruments accept SPY,TLT or "
                    "ticker:SPY,ticker:TLT; groups use canonical IDs. When this provider has an "
                    "artifacts_dir, chart_filename='name.png' also returns a sandboxed file: reference "
                    "to an OOS cumulative-return chart for use in ReportTools."
                ),
            ),
        ]

    def _list_methods(self) -> str:
        return _json(
            {
                "methods": {
                    "min_variance_shrinkage": [
                        "bounds",
                        "groups",
                        "linear_constraints",
                        "costs",
                        "max_turnover",
                        "tracking_error",
                    ],
                    "min_cvar": ["bounds", "groups", "linear_constraints", "costs", "max_turnover", "tracking_error"],
                    "max_sharpe_shrinkage": [
                        "bounds",
                        "groups",
                        "linear_constraints",
                        "costs",
                        "max_turnover",
                        "tracking_error",
                    ],
                    "max_utility_shrinkage": [
                        "bounds",
                        "groups",
                        "linear_constraints",
                        "costs",
                        "max_turnover",
                        "tracking_error",
                        "risk_aversion",
                    ],
                    "max_return_benchmark_vol": [
                        "benchmark",
                        "bounds",
                        "groups",
                        "linear_constraints",
                        "costs",
                        "max_turnover",
                    ],
                    "risk_budget_cvar": ["bounds", "groups", "linear_constraints", "costs"],
                    "hrp_cvar": ["bounds", "costs"],
                },
                "engine": "skfolio",
            }
        )

    def _create_model_portfolio(
        self,
        benchmark_id: str,
        name: str,
        weights: dict[str, float],
        version: int = 1,
    ) -> str:
        model = self._model_portfolio(
            id=benchmark_id,
            name=name,
            weights={instrument: Decimal(str(weight)) for instrument, weight in weights.items()},
            version=version,
            valid_from=datetime.now(tz=UTC).date(),
        )
        self._store.save_model_portfolio(model)
        return _json({"benchmark": model.model_dump(mode="json")})

    def _list_benchmarks(self) -> str:
        return _json({"benchmarks": [model.model_dump(mode="json") for model in self._store.list_model_portfolios()]})

    def _optimize(
        self,
        instruments: str,
        method: str = "min_variance_shrinkage",
        start: str = "",
        end: str = "",
        frequency: str = "D",
        max_weight: float = 1.0,
        min_cash_weight: float = 0.0,
        transaction_cost_bps: float = 0.0,
        max_turnover: str = "",
        max_tracking_error: str = "",
        benchmark_id: str = "",
        groups: dict[str, list[str]] | None = None,
        linear_constraints: list[str] | None = None,
        risk_aversion: float = 1.0,
    ) -> str:
        spec, benchmark = self._request(
            instruments=instruments,
            method=method,
            start=start,
            end=end,
            frequency=frequency,
            max_weight=max_weight,
            min_cash_weight=min_cash_weight,
            transaction_cost_bps=transaction_cost_bps,
            max_turnover=max_turnover,
            max_tracking_error=max_tracking_error,
            benchmark_id=benchmark_id,
            groups=groups,
            linear_constraints=linear_constraints,
            risk_aversion=risk_aversion,
        )
        dataset = self._resolve_backend().load_returns(spec.universe, start=start, end=end, frequency="D")
        run = self._optimizer.optimize(spec, dataset, benchmark=benchmark)
        return _json(_run_summary(run))

    def _get_run(self, run_id: str) -> str:
        return _json(_run_summary(self._store.get_run(run_id)))

    def _get_backtest(self, backtest_id: str) -> str:
        return _json(self._store.get_backtest(backtest_id).model_dump(mode="json"))

    def _backtest(
        self,
        instruments: str,
        method: str = "min_variance_shrinkage",
        start: str = "",
        end: str = "",
        frequency: str = "D",
        max_weight: float = 1.0,
        min_cash_weight: float = 0.0,
        transaction_cost_bps: float = 0.0,
        max_turnover: str = "",
        max_tracking_error: str = "",
        benchmark_id: str = "",
        groups: dict[str, list[str]] | None = None,
        linear_constraints: list[str] | None = None,
        risk_aversion: float = 1.0,
        train_size: int = 252,
        rebalance_frequency: str = "W",
        chart_filename: str = "",
    ) -> str:
        spec, benchmark = self._request(
            instruments=instruments,
            method=method,
            start=start,
            end=end,
            frequency=frequency,
            max_weight=max_weight,
            min_cash_weight=min_cash_weight,
            transaction_cost_bps=transaction_cost_bps,
            max_turnover=max_turnover,
            max_tracking_error=max_tracking_error,
            benchmark_id=benchmark_id,
            groups=groups,
            linear_constraints=linear_constraints,
            risk_aversion=risk_aversion,
        )
        dataset = self._resolve_backend().load_returns(spec.universe, start=start, end=end, frequency="D")
        chart_path = self._chart_path(chart_filename) if chart_filename else None
        result = self._optimizer.backtest(
            spec,
            self._backtest_spec(
                id=f"backtest:{spec.id}",
                train_size=train_size,
                rebalance_frequency=rebalance_frequency,
            ),
            dataset,
            benchmark=benchmark,
            chart_path=chart_path,
        )
        payload = result.model_dump(mode="json")
        if chart_path is not None and result.status == "optimal":
            payload["chart"] = {
                "ref": f"file:{chart_path}",
                "caption": f"{method} walk-forward cumulative return versus benchmark",
            }
        return _json(payload)

    def _request(
        self,
        *,
        instruments: str,
        method: str,
        start: str,
        end: str,
        frequency: str,
        max_weight: float,
        min_cash_weight: float,
        transaction_cost_bps: float,
        max_turnover: str,
        max_tracking_error: str,
        benchmark_id: str,
        groups: dict[str, list[str]] | None,
        linear_constraints: list[str] | None,
        risk_aversion: float,
    ) -> tuple[OptimizationSpec, ModelPortfolio | None]:
        universe = _canonical_ticker_instruments(instruments)
        benchmark = self._store.get_model_portfolio(benchmark_id) if benchmark_id else None
        spec_id = f"opt:{datetime.now(tz=UTC).strftime('%Y%m%d%H%M%S%f')}"
        spec = self._optimization_spec(
            id=spec_id,
            method=self._optimization_method(method),
            universe=universe,
            start=_optional_date(start),
            end=_optional_date(end),
            frequency=frequency,
            max_weights={instrument: Decimal(str(max_weight)) for instrument in universe},
            min_cash_weight=Decimal(str(min_cash_weight)),
            transaction_cost_bps={instrument: Decimal(str(transaction_cost_bps)) for instrument in universe},
            max_turnover=Decimal(max_turnover) if max_turnover else None,
            max_tracking_error=Decimal(max_tracking_error) if max_tracking_error else None,
            risk_aversion=Decimal(str(risk_aversion)),
            benchmark_id=benchmark_id or None,
            groups=_canonical_group_keys(groups),
            linear_constraints=linear_constraints or [],
        )
        return spec, benchmark

    def _chart_path(self, filename: str) -> Path:
        if self._artifacts_dir is None:
            raise ValueError("backtest chart requested but PortfolioOptimizationTools has no artifacts_dir")
        name = Path(filename).name
        if not name or name != filename or Path(name).suffix.lower() != ".png":
            raise ValueError("chart_filename must be a PNG basename")
        path = (self._artifacts_dir / name).resolve()
        if path.parent != self._artifacts_dir:
            raise ValueError("chart_filename resolves outside artifacts_dir")
        return path


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
        from datetime import UTC, datetime

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


def _run_summary(run: OptimizationRun) -> dict[str, object]:
    """Allow-list an optimization response before it enters model context."""
    return {
        "id": run.id,
        "as_of": run.as_of.isoformat(),
        "objective": run.objective,
        "status": run.status.value,
        "portfolio_id": run.portfolio_id,
        "target_weights": [target.model_dump(mode="json") for target in run.target_weights],
        "expected_return": str(run.expected_return) if run.expected_return is not None else None,
        "expected_risk": str(run.expected_risk) if run.expected_risk is not None else None,
        "solver": run.solver,
        "reason_codes": run.reason_codes,
    }


def _json(payload: object) -> str:
    return json.dumps(payload, default=str, sort_keys=True)


def _optional_date(value: str) -> date | None:
    return date.fromisoformat(value) if value else None


def _canonical_ticker_instruments(instruments: str) -> list[str]:
    requested = [item.strip() for item in instruments.split(",") if item.strip()]
    return [item if ":" in item else f"ticker:{item}" for item in requested]


def _canonical_group_keys(groups: dict[str, list[str]] | None) -> dict[str, list[str]]:
    return {
        instrument if ":" in instrument else f"ticker:{instrument}": labels
        for instrument, labels in (groups or {}).items()
    }
