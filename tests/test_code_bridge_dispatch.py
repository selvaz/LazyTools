from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from lazytools.code_bridge._dispatch import BridgeLauncher, Dispatcher, Task, _dry_run
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

    def launch(self, task: dict[str, Any], claim: dict[str, Any]) -> str:
        name = claim["task_id"].rsplit("/", 1)[1]
        self.launches.append(name)
        job = f"job-{name}"
        self.jobs[job] = {"job_id": job, "status": "running"}
        return job

    def job(self, job_ref: str) -> dict[str, Any] | None: return self.jobs.get(job_ref)

    def find(self, task_id: str, attempt: int) -> dict[str, Any] | None:
        return self.jobs.get(f"job-{task_id.rsplit('/', 1)[1]}")

    def finish(self, name: str, status: str = "done") -> None:
        self.jobs[f"job-{name}"].update(status=status, artifact_ref="a" * 40,
                                          error="boom" if status != "done" else None)


def runner(service: FakeService, launcher: FakeLauncher, events: list[str]) -> Dispatcher:
    principal = TaskPrincipal(actor="dispatcher", role="orchestrator", projects=("p",))
    return Dispatcher(service, principal, launcher, "p", parallel=2, emit=events.append,  # type: ignore[arg-type]
                      task_reader=service.tasks)  # type: ignore[arg-type]


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
    assert events == ["ready p/A", "ready p/B", "ready p/C", "ready p/D"]
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
