"""connectors/fin — LazyFin's agentic surface lives here (plan v3.1 Fase 5)."""

from __future__ import annotations

import pytest

lazyfin = pytest.importorskip("lazyfin", reason="fin connector requires lazyfin")

from lazytools.connectors.fin import (
    OptimizerTools,
    PortfolioTools,
    ResolveTools,
    RiskTools,
    ScoringTools,
    pm_supervisor,
)


def _ledger_and_mandate():
    from lazyfin.kernel import Mandate, PortfolioLedger

    return PortfolioLedger(), Mandate(id="m:test", base_currency="USD")


def test_providers_expose_expected_tool_names() -> None:
    ledger, mandate = _ledger_and_mandate()
    from lazyfin.kernel import OptimizationConstraints

    cases = {
        PortfolioTools(ledger): {
            "load_portfolio", "compute_exposure", "compute_concentration",
            "compute_drift"},
        RiskTools(mandate): {"run_risk_checks"},
        OptimizerTools(OptimizationConstraints()): {"optimize_target_weights"},
        ScoringTools(lambda sid: []): {"score_security"},
    }
    for provider, expected in cases.items():
        assert provider._is_lazy_tool_provider is True
        assert {t.name for t in provider.as_tools()} == expected


def test_resolve_tools_requires_only_a_client_protocol() -> None:
    class FakeEdgar:
        def resolve(self, query):  # pragma: no cover - shape only
            raise NotImplementedError

        def company_facts(self, cik):  # pragma: no cover - shape only
            raise NotImplementedError

    provider = ResolveTools(FakeEdgar())
    assert {t.name for t in provider.as_tools()} == {
        "resolve_security", "get_financial_facts"}


def test_pm_supervisor_builds_agent_with_kernel_tools() -> None:
    from lazybridge import Agent

    ledger, mandate = _ledger_and_mandate()

    class NullEngine:
        async def run(self, *a, **k):  # pragma: no cover - never invoked
            raise NotImplementedError

    agent = pm_supervisor(NullEngine(), ledger=ledger, mandate=mandate)
    assert isinstance(agent, Agent)


def test_lazyfin_shims_still_work_but_warn() -> None:
    """One-release compatibility: the old lazyfin classes keep working but
    emit a DeprecationWarning pointing here."""
    ledger, _ = _ledger_and_mandate()
    from lazyfin.kernel import PortfolioTools as OldPortfolioTools

    with pytest.warns(DeprecationWarning, match="lazytools.connectors.fin"):
        old = OldPortfolioTools(ledger)
    assert {t.name for t in old.as_tools()} == {
        "load_portfolio", "compute_exposure", "compute_concentration",
        "compute_drift"}


def test_marketdata_and_edgar_tools_are_deprecated() -> None:
    from lazytools.connectors.edgar import EdgarTools
    from lazytools.connectors.marketdata import MarketDataTools

    class FakeClient:
        pass

    with pytest.warns(DeprecationWarning, match="DataHubTools"):
        MarketDataTools(FakeClient())
    with pytest.warns(DeprecationWarning, match="DataHubTools"):
        EdgarTools(FakeClient())

