"""The ``pr`` MCP provider (SPEC R5)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from lazybridge.policy import PolicyDenied

from lazytools.code_bridge import _gh, _jobs, _store, _watch
from lazytools.code_bridge._mcp import SUBMIT_TIMEOUT
from lazytools.code_bridge._mcp_pr import NAVIGATE_ROUND_TIMEOUT, PrTools
from lazytools.mcp_server.providers import PROVIDER_FACTORIES
from lazytools.mcp_server.server import _scoped_tools, expand_tools

WRITE_TOOLS = {"pr_navigate", "bridge_publish", "bridge_apply"}
HEAD = "a" * 40


@pytest.fixture(autouse=True)
def _bridge_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_DB", str(tmp_path / "bridge.sqlite"))


def _tools(tmp_path: Path, *, allow_write: bool) -> dict[str, Any]:
    provider = PROVIDER_FACTORIES["pr"](allow_write, data_source={"code_root": str(tmp_path)})
    return expand_tools([provider], read_only=not allow_write)


def _run(tool: Any, **kwargs: Any) -> Any:
    return asyncio.run(tool.run(**kwargs))


def _cli(monkeypatch: pytest.MonkeyPatch, code: int = 0, out: str = '{"value": 1}\n', err: str = "") -> list[Any]:
    calls: list[Any] = []

    def invoke(self: PrTools, argv: list[str], timeout: float) -> tuple[int, str, str]:
        calls.append((argv, timeout))
        return code, out, err

    monkeypatch.setattr(PrTools, "_invoke", invoke)
    return calls


def _github(
    monkeypatch: pytest.MonkeyPatch, comments: list[dict[str, Any]], *, broken: int | None = None
) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    def run_gh(*args: str, **kwargs: Any) -> str:
        calls.append(args)
        path = args[1]
        if broken is not None and f"/pulls/{broken}" in path:
            raise RuntimeError("gh api exited 1: not found")
        if path.rsplit("/", 2)[-2] == "pulls":
            return json.dumps({"head": {"sha": HEAD}, "state": "open", "mergeable": True})
        return json.dumps([comments] if "/issues/" in path else [])

    monkeypatch.setattr(_gh, "run_gh", run_gh)
    return calls


def _comment(number: int) -> dict[str, Any]:
    return {"id": number, "user": {"login": "alice"}, "body": "hi", "created_at": f"2026-01-0{number}T00:00:00Z"}


def test_write_tools_exist_only_with_allow_write(tmp_path: Path) -> None:
    assert set(_tools(tmp_path, allow_write=False)) == {"pr_watch_poll"}
    assert set(_tools(tmp_path, allow_write=True)) == {"pr_watch_poll"} | WRITE_TOOLS


def test_read_only_server_drops_writers_of_a_write_enabled_provider(tmp_path: Path) -> None:
    provider = PrTools(str(tmp_path), allow_write=True)
    assert set(expand_tools([provider], read_only=True)) == {"pr_watch_poll"}


def test_task_scoped_servers_expose_no_pr_tool(tmp_path: Path) -> None:
    assert _scoped_tools(_tools(tmp_path, allow_write=True)) == {}


def test_tools_refuse_inside_a_task_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lazytools.connectors.code_support import _policy

    calls = _cli(monkeypatch)
    tools = _tools(tmp_path, allow_write=True)
    token = _policy._task.set(object())  # type: ignore[arg-type]
    try:
        for name in tools:
            with pytest.raises(PermissionError):
                tools[name].func(**{"pulls": ["o/r#1"]} if name == "pr_watch_poll" else {})
    finally:
        _policy._task.reset(token)
    assert calls == []


def test_poll_does_not_repeat_events_and_shares_the_watcher_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comments = [_comment(1)]
    calls = _github(monkeypatch, comments)
    poll = _tools(tmp_path, allow_write=False)["pr_watch_poll"]

    first = _run(poll, pulls=["owner/repo#7"])
    assert first["ok"] and [e["kind"] for e in first["events"]] == ["comment"]
    assert _run(poll, pulls=["owner/repo#7"])["events"] == []

    comments.append(_comment(2))
    third = _run(poll, pulls=["owner/repo#7"])
    assert [e["time"] for e in third["events"]] == ["2026-01-02T00:00:00Z"]
    # The same Store key the CLI watcher reads.
    state = _store.build_store().read(f"{_watch.WATCH_PREFIX}owner/repo#7")
    assert state["seen"] == ["comment:1", "comment:2"]
    assert all(call[0] == "api" and "-X" not in call and "-f" not in call for call in calls)


def test_poll_without_ci_reports_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    receipt = {"id": 1, "user": {"login": "chatgpt-codex-connector[bot]"},
               "body": f"Didn't find any major issues. **Reviewed commit:** `{HEAD[:10]}`"}
    _github(monkeypatch, [receipt])
    monkeypatch.delenv("LAZYTOOLS_CODE_BRIDGE_REVIEW_BOTS", raising=False)
    poll = _tools(tmp_path, allow_write=False)["pr_watch_poll"]

    events = _run(poll, pulls=["owner/repo#7"], no_ci=True)["events"]

    assert [e["detail"] for e in events if e["kind"] == "ready"] == [f"{HEAD[:7]} checks=none review=clean"]


def test_one_failing_pull_keeps_the_events_of_the_others(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _github(monkeypatch, [_comment(1)], broken=8)
    poll = _tools(tmp_path, allow_write=False)["pr_watch_poll"]

    result = _run(poll, pulls=["owner/repo#7", "owner/repo#8"])

    assert not result["ok"]
    assert [e["pull"] for e in result["events"]] == ["owner/repo#7"]
    assert [e["pull"] for e in result["errors"]] == ["owner/repo#8"]


def test_write_tools_build_cli_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _cli(monkeypatch)
    tools = _tools(tmp_path, allow_write=True)

    result = _run(tools["pr_navigate"], repo="owner/repo", pr=7, reviewer_cmd="review --json",
                  checks=["pytest -q", "ruff check"])

    assert result == {"ok": True, "exit_code": 0, "value": 1}
    assert calls == [(["navigate", "--repo", "owner/repo", "--pr", "7", "--reviewer-cmd", "review --json",
                       "--checks", "pytest -q", "--checks", "ruff check", "--max-rounds", "3"],
                      3 * NAVIGATE_ROUND_TIMEOUT)]


def test_publish_returns_the_cli_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _cli(monkeypatch, code=1, out="approval t-1 owner/repo topic abc\n")
    publish = _tools(tmp_path, allow_write=True)["bridge_publish"]

    result = _run(publish, task="project/task", repo=".", branch="topic", base="main")

    assert result == {"ok": False, "exit_code": 1, "output": "approval t-1 owner/repo topic abc"}
    assert calls == [(["publish", "--task", "project/task", "--repo", str(tmp_path.resolve()),
                       "--branch", "topic", "--base", "main"], SUBMIT_TIMEOUT)]


def test_apply_resolves_the_job_inside_the_code_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _cli(monkeypatch)
    apply = _tools(tmp_path, allow_write=True)["bridge_apply"]
    assert _run(apply, job="abcd")["error"] == "no job found matching 'abcd'"

    _store.build_store().write("present", 1)
    record = {"job_id": "abcd-1234", "cwd": str(tmp_path)}
    monkeypatch.setattr(_jobs, "find_job", lambda store, registry, ref: record)
    _run(apply, job="abcd")
    _run(apply, job="abcd", land=True)
    assert [argv for argv, _ in calls] == [["apply", "abcd-1234", "--json"], ["apply", "abcd-1234", "--land", "--json"]]

    record["cwd"] = str(tmp_path.parent)
    with pytest.raises(PolicyDenied):
        _run(apply, job="abcd")
    assert len(calls) == 2


NAVIGATE = {"repo": "owner/repo", "pr": 7, "reviewer_cmd": "review", "checks": ["pytest"]}
PUBLISH = {"task": "project/task", "repo": ".", "branch": "topic", "base": "main"}


@pytest.mark.parametrize(("method", "kwargs"), [
    ("pr_watch_poll", {"pulls": []}),
    ("pr_watch_poll", {"pulls": "owner/repo#1"}),
    ("pr_watch_poll", {"pulls": ["owner/repo"]}),
    ("pr_watch_poll", {"pulls": ["-owner/repo#1"]}),
    ("pr_watch_poll", {"pulls": ["owner/..#1"]}),
    ("pr_watch_poll", {"pulls": ["owner/repo#1", "owner/re?po#2"]}),
    ("pr_watch_poll", {"pulls": ["owner/repo#1"], "no_ci": "yes"}),
    ("pr_navigate", {**NAVIGATE, "repo": "--help"}),
    ("pr_navigate", {**NAVIGATE, "repo": "owner"}),
    ("pr_navigate", {**NAVIGATE, "pr": 0}),
    ("pr_navigate", {**NAVIGATE, "pr": True}),
    ("pr_navigate", {**NAVIGATE, "reviewer_cmd": "-x"}),
    ("pr_navigate", {**NAVIGATE, "checks": []}),
    ("pr_navigate", {**NAVIGATE, "checks": ["pytest", "--db=x"]}),
    ("pr_navigate", {**NAVIGATE, "max_rounds": 0}),
    ("pr_navigate", {**NAVIGATE, "max_rounds": 11}),
    ("bridge_publish", {**PUBLISH, "task": "--ticket=x"}),
    ("bridge_publish", {**PUBLISH, "repo": "-r"}),
    ("bridge_publish", {**PUBLISH, "repo": ".."}),
    ("bridge_publish", {**PUBLISH, "branch": ""}),
    ("bridge_publish", {**PUBLISH, "base": "-main"}),
    ("bridge_apply", {"job": "--land"}),
    ("bridge_apply", {"job": "../x"}),
    ("bridge_apply", {"job": "abcd", "land": "yes"}),
])
def test_invalid_arguments_are_rejected_before_anything_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, kwargs: dict[str, Any]
) -> None:
    calls = _cli(monkeypatch)
    github = _github(monkeypatch, [])

    with pytest.raises(ValueError):
        getattr(PrTools(str(tmp_path), allow_write=True), method)(**kwargs)

    assert calls == [] and github == []
