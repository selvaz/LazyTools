"""LazyBridge tool providers over LazyFin's pure kernel (moved from lazyfin).

Each provider wraps deterministic LazyFin functions with ``Tool.wrap`` so an
``Agent(tools=[PortfolioTools(ledger)])`` can call them. The computation stays
in lazyfin; only the LLM-facing wrapping lives here (plan v3.1, Fase 5 — the
same classes in ``lazyfin.kernel``/``lazyfin.scoring``/``lazyfin.resolve`` are
deprecated shims for one release).
"""

from __future__ import annotations

from collections.abc import Callable
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
    from lazyfin.resolve import (
        EdgarClientLike,
        Resolution,
        normalize_company_facts,
        resolve_security,
    )
    from lazyfin.resolve._resolver import _cik_for
    from lazyfin.scoring import score_security
except ImportError as exc:  # pragma: no cover - clear hint over bare failure
    raise ImportError(
        "lazytools.connectors.fin requires the lazyfin package: "
        "pip install 'lazyfin @ git+https://github.com/selvaz/LazyFin.git'"
    ) from exc

if TYPE_CHECKING:
    from datetime import datetime

__all__ = [
    "PortfolioTools",
    "RiskTools",
    "OptimizerTools",
    "ScoringTools",
    "ResolveTools",
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

    def _optimize_target_weights(
        self, scores: list[SecurityScore], as_of: datetime
    ) -> OptimizationRun:
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


class ResolveTools:
    """LazyBridge tool provider for resolution + fact retrieval.

    .. deprecated::
        ``get_financial_facts`` fetches RAW EDGAR company facts directly
        through the injected client, bypassing market-data-hub entirely — no
        DB, no coverage, no run ledger, no provenance (audit finding CA-03).
        Use ``datahub_resolve_instrument`` + ``datahub_get_financial_facts`` /
        ``datahub_ensure_financials`` (``connectors.datahub.DataHubTools``)
        instead. Kept for one compatibility release.

    ``get_financial_facts`` accepts canonical ids (``ticker:AAPL`` /
    ``cik:0000320193``) or a bare query, fetches the raw EDGAR company facts
    through the client and returns them normalised with provenance.
    """

    _is_lazy_tool_provider = True

    def __init__(
        self,
        client: EdgarClientLike,
        *,
        tool_version: str | None = None,
        concepts: Callable[[], list[str] | None] | list[str] | None = None,
    ) -> None:
        import warnings

        warnings.warn(
            "ResolveTools.get_financial_facts fetches EDGAR directly and "
            "bypasses market-data-hub (no DB, coverage, run ledger or "
            "provenance): use lazytools.connectors.datahub.DataHubTools "
            "(datahub_resolve_instrument + datahub_get_financial_facts / "
            "datahub_ensure_financials) instead. Removal after one "
            "compatibility release.",
            DeprecationWarning,
            stacklevel=2,
        )
        self._client = client
        self._tool_version = tool_version
        self._concepts = concepts

    def _resolve_security(self, query: str) -> Resolution:
        return resolve_security(query, self._client)

    def _get_financial_facts(self, security_id: str) -> list[FinancialFact]:
        cik = _cik_for(security_id, self._client)
        concepts = self._concepts() if callable(self._concepts) else self._concepts
        return normalize_company_facts(
            self._client.company_facts(cik),
            concepts=concepts,
            tool_version=self._tool_version,
        )

    def as_tools(self) -> list[Tool]:
        return [
            Tool.wrap(
                self._resolve_security,
                name="resolve_security",
                description=(
                    "Resolve a ticker, CIK or company name to the canonical "
                    "Company + Security pair via SEC EDGAR."
                ),
            ),
            Tool.wrap(
                self._get_financial_facts,
                name="get_financial_facts",
                description=(
                    "Fetch and normalise SEC EDGAR XBRL company facts for a security "
                    "(canonical id or query) into FinancialFact records with provenance."
                ),
            ),
        ]

