from __future__ import annotations

import json
from typing import Any

import pytest

from lazytools.code_bridge._mcp import QUERY_TIMEOUT
from lazytools.code_bridge._mcp_fleet import FleetTools
from lazytools.mcp_server.providers import PROVIDER_FACTORIES


def _fake(tools: FleetTools, payload: Any = None) -> list[list[str]]:
    calls: list[list[str]] = []

    def invoke(argv: list[str], timeout: float) -> tuple[int, str, str]:
        assert timeout == QUERY_TIMEOUT
        calls.append(argv)
        return 0, json.dumps({"value": 1} if payload is None else payload) + "\n", ""

    tools._invoke = invoke  # type: ignore[method-assign]
    return calls


def test_jobs_and_pending_build_cli_argv_and_return_json() -> None:
    tools = FleetTools()
    calls = _fake(tools, [{"job_id": "job-1"}])
    assert tools.bridge_jobs() == [{"job_id": "job-1"}]
    assert tools.bridge_jobs(all=True) == [{"job_id": "job-1"}]
    assert tools.bridge_pending() == [{"job_id": "job-1"}]
    assert calls == [["jobs", "--json"], ["jobs", "--all", "--json"], ["pending", "--json"]]


def test_route_builds_cli_argv_and_returns_json() -> None:
    tools = FleetTools()
    calls = _fake(tools)
    assert tools.bridge_route("thinking", engine="codex", where="local", needs="images") == {"value": 1}
    assert calls == [["route", "--tier", "thinking", "--cwd", tools._root, "--root", tools._root,
                      "--engine", "codex", "--where", "local", "--needs", "images", "--json"]]


def test_autonomy_builds_each_supported_range() -> None:
    tools = FleetTools()
    calls = _fake(tools)
    tools.bridge_autonomy(week="2026-W09")
    tools.bridge_autonomy(since="2026-01-01", until="2026-02-01")
    assert calls == [["autonomy", "--week", "2026-W09", "--json"],
                     ["autonomy", "--since", "2026-01-01", "--until", "2026-02-01", "--json"]]


def test_models_never_enables_claude_probe() -> None:
    tools = FleetTools()
    calls = _fake(tools)
    assert tools.bridge_models() == {"value": 1}
    assert calls == [["models", "--json"]]


def test_capabilities_parses_cli_rows() -> None:
    tools = FleetTools()
    calls: list[list[str]] = []
    tools._invoke = lambda argv, timeout: (calls.append(argv) or 0,  # type: ignore[method-assign]
                                           "Section\tCapability\tcommand --flag\ttests-only\n", "")
    assert tools.capabilities_search("Capability") == [{
        "section": "Section", "capability": "Capability", "call": "command --flag", "status": "tests-only",
    }]
    assert calls == [["capabilities", "Capability"]]


@pytest.mark.parametrize(("method", "kwargs"), [
    ("bridge_jobs", {"all": "yes"}),
    ("bridge_route", {"tier": "unknown"}),
    ("bridge_route", {"tier": "basic", "engine": "unknown"}),
    ("bridge_route", {"tier": "basic", "where": "unknown"}),
    ("bridge_route", {"tier": "basic", "needs": "text"}),
    ("bridge_autonomy", {"week": "2026-W54"}),
    ("bridge_autonomy", {"since": "2026-1-01"}),
    ("bridge_autonomy", {"week": "2026-W01", "until": "2026-01-02"}),
    ("capabilities_search", {"query": "--help"}),
])
def test_invalid_arguments_are_rejected(method: str, kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        getattr(FleetTools(), method)(**kwargs)


def test_fleet_provider_is_registered_read_only() -> None:
    provider = PROVIDER_FACTORIES["fleet"](False, data_source={"code_root": "."})
    assert isinstance(provider, FleetTools)


def test_fleet_exposes_exactly_its_six_read_tools_without_unsafe():
    # Codex review on #408: FleetTools inherited CodeBridgeTools.as_tools and exposed nothing read-only.
    from lazytools.mcp_server.providers import PROVIDER_FACTORIES
    from lazytools.mcp_server.server import expand_tools

    names = set(expand_tools([PROVIDER_FACTORIES["fleet"]()], read_only=True))
    assert names == {"bridge_jobs", "bridge_pending", "bridge_route", "bridge_autonomy",
                     "bridge_models", "capabilities_search"}


def test_bridge_jobs_empty_output_is_an_empty_fleet(monkeypatch):
    from lazytools.code_bridge._mcp_fleet import FleetTools

    tools = FleetTools()
    monkeypatch.setattr(tools, "_invoke", lambda argv, timeout: (0, "", ""))
    assert tools.bridge_jobs() == []


def test_fleet_tools_are_never_exposed_or_callable_in_a_task_scope(monkeypatch):
    # Codex review on #408: fleet-wide job and ticket views must not leak across task scopes.
    import pytest

    from lazytools.code_bridge import _mcp_fleet
    from lazytools.connectors.code_support import _policy
    from lazytools.mcp_server.providers import PROVIDER_FACTORIES
    from lazytools.mcp_server.server import _scoped_tools, expand_tools

    tool_map = expand_tools([PROVIDER_FACTORIES["fleet"]()], read_only=True)
    assert _scoped_tools(tool_map) == {}

    tools = _mcp_fleet.FleetTools()
    monkeypatch.setattr(tools, "_invoke", lambda argv, timeout: (0, "", ""))
    call = _mcp_fleet._unscoped(tools.bridge_jobs)
    token = _policy._task.set(object())  # type: ignore[arg-type]
    try:
        with pytest.raises(PermissionError):
            call()
    finally:
        _policy._task.reset(token)
    assert call() == []


def test_bridge_route_describes_the_configured_root(monkeypatch, tmp_path):
    from lazytools.code_bridge._mcp_fleet import FleetTools

    seen = []
    tools = FleetTools(root=str(tmp_path))
    monkeypatch.setattr(tools, "_invoke", lambda argv, timeout: seen.append(argv) or (0, "{}", ""))
    tools.bridge_route("writing", engine="codex", where="cloud")
    argv = seen[0]
    assert argv[argv.index("--cwd") + 1] == str(tmp_path.resolve())
    assert argv[argv.index("--root") + 1] == str(tmp_path.resolve())
