"""connectors/fin — LazyFin's agentic surface lives here (plan v3.1 Fase 5)."""

from __future__ import annotations

import pytest

lazyfin = pytest.importorskip("lazyfin", reason="fin connector requires lazyfin")

from lazytools.connectors.fin import (
    OptimizerTools,
    PortfolioTools,
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


def test_direct_provider_tools_are_gone() -> None:
    """Audit CA-03, final state (single user, no compatibility window): the
    direct-fetch ToolProviders are REMOVED, not just deprecated."""
    import lazytools.connectors.edgar as edgar
    import lazytools.connectors.fin as fin
    import lazytools.connectors.marketdata as marketdata

    assert not hasattr(edgar, "EdgarTools")
    assert not hasattr(marketdata, "MarketDataTools")
    assert not hasattr(fin, "ResolveTools")
