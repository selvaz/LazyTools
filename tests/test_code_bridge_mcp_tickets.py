"""Ticket decisions and job cancellation through the ``fleet`` MCP provider (SPEC R3)."""

from __future__ import annotations

import asyncio
import contextlib
import io
from pathlib import Path
from typing import Any

import pytest

from lazytools.code_bridge import _coder, _jobs, _lockfile, _store, cli
from lazytools.code_bridge._mcp_fleet import FleetTools
from lazytools.mcp_server.catalog import tool_metadata
from lazytools.mcp_server.providers import PROVIDER_FACTORIES
from lazytools.mcp_server.server import _scoped_tools, expand_tools
from test_code_bridge_autonomy import autonomy_registry, channel_for, configure  # noqa: F401
from test_code_bridge_cancel import job_record
from test_code_bridge_coder_cards import request

WRITE_TOOLS = {"bridge_approve", "bridge_reject", "bridge_cancel"}


def _tools(*, allow_write: bool) -> dict[str, Any]:
    return expand_tools([PROVIDER_FACTORIES["fleet"](allow_write)], read_only=not allow_write)


def _run(tools: dict[str, Any], name: str, **kwargs: Any) -> Any:
    return asyncio.run(tools[name].run(**kwargs))


def _route_to_cli(monkeypatch: pytest.MonkeyPatch, *extra: str) -> list[list[str]]:
    """Run the bridge CLI in-process against the test stores instead of a child process."""
    calls: list[list[str]] = []

    def invoke(self: FleetTools, argv: list[str], timeout: float) -> tuple[int, str, str]:
        calls.append(argv)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([*argv, *extra])
        return code, out.getvalue(), err.getvalue()

    monkeypatch.setattr(FleetTools, "_invoke", invoke)
    return calls


@pytest.fixture
def tickets(autonomy_registry, monkeypatch):  # noqa: F811
    """Pending tickets of three classes from one job, and the tools wired to its stores."""
    cwd, registry = autonomy_registry
    assert configure(registry, "supervised", "--preset", "with-production") == 0
    # Network off: the job's own push and merge cards cannot apply, so each request waits for a decision.
    channel = channel_for(registry, cwd, "ceo-job", network="off")
    held = {}
    for kind, command in [("git_push_job_branch", "git push origin feature/work"),
                          ("gh_pr_merge", "gh pr merge 17 --squash"),
                          ("production_task", "schtasks /Run /TN Daily")]:
        ticket, _ = channel._submit(request(cwd, command), command)
        assert ticket.status == "pending" and ticket.request.payload["ticket_class"] == kind
        held[kind] = ticket.approval_id
    calls = _route_to_cli(monkeypatch, *_stores(cwd, registry))
    return registry, held, calls, _tools(allow_write=True)


def _stores(cwd: Path, registry: Any) -> list[str]:
    # The autonomy fixture keeps its job store beside the checkout.
    return ["--db", str(cwd.parent / "jobs.sqlite"), "--registry-db", str(registry.path)]


def test_write_tools_exist_only_with_allow_write() -> None:
    read = set(_tools(allow_write=False))
    assert not read & WRITE_TOOLS
    assert set(_tools(allow_write=True)) == read | WRITE_TOOLS
    # A read-only server drops them even from a write-enabled provider, and the catalog calls them writes.
    assert not set(expand_tools([FleetTools(allow_write=True)], read_only=True)) & WRITE_TOOLS
    assert {tool_metadata("fleet", name)["access"] for name in WRITE_TOOLS} == {"write"}


def test_task_scoped_servers_never_expose_the_write_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    from lazytools.connectors.code_support import _policy

    calls = _route_to_cli(monkeypatch)
    tools = _tools(allow_write=True)
    assert _scoped_tools(tools) == {}
    token = _policy._task.set(object())  # type: ignore[arg-type]
    try:
        for name in WRITE_TOOLS:
            with pytest.raises(PermissionError):
                tools[name].func("ticket-1", "reason")
    finally:
        _policy._task.reset(token)
    assert calls == []


@pytest.mark.parametrize(("name", "args"), [
    ("bridge_approve", ("--help", "reason")),
    ("bridge_approve", ("ab", "reason")),
    ("bridge_approve", ("ticket-1", "")),
    ("bridge_approve", ("ticket-1", "   ")),
    ("bridge_approve", ("ticket-1", "x" * 2001)),
    ("bridge_reject", ("ticket 1", "reason")),
    ("bridge_reject", ("ticket-1", None)),
    ("bridge_cancel", ("-job", "reason")),
    ("bridge_cancel", ("job/1", "reason")),
    ("bridge_cancel", ("job-1", "")),
])
def test_invalid_arguments_are_refused_before_the_cli_runs(
    monkeypatch: pytest.MonkeyPatch, name: str, args: tuple[Any, ...]
) -> None:
    calls = _route_to_cli(monkeypatch)
    with pytest.raises(ValueError):
        getattr(FleetTools(allow_write=True), name)(*args)
    assert calls == []


def test_tools_build_the_cli_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(FleetTools, "_invoke", lambda self, argv, timeout: (calls.append(argv) or 0, "done\n", ""))
    tools = FleetTools(allow_write=True)
    assert tools.bridge_approve("ticket-1", "-looks fine") == {"ok": True, "exit_code": 0, "output": "done"}
    tools.bridge_reject("ticket-1", "not now")
    tools.bridge_cancel("job-1", "stuck")
    assert calls == [["approve", "ticket-1", "--ceo", "--reason=-looks fine"],
                     ["reject", "ticket-1", "--ceo", "--reason=not now"],
                     ["cancel", "job-1", "--reason=actor=ceo: stuck"]]


def test_supervised_mode_approves_only_delegated_classes(tickets) -> None:
    registry, held, _, tools = tickets
    result = _run(tools, "bridge_approve", ticket=held["git_push_job_branch"], reason="pushed after review")
    assert result["ok"], result
    ticket = registry.inspect(held["git_push_job_branch"])
    assert ticket.status == "approved"
    assert ticket.reason == "actor=ceo mode=supervised class=git_push_job_branch: pushed after review"

    for kind in ("gh_pr_merge", "production_task"):
        result = _run(tools, "bridge_approve", ticket=held[kind], reason="ship it")
        assert not result["ok"] and "owner decision required" in result["error"]
        assert f"class {kind} is not delegated to the CEO in supervised mode" in result["error"]
        assert registry.inspect(held[kind]).status == "pending"


def test_total_mode_approves_outside_the_owner_floor(tickets) -> None:
    registry, held, _, tools = tickets
    assert configure(registry, "total") == 0
    result = _run(tools, "bridge_approve", ticket=held["gh_pr_merge"], reason="checks green")
    assert result["ok"], result
    assert registry.inspect(held["gh_pr_merge"]).reason == "actor=ceo mode=total class=gh_pr_merge: checks green"

    result = _run(tools, "bridge_approve", ticket=held["production_task"], reason="ship it")
    assert not result["ok"] and "owner decision required" in result["error"]
    assert registry.inspect(held["production_task"]).status == "pending"


def test_reject_is_always_allowed_and_recorded(tickets) -> None:
    registry, held, _, tools = tickets
    result = _run(tools, "bridge_reject", ticket=held["production_task"], reason="owner-only work")
    assert result["ok"], result
    ticket = registry.inspect(held["production_task"])
    assert ticket.status == "rejected"
    assert ticket.reason == "actor=ceo mode=supervised class=production_task: owner-only work"
    # The decision is also a manual intervention on the job, with the same record.
    intervention = _store.read_meta(registry.job_store, "ceo-job")["manual_interventions"][-1]
    assert intervention["kind"] == "reject" and intervention["reason"] == ticket.reason


def test_only_an_owner_configured_mode_delegates(autonomy_registry) -> None:  # noqa: F811
    _, registry = autonomy_registry
    # The owner credential exists, but no mode is configured, then only the original coder hand.
    assert _coder.ceo_may_approve(registry.path, "git_push_job_branch") == ("supervised", False)
    assert _coder.provision(registry.path)
    assert _coder.ceo_may_approve(registry.path, "git_push_job_branch") == ("supervised", False)
    assert configure(registry, "supervised") == 0
    assert _coder.ceo_may_approve(registry.path, "git_push_job_branch") == ("supervised", True)
    assert _coder.ceo_may_approve(registry.path, "unknown") == ("supervised", False)


def test_cancel_interrupts_the_job_and_records_the_ceo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db, _, store, registry = job_record(tmp_path)
    monkeypatch.setattr(_lockfile, "_pid_alive", lambda pid: False)  # the worker already exited
    _route_to_cli(monkeypatch, "--db", str(db))
    result = _run(_tools(allow_write=True), "bridge_cancel", job="cancel-this", reason="stuck on a prompt")
    assert result["ok"], result
    row = _jobs.find_job(store, registry, "cancel-this-job")
    assert row["status"] == "interrupted"
    assert row["error"].startswith("cancelled by operator: actor=ceo: stuck on a prompt")
