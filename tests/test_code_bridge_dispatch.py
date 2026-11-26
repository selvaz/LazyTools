from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from lazybridge import Store
from lazybridge.ext.approval import ApprovalQueue, ApprovalTarget, TypedApprovalRequest
from lazybridge.ext.knowledge import DurableKnowledgeBase

from lazytools.code_bridge import _store
from lazytools.code_bridge._dispatch import STALE_HEARTBEAT, BridgeLauncher, Dispatcher, Task, _dry_run
from lazytools.code_bridge.cli import build_parser
from lazytools.projects.task_service import TaskPrincipal


class FakeService:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {
            "A": {"dependency_ids": [], "status": "todo"},
            "B": {"dependency_ids": ["A"], "status": "todo"},
            "C": {"dependency_ids": ["A"], "status": "todo"},
            "D": {"dependency_ids": ["B", "C"], "status": "todo"},
        }
        self.claims: dict[str, dict[str, Any]] = {}

    def tasks(self, _service: Any, project: str) -> list[Task]:
        return [Task(f"{project}/{name}", "rev", row["status"], row,
                     self.claims.get(name)) for name, row in self.rows.items()]

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

    def claim(self, principal: Any, identifier: str, revision: str, key: str) -> dict[str, Any]:
        name = identifier.rsplit("/", 1)[1]
        receipt = {"task_id": identifier, "revision": revision, "claim_token": f"token-{name}",
                   "attempt": 1, "executor": principal.actor, "engine_family": "codex",
                   "effective_placement": "local", "lease_expires_at": 9999999999}
        self.rows[name]["status"] = "claimed"
        self.claims[name] = {"state": "submitted", "job_ref": f"job-{name}", "receipt": receipt}
        return {"claim": receipt, "claim_active": True}

    def record_completion_evidence(self, *args: Any) -> None: pass

    def complete(self, _principal: Any, identifier: str, _token: str, _key: str,
                 state: str, _evidence: str, _outcome: str) -> None:
        self.rows[identifier.rsplit("/", 1)[1]]["status"] = "done" if state == "accepted" else "failed"

    def fail_local_launch(self, _principal: Any, identifier: str, _token: str, _error: str) -> None:
        self.rows[identifier.rsplit("/", 1)[1]]["status"] = "failed"

    def reconcile_dispatch(self, *args: Any, **kwargs: Any) -> None: pass

    def fail_dispatch_launch(self, _principal: Any, identifier: str, _token: str, _error: str) -> None:
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


def test_failure_blocks_dependent_but_independent_branch_finishes() -> None:
    service, events = FakeService(), []  # type: FakeService, list[str]
    launcher = FakeLauncher(service)
    dispatch = runner(service, launcher, events)
    dispatch.pass_once()
    launcher.finish("A")
    dispatch.pass_once()
    launcher.finish("B", "failed")
    launcher.finish("C")
    dispatch.pass_once()
    assert service.rows["C"]["status"] == "done"
    assert "D" not in launcher.launches
    assert "blocked p/D" in events


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
    assert "failed p/A cloud placement not supported by dispatch v1" in events


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
    assert "failed p/A no job after dispatch grace period" in events
    assert launcher.launches == ["A"]  # released task became ready and was safely dispatched again


def test_settlement_failure_does_not_stop_other_tasks() -> None:
    service, events = FakeService(), []  # type: FakeService, list[str]
    launcher = FakeLauncher(service)
    dispatch = runner(service, launcher, events)
    dispatch.pass_once()
    launcher.finish("A")
    service.complete = lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("Claim lease has expired"))  # type: ignore[method-assign]
    dispatch.pass_once()
    assert "failed p/A Claim lease has expired" in events


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
    captured: list[Any] = []
    service.record_completion_evidence = lambda *args: captured.append(args[-1])  # type: ignore[method-assign]
    launcher = FakeLauncher(service)
    dispatch = runner(service, launcher, events)
    dispatch.pass_once()
    launcher.finish("A")
    launcher.jobs["job-A"]["cwd"] = str(clone)

    calls: list[list[str]] = []
    real_run = subprocess.run
    output = b"output head\n" + b"." * 2200 + b"\nApp Server exited is test output\n"

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

    assert calls == [["test runner", "first"], ["test runner", "test with spaces.py"], ["test runner", "last"]]
    if failure is None:
        assert service.rows["A"]["status"] == "done"
        assert source_git("rev-parse", "dispatch/p/A") == bridge._git(clone, "rev-parse", "HEAD")
        assert launcher.launches == ["A", "B", "C"]
        return
    assert service.rows["A"]["status"] == "failed"
    assert not source_git("branch", "--list", "dispatch/p/A")
    assert launcher.launches == ["A"]
    message = f"check failed: {commands[1]} ({detail})"
    assert f"failed p/A {message}" in events
    evidence, = captured
    assert evidence.state == "blocked" and evidence.outcome == "task_failure"
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


def test_launch_pins_the_claimed_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    principal = TaskPrincipal(actor="dispatcher", role="orchestrator", projects=("p",))
    launcher = BridgeLauncher(None, principal, "p", worktree_root=tmp_path / "trees")
    monkeypatch.setattr(launcher, "_checkout", lambda task: tmp_path)
    seen: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, "a" * 32 + "\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    claim = {"task_id": "p/A", "claim_token": "t", "effective_placement": "local", "engine_family": "claude_code"}
    launcher.launch({"text": "do it", "tier": "writing"}, claim)
    assert seen[0][seen[0].index("--engine") + 1] == "claude"


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
    recorded: list[tuple[str, str, Any]] = []

    class PlacingService:
        lease_seconds = 900

        def record_placement(self, task_id: str, revision: str, evidence: Any) -> None:
            recorded.append((task_id, revision, evidence))

    def task(identifier: str, status: str = "todo", deps: tuple[str, ...] = (), **extra: Any) -> Task:
        return Task(f"p/{identifier}", "r1", status, {"dependency_ids": list(deps), "tier": "basic", **extra}, None)

    tasks = [task("A", "done"), task("B", deps=("A",)), task("C", deps=("B",)),
             task("R", deps=("A",), review_of="B"), task("K", deps=("A",), requested_placement="cloud")]
    events: list[str] = []
    dispatch = Dispatcher(PlacingService(), TaskPrincipal(actor="d", role="orchestrator", projects=("p",)),
                          None, "p", emit=events.append, clock=lambda: 1000.0,  # type: ignore[arg-type]
                          router=lambda tier: "codex")
    dispatch._place(tasks)
    assert [(task_id, evidence.engine_family, evidence.placement) for task_id, _, evidence in recorded] == [
        ("p/B", "codex", "local")]
    assert events == ["placed p/B local codex tier=basic"]


def test_place_reports_waiting_when_no_engine_is_eligible() -> None:
    class PlacingService:
        lease_seconds = 900

        def record_placement(self, *args: Any) -> None:
            raise AssertionError("must not record without an engine")

    events: list[str] = []
    dispatch = Dispatcher(PlacingService(), TaskPrincipal(actor="d", role="orchestrator", projects=("p",)),
                          None, "p", emit=events.append, router=lambda tier: None)  # type: ignore[arg-type]
    dispatch._place([Task("p/A", "r1", "todo", {"tier": "writing"}, None)])
    dispatch._place([Task("p/A", "r1", "todo", {"tier": "writing"}, None)])
    assert events == ["waiting p/A no eligible engine for tier writing"]


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


def test_engine_crash_is_retried_once_then_fails(knowledge_store: Store) -> None:
    service, events = FakeService(), []  # type: FakeService, list[str]
    service.rows["A"].update(repo="org/repo", text="Update Codex App Server")
    attempts: list[str] = []

    def prepare_attempt(_principal: Any, identifier: str, _revision: str, reason: str) -> str:
        attempts.append(reason)
        name = identifier.rsplit("/", 1)[1]
        service.rows[name]["status"] = "todo"
        service.claims.pop(name, None)
        return "rev"

    service.prepare_attempt = prepare_attempt  # type: ignore[attr-defined]
    launcher = FakeLauncher(service)
    dispatch = runner(service, launcher, events, knowledge_store)
    dispatch.pass_once()
    launcher.jobs["job-A"].update(status="failed", error="Codex App Server exited before completing the request")
    dispatch.pass_once()
    assert len(attempts) == 1 and any(event.startswith("retry p/A") for event in events)
    assert launcher.launches == ["A", "A"]
    assert "Codex App Server exited before completing the request" in launcher.prompts[1]
    assert launcher.prompts[1].count("Known lessons") == 1
    launcher.jobs["job-A"].update(status="failed", error="Codex App Server exited again")
    dispatch.pass_once()
    assert len(attempts) == 1
    assert any(event.startswith("failed p/A") for event in events)
    lesson, = DurableKnowledgeBase(knowledge_store).find_lessons("engine crash")
    assert lesson["topic"] == "org/repo: engine crash"
    assert lesson["revision"] == 2
    assert lesson["what_worked"] == ""
    assert lesson["gotchas"] == "Codex App Server exited again"


def test_task_failure_is_not_retried(knowledge_store: Store) -> None:
    service, events = FakeService(), []  # type: FakeService, list[str]
    service.rows["A"]["repo"] = "org/repo"
    service.prepare_attempt = lambda *args: (_ for _ in ()).throw(AssertionError("no retry"))  # type: ignore[attr-defined]
    launcher = FakeLauncher(service)
    dispatch = runner(service, launcher, events, knowledge_store)
    dispatch.pass_once()
    launcher.finish("A", "failed")
    dispatch.pass_once()
    assert any(event.startswith("failed p/A boom") for event in events)
    lesson, = DurableKnowledgeBase(knowledge_store).find_lessons("boom")
    assert (lesson["topic"], lesson["what_worked"], lesson["gotchas"]) == ("org/repo: task failure", "", "boom")


def test_launch_failure_records_a_lesson(knowledge_store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeService()
    service.rows["A"]["repo"] = "org/repo"
    launcher = FakeLauncher(service)

    def fail_launch(*args: Any) -> str:
        raise RuntimeError("checkout unavailable")

    monkeypatch.setattr(launcher, "launch", fail_launch)
    runner(service, launcher, [], knowledge_store).pass_once()
    lesson, = DurableKnowledgeBase(knowledge_store).find_lessons("checkout")
    assert service.rows["A"]["status"] == "failed"
    assert lesson["topic"] == "org/repo: launch failure"
    assert lesson["gotchas"] == "checkout unavailable"


def test_conclude_workspace_commits_a_resolved_merge_and_rejects_markers(tmp_path: Path) -> None:
    from lazytools.code_bridge._dispatch import _conclude_workspace
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
    from lazytools.code_bridge._dispatch import _has_new_commit
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
    assert {"kind": "failed", "job_id": "job-A", "task_id": "p/A", "detail": "boom"} in rows


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
def test_terminal_job_records_ticket_rejection_even_if_task_succeeds(
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
    principal = TaskPrincipal(actor="dispatcher", role="orchestrator", projects=("p",))
    launcher = BridgeLauncher(str(db), principal, "p")
    monkeypatch.setattr(launcher, "launch", lambda task, claim: "job-A")
    dispatch = Dispatcher(service, principal, launcher, "p", emit=lambda line: None,  # type: ignore[arg-type]
                          task_reader=service.tasks, knowledge_store=knowledge_store)  # type: ignore[arg-type]
    dispatch.pass_once()
    dispatch.pass_once()
    dispatch.pass_once()
    assert service.rows["A"]["status"] == "done"
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
    from lazytools.code_bridge._dispatch import _conclude_workspace
    git = _repo(tmp_path / "r")
    (tmp_path / "r" / "work.txt").write_text("work\n")
    (tmp_path / "r" / "error.log").write_text("account_id=secret\n")
    git("add", "error.log")
    error, commit = _conclude_workspace(tmp_path / "r", "p/A")
    assert error is None and commit
    assert git("show", "--name-only", "--format=", "HEAD") == "work.txt"


def test_publish_checks_branch_and_never_overwrites(tmp_path: Path) -> None:
    from lazytools.code_bridge._dispatch import _publish
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


def test_base_is_set_after_dependency_merges(tmp_path: Path) -> None:
    from lazytools.code_bridge._dispatch import _has_new_commit
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

    def prepare_attempt(_principal: Any, task_id: str, _revision: str, reason: str) -> None:
        assert task_id == "p/A"
        assert "retry after engine crash" in reason and "heartbeat" in reason
        assert service.rows["A"]["status"] == "failed"
        service.rows["A"]["status"] = "todo"

    monkeypatch.setattr(launcher, "cancel", cancel, raising=False)
    monkeypatch.setattr(service, "prepare_attempt", prepare_attempt, raising=False)
    dispatch.pass_once()
    assert service.rows["A"]["status"] == "claimed"
    dispatch.pass_once()
    assert cancellations == ["job-A", "job-A"]
    assert launcher.launches == ["A", "A"]
    assert any(event.startswith("retry p/A engine crash") for event in events)


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


def test_dispatch_accepts_a_stale_minutes_override() -> None:
    parser = build_parser()
    args = ["dispatch", "--project", "p"]
    assert parser.parse_args(args).stale_minutes == 10
    assert parser.parse_args([*args, "--stale-minutes", "4"]).stale_minutes == 4


def test_litter_only_workspace_counts_as_clean(tmp_path: Path) -> None:
    from lazytools.code_bridge._dispatch import _conclude_workspace
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


def test_place_switches_engine_when_the_routed_one_is_full() -> None:
    recorded: list[tuple[str, str]] = []

    class PlacingService:
        lease_seconds = 900

        def record_placement(self, task_id: str, revision: str, evidence: Any) -> None:
            recorded.append((task_id, evidence.engine_family))

    def router(tier: str, engine_family: str | None = None) -> str | None:
        return engine_family or "codex"

    events: list[str] = []
    tasks = [Task(f"p/{name}", "r1", "todo", {"tier": "thinking"}, None) for name in ("A", "B", "C")]
    dispatch = Dispatcher(PlacingService(), TaskPrincipal(actor="d", role="orchestrator", projects=("p",)),
                          None, "p", emit=events.append, clock=lambda: 1000.0,  # type: ignore[arg-type]
                          router=router, capacity=lambda family: 1 if family == "codex" else None,
                          router_with_engine=True)
    dispatch._place(tasks)
    assert recorded == [("p/A", "codex"), ("p/B", "claude_code"), ("p/C", "claude_code")]


def test_fallback_engine_needs_a_usable_recent_reading() -> None:
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from lazytools.code_bridge._dispatch import usable_reading
    now = datetime.now(UTC)

    def reading(**changes: Any) -> Any:
        base = {"error": None, "windows": ("w",), "observed_at": now}
        base.update(changes)
        return SimpleNamespace(**base, age_seconds=lambda *, now: (now - base["observed_at"]).total_seconds())

    assert usable_reading(reading())
    assert not usable_reading(None)
    assert not usable_reading(reading(error="timed out"))
    assert not usable_reading(reading(windows=()))
    assert not usable_reading(reading(observed_at=now - timedelta(hours=1)))


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
    from lazytools.code_bridge import _dispatch, _navigator
    monkeypatch.setattr(_navigator.os, "name", "nt")
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _dispatch._check_failure(r".venv\Scripts\python.exe -m pytest tests\test_api.py", tmp_path) is None
    assert seen == [[r".venv\Scripts\python.exe", "-m", "pytest", r"tests\test_api.py"]]
