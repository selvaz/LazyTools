"""Cross-repo boundary: financial agent-assembly code must never construct a
direct provider client (EDGAR, market-data quotes) — market-data-hub is the
sole data owner (audit finding CA-03, plan v3.1 §5.2/Fase 5).

This is a STATIC guard: the finance agent factories (`connectors/fin/agents.py`
and anything that becomes a "financial bundle" assembly point) must not
import `connectors.edgar` / `connectors.marketdata`. The direct-fetch
ToolProviders (EdgarTools, MarketDataTools, ResolveTools) are REMOVED
outright; the transport clients remain as injectable plumbing for non-agent
code, but no bundled/"default" finance agent can silently bypass the hub.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

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
    pytest.importorskip("lazyfin")
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


def test_resolve_tools_is_removed() -> None:
    """Audit CA-03 final: ResolveTools fetched EDGAR directly and is removed
    outright (sole user, no compatibility release needed). The hub-backed
    datahub_* tools are the only financial resolution/facts surface."""
    __import__("pytest").importorskip("lazyfin")
    import lazytools.connectors.fin.tools as fin_tools

    assert not hasattr(fin_tools, "ResolveTools")
    assert "ResolveTools" not in getattr(fin_tools, "__all__", [])
