"""Cross-repo boundary: financial agent-assembly code must never construct a
direct provider client (EDGAR, market-data quotes) — market-data-hub is the
sole data owner (audit finding CA-03, plan v3.1 §5.2/Fase 5).

This is a STATIC guard: the finance agent factories (`connectors/fin/agents.py`
and anything that becomes a "financial bundle" assembly point) must not
import `connectors.edgar` / `connectors.marketdata`, and must not construct
`ResolveTools` by default. It does not (and cannot) forbid a caller from
manually wiring a deprecated provider in — that path already emits a
DeprecationWarning at construction — but it guarantees no bundled/"default"
finance agent silently bypasses the hub.
"""

from __future__ import annotations

import ast
import warnings
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "lazytools"
FIN_AGENT_ASSEMBLY_FILES = [SRC / "connectors" / "fin" / "agents.py"]
FORBIDDEN_MODULES = (
    "lazytools.connectors.edgar",
    "lazytools.connectors.marketdata",
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module)
    return mods


def test_finance_agent_factories_never_import_direct_providers() -> None:
    offenders = []
    for path in FIN_AGENT_ASSEMBLY_FILES:
        for mod in _imported_modules(path):
            if mod in FORBIDDEN_MODULES or mod.startswith(
                tuple(m + "." for m in FORBIDDEN_MODULES)
            ):
                offenders.append(f"{path.name}: {mod}")
    assert not offenders, (
        f"finance agent assembly imports a direct-fetch provider, bypassing "
        f"market-data-hub: {offenders}"
    )


def test_pm_supervisor_defaults_never_include_a_direct_provider() -> None:
    """pm_supervisor's own default tool list (PortfolioTools + RiskTools) must
    never grow to include EdgarTools/MarketDataTools/ResolveTools without a
    caller explicitly opting in via extra_tools."""
    pytest_lazyfin = __import__("pytest").importorskip("lazyfin")
    del pytest_lazyfin
    from lazyfin.kernel import Mandate, PortfolioLedger

    from lazytools.connectors.fin.agents import pm_supervisor

    class _NullEngine:
        async def run(self, *a, **k):  # pragma: no cover - never invoked
            raise NotImplementedError

    agent = pm_supervisor(
        _NullEngine(), ledger=PortfolioLedger(),
        mandate=Mandate(id="m:test", base_currency="USD"),
    )
    # Agent doesn't expose the raw provider list publicly; _tools_raw is the
    # pre-expansion input (what pm_supervisor actually passed in), which is
    # exactly what this guard needs to inspect.
    tool_types = {type(t).__name__ for t in agent._tools_raw}
    assert tool_types <= {"PortfolioTools", "RiskTools", "Tool"}
    assert "ResolveTools" not in tool_types
    assert "EdgarTools" not in tool_types
    assert "MarketDataTools" not in tool_types


def test_resolve_tools_is_deprecated() -> None:
    """ResolveTools remains importable for one compatibility release, but
    must warn at construction (audit CA-03: it bypasses the hub)."""
    from lazytools.connectors.fin.tools import ResolveTools

    class _FakeClient:
        def resolve(self, query):  # pragma: no cover - shape only
            raise NotImplementedError

        def company_facts(self, cik):  # pragma: no cover - shape only
            raise NotImplementedError

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ResolveTools(_FakeClient())
    assert any(
        issubclass(w.category, DeprecationWarning) and "datahub" in str(w.message)
        for w in caught
    )
