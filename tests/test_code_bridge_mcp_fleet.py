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
                     "bridge_models", "capabilities_search", "bridge_events"}


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


@pytest.fixture
def events_tool(tmp_path, monkeypatch):
    """``bridge_events`` as the MCP server exposes it, on a temporary job store."""
    from lazytools.mcp_server.server import expand_tools

    db = tmp_path / "jobs.sqlite"
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_DB", str(db))
    monkeypatch.setenv("LAZYTOOLS_PROJECTS_STORE_DB", str(tmp_path / "projects.sqlite"))
    tool = expand_tools([PROVIDER_FACTORIES["fleet"]()], read_only=True)["bridge_events"]
    return db, tool


def _bridge_job(db, job_id: str, status: str):
    from lazytools.code_bridge import _store

    store = _store.build_store(db)
    _store.build_job_registry(store).write(job_id, "task", tool_name="codex", status=status)
    _store.write_meta(store, job_id, {"job_id": job_id, "engine": "codex", "where": "local", "cwd": "."})
    return store


def test_bridge_events_starts_from_now_and_keeps_events_between_calls(events_tool):
    db, tool = events_tool
    _bridge_job(db, "old-job", "done")
    first = tool.run_sync()
    assert first["events"] == [] and isinstance(first["cursor"], str)

    _bridge_job(db, "quick-job", "done")  # starts and finishes between two calls
    second = tool.run_sync(cursor=first["cursor"])
    assert [(e["kind"], e["job_id"]) for e in second["events"]] == [("done", "quick-job")]
    assert set(second["events"][0]) >= {"kind", "job_id", "task_id", "detail"}
    assert tool.run_sync(cursor=second["cursor"])["events"] == []


def test_bridge_events_reports_ticket_created_and_decided_between_calls(events_tool):
    from lazytools.code_bridge import _store

    db, tool = events_tool
    store = _bridge_job(db, "running-job", "running")
    cursor = tool.run_sync()["cursor"]
    queue = _store.build_approval_queue(store)
    ticket = queue.create_ticket(task_id="running-job", prompt="Allow git push?")
    queue.approve_ticket(ticket.approval_id, actor="owner", channel="cli")

    events = tool.run_sync(cursor=cursor)["events"]
    assert [(e["kind"], e["approval"]["approval_id"], e["approval"]["status"]) for e in events] == [
        ("approval", ticket.approval_id, "approved")
    ]


def test_bridge_events_waits_for_an_event(events_tool, monkeypatch):
    from lazytools.code_bridge import _mcp_fleet

    db, tool = events_tool
    cursor = tool.run_sync()["cursor"]
    sleeps = []

    def finish_job(seconds):
        sleeps.append(seconds)
        _bridge_job(db, "late-job", "failed")

    monkeypatch.setattr(_mcp_fleet.time, "sleep", finish_job)
    result = tool.run_sync(cursor=cursor, timeout_s=5)
    assert [(e["kind"], e["job_id"]) for e in result["events"]] == [("failed", "late-job")]
    assert len(sleeps) == 1


def test_bridge_events_times_out_empty(events_tool, monkeypatch):
    from lazytools.code_bridge import _mcp_fleet

    _db, tool = events_tool
    now = [0.0]
    sleeps = []
    monkeypatch.setattr(_mcp_fleet.time, "monotonic", lambda: now[0])

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(_mcp_fleet.time, "sleep", sleep)
    result = tool.run_sync(timeout_s=1)
    assert result["events"] == [] and result["cursor"]
    assert sleeps == [0.5, 0.5]


@pytest.mark.parametrize("kwargs", [
    {"cursor": "not-a-cursor"},
    {"cursor": "ev1.eJyrrgUAAXUA-Q.AAAAAAAAAAAAAAAA"},
    {"timeout_s": 61},
    {"timeout_s": -1},
    {"timeout_s": True},
    {"timeout_s": 1.5},
])
def test_bridge_events_refuses_malformed_input(events_tool, kwargs):
    _db, tool = events_tool
    with pytest.raises(ValueError):
        FleetTools().bridge_events(**kwargs)
    if kwargs.get("timeout_s") is True:
        return  # the server's argument schema coerces a JSON true to 1 before the call
    with pytest.raises(ValueError):  # the server refuses too, rather than resetting the cursor
        tool.run_sync(**kwargs)


def test_bridge_events_is_hidden_and_refused_in_a_task_scope(events_tool):
    from lazytools.connectors.code_support import _policy
    from lazytools.mcp_server.server import _scoped_tools, expand_tools

    _db, tool = events_tool
    tool_map = expand_tools([PROVIDER_FACTORIES["fleet"]()], read_only=True)
    assert "bridge_events" in tool_map
    assert "bridge_events" not in _scoped_tools(tool_map)
    token = _policy._task.set(object())  # type: ignore[arg-type]
    try:
        with pytest.raises(PermissionError):
            tool.run_sync()
    finally:
        _policy._task.reset(token)
