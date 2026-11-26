from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

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
                              "tier": "basic", "requested_profile": None, "requested_network": None})
        return {"tasks": ready[:limit]}

    def claim(self, principal: Any, identifier: str, revision: str, key: str) -> dict[str, Any]:
        name = identifier.rsplit("/", 1)[1]
        receipt = {"task_id": identifier, "revision": revision, "claim_token": f"token-{name}",
                   "attempt": 1, "executor": principal.actor, "engine_family": "codex",
                   "effective_placement": "local", "lease_expires_at": 9999999999}
        self.rows[name]["status"] = "claimed"
        self.claims[name] = {"state": "submitted", "job_ref": f"job-{name}", "receipt": receipt}
        return {"claim": receipt}

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


def test_cloud_claim_is_released_without_launch() -> None:
    service, events = FakeService(), []  # type: FakeService, list[str]
    launcher = FakeLauncher(service)
    original = service.claim

    def cloud(*args: Any) -> dict[str, Any]:
        result = original(*args)
        result["claim"]["effective_placement"] = "cloud"
        return result

    service.claim = cloud  # type: ignore[assignment]
    runner(service, launcher, events).pass_once()
    assert not launcher.launches
    assert service.rows["A"]["status"] == "todo"
    assert "failed p/A cloud placement not supported by dispatch yet" in events


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


def test_review_without_verdict_is_failed_with_clear_event() -> None:
    service, events = FakeService(), []  # type: FakeService, list[str]
    service.rows["A"]["review_of"] = "writer"
    launcher = FakeLauncher(service)
    dispatch = runner(service, launcher, events)
    dispatch.pass_once()
    launcher.finish("A")
    dispatch.pass_once()
    assert service.rows["A"]["status"] == "failed"
    assert "failed p/A needs review evidence" in events


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
    first = launcher._checkout({"task_id": "p/A", "repo": "org/repo", "assigned_branch": "main"})
    second = launcher._checkout({"task_id": "p/B", "repo": "org/repo", "assigned_branch": "main"})
    assert first != second
    assert (first / ".git").exists() and (second / ".git").exists()
