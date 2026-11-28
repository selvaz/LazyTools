from __future__ import annotations

import argparse
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from lazytools.code_bridge import _dispatch, _mcp_dispatch
from lazytools.mcp_server.server import expand_tools


def test_status_uses_board_jobs_tickets_and_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_DB", str(tmp_path / "bridge.sqlite"))
    monkeypatch.setattr(_dispatch, "_tasks", lambda service, project: [
        _dispatch.Task(f"{project}/a", "0", "todo", {}, None),
        _dispatch.Task(f"{project}/b", "0", "running", {}, None),
    ])
    monkeypatch.setattr(_mcp_dispatch._jobs, "list_jobs", lambda *a, **k: [
        {"job_id": "one", "task_id": "p/b", "status": "running"},
        {"job_id": "two", "task_id": "other/b", "status": "running"},
    ])
    monkeypatch.setattr(_mcp_dispatch._dispatch_launch.BridgeLauncher, "pending", lambda self: [
        {"job_id": "one", "approval_id": "ticket"}, {"job_id": "two", "approval_id": "other"},
    ])
    tools = _mcp_dispatch.DispatchTools(projects_db=str(tmp_path / "projects.db"))
    tools._log("p").write_text('{"kind":"ready"}\n{"kind":"launched"}\n', encoding="utf-8")
    status = tools.dispatch_status("p", last_n=1)
    assert status["tasks_by_status"] == {"todo": 1, "running": 1}
    assert [j["job_id"] for j in status["running_jobs"]] == ["one"]
    assert [t["approval_id"] for t in status["pending_tickets"]] == ["ticket"]
    assert status["events"] == [{"kind": "launched"}]
    assert not status["running"]


def test_write_gate_and_live_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_DB", str(tmp_path / "bridge.sqlite"))
    tools = _mcp_dispatch.DispatchTools(projects_db=str(tmp_path / "projects.db"))
    assert set(expand_tools([tools], read_only=True)) == {"dispatch_status"}
    with pytest.raises(PermissionError):
        tools.dispatch_start("p")
    lock = tools._lock("p")
    import socket
    lock.acquire("owner", metadata={"host": socket.gethostname().casefold(),
                                    "started_at": _dispatch._lockfile.process_start_time(os.getpid()),
                                    "instance_id": "owner"})
    try:
        assert not _mcp_dispatch.DispatchTools(allow_write=True, projects_db=tools.projects_db).dispatch_start("p")["ok"]
    finally:
        lock.release()


def test_start_detaches_and_stop_requests_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_DB", str(tmp_path / "bridge.sqlite"))
    tools = _mcp_dispatch.DispatchTools(allow_write=True, projects_db=str(tmp_path / "projects.db"))
    calls = []
    monkeypatch.setattr(_mcp_dispatch.cli, "_spawn_detached", lambda argv, path, **kw: calls.append((argv, path, kw)) or 123)
    result = tools.dispatch_start("p", {"repo": "checkout"}, "worktrees", True)
    assert result["pid"] == 123
    assert calls[0][0][3:] == ["dispatch", "--project", "p", "--supervise", "--json", "--repo-root",
                              "repo=checkout", "--worktree-root", "worktrees", "--land"]
    owner = {"pid": 123, "instance_id": "abc"}
    owners = iter([owner, None])
    monkeypatch.setattr(_dispatch, "live_owner", lambda lock: next(owners))
    tools._lock("p").lock_dir.mkdir(parents=True, exist_ok=True)
    assert tools.dispatch_stop("p")["running"] is False
    assert _dispatch.stop_path(tools._lock("p")).read_text() == "abc"


def test_supervise_restarts_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    class FakeDispatcher:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.sleep = lambda seconds: calls.append(seconds)
        def run(self, **kwargs: object) -> int:
            calls.append("run")
            if calls.count("run") == 1:
                raise RuntimeError("crash")
            return 0
        def _event(self, *args: object) -> None:
            calls.append(args)
    monkeypatch.setattr(_dispatch, "Dispatcher", FakeDispatcher)
    monkeypatch.setattr(_dispatch, "_routers", lambda *a: (None, None))
    monkeypatch.setattr(_dispatch._concurrency, "load_config", lambda: SimpleNamespace(limits={}))
    args = argparse.Namespace(project="p", dry_run=False, repo_root=[], worktree_root=None, db=None,
                              parallel=None, once=False, supervise=True)
    assert _dispatch._dispatch_main(args) == 0
    assert calls[0] == "run" and calls[1][0] == "restart" and calls[2:] == [5.0, "run"]
