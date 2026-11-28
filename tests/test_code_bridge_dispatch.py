from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from lazybridge import Store
from lazybridge.ext.approval import ApprovalQueue, ApprovalTarget, TypedApprovalRequest
from lazybridge.ext.knowledge import DurableKnowledgeBase

from lazytools.code_bridge import _concurrency, _routing, _store
from lazytools.code_bridge._dispatch import STALE_HEARTBEAT, Dispatcher, Task, _dry_run, _routers, _tasks
from lazytools.code_bridge._dispatch_launch import BridgeLauncher
from lazytools.code_bridge.cli import build_parser
from lazytools.connectors.projects import ProjectsTools
from lazytools.projects.admission import TelemetryReading, WindowReading
from lazytools.projects.task_service import ProjectTaskService, TaskPrincipal
from lazytools.projects.task_store import transaction
from lazytools.routing import RoutingDecision
from lazytools.routing.catalogue import DEFAULT_PATH, load_tiers


class FakeService:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {
            "A": {"dependency_ids": [], "status": "todo"},
            "B": {"dependency_ids": ["A"], "status": "todo"},
            "C": {"dependency_ids": ["A"], "status": "todo"},
            "D": {"dependency_ids": ["B", "C"], "status": "todo"},
        }
        self.claims: dict[str, dict[str, Any]] = {}
        self.evidence: list[Any] = []
        self.reasons: list[str] = []

    def tasks(self, _service: Any, project: str) -> list[Task]:
        return [Task(f"{project}/{name}", "rev", row["status"], row,
                     self.claims.get(name)) for name, row in self.rows.items()]

    def block_failed_dependencies(self, _principal: Any, _project: str) -> list[tuple[str, str]]:
        from lazytools.projects.task_fields import block_failed_dependencies

        rows = [{**row, "task_id": name} for name, row in self.rows.items()]
        blocked = block_failed_dependencies(rows, 0)
        for row in rows:
            self.rows[row["task_id"]].update(row)
        return blocked

    def next_ready(self, _principal: Any, project: str, limit: int = 20) -> dict[str, Any]:
        ready = []
        for name, row in self.rows.items():
            if row["status"] == "todo" and all(self.rows[dep]["status"] == "done"
                                                  for dep in row["dependency_ids"]):
                ready.append({"task_id": f"{project}/{name}", "revision": "rev", "text": name,
                              "tier": "basic", "requested_profile": None, "requested_network": None,
                              "effective_placement": row.get("effective_placement", "local"),
                              "dependencies": row.get("dependencies", [])})
        return {"tasks": ready[:limit]}

    def claim(self, principal: Any, identifier: str, revision: str, key: str, **_kwargs: Any) -> dict[str, Any]:
        name = identifier.rsplit("/", 1)[1]
        receipt = {"task_id": identifier, "revision": revision, "text": name, "claim_token": f"token-{name}",
                   "attempt": 1, "executor": principal.actor, "engine_family": "codex",
                   "effective_placement": "local", "lease_expires_at": 9999999999}
        self.rows[name]["status"] = "claimed"
        self.claims[name] = {"state": "submitted", "job_ref": f"job-{name}", "receipt": receipt}
        return {"claim": receipt, "claim_active": True}

    def record_completion_evidence(self, *args: Any) -> None:
        self.evidence.append(args[-1])

    def complete(self, _principal: Any, identifier: str, _token: str, _key: str,
                 state: str, evidence_ref: str, _outcome: str, **_kwargs: Any) -> None:
        name = identifier.rsplit("/", 1)[1]
        evidence = next((item for item in reversed(self.evidence) if item.reference == evidence_ref), None)
        if evidence and evidence.continuation:
            self._queue_retry(name, evidence.failure_reason, evidence.continuation)
        else:
            self.rows[name]["status"] = "done" if state == "accepted" else "failed"

    def _queue_retry(self, name: str, reason: str, continuation: Any) -> None:
        self.reasons.append(reason)
        self.rows[name].update(status="todo", attempt_continuation=continuation.model_dump(mode="json"))
        self.claims.pop(name, None)

    def fail_local_launch(
        self, _principal: Any, identifier: str, _token: str, _error: str, **_kwargs: Any,
    ) -> None:
        name = identifier.rsplit("/", 1)[1]
        if continuation := _kwargs.get("continuation"):
            self._queue_retry(name, _error, continuation)
        else:
            self.rows[name]["status"] = "failed"

    def reconcile_dispatch(self, *args: Any, **kwargs: Any) -> None: pass

    def fail_dispatch_launch(self, _principal: Any, identifier: str, _token: str, _error: str, **_kwargs: Any) -> None:
        self.rows[identifier.rsplit("/", 1)[1]]["status"] = "todo"


class FakeLauncher:
    def __init__(self, service: FakeService) -> None:
        self.service = service
        self.jobs: dict[str, dict[str, Any]] = {}
        self.launches: list[str] = []
        self.prompts: list[str] = []

    def launch(self, task: dict[str, Any], claim: dict[str, Any]) -> str:
        name = claim["task_id"].rsplit("/", 1)[1]
        self.launches.append(name)
        self.prompts.append(task["text"])
        job = f"job-{name}"
        self.jobs[job] = {"job_id": job, "status": "running"}
        return job

    def job(self, job_ref: str) -> dict[str, Any] | None: return self.jobs.get(job_ref)

    def find(self, task_id: str, attempt: int) -> dict[str, Any] | None:
        return self.jobs.get(f"job-{task_id.rsplit('/', 1)[1]}")

    def finish(self, name: str, status: str = "done") -> None:
        self.jobs[f"job-{name}"].update(status=status, artifact_ref="a" * 40,
                                          error="boom" if status != "done" else None)


def runner(service: FakeService, launcher: FakeLauncher, events: list[str],
           knowledge_store: Store | None = None) -> Dispatcher:
    principal = TaskPrincipal(actor="dispatcher", role="orchestrator", projects=("p",))
    return Dispatcher(service, principal, launcher, "p", parallel=2, emit=events.append,  # type: ignore[arg-type]
                      task_reader=service.tasks, knowledge_store=knowledge_store)  # type: ignore[arg-type]


@pytest.fixture
def knowledge_store(tmp_path: Path) -> Iterator[Store]:
    with Store(db=str(tmp_path / "knowledge.sqlite")) as store:
        yield store


def test_dispatch_prepends_the_three_closest_lessons(knowledge_store: Store) -> None:
    knowledge = DurableKnowledgeBase(knowledge_store)
    knowledge.save_lesson(topic="SQLite", what_worked="fourth match")
    knowledge.save_lesson(topic="SQLite restore", what_worked="third match")
    knowledge.save_lesson(topic="SQLite backup", what_worked="second match for SQLite")
    knowledge.save_lesson(topic="SQLite restore backup", what_worked="first match", gotchas="check WAL")
    service = FakeService()
    service.rows["A"]["text"] = "Restore a SQLite backup"
    service.rows["B"]["text"] = "Run formatter"
    launcher = FakeLauncher(service)
    dispatch = runner(service, launcher, [], knowledge_store)
    dispatch.pass_once()
    assert launcher.prompts == [
        "Known lessons\n"
        "- SQLite restore backup | What worked — first match | Gotchas — check WAL\n"
        "- SQLite backup | What worked — second match for SQLite\n"
        "- SQLite restore | What worked — third match\n\n"
        "Restore a SQLite backup"
    ]
    assert service.rows["A"]["text"] == "Restore a SQLite backup"
    launcher.finish("A")
    dispatch.pass_once()
    assert launcher.prompts[1] == "Run formatter"


def test_dispatch_accepts_a_knowledge_db_path(tmp_path: Path) -> None:
    path = tmp_path / "lessons.sqlite"
    args = build_parser().parse_args(["dispatch", "--project", "p", "--knowledge-db", str(path)])
    assert args.knowledge_db == path


def test_graph_runs_in_topological_parallel_waves() -> None:
    service, events = FakeService(), []  # type: FakeService, list[str]
    launcher = FakeLauncher(service)
    dispatch = runner(service, launcher, events)
    dispatch.pass_once()
    launcher.finish("A")
    dispatch.pass_once()
    assert launcher.launches == ["A", "B", "C"]
    launcher.finish("B")
    launcher.finish("C")
    dispatch.pass_once()
    assert launcher.launches == ["A", "B", "C", "D"]


@pytest.mark.parametrize("second,parallel", [
    ("src/**/dispatch*.py", False), ("tests/*.py", True),
])
def test_touches_control_parallel_dispatch(second: str, parallel: bool) -> None:
    service, events = FakeService(), []  # type: FakeService, list[str]
    service.rows = {
        "A": {"dependency_ids": [], "status": "todo", "repo": "r", "touches": ["src/*/dispatcher.py"]},
        "B": {"dependency_ids": [], "status": "todo", "repo": "r", "touches": [second]},
    }
    launcher = FakeLauncher(service)
    dispatch = runner(service, launcher, events)
    dispatch.pass_once()
    assert launcher.launches == (["A", "B"] if parallel else ["A"])
    if not parallel:
        dispatch.pass_once()
        assert events.count("waiting p/B touches overlap with p/A") == 1
        launcher.finish("A")
        dispatch.pass_once()
        assert launcher.launches == ["A", "B"]


def test_waiting_reason_changes_from_dependency_to_parallel_cap() -> None:
    service, events = FakeService(), []  # type: FakeService, list[str]
    launcher = FakeLauncher(service)
    dispatch = runner(service, launcher, events)
    dispatch.parallel = 1
    dispatch.pass_once()
    dispatch.pass_once()
    launcher.finish("A")
    dispatch.pass_once()
    dispatch.pass_once()
    assert [line for line in events if line.startswith("waiting p/C ")] == [
        "waiting p/C dependency not done: A", "waiting p/C --parallel limit reached (1)",
    ]
    launcher.finish("B")
    dispatch.pass_once()
    assert launcher.launches == ["A", "B", "C"]


def test_local_capacity_counts_external_holders_and_pending_claims_once() -> None:
    service = FakeService()
    dispatch = Dispatcher(service, TaskPrincipal(actor="d", role="orchestrator", projects=("p",)),
                          FakeLauncher(service), "p", capacity=lambda engine: 2 if engine == "codex" else None,
                          local_holders=lambda _engine: {"live"})  # type: ignore[arg-type]
    claims = [{"engine_family": "codex", "job_ref": "live"},
              {"engine_family": "codex", "effective_placement": "cloud"}]
    assert dispatch._local_busy(claims) == {}
    claims.append({"engine_family": "codex"})
    assert "codex 2/2" in dispatch._local_busy(claims)["codex"]


@pytest.mark.parametrize("missing", [False, True])
def test_settlement_defers_busy_store_but_fails_missing_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, missing: bool,
) -> None:
    path = tmp_path / "projects.sqlite"
    if not missing:
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE store (key TEXT PRIMARY KEY, value TEXT)")
        connection.close()
    monkeypatch.setenv("LAZYBRIDGE_STORE_BUSY_TIMEOUT", "0.01")
    monkeypatch.setenv("LAZYBRIDGE_STORE_SLOW_WRITE", "0.001")
    service, events = FakeService(), []  # type: FakeService, list[str]
    launcher = FakeLauncher(service)
    dispatch = runner(service, launcher, events)
    dispatch.pass_once()
    launcher.finish("A")

    def record(*args: Any) -> None:
        with transaction(path, write=True):
            pass

    monkeypatch.setattr(service, "record_completion_evidence", record)
    if missing:
        dispatch.pass_once()
        assert any(event.startswith("failed p/A The projects store is unavailable") for event in events)
    else:
        with sqlite3.connect(path) as lock:
            lock.execute("BEGIN IMMEDIATE")
            dispatch.pass_once()
            assert any(event.startswith("deferred p/A The projects store is busy") for event in events)
            assert "slow projects Store lock wait" in caplog.text
            assert service.rows["A"]["status"] == "claimed"
            assert not any(event.startswith("failed p/A") for event in events)
        dispatch.pass_once()
        assert service.rows["A"]["status"] == "done"
        assert launcher.launches.count("A") == 1
        assert not service.reasons


def test_failure_blocks_dependent_but_independent_branch_finishes() -> None:
    service, events = FakeService(), []  # type: FakeService, list[str]
    launcher = FakeLauncher(service)
    dispatch = runner(service, launcher, events)
    dispatch.max_retries = 0
    dispatch.pass_once()
    launcher.finish("A")
    dispatch.pass_once()
    launcher.finish("B", "failed")
    launcher.finish("C")
    dispatch.pass_once()
    assert service.rows["C"]["status"] == "done"
    assert "D" not in launcher.launches
    assert "blocked p/D dependency B failed" in events
    assert service.rows["D"]["status"] == "failed"


def test_failed_dependency_chain_diamond_and_retry(tmp_path: Path) -> None:
    from lazytools.projects.plan_edit import _board_key, reopen_task
    from test_projects_task_service import PROJECT, admit, task

    path = str(tmp_path / "projects.sqlite")
    projects = ProjectsTools(store_db=path, allow_write=True)
    projects.projects_create(PROJECT, "Dependencies", "Finish the graph")
    admit(projects, [task("A"), task("B", dependency_ids=["A"]),
                     task("C", dependency_ids=["A"]), task("D", dependency_ids=["B", "C"]),
                     task("E", dependency_ids=["D"])])
    service = ProjectTaskService(path)
    principal = TaskPrincipal(actor="dispatcher", role="orchestrator", projects=(PROJECT,))
    with Store(db=path) as store:
        board = store.read(_board_key(PROJECT))
        board["tasks"][0].update(status="failed", error="boom")
        board["tasks"][4].update(status="claimed", owner="worker")
        store.write(_board_key(PROJECT), board)
    events: list[str] = []
    dispatch = Dispatcher(service, principal, FakeLauncher(FakeService()), PROJECT, emit=events.append)
    dispatch.pass_once()
    with Store(db=path) as store:
        board = store.read(_board_key(PROJECT))
        assert [t["status"] for t in board["tasks"]] == ["failed", "failed", "failed", "failed", "claimed"]
        assert [t.get("failed_dependency_id") for t in board["tasks"][1:4]] == ["A"] * 3
        assert sorted(e for e in events if e.startswith("blocked ")) == [
            f"blocked {PROJECT}/{name} dependency A failed" for name in ("B", "C", "D")
        ]
        board["tasks"][4].update(status="done", owner=None)
        store.write(_board_key(PROJECT), board)
        assert dispatch.run() == 1
        result = reopen_task(store, PROJECT, 0, board["tasks"][0]["text"],
                             "Retry after fixing the original failure")
        assert result.startswith("reopened")
        board = store.read(_board_key(PROJECT))
        assert [t["status"] for t in board["tasks"]] == ["todo", "todo", "todo", "todo", "done"]


def test_restart_adopts_submitted_job_without_launching_again() -> None:
    service, events = FakeService(), []  # type: FakeService, list[str]
    launcher = FakeLauncher(service)
    runner(service, launcher, events).pass_once()
    restarted = runner(service, launcher, events)
    restarted.pass_once()
    assert launcher.launches == ["A"]


def test_dry_run_prints_topological_waves_without_claiming() -> None:
    service, events = FakeService(), []  # type: FakeService, list[str]
    _dry_run(service, "p", events.append, service.tasks)  # type: ignore[arg-type]
    assert events == ["wave 1: A", "ready p/A", "wave 2: B C", "ready p/B", "ready p/C", "wave 3: D", "ready p/D"]
    assert not service.claims


def test_dry_run_holds_start_held_task_and_blocks_its_dependants() -> None:
    service, events = FakeService(), []  # type: FakeService, list[str]
    service.rows["A"].update(start_hold=True, hold_reason="waiting on data release")

    def reader(_service: Any, project: str) -> list[Task]:
        return [Task(f"{project}/{name}", "rev", row["status"], row, None,
                     hold_reason=row.get("hold_reason"))
                for name, row in service.rows.items()]

    _dry_run(service, "p", events.append, reader)  # type: ignore[arg-type]
    assert events == ["held p/A (waiting on data release)", "blocked p/B", "blocked p/C", "blocked p/D"]
    assert not service.claims


def test_cloud_task_is_skipped_without_claim_or_launch() -> None:
    service, events = FakeService(), []  # type: FakeService, list[str]
    launcher = FakeLauncher(service)
    service.rows["A"]["effective_placement"] = "cloud"
    runner(service, launcher, events).pass_once()
    assert not launcher.launches
    assert service.rows["A"]["status"] == "todo"
    assert not service.claims
    assert "failed p/A cloud placement not supported by this launcher" in events


def test_expired_orphan_is_reconciled_and_released() -> None:
    service, events = FakeService(), []  # type: FakeService, list[str]
    service.rows["A"]["status"] = "claimed"
    service.claims["A"] = {"state": "intent", "created_at": 1, "receipt": {
        "attempt": 1, "lease_expires_at": 2, "claim_token": "token-A",
        "executor": "dispatcher", "engine_family": "codex",
    }}

    def reconcile(_principal: Any, identifier: str, *_args: Any, **_kwargs: Any) -> None:
        service.rows[identifier.rsplit("/", 1)[1]]["status"] = "todo"

    service.reconcile_dispatch = reconcile  # type: ignore[method-assign]
    launcher = FakeLauncher(service)
    dispatch = runner(service, launcher, events)
    dispatch.clock = lambda: 100.0
    dispatch.pass_once()
    assert "retry p/A no job after dispatch grace period [infra]" in events
    assert launcher.launches == ["A"]  # released task became ready and was safely dispatched again


def test_settlement_failure_does_not_stop_other_tasks() -> None:
    service, events = FakeService(), []  # type: FakeService, list[str]
    launcher = FakeLauncher(service)
    dispatch = runner(service, launcher, events)
    dispatch.pass_once()
    launcher.finish("A")
    service.complete = lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("Claim lease has expired"))  # type: ignore[method-assign]
    dispatch.pass_once()
    assert "failed p/A Claim lease has expired [infra]" in events


def test_review_task_is_skipped_without_claim() -> None:
    service, events = FakeService(), []  # type: FakeService, list[str]
    service.rows["A"]["review_of"] = "writer"
    launcher = FakeLauncher(service)
    dispatch = runner(service, launcher, events)
    dispatch.pass_once()
    assert service.rows["A"]["status"] == "todo"
    assert not launcher.launches and not service.claims
    assert "failed p/A review/contract tasks not supported by dispatch v1" in events


def test_accepted_artifact_uses_recorded_commit_not_checkout_head() -> None:
    service, events = FakeService(), []  # type: FakeService, list[str]
    launcher = FakeLauncher(service)
    dispatch = runner(service, launcher, events)
    dispatch.pass_once()
    launcher.jobs["job-A"].update(status="done", candidate_commit="b" * 40,
                                  artifact_ref="a" * 40)
    captured: list[Any] = []
    service.record_completion_evidence = lambda *args: captured.append(args[-1])  # type: ignore[method-assign]
    dispatch.pass_once()
    assert captured[0].artifact_ref == "b" * 40


@pytest.mark.parametrize("failure, detail", [
    (None, ""), ("exit", "exit 7"), ("timeout", "timed out after 15 minutes"), ("missing", "could not run"),
])
def test_dispatch_runs_plan_checks_before_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None, detail: str,
) -> None:
    source = tmp_path / "source"
    source_git = _repo(source)
    principal = TaskPrincipal(actor="dispatcher", role="orchestrator", projects=("p",))
    bridge = BridgeLauncher(None, principal, "p", repo_roots={"r": source}, worktree_root=tmp_path / "trees")
    clone = bridge._checkout({"task_id": "p/A", "repo": "r"})
    (clone / "work.txt").write_text("task work\n")
    bridge._git(clone, "add", "work.txt")
    bridge._git(clone, "commit", "-qm", "Task work")

    service, events = FakeService(), []  # type: FakeService, list[str]
    commands = ['"test runner" first', '"test runner" "test with spaces.py"', '"test runner" last']
    service.rows["A"]["checks"] = commands
    service.rows["A"]["max_check_retries"] = 0
    service.rows["A"]["tier"] = "critical"
    captured: list[Any] = []
    service.record_completion_evidence = lambda *args: captured.append(args[-1])  # type: ignore[method-assign]
    launcher = FakeLauncher(service)
    dispatch = runner(service, launcher, events)
    dispatch.pass_once()
    launcher.finish("A")
    launcher.jobs["job-A"]["cwd"] = str(clone)

    calls: list[list[str]] = []
    real_run = subprocess.run
    output = b"output head\n" + b"more output\n" * 60 + b"App Server exited is test output\n"

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        if argv[0] != "test runner":
            return real_run(argv, **kwargs)
        assert kwargs["cwd"] == clone
        assert kwargs["env"]["PYTHONPATH"] == "src"
        assert kwargs["shell"] is False and kwargs["timeout"] == 900
        assert kwargs["stdout"] == subprocess.PIPE and kwargs["stderr"] == subprocess.STDOUT
        assert not source_git("branch", "--list", "dispatch/p/A")
        calls.append(argv)
        if argv[1] == "test with spaces.py":
            if failure == "exit":
                return subprocess.CompletedProcess(argv, 7, output)
            if failure == "timeout":
                raise subprocess.TimeoutExpired(argv, 900, output=output)
            if failure == "missing":
                raise FileNotFoundError("no test runner")
        return subprocess.CompletedProcess(argv, 0, b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    dispatch.pass_once()

    middle = [["test runner", "test with spaces.py"]] * (2 if failure else 1)
    assert calls == [["test runner", "first"], *middle, ["test runner", "last"]]
    if failure is None:
        assert service.rows["A"]["status"] == "done"
        assert source_git("rev-parse", "dispatch/p/A") == bridge._git(clone, "rev-parse", "HEAD")
        assert launcher.launches == ["A", "B", "C"]
        return
    assert service.rows["A"]["status"] == "failed"
    assert not source_git("branch", "--list", "dispatch/p/A")
    assert launcher.launches == ["A"]
    message = f"check failed: {commands[1]} ({detail})"
    assert f"failed p/A {message} [check]" in events
    evidence, = captured
    assert evidence.state == "blocked" and evidence.outcome == "task_failure"
    assert evidence.failure_class == "check"
    assert evidence.failure_reason.startswith(f"{message}\n")
    assert "output head" not in evidence.failure_reason
    assert evidence.failure_reason.endswith("no test runner" if failure == "missing" else "App Server exited is test output")


def test_two_tasks_get_separate_retained_worktrees(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README").write_text("test\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
    principal = TaskPrincipal(actor="dispatcher", role="orchestrator", projects=("p",))
    launcher = BridgeLauncher(None, principal, "p", repo_roots={"org/repo": repo},
                              worktree_root=tmp_path / "trees")
    first = launcher._checkout({"task_id": "p/A", "repo": "org/repo", "assigned_branch": "task-a"})
    second = launcher._checkout({"task_id": "p/B", "repo": "org/repo", "assigned_branch": "task-b"})
    assert first != second
    assert (first / ".git").exists() and (second / ".git").exists()
    assert subprocess.run(["git", "branch", "--show-current"], cwd=first, text=True,
                          capture_output=True, check=True).stdout.strip() == "task-a"


def test_launch_pins_the_claimed_engine_model_and_effort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    principal = TaskPrincipal(actor="dispatcher", role="orchestrator", projects=("p",))
    launcher = BridgeLauncher(None, principal, "p", worktree_root=tmp_path / "trees")
    monkeypatch.setattr(launcher, "_checkout", lambda task: tmp_path)
    seen: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, "a" * 32 + "\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    claim = {"task_id": "p/A", "claim_token": "t", "effective_placement": "local", "engine_family": "claude_code",
             "model": "claude-opus-5-5", "effort": "high"}
    launcher.launch({"text": "do it", "tier": "writing"}, claim)
    assert seen[0][seen[0].index("--engine") + 1] == "claude"
    assert seen[0][seen[0].index("--model") + 1] == "claude-opus-5-5"
    assert seen[0][seen[0].index("--effort") + 1] == "high"


def test_dependency_artifacts_are_merged_into_descendant_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "A").write_text("A\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "A"], cwd=repo, check=True)
    a_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, text=True,
                              capture_output=True, check=True).stdout.strip()
    principal = TaskPrincipal(actor="dispatcher", role="orchestrator", projects=("p",))
    launcher = BridgeLauncher(None, principal, "p", repo_roots={"r": repo},
                              worktree_root=tmp_path / "trees")

    commits = []
    for name in ("B", "C"):
        tree = launcher._checkout({"task_id": f"p/{name}", "repo": "r",
                                   "dependencies": [{"artifact_ref": a_commit}]})
        (tree / name).write_text(f"{name}\n")
        subprocess.run(["git", "add", "."], cwd=tree, check=True)
        subprocess.run(["git", "commit", "-qm", name], cwd=tree, check=True)
        commits.append(subprocess.run(["git", "rev-parse", "HEAD"], cwd=tree, text=True,
                                      capture_output=True, check=True).stdout.strip())

    descendant = launcher._checkout({
        "task_id": "p/D", "repo": "r",
        "dependencies": [{"artifact_ref": commit} for commit in commits],
    })
    assert all((descendant / name).read_text() == f"{name}\n" for name in ("A", "B", "C"))


def test_place_records_routed_local_evidence_only_for_unblocked_todo_tasks() -> None:
    service = FakeService()
    service.rows = {
        "A": {"status": "done", "dependency_ids": []},
        "B": {"status": "todo", "dependency_ids": ["A"]},
        "C": {"status": "todo", "dependency_ids": ["B"]},
        "R": {"status": "todo", "dependency_ids": ["A"], "review_of": "B"},
        "K": {"status": "todo", "dependency_ids": ["A"], "requested_placement": "cloud"},
    }
    recorded: list[tuple[str, str, Any]] = []
    service.record_placement = lambda *args: recorded.append(args)  # type: ignore[attr-defined]
    events: list[str] = []
    dispatch = Dispatcher(service, TaskPrincipal(actor="d", role="orchestrator", projects=("p",)),
                          FakeLauncher(service), "p", emit=events.append, clock=lambda: 1000.0,  # type: ignore[arg-type]
                          task_reader=service.tasks,  # type: ignore[arg-type]
                          router=lambda tier, *_: RoutingDecision(tier, "codex", None, None, "weekly_margin", True))
    dispatch.pass_once()
    assert [(task_id, evidence.engine_family, evidence.placement) for task_id, _, evidence in recorded] == [
        ("p/B", "codex", "local")]
    assert "waiting p/C dependency not done: B" in events


@pytest.mark.parametrize("continuation", [
    {"infra_retries": 1},
    {"engine": "claude", "wait_for_budget": True},
    {"engine": "claude", "native_id": "old", "fresh": True, "check_failures": ["failed"]},
    {"engine": "claude", "fresh": True, "escalation_tier": "critical"},
])
def test_retry_without_native_resume_is_rerouted(continuation: dict[str, Any]) -> None:
    recorded: list[Any] = []
    required: list[Any] = []
    events: list[str] = []

    class PlacingService:
        lease_seconds = 900

        def record_placement(self, task_id: str, revision: str, evidence: Any) -> None:
            recorded.append(evidence)

    def route(tier: str, busy: Any, preference: Any, fallback: Any, engine: Any) -> RoutingDecision:
        required.append(engine)
        return RoutingDecision(tier, "codex", None, None, "weekly_margin", True)

    dispatch = Dispatcher(PlacingService(), TaskPrincipal(actor="d", role="orchestrator", projects=("p",)),
                          None, "p", emit=events.append, clock=lambda: 1000.0, router=route)  # type: ignore[arg-type]
    retry = Task("p/A", "r2", "todo", {"attempt_continuation": continuation},
                 {"receipt": {"engine_family": "claude_code"}})
    dispatch._place(retry, dispatch._requested(retry), {}, {})
    assert required == [None] and recorded[0].engine_family == "codex"
    assert events[0].startswith("rerouted p/A local claude_code -> codex ")


@pytest.mark.parametrize("escalation_tier", [None, "critical"])
def test_retry_with_native_resume_keeps_engine(escalation_tier: str | None) -> None:
    required: list[Any] = []
    events: list[str] = []

    class PlacingService:
        lease_seconds = 900

        def record_placement(self, task_id: str, revision: str, evidence: Any) -> None:
            assert evidence.engine_family == "codex"

    def route(tier: str, busy: Any, preference: Any, fallback: Any, engine: Any) -> RoutingDecision:
        required.append(engine)
        return RoutingDecision(tier, "codex", None, None, "weekly_margin", True)

    dispatch = Dispatcher(PlacingService(), TaskPrincipal(actor="d", role="orchestrator", projects=("p",)),
                          None, "p", emit=events.append, clock=lambda: 1000.0, router=route)  # type: ignore[arg-type]
    retry = Task("p/A", "r2", "todo", {"attempt_continuation": {"engine": "codex", "native_id": "thread",
                                                                 "escalation_tier": escalation_tier}},
                 {"receipt": {"engine_family": "codex"}})
    dispatch._place(retry, dispatch._requested(retry), {}, {})
    assert required == ["codex"]
    assert events[0].startswith("placed p/A local codex ")


def test_place_reports_waiting_when_no_engine_is_eligible() -> None:
    class PlacingService:
        lease_seconds = 900

        def record_placement(self, *args: Any) -> None:
            raise AssertionError("must not record without an engine")

    events: list[str] = []
    dispatch = Dispatcher(PlacingService(), TaskPrincipal(actor="d", role="orchestrator", projects=("p",)),
                          None, "p", emit=events.append,  # type: ignore[arg-type]
                          router=lambda tier, *_: RoutingDecision(tier, None, None, None, "no_eligible_provider", False))
    dispatch._place(Task("p/A", "r1", "todo", {"tier": "writing"}, None), "auto", {}, {})
    dispatch._place(Task("p/A", "r1", "todo", {"tier": "writing"}, None), "auto", {}, {})
    assert events == [
        "waiting p/A local tier=writing | no_eligible_provider | codex weekly projected unavailable | "
        "claude weekly projected unavailable | quota/pace"
    ]


def test_last_dependency_conflict_is_left_for_the_agent(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*argv: str, cwd: Path = repo) -> str:
        return subprocess.run(["git", *argv], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()
    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (repo / "calc.py").write_text("base\n")
    git("add", ".")
    git("commit", "-qm", "base")
    git("checkout", "-qb", "b")
    (repo / "calc.py").write_text("from b\n")
    git("commit", "-qam", "b")
    b_commit = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")
    (repo / "calc.py").write_text("from c\n")
    git("commit", "-qam", "c")
    c_commit = git("rev-parse", "HEAD")
    principal = TaskPrincipal(actor="dispatcher", role="orchestrator", projects=("p",))
    launcher = BridgeLauncher(None, principal, "p", repo_roots={"r": repo}, worktree_root=tmp_path / "trees")
    checkout = launcher._checkout({"task_id": "p/D", "repo": "r", "dependencies": [
        {"artifact_ref": b_commit}, {"artifact_ref": c_commit}]})
    assert launcher.conflicts == {"p/D": c_commit}
    assert "<<<<<<<" in (checkout / "calc.py").read_text()
    assert git("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=checkout) == c_commit


def test_recreated_clone_restores_preserved_conflict_resolution_without_replaying_merges(tmp_path: Path) -> None:
    git = _repo(tmp_path / "repo")
    repo = tmp_path / "repo"
    git("checkout", "-qb", "b")
    (repo / "f.txt").write_text("from b\n")
    git("commit", "-qam", "b")
    b_commit = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")
    (repo / "f.txt").write_text("from c\n")
    git("commit", "-qam", "c")
    c_commit = git("rev-parse", "HEAD")
    # The interrupted attempt resolved the conflict and committed; its copy is in the source.
    git("checkout", "-qb", "dispatch-attempt/p/D/1", b_commit)
    subprocess.run(["git", "merge", "-q", c_commit], cwd=repo, capture_output=True, check=False)
    (repo / "f.txt").write_text("from b and c\n")
    git("commit", "-qam", "resolve")
    preserved = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")
    principal = TaskPrincipal(actor="dispatcher", role="orchestrator", projects=("p",))
    launcher = BridgeLauncher(None, principal, "p", repo_roots={"r": repo}, worktree_root=tmp_path / "trees")
    checkout = launcher._checkout({
        "task_id": "p/D", "repo": "r", "dependencies": [{"artifact_ref": b_commit}, {"artifact_ref": c_commit}],
        "preserved_branch": "dispatch-attempt/p/D/1", "preserved_commit": preserved, "preserved_base": b_commit})
    assert launcher.conflicts == {}
    assert (checkout / "f.txt").read_text() == "from b and c\n"
    assert git("rev-parse", "HEAD", cwd=checkout) == preserved
    assert git("branch", "--show-current", cwd=checkout) == "dispatch/p/D"
    assert git("rev-parse", "dispatch-base", cwd=checkout) == b_commit


def test_engine_crash_is_retried_once_then_fails(knowledge_store: Store) -> None:
    service, events = FakeService(), []  # type: FakeService, list[str]
    service.rows["A"].update(repo="org/repo", text="Update Codex App Server")
    launcher = FakeLauncher(service)
    dispatch = runner(service, launcher, events, knowledge_store)
    delays: list[float] = []
    dispatch.sleep = delays.append
    dispatch.pass_once()
    launcher.jobs["job-A"].update(status="failed", error="Codex App Server exited before completing the request")
    dispatch.pass_once()
    assert len(service.reasons) == 1 and any(event.startswith("retry p/A") and "[infra]" in event for event in events)
    assert service.evidence[-1].failure_class == "infra" and delays == [0.25]
    assert launcher.launches == ["A", "A"]
    assert "Codex App Server exited before completing the request" in launcher.prompts[1]
    assert launcher.prompts[1].count("Known lessons") == 1
    launcher.jobs["job-A"].update(status="failed", error="Codex App Server exited again")
    dispatch.pass_once()
    assert len(service.reasons) == 1 and delays == [0.25]
    assert any(event.startswith("failed p/A") for event in events)
    lesson, = DurableKnowledgeBase(knowledge_store).find_lessons("engine crash")
    assert lesson["topic"] == "org/repo: engine crash"
    assert lesson["revision"] == 2
    assert lesson["what_worked"] == ""
    assert lesson["gotchas"] == "Codex App Server exited again"


def test_database_locked_failure_is_retried_once(knowledge_store: Store) -> None:
    service, events = FakeService(), []  # type: FakeService, list[str]
    service.rows["A"].update(repo="org/repo", text="Update Codex App Server")
    launcher = FakeLauncher(service)
    dispatch = runner(service, launcher, events, knowledge_store)
    dispatch.pass_once()
    launcher.jobs["job-A"].update(status="failed", error="error: database is locked")
    dispatch.pass_once()
    assert len(service.reasons) == 1 and any(event.startswith("retry p/A") for event in events)
    assert launcher.launches == ["A", "A"]


def test_infra_failure_is_final_when_retries_are_disabled(knowledge_store: Store) -> None:
    service, events = FakeService(), []  # type: FakeService, list[str]
    service.rows["A"]["repo"] = "org/repo"
    launcher = FakeLauncher(service)
    dispatch = runner(service, launcher, events, knowledge_store)
    dispatch.max_retries = 0
    dispatch.pass_once()
    launcher.finish("A", "failed")
    dispatch.pass_once()
    assert any(event.startswith("failed p/A boom") for event in events)
    lesson, = DurableKnowledgeBase(knowledge_store).find_lessons("boom")
    assert (lesson["topic"], lesson["what_worked"], lesson["gotchas"]) == ("org/repo: engine crash", "", "boom")


def test_launch_failure_records_a_lesson(knowledge_store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeService()
    service.rows["A"]["repo"] = "org/repo"
    launcher = FakeLauncher(service)

    def fail_launch(*args: Any) -> str:
        raise RuntimeError("checkout unavailable")

    monkeypatch.setattr(launcher, "launch", fail_launch)
    dispatch = runner(service, launcher, [], knowledge_store)
    dispatch.max_retries = 0
    dispatch.pass_once()
    lesson, = DurableKnowledgeBase(knowledge_store).find_lessons("checkout")
    assert service.rows["A"]["status"] == "failed"
    assert lesson["topic"] == "org/repo: launch failure"
    assert lesson["gotchas"] == "checkout unavailable"


def test_conclude_workspace_commits_a_resolved_merge_and_rejects_markers(tmp_path: Path) -> None:
    from lazytools.code_bridge._dispatch_workspace import _conclude_workspace
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*argv: str) -> str:
        return subprocess.run(["git", *argv], cwd=repo, text=True, capture_output=True, check=False).stdout.strip()
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    (repo / "f.txt").write_text("base\n")
    git("add", ".")
    git("commit", "-qm", "base")
    git("checkout", "-qb", "side")
    (repo / "f.txt").write_text("side\n")
    git("commit", "-qam", "side")
    git("checkout", "-q", "main")
    (repo / "f.txt").write_text("main\n")
    git("commit", "-qam", "main")
    git("merge", "--no-edit", "side")
    assert _conclude_workspace(repo, "p/D")[0].startswith("unresolved conflict markers")
    (repo / "f.txt").write_text("main and side\n")
    error, commit = _conclude_workspace(repo, "p/D")
    assert error is None and commit == git("rev-parse", "HEAD")
    assert git("status", "--porcelain") == ""
    (repo / "g.txt").write_text("work\n")
    (repo / "error.log").write_text("account_id=secret\n")
    error, commit = _conclude_workspace(repo, "p/D")
    assert error is None and commit == git("rev-parse", "HEAD")
    assert git("show", "--name-only", "--format=", "HEAD") == "g.txt"
    assert "error.log" in git("status", "--porcelain")


def test_has_new_commit_requires_work_after_the_base_tag(tmp_path: Path) -> None:
    from lazytools.code_bridge._dispatch_workspace import _has_new_commit
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*argv: str) -> None:
        subprocess.run(["git", *argv], cwd=repo, text=True, capture_output=True, check=True)
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    (repo / "f").write_text("1\n")
    git("add", ".")
    git("commit", "-qm", "base")
    git("tag", "dispatch-base")
    assert not _has_new_commit(repo)
    (repo / "f").write_text("2\n")
    git("commit", "-qam", "work")
    assert _has_new_commit(repo)


def test_pending_tickets_of_running_jobs_are_surfaced_once() -> None:
    service, events = FakeService(), []  # type: FakeService, list[str]
    launcher = FakeLauncher(service)
    tickets = [{"approval_id": "t-1", "job_id": "job-A", "command": "git add calc.py; git commit -m x",
                "reason": "Git needs write access"},
               {"approval_id": "t-2", "job_id": "other-job", "command": "rm -rf /", "reason": "not ours"}]
    launcher.pending = lambda: tickets  # type: ignore[attr-defined]
    dispatch = runner(service, launcher, events)
    dispatch.pass_once()
    dispatch.pass_once()
    dispatch.pass_once()
    approvals = [event for event in events if event.startswith("approval ")]
    assert approvals == ["approval p/A t-1 git add calc.py; git commit -m x | Git needs write access"]


def test_json_events_carry_the_wait_any_keys() -> None:
    import json

    service, events = FakeService(), []  # type: FakeService, list[str]
    launcher = FakeLauncher(service)
    launcher.pending = lambda: [{"approval_id": "t-1", "job_id": "job-A", "command": "git push",  # type: ignore[attr-defined]
                                 "reason": "publish"}]
    dispatch = runner(service, launcher, events)
    dispatch.json_events = True
    dispatch.pass_once()
    dispatch.pass_once()  # A is running: its ticket is surfaced
    launcher.finish("A", "failed")
    dispatch.pass_once()
    rows = [json.loads(line) for line in events]
    assert all(set(row) == {"kind", "job_id", "task_id", "detail"} for row in rows)
    assert {"kind": "launched", "job_id": "job-A", "task_id": "p/A", "detail": ""} in rows
    assert {"kind": "approval", "job_id": "job-A", "task_id": "p/A", "detail": "t-1 git push | publish"} in rows
    assert {"kind": "retry", "job_id": "job-A", "task_id": "p/A", "detail": "boom [infra]"} in rows


def test_bridge_launcher_reads_tickets_like_wait_any(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "jobs.sqlite"
    store = _store.build_store(db)
    _store.build_job_registry(store).write("job-A", "task", tool_name="codex", status="awaiting_approval")
    ticket = _store.build_approval_queue(store).create_ticket(
        task_id="job-A", prompt='agent asks\n  arguments: {"command":"git push","reason":"publish?"}')
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("pending must not shell out"))
    principal = TaskPrincipal(actor="dispatcher", role="orchestrator", projects=("p",))
    rows = BridgeLauncher(str(db), principal, "p").pending()
    assert [(row["approval_id"], row["job_id"], row["command"], row["reason"]) for row in rows] == [
        (ticket.approval_id, "job-A", "git push", "publish?")]


@pytest.mark.parametrize("typed", [True, False])
def test_terminal_job_fails_with_the_rejected_ticket_reason(
    tmp_path: Path, knowledge_store: Store, monkeypatch: pytest.MonkeyPatch, typed: bool,
) -> None:
    db = tmp_path / "jobs.sqlite"
    registry_db = tmp_path / "approvals.sqlite"
    reason = "Publish to the project branch"
    with _store.build_store(db) as store:
        _store.build_job_registry(store).write("job-A", "task", tool_name="codex", status="done")
        if typed:
            with Store(db=str(registry_db)) as approvals:
                queue = ApprovalQueue(approvals, allow_credential_issuance=True)
                request = TypedApprovalRequest.create(
                    task_id="p/A", attempt=1, requested_by="dispatcher", action="native_shell",
                    target=ApprovalTarget(path=str(tmp_path)), policy_revision="test",
                    payload={"registry": "code-bridge", "job_ref": "job-A"},
                )
                ticket = queue.create_typed_ticket(request, operator_only=True)
                credential = queue.issue_approver_credential("owner", {"owner"})
                assert queue.decide_typed_ticket(
                    ticket.approval_id, credential=credential, verdict="rejected", reason=reason,
                )
            _store.write_meta(store, "job-A", {
                "registry_db": str(registry_db), "registry_ticket_ids": [ticket.approval_id],
            })
        else:
            queue = _store.build_approval_queue(store)
            cleanup = queue.create_ticket(task_id="job-A", prompt="unused")
            assert queue.reject_ticket(cleanup.approval_id, actor="bridge-cleanup", channel="cli", reason="job ended")
            ticket = queue.create_ticket(task_id="job-A", prompt="publish?")
            assert queue.reject_ticket(ticket.approval_id, actor="owner", channel="cli", reason=reason)

    service = FakeService()
    service.rows = {"A": {"dependency_ids": [], "status": "todo", "repo": "org/repo"}}
    events: list[str] = []
    principal = TaskPrincipal(actor="dispatcher", role="orchestrator", projects=("p",))
    launcher = BridgeLauncher(str(db), principal, "p")
    monkeypatch.setattr(launcher, "launch", lambda task, claim: "job-A")
    dispatch = Dispatcher(service, principal, launcher, "p", emit=events.append,  # type: ignore[arg-type]
                          task_reader=service.tasks, knowledge_store=knowledge_store)  # type: ignore[arg-type]
    dispatch.pass_once()
    dispatch.pass_once()
    dispatch.pass_once()
    assert service.rows["A"]["status"] == "failed"
    assert len(service.evidence) == 1
    assert service.evidence[0].failure_class == "policy"
    assert service.evidence[0].failure_reason == reason
    assert f"failed p/A {reason} [policy]" in events
    lesson, = DurableKnowledgeBase(knowledge_store).find_lessons("rejected ticket")
    assert (lesson["topic"], lesson["gotchas"], lesson["revision"]) == ("org/repo: rejected ticket", reason, 1)


def _repo(path: Path) -> Any:
    path.mkdir(parents=True)
    def git(*argv: str, cwd: Path = path) -> str:
        return subprocess.run(["git", *argv], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    (path / "f.txt").write_text("base\n")
    git("add", ".")
    git("commit", "-qm", "base")
    return git


def test_staged_litter_is_never_committed(tmp_path: Path) -> None:
    from lazytools.code_bridge._dispatch_workspace import _conclude_workspace
    git = _repo(tmp_path / "r")
    (tmp_path / "r" / "work.txt").write_text("work\n")
    (tmp_path / "r" / "error.log").write_text("account_id=secret\n")
    git("add", "error.log")
    error, commit = _conclude_workspace(tmp_path / "r", "p/A")
    assert error is None and commit
    assert git("show", "--name-only", "--format=", "HEAD") == "work.txt"


def test_publish_checks_branch_and_never_overwrites(tmp_path: Path) -> None:
    from lazytools.code_bridge._dispatch_workspace import _publish
    source_git = _repo(tmp_path / "source")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--local", str(tmp_path / "source"), str(clone)], check=True)
    def git(*argv: str) -> str:
        return subprocess.run(["git", "-C", str(clone), *argv], text=True, capture_output=True, check=True).stdout.strip()
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    git("remote", "add", "dispatch-source", str(tmp_path / "source"))
    git("checkout", "-qb", "dispatch/p/A")
    git("config", "dispatch.branch", "dispatch/p/A")
    (clone / "a.txt").write_text("a\n")
    git("add", "a.txt")
    git("commit", "-qm", "a")
    assert _publish(clone) == ("dispatch/p/A", None)
    source_git("branch", "-f", "dispatch/p/A", "main")  # someone moved the published branch elsewhere
    (tmp_path / "source" / "f.txt").write_text("other\n")
    source_git("checkout", "-q", "dispatch/p/A")
    source_git("commit", "-qam", "other")
    source_git("checkout", "-q", "main")
    branch, error = _publish(clone)
    assert branch is None and error and error.startswith("publish failed")
    git("checkout", "-qb", "elsewhere")
    assert _publish(clone)[1] == "checkout is on elsewhere, expected dispatch/p/A"


class FakeGh:
    """Stands in for `_gh.run_gh`: answers repo view, pr list and pr create, records every call."""

    def __init__(self, *, existing_pr: str | None = None, fail: str | None = None) -> None:
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self.existing_pr, self.fail = existing_pr, fail

    def __call__(self, *args: str, stdin: str | None = None, timeout: float = 0) -> str:
        from lazytools.code_bridge._cloud import CloudError
        self.calls.append((args, stdin))
        if self.fail and args[:2] == tuple(self.fail.split()):
            raise CloudError(f"gh {self.fail} exited 1: HTTP 401 token=ghp_secretsecretsecret")
        if args[:2] == ("repo", "view"):
            return '{"defaultBranchRef": {"name": "main"}}'
        if args[:2] == ("pr", "list"):
            url = self.existing_pr
            return f'[{{"number": 7, "url": "{url}", "headRefOid": "x", "baseRefName": "main"}}]' if url else "[]"
        if args[:2] == ("pr", "create"):
            self.existing_pr = "https://github.com/o/r/pull/7"
            return self.existing_pr + "\n"
        raise AssertionError(f"unexpected gh call {args}")

    def created(self) -> list[tuple[tuple[str, ...], str | None]]:
        return [call for call in self.calls if call[0][:2] == ("pr", "create")]


def _landing_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gh: FakeGh, *, task_id: str = "p/A",
) -> tuple[Path, Path, Any, Any]:
    """A source repo whose origin is a GitHub URL pushed (via pushInsteadOf) to a local bare remote."""
    from lazytools.code_bridge import _gh
    source = tmp_path / "source"
    _repo(source)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(source), str(remote)], check=True)
    project_id = task_id.split("/", 1)[0]
    principal = TaskPrincipal(actor="dispatcher", role="orchestrator", projects=(project_id,))
    bridge = BridgeLauncher(None, principal, project_id, repo_roots={"o/r": source}, worktree_root=tmp_path / "trees")
    clone = bridge._checkout({"task_id": task_id, "repo": "o/r"})
    # Set on the clone only: a GitHub origin on the source would be fetched over the network.
    bridge._git(clone, "remote", "set-url", "origin", "https://github.com/o/r.git")
    bridge._git(clone, "config", f"url.{remote.as_uri()}.pushInsteadOf", "https://github.com/o/r.git")
    (clone / "work.txt").write_text("task work\n")
    bridge._git(clone, "add", "work.txt")
    bridge._git(clone, "commit", "-qm", "Task work")
    monkeypatch.setattr(_gh, "run_gh", gh)

    def remote_git(*argv: str) -> str:
        return subprocess.run(["git", "--git-dir", str(remote), *argv], text=True, capture_output=True,
                              check=False).stdout.strip()
    return clone, remote, bridge, remote_git


def _land_run(clone: Path, events: list[str], captured: list[Any]) -> FakeService:
    service = FakeService()
    service.rows["A"].update(text="Add landing to the dispatcher\nMore detail.",
                             acceptance_criteria=["PR opened", "branch pushed"])
    service.record_completion_evidence = lambda *args: captured.append(args[-1])  # type: ignore[method-assign]
    launcher = FakeLauncher(service)
    dispatch = runner(service, launcher, events)
    dispatch.land = True
    dispatch.pass_once()
    launcher.finish("A")
    launcher.jobs["job-A"]["cwd"] = str(clone)
    dispatch.pass_once()
    return service


def test_land_pushes_the_published_branch_and_opens_a_pr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gh = FakeGh()
    clone, _remote, bridge, remote_git = _landing_setup(tmp_path, monkeypatch, gh)
    events: list[str] = []
    captured: list[Any] = []
    service = _land_run(clone, events, captured)

    head = bridge._git(clone, "rev-parse", "HEAD")
    assert service.rows["A"]["status"] == "done"
    assert remote_git("rev-parse", "refs/heads/dispatch/p/A") == head
    assert remote_git("rev-parse", "refs/heads/main") != head  # the default branch is never pushed
    (args, body), = gh.created()
    assert args[args.index("--head") + 1] == "dispatch/p/A"
    assert args[args.index("--base") + 1] == "main"
    assert args[args.index("--title") + 1] == "Add landing to the dispatcher"
    assert body and "p/A" in body and "- PR opened" in body and "- branch pushed" in body
    assert "landed p/A https://github.com/o/r/pull/7" in events
    assert events.index("published p/A dispatch/p/A") < events.index("done p/A") < events.index(
        "landed p/A https://github.com/o/r/pull/7",
    )
    evidence, = captured
    assert evidence.state == "accepted" and evidence.pr_url is None and evidence.land_failure is None

    # A later revision of the branch updates the open PR: push only, no second PR.
    from lazytools.code_bridge._dispatch_workspace import _land
    (clone / "work.txt").write_text("revised\n")
    bridge._git(clone, "commit", "-qam", "Revise")
    assert _land(clone, "dispatch/p/A", "p/A", {}, []) == ("https://github.com/o/r/pull/7", None)
    assert remote_git("rev-parse", "refs/heads/dispatch/p/A") == bridge._git(clone, "rev-parse", "HEAD")
    assert len(gh.created()) == 1


def test_land_uses_task_title_and_sanitizes_check_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lazytools.code_bridge._dispatch_workspace import _land

    gh = FakeGh()
    clone, _remote, _bridge, _remote_git = _landing_setup(tmp_path, monkeypatch, gh)
    assert _land(clone, "dispatch/p/A", "p/A", {
        "title": "Chosen task title", "text": "Fallback sentence.",
    }, [r"D:\\work\\venv\\Scripts\\python.exe -m pytest D:\\work\\repo\\tests",
        "pytest --rootdir=/home/me/repo tests/test_x.py"])[1] is None
    (args, body), = gh.created()
    assert args[args.index("--title") + 1] == "Chosen task title"
    assert "python.exe -m pytest <local>" in body
    assert "D:\\work" not in body
    assert "pytest --rootdir=<local> tests/test_x.py" in body


def test_land_shortens_explicit_title_at_word_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lazytools.code_bridge._dispatch_workspace import _land

    gh = FakeGh()
    clone, _remote, _bridge, _remote_git = _landing_setup(tmp_path, monkeypatch, gh)
    assert _land(clone, "dispatch/p/A", "p/A", {"title": "word " * 20}, [])[1] is None
    (args, _body), = gh.created()
    title = args[args.index("--title") + 1]
    assert len(title) <= 72
    assert title == "word " * 13 + "word"


@pytest.mark.parametrize(("text", "expected"), [
    ("A concise first sentence. More details follow.", "A concise first sentence"),
    ("word " * 20, "word " * 13 + "word"),
    ("Fix _land.py in v1.2\nMore detail.", "Fix _land.py in v1.2"),
])
def test_land_falls_back_to_first_sentence_at_word_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str, expected: str,
) -> None:
    from lazytools.code_bridge._dispatch_workspace import _land

    gh = FakeGh()
    clone, _remote, _bridge, _remote_git = _landing_setup(tmp_path, monkeypatch, gh)
    assert _land(clone, "dispatch/p/A", "p/A", {"text": text}, [])[1] is None
    (args, _body), = gh.created()
    assert args[args.index("--title") + 1] == expected


def test_land_can_outlast_the_claim_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lazytools.code_bridge import _gh
    from lazytools.code_bridge._dispatch import _tasks
    from lazytools.connectors.projects import ProjectsTools
    from lazytools.projects.task_service import PlacementEvidence, ProjectTaskService

    gh = FakeGh()
    clone, _remote, _bridge, _remote_git = _landing_setup(tmp_path, monkeypatch, gh, task_id="landing/A")
    path = str(tmp_path / "projects.sqlite")
    projects = ProjectsTools(store_db=path, allow_write=True)
    assert "AS A DRAFT" in projects.projects_create("landing", "Landing", "Test landing after completion")
    plan = [
        {"task_id": "A", "text": "Publish A", "acceptance_criteria": ["A is published"]},
        {"task_id": "B", "text": "Use A", "acceptance_criteria": ["B uses A"], "dependency_ids": ["A"]},
    ]
    args = ("landing", "Publish and use A", "2027-01-01T00:00:00Z", plan)
    verdict = projects.projects_review_plan(*args)
    assert verdict["ok"], verdict
    assert projects.projects_promote(*args).startswith("promoted")

    now = 1000.0

    def clock() -> float:
        return now

    principal = TaskPrincipal(actor="dispatcher", role="orchestrator", projects=("landing",))
    service = ProjectTaskService(path, lease_seconds=60, clock=clock)
    for task in _tasks(service, "landing"):
        service.record_placement(task.task_id, task.revision, PlacementEvidence(
            placement="local", eligible=True, reason="test runner", reference="test-placement",
            expires_at=2000, engine_family="codex",
        ))
    launcher = FakeLauncher(FakeService())
    events: list[str] = []
    dispatch = Dispatcher(service, principal, launcher, "landing", clock=clock, emit=events.append, land=True)
    dispatch.pass_once()
    task = next(task for task in _tasks(service, "landing") if task.task_id == "landing/A")
    assert task.dispatch is not None, events
    receipt = task.dispatch["receipt"]
    service.begin_submit(principal, task.task_id, receipt["claim_token"])
    service.record_submit(principal, task.task_id, receipt["claim_token"], "job-A")
    launcher.finish("A")
    launcher.jobs["job-A"]["cwd"] = str(clone)
    now = receipt["lease_expires_at"] - 1

    def slow_gh(*args: str, **kwargs: Any) -> str:
        nonlocal now
        if args[:2] == ("repo", "view"):
            now += 2
            assert [row["task_id"] for row in service.next_ready(principal, "landing")["tasks"]] == ["landing/B"]
        return gh(*args, **kwargs)

    monkeypatch.setattr(_gh, "run_gh", slow_gh)
    dispatch.pass_once()

    assert now > receipt["lease_expires_at"]
    assert "landed landing/A https://github.com/o/r/pull/7" in events, events
    assert launcher.launches == ["A", "B"]
    task = next(task for task in _tasks(service, "landing") if task.task_id == "landing/A")
    assert task.status == "done"
    settled = task.data["dispatches"][-1]
    assert settled["completion"]["result"]["state"] == "accepted"
    evidence, = settled["evidence"].values()
    assert evidence["state"] == "accepted" and "pr_url" not in evidence and "land_failure" not in evidence


def test_slow_landing_does_not_consume_another_tasks_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lazytools.code_bridge import _gh
    from lazytools.code_bridge._dispatch import _tasks
    from lazytools.connectors.projects import ProjectsTools
    from lazytools.projects.task_service import PlacementEvidence, ProjectTaskService

    gh = FakeGh()
    clone_a, remote, bridge, remote_git = _landing_setup(tmp_path, monkeypatch, gh, task_id="landing/A")
    clone_b = bridge._checkout({"task_id": "landing/B", "repo": "o/r"})
    bridge._git(clone_b, "remote", "set-url", "origin", "https://github.com/o/r.git")
    bridge._git(clone_b, "config", f"url.{remote.as_uri()}.pushInsteadOf", "https://github.com/o/r.git")
    (clone_b / "other.txt").write_text("task B work\n")
    bridge._git(clone_b, "add", "other.txt")
    bridge._git(clone_b, "commit", "-qm", "Task B work")
    path = str(tmp_path / "projects.sqlite")
    projects = ProjectsTools(store_db=path, allow_write=True)
    assert "AS A DRAFT" in projects.projects_create("landing", "Landing", "Test independent landings")
    plan = [
        {"task_id": "A", "text": "Publish A", "acceptance_criteria": ["A is published"]},
        {"task_id": "B", "text": "Publish B", "acceptance_criteria": ["B is published"]},
    ]
    args = ("landing", "Publish A and B", "2027-01-01T00:00:00Z", plan)
    verdict = projects.projects_review_plan(*args)
    assert verdict["ok"], verdict
    assert projects.projects_promote(*args).startswith("promoted")

    now = 1000.0

    def clock() -> float:
        return now

    principal = TaskPrincipal(actor="dispatcher", role="orchestrator", projects=("landing",))
    service = ProjectTaskService(path, lease_seconds=60, clock=clock)
    for task in _tasks(service, "landing"):
        service.record_placement(task.task_id, task.revision, PlacementEvidence(
            placement="local", eligible=True, reason="test runner", reference="test-placement",
            expires_at=2000, engine_family="codex",
        ))
    launcher = FakeLauncher(FakeService())
    events: list[str] = []
    dispatch = Dispatcher(service, principal, launcher, "landing", parallel=2, clock=clock,
                          emit=events.append, land=True)
    dispatch.pass_once()
    assert sorted(launcher.launches) == ["A", "B"], events
    leases = []
    for task in _tasks(service, "landing"):
        assert task.dispatch is not None, events
        receipt = task.dispatch["receipt"]
        service.begin_submit(principal, task.task_id, receipt["claim_token"])
        service.record_submit(principal, task.task_id, receipt["claim_token"], f"job-{task.task_id[-1]}")
        leases.append(receipt["lease_expires_at"])
    for name, clone in (("A", clone_a), ("B", clone_b)):
        launcher.finish(name)
        launcher.jobs[f"job-{name}"]["cwd"] = str(clone)
    now = min(leases) - 1

    def slow_gh(*args: str, **kwargs: Any) -> str:
        nonlocal now
        if args[:2] == ("repo", "view"):
            now += 2  # each landing outlasts every remaining lease
        return gh(*args, **kwargs)

    monkeypatch.setattr(_gh, "run_gh", slow_gh)
    dispatch.pass_once()

    assert now > max(leases)
    for task in _tasks(service, "landing"):
        assert task.status == "done", events
        assert task.data["dispatches"][-1]["completion"]["result"]["state"] == "accepted"
    assert events.index("done landing/A") < events.index("landed landing/A https://github.com/o/r/pull/7")
    assert events.index("done landing/B") < events.index("landed landing/A https://github.com/o/r/pull/7")
    assert not [line for line in events if line.startswith(("failed", "land-failed"))], events
    assert remote_git("rev-parse", "refs/heads/dispatch/landing/B") == bridge._git(clone_b, "rev-parse", "HEAD")


@pytest.mark.parametrize("failure", ["push", "gh", "push-timeout"])
def test_land_failure_keeps_the_task_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    gh = FakeGh(fail="pr create" if failure == "gh" else None)
    clone, remote, _bridge, remote_git = _landing_setup(tmp_path, monkeypatch, gh)
    if failure == "push":  # someone else's commit on the remote branch: no fast-forward, never forced
        other = tmp_path / "other"
        subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
        for argv in (["config", "user.email", "o@example.com"], ["config", "user.name", "O"],
                     ["commit", "-q", "--allow-empty", "-m", "elsewhere"],
                     ["push", "-q", "origin", "HEAD:refs/heads/dispatch/p/A"]):
            subprocess.run(["git", "-C", str(other), *argv], check=True)
    foreign = remote_git("rev-parse", "refs/heads/dispatch/p/A")
    if failure == "push-timeout":
        run = subprocess.run

        def timeout_push(argv: list[str], **kwargs: Any) -> Any:
            if argv[3:5] == ["push", "origin"]:
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            return run(argv, **kwargs)

        monkeypatch.setattr(subprocess, "run", timeout_push)
    events: list[str] = []
    captured: list[Any] = []
    service = _land_run(clone, events, captured)

    assert service.rows["A"]["status"] == "done"
    assert "done p/A" in events and not [line for line in events if line.startswith("landed")]
    failed = [line for line in events if line.startswith("land-failed p/A ")]
    assert len(failed) == 1
    assert events.index("done p/A") < events.index(failed[0])
    assert not any(line.startswith("failed p/A") for line in events)
    evidence, = captured
    assert evidence.state == "accepted" and evidence.pr_url is None and evidence.land_failure is None
    assert "ghp_secret" not in failed[0]
    if failure == "push":
        assert "rejected" in failed[0] and not gh.created()
        assert remote_git("rev-parse", "refs/heads/dispatch/p/A") == foreign
    elif failure == "push-timeout":
        assert "timed out after 300 seconds" in failed[0] and not gh.created()
    else:
        assert "pr create" in failed[0]


def test_land_refuses_the_default_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lazytools.code_bridge._dispatch_workspace import _land
    gh = FakeGh()
    clone, _remote, _bridge, remote_git = _landing_setup(tmp_path, monkeypatch, gh)
    before = remote_git("rev-parse", "refs/heads/main")
    pr_url, error = _land(clone, "main", "p/A", {}, [])
    assert pr_url is None and error == "refusing to land on the default branch main"
    assert remote_git("rev-parse", "refs/heads/main") == before and not gh.created()


def test_dispatch_land_flag_parses() -> None:
    assert build_parser().parse_args(["dispatch", "--project", "p", "--land"]).land is True
    assert build_parser().parse_args(["dispatch", "--project", "p"]).land is False


def test_base_is_set_after_dependency_merges(tmp_path: Path) -> None:
    from lazytools.code_bridge._dispatch_workspace import _has_new_commit
    git = _repo(tmp_path / "repo")
    git("checkout", "-qb", "b")
    (tmp_path / "repo" / "b.txt").write_text("b\n")
    git("add", "b.txt")
    git("commit", "-qm", "b")
    b_commit = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")
    (tmp_path / "repo" / "c.txt").write_text("c\n")
    git("add", "c.txt")
    git("commit", "-qm", "c")
    c_commit = git("rev-parse", "HEAD")
    principal = TaskPrincipal(actor="dispatcher", role="orchestrator", projects=("p",))
    launcher = BridgeLauncher(None, principal, "p", repo_roots={"r": tmp_path / "repo"}, worktree_root=tmp_path / "trees")
    checkout = launcher._checkout({"task_id": "p/D", "repo": "r", "dependencies": [
        {"artifact_ref": b_commit}, {"artifact_ref": c_commit}]})
    head = launcher._git(checkout, "rev-parse", "HEAD")
    assert launcher.base_commits["p/D"] == launcher._git(checkout, "rev-parse", "dispatch-base") == head
    assert not _has_new_commit(checkout)  # the clean dependency merge is not the task's work


def test_expired_running_job_is_cancelled_once() -> None:
    service, events = FakeService(), []  # type: FakeService, list[str]
    launcher = FakeLauncher(service)
    cancelled: list[str] = []
    launcher.cancel = lambda job_ref: cancelled.append(job_ref) or True  # type: ignore[attr-defined]
    dispatch = runner(service, launcher, events)
    dispatch.pass_once()
    service.claims["A"]["receipt"]["lease_expires_at"] = 0
    dispatch.pass_once()
    dispatch.pass_once()
    assert cancelled == ["job-A"]
    assert any(event.startswith("cancelled p/A") for event in events)


@pytest.mark.parametrize("alive", [True, False])
def test_pending_approval_holds_only_a_live_worker(monkeypatch: pytest.MonkeyPatch, alive: bool) -> None:
    from lazytools.code_bridge import _dispatch

    monkeypatch.setattr(_dispatch, "_pid_alive", lambda pid: alive and pid == 4321)
    service, events = FakeService(), []  # type: FakeService, list[str]
    launcher = FakeLauncher(service)
    tickets = [{"approval_id": "ticket-1", "job_id": "job-A"}]
    launcher.pending = lambda: tickets  # type: ignore[attr-defined]
    cancelled: list[str] = []
    launcher.cancel = lambda job_ref, **_: cancelled.append(job_ref) or False  # type: ignore[attr-defined]
    dispatch = runner(service, launcher, events)
    dispatch.clock = lambda: 2_000
    dispatch.pass_once()
    launcher.jobs["job-A"].update(heartbeat_at=1_000, pid=4321)
    dispatch.pass_once()
    dispatch.pass_once()
    assert cancelled == ([] if alive else ["job-A", "job-A"])
    assert [event for event in events if event == "waiting p/A awaiting approval ticket-1"] == (
        ["waiting p/A awaiting approval ticket-1"] if alive else [])
    if alive:
        tickets.clear()
        dispatch.pass_once()
        assert cancelled == ["job-A"]


def test_stale_heartbeat_cancels_and_retries_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    service, events = FakeService(), []  # type: FakeService, list[str]
    launcher = FakeLauncher(service)
    dispatch = runner(service, launcher, events)
    dispatch.clock = lambda: 2_000
    dispatch.pass_once()
    launcher.jobs["job-A"]["heartbeat_at"] = 2_000 - 601
    cancellations: list[str] = []

    def cancel(job_ref: str, *, stale_before: float) -> bool:
        assert stale_before == 1_400
        cancellations.append(job_ref)
        if len(cancellations) == 1:
            return False
        launcher.jobs[job_ref].update(status="interrupted", error=f"cancelled by operator: {STALE_HEARTBEAT}")
        return True

    monkeypatch.setattr(launcher, "cancel", cancel, raising=False)
    dispatch.pass_once()
    assert service.rows["A"]["status"] == "claimed"
    dispatch.pass_once()
    assert cancellations == ["job-A", "job-A"]
    assert launcher.launches == ["A", "A"]
    assert any(event.startswith("retry p/A cancelled by operator: ") and "[infra]" in event for event in events)


@pytest.mark.parametrize("status,heartbeat", [("running", 1_400), ("running", None), ("awaiting_approval", 1_000)])
def test_dispatch_leaves_jobs_without_a_stale_running_heartbeat(status: str, heartbeat: float | None) -> None:
    service = FakeService()
    launcher = FakeLauncher(service)
    dispatch = runner(service, launcher, [])
    dispatch.clock = lambda: 2_000
    dispatch.pass_once()
    job = launcher.jobs["job-A"]
    job["status"] = status
    if heartbeat is not None:
        job["heartbeat_at"] = heartbeat
    cancelled: list[str] = []
    launcher.cancel = lambda job_ref, **_: cancelled.append(job_ref) or True  # type: ignore[attr-defined]

    dispatch.pass_once()

    assert cancelled == []
    assert service.rows["A"]["status"] == "claimed"


@pytest.mark.parametrize("engine_alive,tool_started,stale", [
    (True, 2_000 - 20 * 60, False),  # long shell command in a live engine
    (False, 2_000 - 20 * 60, True),  # engine process died with the tool call pending
    (True, 2_000 - 61 * 60, True),   # tool call ran past its own bound
])
def test_tool_call_in_progress_keeps_only_a_live_engine_alive(
    monkeypatch: pytest.MonkeyPatch, engine_alive: bool, tool_started: float, stale: bool,
) -> None:
    from lazytools.code_bridge import _dispatch

    # ``pid`` is the job worker driving the engine; tool_calls come from its tool call events.
    monkeypatch.setattr(_dispatch, "_pid_alive", lambda pid: engine_alive and pid == 4321)
    service = FakeService()
    launcher = FakeLauncher(service)
    dispatch = runner(service, launcher, [])
    dispatch.clock = lambda: 2_000
    dispatch.pass_once()
    launcher.jobs["job-A"].update(heartbeat_at=tool_started, pid=4321,
                                  tool_calls={"toolu_1": {"name": "Bash", "started_at": tool_started}})
    cancelled: list[str] = []
    launcher.cancel = lambda job_ref, **_: cancelled.append(job_ref) or False  # type: ignore[attr-defined]

    dispatch.pass_once()

    assert cancelled == (["job-A"] if stale else [])


def test_dispatch_accepts_a_stale_minutes_override() -> None:
    parser = build_parser()
    args = ["dispatch", "--project", "p"]
    assert parser.parse_args(args).stale_minutes == 10
    assert parser.parse_args([*args, "--stale-minutes", "4"]).stale_minutes == 4


def test_litter_only_workspace_counts_as_clean(tmp_path: Path) -> None:
    from lazytools.code_bridge._dispatch_workspace import _conclude_workspace
    _repo(tmp_path / "r")
    (tmp_path / "r" / "error.log").write_text("noise\n")
    assert _conclude_workspace(tmp_path / "r", "p/A") == (None, None)


def test_without_dependencies_the_base_is_the_source_checkout_head(tmp_path: Path) -> None:
    git = _repo(tmp_path / "repo")
    git("checkout", "-qb", "feature")
    (tmp_path / "repo" / "feature.txt").write_text("x\n")
    git("add", "feature.txt")
    git("commit", "-qm", "feature")
    head = git("rev-parse", "HEAD")
    principal = TaskPrincipal(actor="dispatcher", role="orchestrator", projects=("p",))
    launcher = BridgeLauncher(None, principal, "p", repo_roots={"r": tmp_path / "repo"}, worktree_root=tmp_path / "trees")
    checkout = launcher._checkout({"task_id": "p/A", "repo": "r"})
    assert subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True,
                          capture_output=True, check=True).stdout.strip() == head


def test_failed_cancel_is_retried_on_the_next_pass() -> None:
    service, events = FakeService(), []  # type: FakeService, list[str]
    launcher = FakeLauncher(service)
    results = iter([False, True])
    attempts: list[str] = []
    launcher.cancel = lambda job_ref: attempts.append(job_ref) or next(results)  # type: ignore[attr-defined]
    dispatch = runner(service, launcher, events)
    dispatch.pass_once()
    service.claims["A"]["receipt"]["lease_expires_at"] = 0
    dispatch.pass_once()
    dispatch.pass_once()
    dispatch.pass_once()
    assert attempts == ["job-A", "job-A"]


@pytest.mark.parametrize("claude_projection,claude_age,placed", [
    (110, 0, ("A", "C")),
    (60, 0, ("A", "B", "C")),
    (60, 3600, ("A",)),
])
def test_place_checks_quota_when_a_slot_was_assigned_in_the_same_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claude_projection: int, claude_age: int, placed: tuple[str, ...],
) -> None:
    recorded: list[tuple[str, str]] = []

    class PlacingService:
        lease_seconds = 900

        def record_placement(self, task_id: str, revision: str, evidence: Any) -> None:
            recorded.append((task_id, evidence.engine_family))

    events: list[str] = []
    tasks = [Task(f"p/{name}", "r1", "todo", fields, None) for name, fields in (
        ("A", {"tier": "thinking"}),
        ("B", {"tier": "thinking"}),
        ("C", {"tier": "thinking", "fallback": "allow"}),
    )]
    router = _pace_router(tmp_path, monkeypatch, claude_projection=claude_projection, claude_age=claude_age)
    dispatch = Dispatcher(PlacingService(), TaskPrincipal(actor="d", role="orchestrator", projects=("p",)),
                          None, "p", emit=events.append, clock=lambda: 1000.0,  # type: ignore[arg-type]
                          router=router, capacity=lambda family: 1 if family == "codex" else None)
    picks = {}
    assert dispatch._place(tasks[0], "local", {}, picks) is not None
    busy = dispatch._local_busy([{"engine_family": "codex"}])
    for task in tasks[1:]:
        dispatch._place(task, "local", busy, picks)
    assert recorded == [(f"p/{name}", "codex" if name == "A" else "claude_code") for name in placed]
    if "B" not in placed:
        assert any(line.startswith("waiting p/B ") and "wait_for_slot" in line for line in events)
    if claude_projection == 110:
        assert any(line.startswith("placed p/C ") and "over_pace_fallback_allowed" in line for line in events)


def _pace_router(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, claude_projection: int = 110, claude_age: int = 0,
) -> Any:
    now = datetime.now(UTC)
    claude_observed = now - timedelta(seconds=claude_age)
    readings = {
        "codex": TelemetryReading("codex", "test", now, (WindowReading("codex/10080m", 23, 10080, now + timedelta(hours=84)),)),
        "claude_code": TelemetryReading("claude_code", "test", claude_observed, (
            WindowReading("weekly/all models", (claude_projection - 1) / 2, 10080, claude_observed + timedelta(hours=84)),
        )),
    }
    monkeypatch.setattr(_routing, "read_readings", lambda: readings)
    monkeypatch.setattr(_routing, "load_default_tiers", lambda: load_tiers(DEFAULT_PATH))
    monkeypatch.setattr(_concurrency, "engine_at_limit", lambda *_: [])
    return _routers(tmp_path / "bridge.sqlite", {})[0]


def test_check_repair_waits_for_its_original_engine_slot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    router = _pace_router(tmp_path, monkeypatch, claude_projection=60)
    recorded: list[Any] = []

    class PlacingService:
        lease_seconds = 900

        def record_placement(self, task_id: str, revision: str, evidence: Any) -> None:
            recorded.append(evidence)

    events: list[str] = []
    task = Task("p/A", "r1", "todo", {"tier": "thinking", "fallback": "allow",
                "attempt_continuation": {"engine": "codex", "native_id": "thread",
                                         "check_failures": ["failed"]}}, None)
    dispatch = Dispatcher(PlacingService(), TaskPrincipal(actor="d", role="orchestrator", projects=("p",)),
                          None, "p", router=router, capacity=lambda _: 1, emit=events.append,  # type: ignore[arg-type]
                          clock=lambda: 1000.0)
    busy = dispatch._local_busy([{"engine_family": "codex"}])
    dispatch._place(task, "local", busy, {})
    assert not recorded
    assert any(event.startswith("waiting p/A ") and "wait_for_slot" in event for event in events)
    dispatch._place(task, "local", {}, {})
    assert recorded[0].engine_family == "codex"


@pytest.mark.parametrize("task_fallback", [{}, {"fallback": "allow"}])
def test_dispatch_retries_a_waiting_writer_when_its_slot_frees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task_fallback: dict[str, str],
) -> None:
    router = _pace_router(tmp_path, monkeypatch)
    slot_busy = True
    monkeypatch.setattr(_concurrency, "engine_at_limit",
                        lambda name, _: [{"job_id": "busy"}] if slot_busy and name == "codex" else [])
    path = tmp_path / "projects.sqlite"
    projects = ProjectsTools(store_db=str(path), allow_write=True)
    projects.projects_create("pace", "Pace", "Place a writer", engine_preference=["codex", "claude"])
    plan = [
        {"task_id": "A", "text": "Write A", "tier": "writing", "acceptance_criteria": ["A exists"], **task_fallback},
        {"task_id": "B", "text": "Write B", "dependency_ids": ["A"], "acceptance_criteria": ["B exists"]},
    ]
    args = ("pace", "A and B exist", "2027-01-01T00:00:00Z", plan)
    assert projects.projects_review_plan(*args)["ok"]
    assert projects.projects_promote(*args).startswith("promoted")
    service = ProjectTaskService(path)
    principal = TaskPrincipal(actor="d", role="orchestrator", projects=("pace",))
    launcher, events = FakeLauncher(FakeService()), []
    dispatch = Dispatcher(service, principal, launcher, "pace", router=router, emit=events.append)
    assert _tasks(service, "pace")[0].data["engine_preference"] == ["codex", "claude"]

    dispatch.pass_once()
    if task_fallback:
        assert "over_pace_fallback_allowed" in events[0]
        assert _tasks(service, "pace")[0].dispatch["receipt"]["engine_family"] == "claude_code"
    else:
        assert not launcher.launches and _tasks(service, "pace")[0].status == "todo"
        assert events[0].startswith("waiting pace/A local tier=writing | wait_for_slot")
        assert "codex weekly projected 47% | claude weekly projected 110%" in events[0]
        slot_busy = False
        dispatch.pass_once()
        assert any(line.startswith("placed pace/A local codex tier=writing | weekly_pace")
                   and "codex weekly projected 47% | claude weekly projected 110%" in line for line in events)
        assert _tasks(service, "pace")[0].dispatch["receipt"]["engine_family"] == "codex"
        assert _tasks(service, "pace")[0].dispatch["receipt"]["model"] == "gpt-6.1-terra"
    assert launcher.launches == ["A"]


@pytest.mark.parametrize("change", ["edit", "commit"])
def test_checks_that_change_the_checkout_reject_the_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    source = tmp_path / "source"
    source_git = _repo(source)
    principal = TaskPrincipal(actor="dispatcher", role="orchestrator", projects=("p",))
    bridge = BridgeLauncher(None, principal, "p", repo_roots={"r": source}, worktree_root=tmp_path / "trees")
    clone = bridge._checkout({"task_id": "p/A", "repo": "r"})
    (clone / "work.txt").write_text("task work\n")
    bridge._git(clone, "add", "work.txt")
    bridge._git(clone, "commit", "-qm", "Task work")

    service, events = FakeService(), []  # type: FakeService, list[str]
    service.rows["A"]["checks"] = ['"fixer" run']
    launcher = FakeLauncher(service)
    dispatch = runner(service, launcher, events)
    dispatch.pass_once()
    launcher.finish("A")
    launcher.jobs["job-A"]["cwd"] = str(clone)

    real_run = subprocess.run

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        if argv[0] != "fixer":
            return real_run(argv, **kwargs)
        (clone / "work.txt").write_text("fixed by the check\n")
        if change == "commit":
            real_run(["git", "-C", str(clone), "commit", "-qam", "check fix"], check=True)
        return subprocess.CompletedProcess(argv, 0, b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    dispatch.pass_once()

    expected = "moved HEAD" if change == "commit" else "modified tracked files: work.txt"
    assert any(expected in event for event in events), events
    assert not any(event.startswith("done") for event in events), events
    assert not source_git("branch", "--list", "dispatch/p/A")


def test_check_commands_keep_windows_backslashes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lazytools.code_bridge import _dispatch_workspace, _navigator
    monkeypatch.setattr(_navigator.os, "name", "nt")
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _dispatch_workspace._check_failure(r".venv\Scripts\python.exe -m pytest tests\test_api.py", tmp_path) is None
    assert seen == [[r".venv\Scripts\python.exe", "-m", "pytest", r"tests\test_api.py"]]


def _retrying_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A dispatcher with the real clone handling and a fake bridge CLI; one task p/A in repo r."""
    source = tmp_path / "source"
    source_git = _repo(source)
    monkeypatch.setenv("LAZYBRIDGE_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    principal = TaskPrincipal(actor="dispatcher", role="orchestrator", projects=("p",))
    bridge = BridgeLauncher(None, principal, "p", repo_roots={"r": source}, worktree_root=tmp_path / "trees")
    jobs: dict[str, dict[str, Any]] = {}
    monkeypatch.setattr(bridge, "job", jobs.get)
    monkeypatch.setattr(bridge, "find", lambda task_id, attempt: jobs.get("job-A"))
    monkeypatch.setattr(bridge, "pending", lambda: [])
    monkeypatch.setattr(bridge, "rejected_ticket_reason", lambda job: "")
    service = FakeService()
    service.rows = {"A": {"dependency_ids": [], "status": "todo", "repo": "r", "text": "Do A"}}
    numbers = iter(range(1, 10))
    real_claim = service.claim

    def claim(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = real_claim(*args, **kwargs)
        result["claim"]["attempt"] = next(numbers)
        jobs["job-A"] = {"job_id": "job-A", "status": "running"}
        return result

    service.claim = claim  # type: ignore[method-assign]
    launched: list[list[str]] = []
    real_run = subprocess.run

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        if argv[1:3] == ["-m", "lazytools.code_bridge"]:
            launched.append(argv)
            return subprocess.CompletedProcess(argv, 0, "a" * 32 + "\n", "")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    events: list[str] = []
    dispatch = Dispatcher(service, principal, bridge, "p", emit=events.append,  # type: ignore[arg-type]
                          task_reader=service.tasks)  # type: ignore[arg-type]
    return dispatch, service, jobs, launched, events, source_git, tmp_path / "trees" / "p" / "A"


def _flag(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_crash_preserves_work_and_the_retry_resumes_the_same_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lazybridge.engines.sessions import default_session_registry

    from lazytools.code_bridge._dispatch_launch import RESUME_PROMPT
    dispatch, service, jobs, launched, events, source_git, clone = _retrying_dispatch(tmp_path, monkeypatch)
    dispatch.pass_once()
    assert _flag(launched[0], "--session") == "dispatch-attempt-1"
    (clone / "work.txt").write_text("half done\n")
    (clone / "error.log").write_text("account_id=secret\n")
    jobs["job-A"].update(status="failed", error="Codex App Server exited before completing the request",
                         cwd=str(clone), session_name="dispatch-attempt-1", native_id="thread-1", engine="codex")
    dispatch.pass_once()

    branch = "dispatch-attempt/p/A/1"
    assert f"preserved p/A {branch}" in events
    assert service.evidence[-1].preserved_branch == branch
    assert service.rows["A"]["attempt_continuation"]["preserved_branch"] == branch

    def git(*argv: str) -> str:
        return subprocess.run(["git", "-C", str(clone), *argv], text=True, capture_output=True, check=True).stdout.strip()
    assert (clone / "work.txt").read_text() == "half done\n"
    assert git("rev-parse", branch) == git("rev-parse", "HEAD")
    assert git("branch", "--show-current") == "dispatch/p/A"
    assert git("show", "--name-only", "--format=", "HEAD") == "work.txt"  # litter is never committed
    assert git("rev-list", "--count", "dispatch-base..HEAD") == "1"  # the kept work counts as the task's
    assert source_git("rev-parse", branch) == git("rev-parse", "HEAD")
    retry = launched[1]
    assert _flag(retry, "--session") == "dispatch-attempt-1" and _flag(retry, "--task") == RESUME_PROMPT
    assert default_session_registry().resolve("codex", clone, "dispatch-attempt-1") == "thread-1"
    assert service.rows["A"]["attempt_continuation"]["infra_retries"] == 1

    # The engine cannot load the conversation: a fresh one starts on the preserved work.
    jobs["job-A"].update(status="failed", cwd=str(clone), session_name="dispatch-attempt-1", native_id="thread-1",
                         engine="codex", error="No bridge policy binding for this native session; use a new session alias")
    dispatch.pass_once()
    assert any(event.startswith("retry p/A resume failed") for event in events)
    fresh = launched[2]
    assert _flag(fresh, "--session") == "dispatch-attempt-3"
    text = _flag(fresh, "--task")
    assert "interrupted by the system" in text and "work.txt" in text and text.endswith("Do A")
    assert "work of interrupted attempt 1" in text
    assert default_session_registry().resolve("codex", clone, "dispatch-attempt-3") is None
    assert git("show", "--name-only", "--format=", "HEAD") == "work.txt"
    assert service.rows["A"]["attempt_continuation"]["infra_retries"] == 1


def test_crash_without_a_recorded_conversation_retries_fresh_on_the_kept_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch, _, jobs, launched, events, _, clone = _retrying_dispatch(tmp_path, monkeypatch)
    dispatch.pass_once()
    (clone / "work.txt").write_text("half done\n")
    jobs["job-A"].update(status="interrupted", error=f"cancelled by operator: {STALE_HEARTBEAT}", cwd=str(clone),
                         engine="codex", native_id=None)
    dispatch.pass_once()
    assert "preserved p/A dispatch-attempt/p/A/1" in events
    assert _flag(launched[1], "--session") == "dispatch-attempt-2"
    assert "work.txt" in _flag(launched[1], "--task")
    assert (clone / "work.txt").read_text() == "half done\n"


def test_check_failure_does_not_retry_when_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dispatch, service, jobs, launched, events, _, clone = _retrying_dispatch(tmp_path, monkeypatch)
    service.rows["A"]["checks"] = [f'"{sys.executable}" -c "raise SystemExit(3)"']
    service.rows["A"]["max_check_retries"] = 0
    service.rows["A"]["tier"] = "critical"
    dispatch.pass_once()
    (clone / "work.txt").write_text("done\n")
    jobs["job-A"].update(status="done", cwd=str(clone), session_name="dispatch-attempt-1", native_id="thread-1",
                         engine="codex", error="App Server exited")
    dispatch.pass_once()
    assert service.rows["A"]["status"] == "failed" and service.reasons == []
    assert not any(event.startswith(("preserved", "retry")) for event in events)
    assert service.evidence[-1].preserved_branch is None
    assert len(launched) == 1
    assert not subprocess.run(["git", "-C", str(clone), "branch", "--list", "dispatch-attempt/*"],
                              text=True, capture_output=True, check=True).stdout.strip()


def test_preserve_never_commits_litter(tmp_path: Path) -> None:
    from lazytools.code_bridge._dispatch_workspace import _preserve_attempt
    git = _repo(tmp_path / "r")
    git("config", "dispatch.branch", "main")
    git("tag", "dispatch-base")
    (tmp_path / "r" / "error.log").write_text("account_id=secret\n")
    (tmp_path / "r" / "__pycache__").mkdir()
    (tmp_path / "r" / "__pycache__" / "m.pyc").write_bytes(b"x")
    git("add", "-f", "error.log")
    assert _preserve_attempt(tmp_path / "r", "p/A", 1) == (None, None)  # litter alone is nothing to keep
    (tmp_path / "r" / "work.txt").write_text("work\n")
    preserved, error = _preserve_attempt(tmp_path / "r", "p/A", 1)
    assert error is None and preserved and preserved.files == ("work.txt",)
    assert git("show", "--name-only", "--format=", "HEAD") == "work.txt"


def test_preserved_conflicted_merge_stays_unresolved_for_the_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lazytools.code_bridge._dispatch_launch import RESUME_PROMPT
    from lazytools.code_bridge._dispatch_workspace import _conclude_workspace
    dispatch, service, jobs, launched, events, _, clone = _retrying_dispatch(tmp_path, monkeypatch)
    dispatch.pass_once()

    def git(*argv: str, check: bool = True) -> str:
        return subprocess.run(["git", "-C", str(clone), *argv], text=True, capture_output=True,
                              check=check).stdout.strip()
    # The engine crashes while resolving a dependency merge: markers remain in f.txt.
    git("checkout", "-qb", "dep")
    (clone / "f.txt").write_text("dep\n")
    git("commit", "-qam", "dep")
    git("checkout", "-q", "dispatch/p/A")
    (clone / "f.txt").write_text("task\n")
    git("commit", "-qam", "task side")
    git("merge", "--no-edit", "dep", check=False)
    assert "<<<<<<<" in (clone / "f.txt").read_text()
    jobs["job-A"].update(status="failed", error="Codex App Server exited before completing the request",
                         cwd=str(clone), session_name="dispatch-attempt-1", native_id="thread-1", engine="codex")
    dispatch.pass_once()

    assert "preserved p/A dispatch-attempt/p/A/1" in events
    assert "UNRESOLVED" in git("log", "-1", "--format=%B")
    assert service.rows["A"]["attempt_continuation"]["preserved_unresolved"] == ["f.txt"]
    retry = _flag(launched[1], "--task")
    assert retry.startswith(RESUME_PROMPT) and "merge is UNRESOLVED" in retry and "f.txt" in retry
    # The preservation commit left a clean checkout, but its markers are still caught.
    assert git("status", "--porcelain") == ""
    assert _conclude_workspace(clone, "p/A") == ("unresolved conflict markers in f.txt", None)

    # The retry reports success without resolving the file: rejected, never published.
    jobs["job-A"].update(status="done", cwd=str(clone))
    dispatch.pass_once()
    assert service.rows["A"]["status"] == "failed"
    assert "failed p/A unresolved conflict markers in f.txt [infra]" in events
    assert not any(event.startswith("published") for event in events)


def test_clean_commit_with_conflict_markers_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dispatch, service, jobs, _, events, _, clone = _retrying_dispatch(tmp_path, monkeypatch)
    dispatch.pass_once()
    (clone / "calc.py").write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> dep\n")
    (clone / "notes.txt").write_text("fine\n")
    subprocess.run(["git", "-C", str(clone), "add", "."], check=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-qm", "work"], check=True)
    jobs["job-A"].update(status="done", cwd=str(clone))
    dispatch.pass_once()
    assert service.rows["A"]["status"] == "failed"
    assert "failed p/A unresolved conflict markers in calc.py [infra]" in events
    assert not any(event.startswith(("published", "done")) for event in events)


def _real_retrying_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, refuse_resume: bool = False, **task_fields: Any,
) -> Any:
    """The real ProjectTaskService and clone handling; the bridge CLI is faked like run_job.

    The fake bridge submits through the service's dispatch fence, or, with refuse_resume,
    records a failed job for a resumed conversation before begin_submit (check_resume).
    """
    from lazytools.code_bridge._dispatch_launch import RESUME_PROMPT, local_job_id
    from lazytools.code_bridge.cli import _read_task
    from lazytools.connectors.projects import ProjectsTools
    from lazytools.projects.plan_edit import _board_key
    from lazytools.projects.task_service import ProjectTaskService
    from test_projects_task_service import PROJECT, admit, placement, task

    monkeypatch.delenv("LAZYTOOLS_PROJECTS_STORE_DB", raising=False)
    monkeypatch.setenv("LAZYBRIDGE_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    source = tmp_path / "source"
    _repo(source)
    path = str(tmp_path / "projects.sqlite")
    projects = ProjectsTools(store_db=path, allow_write=True)
    projects.projects_create(PROJECT, "Retry", "Resume retries")
    admit(projects, [task("A", repo="example/a", text="Do A", **task_fields), task("B", dependency_ids=["A"])])
    service = ProjectTaskService(path)
    principal = TaskPrincipal(actor="dispatcher", role="orchestrator", projects=(PROJECT,))
    jobs: dict[str, dict[str, Any]] = {}
    launched: list[list[str]] = []
    real_run = subprocess.run

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        if argv[1:3] != ["-m", "lazytools.code_bridge"]:
            return real_run(argv, **kwargs)
        argv = argv.copy()
        argv[argv.index("--task") + 1] = _read_task(_flag(argv, "--task"))
        launched.append(argv)
        task_id, token = _flag(argv, "--task-id"), _flag(argv, "--claim-token")
        job_id = local_job_id(token)
        attempt = service.dispatch_status(principal, task_id)["attempt"]
        job = {"job_id": job_id, "task_id": task_id, "task_attempt": attempt, "status": "running",
               "cwd": _flag(argv, "--cwd"), "engine": _flag(argv, "--engine"),
               "session_name": _flag(argv, "--session")}
        if refuse_resume and _flag(argv, "--task") == RESUME_PROMPT:
            job.update(status="failed", error="No bridge policy binding for this native session; use a new session alias")
        else:
            service.begin_submit(principal, task_id, token)
            service.record_submit(principal, task_id, token, job_id)
        jobs[job_id] = job
        return subprocess.CompletedProcess(argv, 0, job_id + "\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    def dispatcher(events: list[str], *, launch_mode: str | None = None, **kwargs: Any) -> Dispatcher:
        bridge = BridgeLauncher(None, principal, PROJECT, repo_roots={"example/a": source},
                                worktree_root=tmp_path / "trees", launch_mode=launch_mode)
        monkeypatch.setattr(bridge, "job", jobs.get)
        monkeypatch.setattr(bridge, "find", lambda task_id, attempt: next(
            (job for job in jobs.values() if job["task_id"] == task_id and job["task_attempt"] == attempt), None))
        monkeypatch.setattr(bridge, "pending", lambda: [])
        monkeypatch.setattr(bridge, "rejected_ticket_reason", lambda job: "")
        return Dispatcher(service, principal, bridge, PROJECT, emit=events.append, **kwargs)

    def git(*argv: str) -> str:
        return real_run(["git", "-C", str(tmp_path / "trees" / PROJECT / "A"), *argv], text=True,
                        capture_output=True, check=True).stdout.strip()

    def crash_first_attempt(events: list[str], *, engine_family: str = "codex",
                            mode: str | None = None, **result: Any) -> Dispatcher:
        first = dispatcher(events, launch_mode=mode)
        placement(service, "A", engine=engine_family)
        first.pass_once()
        clone = Path(_flag(launched[0], "--cwd"))
        (clone / "work.txt").write_text("half done\n")
        jobs[local_job_id(_flag(launched[0], "--claim-token"))].update(
            status="failed", error="Codex App Server exited before completing the request", native_id="thread-1")
        jobs[local_job_id(_flag(launched[0], "--claim-token"))].update(result)
        return first

    def board_task() -> dict[str, Any]:
        with Store(db=path) as store:
            return dict(store.read(_board_key(PROJECT))["tasks"][0])

    return service, launched, crash_first_attempt, dispatcher, placement, git, board_task


def test_approval_wait_renews_the_dispatcher_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lazytools.code_bridge import _dispatch

    service, _, start, _, _, _, board_task = _real_retrying_dispatch(tmp_path, monkeypatch)
    events: list[str] = []
    dispatch = start(events, status="running", error=None, pid=4321, heartbeat_at=1)
    monkeypatch.setattr(_dispatch, "_pid_alive", lambda pid: pid == 4321)
    job_ref = board_task()["dispatches"][-1]["job_ref"]
    monkeypatch.setattr(dispatch.launcher, "pending", lambda: [{"approval_id": "ticket-1", "job_id": job_ref}])
    cancelled: list[str] = []
    monkeypatch.setattr(dispatch.launcher, "cancel", lambda job_ref, **_: cancelled.append(job_ref) or False)
    original_expiry = board_task()["dispatches"][-1]["receipt"]["lease_expires_at"]
    now = original_expiry - 1
    service.clock = dispatch.clock = lambda: now
    dispatch.pass_once()
    now = original_expiry + 1
    dispatch.pass_once()
    receipt = board_task()["dispatches"][-1]["receipt"]
    assert receipt["lease_expires_at"] > now > original_expiry
    assert board_task()["status"] == "claimed" and cancelled == []
    assert events.count("waiting queue-test/A awaiting approval ticket-1") == 1


@pytest.mark.parametrize(("limit", "fixed", "escalation_fixed"), [(1, True, False), (None, False, False),
                                                               (None, False, True)])
def test_check_retries_resume_and_recheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: int | None, fixed: bool, escalation_fixed: bool,
) -> None:
    from lazybridge.engines.sessions import default_session_registry

    script = ("from pathlib import Path; value = Path('work.txt').read_text().strip(); "
              "[print(f'line {i:02d} ' + 'x' * 600) for i in range(64)]; "
              "print('check saw ' + value); raise SystemExit(value != 'fixed')")
    command = f'"{sys.executable}" -c "{script}"'
    settings = {"tier": "thinking" if escalation_fixed else "writing"} if limit is None else {"max_check_retries": limit}
    _, launched, start_first_attempt, dispatcher, _, git, board_task = _real_retrying_dispatch(
        tmp_path, monkeypatch, checks=[command], **settings)
    events: list[str] = []
    first = start_first_attempt(events, status="done", error=None)
    first.pass_once()  # prepare the retry without launching it yet
    assert len(launched) == 1
    assert board_task()["status"] == "todo"
    assert board_task()["attempt_continuation"]["native_id"] == "thread-1"

    routed: list[str | None] = []

    def route(_tier: str, _busy: Any, _preference: Any, _fallback: Any, engine: Any) -> RoutingDecision:
        routed.append(engine)
        return RoutingDecision(_tier, engine or "claude_code", None, None, "required engine", True)

    retries = limit if limit is not None else 2
    for attempt in range(1, retries + 1):
        restarted = dispatcher(events, router=route)
        restarted.pass_once()
        retry = launched[attempt]
        clone = Path(_flag(retry, "--cwd"))
        assert clone == Path(_flag(launched[0], "--cwd"))
        assert _flag(retry, "--session") == _flag(launched[0], "--session")
        assert routed[-1] == _flag(retry, "--engine") == "codex"
        assert default_session_registry().resolve("codex", clone, _flag(retry, "--session")) == "thread-1"
        assert git("show", "HEAD:work.txt") == ("half done" if attempt == 1 else f"broken {attempt - 1}")
        prompt = _flag(retry, "--task")
        assert command in prompt and "commit the fix" in prompt
        assert "line 04 " not in prompt and "line 05 " in prompt and "line 63 " in prompt
        assert "check saw " + ("half done" if attempt == 1 else f"broken {attempt - 1}") in prompt
        (clone / "work.txt").write_text("fixed\n" if fixed else f"broken {attempt}\n")
        git("commit", "-qam", "Repair the check failure")
        job = restarted.launcher.job(board_task()["dispatches"][-1]["job_ref"])
        assert job is not None
        job.update(status="done", native_id="thread-1")
        # Settle without routing so each retry is launched by a new dispatcher.
        restarted.router = None
        restarted.pass_once()

    assert len(launched) == retries + 1
    assert sum(event.startswith("retry queue-test/A check failed") for event in events) == retries
    task = board_task()
    assert len(task["dispatches"]) == retries + 1
    assert all(item["failure_class"] == "check" for item in task["dispatches"][:retries])
    if fixed:
        assert task["status"] == "done" and "done queue-test/A" in events
        assert git("show", "dispatch/queue-test/A:work.txt") == "fixed"
        return
    expected_tier = "critical" if escalation_fixed else "thinking"
    assert task["status"] == "todo" and task["attempt_continuation"]["escalation_tier"] == expected_tier
    escalation = dispatcher(events, router=route)
    escalation.pass_once()
    retry = launched[-1]
    assert len(launched) == retries + 2 and routed[-1] is None
    assert _flag(retry, "--tier") == expected_tier and _flag(retry, "--engine") == "claude"
    assert _flag(retry, "--cwd") == _flag(launched[0], "--cwd")
    assert _flag(retry, "--session") == "dispatch-attempt-4"
    for outcome in ("half done", "broken 1", "broken 2"):
        assert f"check saw {outcome}" in _flag(retry, "--task")
    if escalation_fixed:
        (Path(_flag(retry, "--cwd")) / "work.txt").write_text("fixed\n")
        git("commit", "-qam", "Fix the acceptance failure")
    job = escalation.launcher.job(board_task()["dispatches"][-1]["job_ref"])
    assert job is not None
    job.update(status="done", launch_mode="governed")
    escalation.router = None
    escalation.pass_once()
    escalation.pass_once()
    assert board_task()["status"] == ("done" if escalation_fixed else "failed")
    failures = [event for event in events if event.startswith("escalation_failed queue-test/A")]
    assert len(failures) == (0 if escalation_fixed else 1)
    if failures:
        assert "[check]" in failures[0]
    assert len(launched) == retries + 2


@pytest.mark.parametrize("mode", [None, "auto", "governed"])
def test_governed_claude_repairs_in_a_fresh_conversation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                        mode: str | None) -> None:
    from lazybridge.engines.sessions import default_session_registry

    from lazytools.projects.plan_edit import _board_key

    command = f'"{sys.executable}" -c "print(\'broken assertion\'); raise SystemExit(1)"'
    service, launched, start, dispatcher, placement, git, board_task = _real_retrying_dispatch(
        tmp_path, monkeypatch, checks=[command], max_check_retries=1)
    events: list[str] = []
    first = start(events, engine_family="claude_code", mode=mode, status="done", error=None,
                  launch_mode="governed", native_id="claude-session")
    first.pass_once()
    continuation = board_task()["attempt_continuation"]
    assert continuation["fresh"] is True and continuation["native_id"] is None
    assert len(continuation["check_failures"]) == 1
    if mode is None:
        # A dispatcher can inherit a repair queued before it checked resumability.
        with Store(db=str(service.path)) as store:
            key = _board_key(first.project_id)
            board = store.read(key)
            board["tasks"][0]["attempt_continuation"].update(fresh=False, native_id="claude-session")
            store.write(key, board)
    placement(service, "A", engine="claude_code")
    dispatcher(events, launch_mode=mode).pass_once()
    original, repair = launched
    clone = Path(_flag(repair, "--cwd"))
    assert _flag(repair, "--engine") == "claude" and clone == Path(_flag(original, "--cwd"))
    assert _flag(repair, "--session") == "dispatch-attempt-2"
    assert default_session_registry().resolve("claude", clone, "dispatch-attempt-2") is None
    assert git("show", "HEAD:work.txt") == "half done"
    assert command in _flag(repair, "--task") and "broken assertion" in _flag(repair, "--task")
    assert board_task()["dispatches"][-1]["state"] == "submitted"


@pytest.mark.parametrize("kind", ["flaky", "infra"])
def test_verification_recovers_on_the_same_checkout_without_launching_an_agent(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, knowledge_store: Store,
) -> None:
    from lazytools.code_bridge import _dispatch_workspace

    command = "pytest tests/test_api.py"
    test_id = "tests/test_api.py::test_rate_limit"
    failure = f"check failed: {command} (exit 1)\n"
    failure += f"FAILED {test_id} - AssertionError" if kind == "flaky" else "database is locked"
    results = iter([failure, None])
    calls: list[tuple[str, Path]] = []

    def run_check(command: str, cwd: Path, **_kwargs: Any) -> str | None:
        calls.append((command, cwd))
        return next(results)

    monkeypatch.setattr(_dispatch_workspace, "_check_failure", run_check)
    dispatch, service, jobs, launched, events, _, clone = _retrying_dispatch(tmp_path, monkeypatch)
    dispatch.knowledge_store = knowledge_store
    service.rows["A"]["checks"] = [command]
    dispatch.pass_once()
    (clone / "work.txt").write_text("done\n")
    jobs["job-A"].update(status="done", cwd=str(clone))
    dispatch.pass_once()
    assert service.rows["A"]["status"] == "done" and len(launched) == 1
    assert calls == [(command, clone), (command, clone)]
    assert any(event.endswith(f"[{kind}]") for event in events)
    assert service.evidence[-1].failure_class == kind
    if kind == "flaky":
        lesson, = DurableKnowledgeBase(knowledge_store).find_lessons("flaky check")
        assert lesson["topic"] == f"r: flaky check {test_id}"


@pytest.mark.parametrize("step", ["engine", "check"])
def test_budget_failure_waits_for_reset_without_spending_a_repair(
    step: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lazytools.code_bridge import _dispatch_workspace

    service, launched, start, dispatcher, placement, _, board_task = _real_retrying_dispatch(
        tmp_path, monkeypatch, checks=["pytest"], max_check_retries=1)
    events: list[str] = []
    now = service.clock()
    service.clock = lambda: now
    reason = "Provider quota exhausted. Retry-After: 30"
    first = start(events, status="failed" if step == "engine" else "done", error=reason, quota_resets_at=now + 30)
    first.clock = service.clock
    calls: list[str] = []

    def check(command: str, *_args: Any, **_kwargs: Any) -> str | None:
        calls.append(command)
        return f"check failed: {command}\n{reason}" if len(calls) == 1 else None

    monkeypatch.setattr(_dispatch_workspace, "_check_failure", check)
    first.pass_once()
    assert board_task()["dispatches"][-1]["failure_class"] == "budget"
    assert any(event.startswith("waiting queue-test/A") and "[budget]" in event for event in events)
    if step == "engine":
        assert board_task()["status"] == "todo"
        continuation = board_task()["attempt_continuation"]
        assert continuation["check_failures"] == [] and continuation["infra_retries"] == 0
        placement(service, "A")
        first = dispatcher(events, clock=service.clock)
    else:
        assert board_task()["status"] == "claimed"
    first.pass_once()
    assert len(launched) == 1
    assert len(calls) == (0 if step == "engine" else 1)
    now += 31
    first.pass_once()
    if step == "engine":
        assert len(launched) == 2 and board_task()["status"] == "claimed"
        assert _flag(launched[-1], "--session") == _flag(launched[0], "--session")
    else:
        assert len(launched) == 1 and board_task()["status"] == "done" and len(calls) == 2


def test_restart_between_settlement_and_launch_resumes_the_preserved_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lazybridge.engines.sessions import default_session_registry

    from lazytools.code_bridge._dispatch_launch import RESUME_PROMPT
    service, launched, crash_first_attempt, dispatcher, placement, git, board_task = _real_retrying_dispatch(
        tmp_path, monkeypatch)
    events: list[str] = []
    first = crash_first_attempt(events)
    first.pass_once()  # settles and prepares the retry; the new revision has no placement, so nothing launches
    assert any(event.startswith("retry queue-test/A Codex App Server exited") and "[infra]" in event for event in events)
    assert len(launched) == 1
    continuation = board_task()["attempt_continuation"]
    preserved = git("rev-parse", "dispatch-attempt/queue-test/A/1")
    assert continuation["preserved_commit"] == preserved and continuation["native_id"] == "thread-1"
    git("checkout", "--quiet", "-B", "dispatch/queue-test/A", "dispatch-base")  # the clone moved meanwhile

    restarted = dispatcher(events)  # a new process: nothing survives in memory
    placement(service, "A")
    restarted.pass_once()
    retry = launched[1]
    assert _flag(retry, "--task") == RESUME_PROMPT and _flag(retry, "--session") == "dispatch-attempt-1"
    assert default_session_registry().resolve("codex", Path(_flag(retry, "--cwd")), "dispatch-attempt-1") == "thread-1"
    assert git("rev-parse", "HEAD") == preserved
    assert git("rev-list", "--count", "dispatch-base..HEAD") == "1"  # the artifact check counts the kept work
    assert board_task()["dispatches"][-1]["receipt"]["base_commit"] == git("rev-parse", "dispatch-base")


def _source_with_origin(tmp_path: Path) -> tuple[Any, Any]:
    """A source checkout of a local bare origin, and a function that merges a new commit on that origin."""
    origin, other = tmp_path / "origin.git", tmp_path / "other"
    other_git = _repo(other)
    subprocess.run(["git", "clone", "-q", "--bare", str(other), str(origin)], check=True)
    source = tmp_path / "source"
    subprocess.run(["git", "clone", "-q", str(origin), str(source)], check=True)

    def source_git(*argv: str) -> str:
        return subprocess.run(["git", "-C", str(source), *argv], text=True, capture_output=True,
                              check=True).stdout.strip()
    source_git("config", "user.email", "t@example.com")
    source_git("config", "user.name", "T")

    def merge_on_origin(name: str) -> str:
        (other / name).write_text("merged\n")
        other_git("add", name)
        other_git("commit", "-qm", name)
        other_git("push", "-q", str(origin), "main")
        return str(other_git("rev-parse", "HEAD"))
    return source_git, merge_on_origin


def test_a_commit_merged_on_origin_after_start_is_in_a_later_task_clone(tmp_path: Path) -> None:
    source_git, merge_on_origin = _source_with_origin(tmp_path)
    principal = TaskPrincipal(actor="dispatcher", role="orchestrator", projects=("p",))
    launcher = BridgeLauncher(None, principal, "p", repo_roots={"r": tmp_path / "source"},
                              worktree_root=tmp_path / "trees")
    first = launcher._checkout({"task_id": "p/A", "repo": "r"})
    merged = merge_on_origin("merged.txt")
    second = launcher._checkout({"task_id": "p/B", "repo": "r"})

    assert not (first / "merged.txt").exists() and (second / "merged.txt").exists()
    assert source_git("rev-parse", "HEAD") == merged
    assert launcher._git(second, "rev-parse", "dispatch-base") == merged
    assert launcher.base_commits["p/B"] == merged and not launcher.stale_bases


@pytest.mark.parametrize("state, why", [
    ("dirty", "source checkout is dirty"),
    ("diverged", "source main has diverged from origin/main"),
    ("branch", "source is on feature, not main"),
    ("unreachable", "fetch from origin failed: "),
])
def test_a_source_that_cannot_be_updated_is_left_alone_and_reported_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str, why: str,
) -> None:
    source_git, merge_on_origin = _source_with_origin(tmp_path)
    source = tmp_path / "source"
    if state == "dirty":
        (source / "f.txt").write_text("operator edit\n")
    elif state == "diverged":
        source_git("commit", "-q", "--allow-empty", "-m", "local only")
    elif state == "branch":
        source_git("checkout", "-qb", "feature")
    else:
        source_git("remote", "set-url", "origin", str(tmp_path / "gone.git"))
    merge_on_origin("merged.txt")
    head, status = source_git("rev-parse", "HEAD"), source_git("status", "--porcelain")

    service, events = FakeService(), []  # type: FakeService, list[str]
    service.rows = {name: {"dependency_ids": [], "status": "todo", "repo": "r"} for name in ("A", "B")}
    recorded: dict[str, str] = {}
    service.record_base_commit = (  # type: ignore[attr-defined]
        lambda _principal, task_id, _token, base: recorded.update({task_id: base}))
    principal = TaskPrincipal(actor="dispatcher", role="orchestrator", projects=("p",))
    launcher = BridgeLauncher(None, principal, "p", repo_roots={"r": source}, worktree_root=tmp_path / "trees")
    real_run = subprocess.run

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        if argv[1:3] != ["-m", "lazytools.code_bridge"]:
            return real_run(argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, "a" * 32 + "\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    Dispatcher(service, principal, launcher, "p", parallel=2, emit=events.append,  # type: ignore[arg-type]
               task_reader=service.tasks).pass_once()

    waiting = [event for event in events if event.startswith("waiting ")]
    assert len(waiting) == 1 and waiting[0].startswith(f"waiting p/A source base is stale: {why}")
    assert recorded == {"p/A": head, "p/B": head}
    assert (source_git("rev-parse", "HEAD"), source_git("status", "--porcelain")) == (head, status)


def test_retry_recreates_missing_clone_with_preserved_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, launched, crash_first_attempt, dispatcher, placement, git, board_task = _real_retrying_dispatch(
        tmp_path, monkeypatch)
    events: list[str] = []
    first = crash_first_attempt(events)
    first.pass_once()
    preserved = board_task()["attempt_continuation"]["preserved_commit"]
    base = git("rev-parse", "dispatch-base")
    clone = Path(_flag(launched[0], "--cwd"))
    def remove_readonly(func: Any, path: str, _error: Any) -> None:
        os.chmod(path, stat.S_IWRITE)
        func(path)

    shutil.rmtree(clone, onerror=remove_readonly)

    placement(service, "A")
    dispatcher(events).pass_once()
    assert len(launched) == 2
    assert (clone / "work.txt").read_text() == "half done\n"
    assert git("rev-parse", "HEAD") == preserved
    assert git("rev-parse", "dispatch-base") == base
    assert git("rev-list", "--count", "dispatch-base..HEAD") == "1"


def test_recreated_clone_keeps_original_base_when_source_moved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, launched, crash_first_attempt, dispatcher, placement, git, board_task = _real_retrying_dispatch(
        tmp_path, monkeypatch)
    events: list[str] = []
    first = crash_first_attempt(events)
    first.pass_once()
    continuation = board_task()["attempt_continuation"]
    base = git("rev-parse", "dispatch-base")
    assert continuation["preserved_base"] == base
    clone = Path(_flag(launched[0], "--cwd"))
    def remove_readonly(func: Any, path: str, _error: Any) -> None:
        os.chmod(path, stat.S_IWRITE)
        func(path)

    shutil.rmtree(clone, onerror=remove_readonly)
    # The source checkout now sits on the preserved work itself.
    subprocess.run(["git", "-C", str(tmp_path / "source"), "checkout", "-q", "dispatch-attempt/queue-test/A/1"],
                   capture_output=True, check=True)

    placement(service, "A")
    dispatcher(events).pass_once()
    assert len(launched) == 2
    assert git("rev-parse", "HEAD") == continuation["preserved_commit"]
    assert git("rev-parse", "dispatch-base") == base
    assert git("rev-list", "--count", "dispatch-base..HEAD") == "1"


def test_resume_refused_before_submit_starts_a_fresh_conversation_on_the_preserved_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, launched, crash_first_attempt, _, placement, git, board_task = _real_retrying_dispatch(
        tmp_path, monkeypatch, refuse_resume=True)
    events: list[str] = []
    dispatch = crash_first_attempt(events)
    dispatch.pass_once()  # settles the crash and prepares the retry
    placement(service, "A")
    dispatch.pass_once()  # the resumed attempt is refused before begin_submit
    assert len(launched) == 2 and board_task()["dispatches"][-1]["state"] == "intent"
    preserved = git("rev-parse", "dispatch-attempt/queue-test/A/1")
    dispatch.pass_once()  # settles the refusal (it never reaches the provider boundary)
    assert any(event.startswith("retry queue-test/A resume failed") for event in events)
    refused = board_task()["dispatches"][-1]
    assert refused["state"] == "absent" and "use a new session alias" in refused["launch_error"]
    assert board_task()["attempt_continuation"]["fresh"] is True
    placement(service, "A")
    dispatch.pass_once()
    fresh = launched[2]
    assert _flag(fresh, "--session") == "dispatch-attempt-3"
    text = _flag(fresh, "--task")
    assert "interrupted by the system" in text and "work.txt" in text and text.endswith("Do A")
    assert git("rev-parse", "HEAD") == preserved
    assert git("rev-list", "--count", "dispatch-base..HEAD") == "1"
    assert board_task()["status"] == "claimed" and board_task()["dispatches"][-1]["state"] == "submitted"


def test_engine_pinned_repair_waits_without_usable_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review P2 on #451: an engine-pinned repair must not launch on missing or stale quota telemetry."""
    from types import SimpleNamespace

    from lazytools.code_bridge import _dispatch as dispatch_module
    from lazytools.routing.router import RoutingDecision

    decision = RoutingDecision(tier="writing", provider="codex", model="m", effort="high",
                               reason="operator override", requires_confirmation=False)
    monkeypatch.setattr(dispatch_module._routing, "choose",
                        lambda *a, **k: SimpleNamespace(decision=decision, readings={}))
    router, _ = dispatch_module._routers(None, {})
    pinned = router("writing", {}, (), "wait", "codex")
    assert pinned.provider is None
    assert "usable quota reading" in pinned.reason
    unpinned = router("writing", {}, (), "wait", None)
    assert unpinned.provider == "codex"


def test_every_task_tier_below_critical_escalates() -> None:
    """Review P2 on #456: basic tasks escalate too, so no tier fails without its higher-tier attempt."""
    from lazytools.code_bridge import _dispatch as dispatch_module

    assert dispatch_module.NEXT_TIER["basic"] == "writing"
    assert dispatch_module.NEXT_TIER["writing"] == "thinking"
    assert dispatch_module.NEXT_TIER["thinking"] == "critical"
    assert "critical" not in dispatch_module.NEXT_TIER
