from __future__ import annotations

from typing import Any

from lazytools.code_bridge._dispatch import Dispatcher, Task, _dry_run
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
                   "effective_placement": "local"}
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
