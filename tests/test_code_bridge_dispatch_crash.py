from __future__ import annotations

import os
import signal
import socket
from collections.abc import Callable
from itertools import count
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from lazybridge import Store

from lazytools.code_bridge import _cloud, _cloud_land, _concurrency, _dispatch_workspace, _gh, _lockfile, _store
from lazytools.code_bridge._dispatch import Dispatcher, Task, _tasks
from lazytools.code_bridge._dispatch_launch import BridgeLauncher, local_job_id
from lazytools.connectors.projects import ProjectsTools
from lazytools.projects.task_service import ProjectTaskService, TaskPrincipal
from lazytools.projects.task_store import TransactionStore
from lazytools.routing import RoutingDecision
from test_code_bridge_dispatch import _repo
from test_projects_task_service import PROJECT, Clock, admit, task

TASK_ID = f"{PROJECT}/A"
PRINCIPAL = TaskPrincipal(actor="dispatcher", role="orchestrator", projects=(PROJECT,))


class DispatcherDied(BaseException):
    """An abrupt exit which bypasses shutdown."""


class Launcher(BridgeLauncher):
    def __init__(self, service: ProjectTaskService, root: Path):
        self.source = root / "source"
        self.git = _repo(self.source)
        self.service = service
        self.jobs: dict[str, dict[str, Any]] = {}
        self.launched: list[tuple[int, str]] = []
        self.stop_at = ""
        super().__init__(None, PRINCIPAL, PROJECT, repo_roots={"example/a": self.source},
                         worktree_root=root / "trees")

    def launch(self, data: dict[str, Any], claim: dict[str, Any]) -> str:
        token = claim["claim_token"]
        job_id = local_job_id(token)
        assert job_id not in self.jobs
        self.service.begin_submit(PRINCIPAL, claim["task_id"], token)
        if self.stop_at == "submit intent":
            raise DispatcherDied
        cwd = self._checkout({**data, "task_id": claim["task_id"]})
        self.launched.append((claim["attempt"], job_id))
        self.jobs[job_id] = {"job_id": job_id, "status": "running", "cwd": str(cwd), "engine": "codex"}
        if self.stop_at == "before acknowledgement":
            raise DispatcherDied
        self.service.record_submit(PRINCIPAL, claim["task_id"], token, job_id)
        return job_id

    def job(self, job_ref: str) -> dict[str, Any] | None:
        return self.jobs.get(job_ref)

    def pending(self) -> list[dict[str, Any]]:
        return []

    def rejected_ticket_reason(self, job: dict[str, Any]) -> str:
        return ""

    def commit_work(self, job: dict[str, Any]) -> str:
        cwd = Path(job["cwd"])
        (cwd / "result.txt").write_text("task output\n")
        self._git(cwd, "add", "result.txt")
        self._git(cwd, "commit", "-qm", "Implement the task")
        return self._git(cwd, "rev-parse", "HEAD")


class Queue:
    def __init__(self, root: Path, patch: pytest.MonkeyPatch, *, independent: bool = False):
        path = str(root / "projects.sqlite")
        projects = ProjectsTools(store_db=path, allow_write=True)
        projects.projects_create(PROJECT, "Recovery", "Recover a dispatcher after it stops")
        second = task("B", repo="example/a")
        if not independent:
            second["dependency_ids"] = ["A"]
        admit(projects, [task("A", repo="example/a", checks=["test artifact"]), second])
        if not independent:
            assert "scheduled" in projects.projects_schedule_task(PROJECT, 1, "Produce B", "Test hold", hold=True)
        self.clock = Clock()
        self.service = ProjectTaskService(path, clock=self.clock)
        self.launcher = Launcher(self.service, root)
        self.events: list[str] = []
        self.processes: dict[int, str] = {}
        self.pids = count(100_000)
        patch.setattr(_lockfile, "process_start_time", self.process_start_time)
        patch.setattr(_dispatch_workspace, "_check_failure", lambda *_args, **_kwargs: None)

    def process_start_time(self, pid: int) -> str:
        if pid == os.getpid():
            return "test process"
        if pid not in self.processes:
            raise ProcessLookupError(pid)
        return self.processes[pid]

    def start(self, *, emit: Callable[[str], None] | None = None, host: str | None = None,
              parallel: int = 1, land: bool = False) -> Dispatcher:
        instance = Dispatcher(
            self.service, PRINCIPAL, self.launcher, PROJECT, emit=emit or self.events.append,
            clock=self.clock, land=land, parallel=parallel,
            router=lambda tier, *_: RoutingDecision(tier, "codex", None, None, "weekly_margin", True),
        )
        pid = next(self.pids)
        if pid == os.getpid():
            pid = next(self.pids)
        self.processes[pid] = f"started {pid}"
        instance.owner.update(pid=pid, started_at=self.processes[pid])
        if host:
            instance.owner["host"] = host
        return instance

    def row(self, task_id: str = TASK_ID) -> Task:
        return next(row for row in _tasks(self.service, PROJECT) if row.task_id == task_id)

    def finish(self, dispatcher: Dispatcher, commit: str, *, task_id: str = TASK_ID) -> None:
        row = self.row(task_id)
        assert row.dispatch is not None
        self.launcher.jobs[row.dispatch["job_ref"]].update(status="done")
        dispatcher.pass_once()
        row = self.row(task_id)
        assert row.status == "done", self.events
        assert row.dispatch is not None
        assert row.dispatch["completion"]["result"]["artifact_ref"] == commit
        assert self.launcher.git("rev-parse", f"dispatch/{task_id}") == commit


@pytest.mark.parametrize("stage", ["claim", "submit intent", "before acknowledgement", "job", "checks", "landing"])
def test_crash_recovery(stage: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue = Queue(tmp_path, monkeypatch)
    launcher = queue.launcher
    commit: str | None = None
    pushes: list[tuple[str, str]] = []
    pull_requests: dict[str, str] = {}

    def emit(line: str) -> None:
        queue.events.append(line)
        if stage == "claim" and line == f"claimed {TASK_ID}":
            raise DispatcherDied

    def push(cwd: Path, source: str, branch: str) -> None:
        pushes.append((branch, launcher._git(cwd, "rev-parse", source)))

    def open_pr(_repo: str, *, head: str, **_kwargs: Any) -> str:
        assert head not in pull_requests
        pull_requests[head] = "https://github.com/example/a/pull/1"
        raise DispatcherDied  # the remote effect succeeded just before the process died

    def die_during_check(*_args: Any, **_kwargs: Any) -> None:
        raise DispatcherDied

    if stage == "landing":
        monkeypatch.setattr(_cloud, "repo_slug", lambda _cwd: "example/a")
        monkeypatch.setattr(_cloud_land, "_push", push)
        monkeypatch.setattr(_gh, "default_branch", lambda _repo: "main")
        monkeypatch.setattr(_gh, "pr_for_branch", lambda _repo, branch: {"url": pull_requests.get(branch)})
        monkeypatch.setattr(_gh, "pr_create", open_pr)
    first = queue.start(emit=emit, land=stage == "landing")
    launcher.stop_at = stage
    if stage in ("claim", "submit intent", "before acknowledgement"):
        with pytest.raises(DispatcherDied):
            first.pass_once()
    else:
        first.pass_once()
        job, = launcher.jobs.values()
        commit = launcher.commit_work(job)
        if stage != "job":
            job.update(status="done")
            with monkeypatch.context() as patch:
                if stage == "checks":
                    patch.setattr(_dispatch_workspace, "_check_failure", die_during_check)
                with pytest.raises(DispatcherDied):
                    first.pass_once()
    row = queue.row()
    assert row.dispatch is not None
    old_lease = row.dispatch["receipt"]["lease_expires_at"]
    # A second live dispatcher cannot steal the claim or the pending landing.
    other = queue.start()
    other.pass_once()
    assert len(launcher.launched) == (0 if stage in ("claim", "submit intent") else 1)
    assert queue.row().dispatch["receipt"]["dispatcher_owner"] == first.owner

    if stage == "before acknowledgement":
        queue.processes[first.owner["pid"]] = "pid reused by another process"
    else:
        del queue.processes[first.owner["pid"]]
    queue.clock.now += 15
    assert queue.clock() < old_lease
    launcher.stop_at = ""
    other.pass_once()
    row = queue.row()
    assert row.dispatch["receipt"]["dispatcher_owner"] == other.owner
    if stage in ("claim", "submit intent"):
        abandoned, active = row.data["dispatches"]
        assert abandoned["state"] == "absent" and not abandoned.get("job_ref")
        assert active["receipt"]["attempt"] == 2
        assert launcher.launched == [(2, active["job_ref"])]
        commit = launcher.commit_work(launcher.jobs[active["job_ref"]])
    else:
        assert len(row.data["dispatches"]) == 1
        assert launcher.launched == [(1, row.dispatch["job_ref"])]
        if stage == "before acknowledgement":
            commit = launcher.commit_work(launcher.jobs[row.dispatch["job_ref"]])
    assert commit is not None
    queue.finish(other, commit)
    if stage == "landing":
        assert pushes == [(f"dispatch/{TASK_ID}", commit)] * 2
        assert len(pull_requests) == 1
        assert queue.row().dispatch["landing_result"]["pr_url"].endswith("/pull/1")


@pytest.mark.parametrize("restart", [False, True])
def test_operator_cancel_retries_the_preserved_commit(
    restart: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = Queue(tmp_path, monkeypatch)
    first = queue.start()
    first.pass_once()
    job, = queue.launcher.jobs.values()
    commit = queue.launcher.commit_work(job)
    job.update(status="interrupted", error="cancelled by operator: test requested cancellation")
    queue.clock.now += 15
    assert queue.clock() < queue.row().dispatch["receipt"]["lease_expires_at"]
    if restart:
        del queue.processes[first.owner["pid"]]
    recovery = queue.start() if restart else first
    recovery.pass_once()
    previous, current = queue.row().data["dispatches"]
    assert previous["completion"]["result"]["state"] == "blocked"
    assert queue.launcher.git("rev-parse", f"dispatch-attempt/{TASK_ID}/1") == commit
    assert queue.launcher.launched == [(1, previous["job_ref"]), (2, current["job_ref"])]
    assert queue.row().data["attempt_continuation"]["preserved_commit"] == commit
    queue.finish(recovery, commit)
    assert len(queue.launcher.launched) == 2


@pytest.mark.parametrize("stop_signal", [signal.SIGINT, signal.SIGTERM])
def test_clean_shutdown_releases_only_unsubmitted_claims(
    stop_signal: signal.Signals, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = Queue(tmp_path, monkeypatch, independent=True)
    second_task = f"{PROJECT}/B"

    def emit(line: str) -> None:
        queue.events.append(line)
        if line == f"claimed {second_task}":
            signal.raise_signal(stop_signal)

    first = queue.start(emit=emit, parallel=2)
    assert first.run() == 128 + stop_signal
    a, b = queue.row(), queue.row(second_task)
    assert a.status == "claimed" and a.dispatch["state"] == "submitted"
    assert b.status == "todo" and b.dispatch["reconciliation_ref"] == "dispatcher:shutdown"
    old_lease = a.dispatch["receipt"]["lease_expires_at"]
    first_job = queue.launcher.jobs[a.dispatch["job_ref"]]
    first_commit = queue.launcher.commit_work(first_job)

    del queue.processes[first.owner["pid"]]
    queue.clock.now += 15
    assert queue.clock() < old_lease
    other = queue.start(parallel=2)
    other.pass_once()
    b = queue.row(second_task)
    assert queue.launcher.launched == [(1, a.dispatch["job_ref"]), (2, b.dispatch["job_ref"])]
    second_commit = queue.launcher.commit_work(queue.launcher.jobs[b.dispatch["job_ref"]])
    queue.finish(other, first_commit)
    queue.finish(other, second_commit, task_id=second_task)


def test_renewal_writes_the_board_once_and_foreign_host_recovers_at_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = Queue(tmp_path, monkeypatch, independent=True)
    first = queue.start(parallel=2)
    first.pass_once()
    original_write = TransactionStore.write
    writes: list[str] = []

    def count_write(store: TransactionStore, key: str, value: Any, **kwargs: Any) -> None:
        writes.append(key)
        original_write(store, key, value, **kwargs)

    queue.clock.now += 15
    with monkeypatch.context() as patch:
        patch.setattr(TransactionStore, "write", count_write)
        first.pass_once()
    assert writes == [f"blackboard:project:{PROJECT}"]
    receipt = queue.row().dispatch["receipt"]
    assert receipt["lease_expires_at"] == queue.clock() + 180
    del queue.processes[first.owner["pid"]]
    other = queue.start(host="different-host", parallel=2)
    queue.clock.now = receipt["lease_expires_at"] - 1
    other.pass_once()
    assert queue.row().dispatch["receipt"]["dispatcher_owner"] == first.owner
    queue.clock.now += 1
    other.pass_once()
    for name in ("A", "B"):
        row = queue.row(f"{PROJECT}/{name}")
        assert row.dispatch["receipt"]["dispatcher_owner"] == other.owner
        assert len(row.data["dispatches"]) == 1
        commit = queue.launcher.commit_work(queue.launcher.jobs[row.dispatch["job_ref"]])
        queue.finish(other, commit, task_id=row.task_id)
    assert len(queue.launcher.launched) == 2


def test_renewal_continues_during_a_slow_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue = Queue(tmp_path, monkeypatch)
    queue.service.lease_seconds = 6
    first = queue.start()
    first.pass_once()
    job, = queue.launcher.jobs.values()
    commit = queue.launcher.commit_work(job)
    job.update(status="done")
    renewed = Event()
    renew = queue.service.renew_dispatcher_claims

    def record_renewal(*args: Any, **kwargs: Any) -> None:
        renew(*args, **kwargs)
        if not kwargs and checking.is_set():
            renewed.set()

    checking = Event()

    def slow_check(*_args: Any, **_kwargs: Any) -> None:
        queue.clock.now += 5
        checking.set()
        assert renewed.wait(10), "the dispatcher did not renew while the check was running"
        queue.clock.now += 2  # past the original lease, within the renewed lease

    monkeypatch.setattr(queue.service, "renew_dispatcher_claims", record_renewal)
    monkeypatch.setattr(_dispatch_workspace, "_check_failure", slow_check)
    assert first.run(once=True) == 0
    row = queue.row()
    assert row.status == "done", queue.events
    assert row.dispatch["completion"]["result"]["artifact_ref"] == commit
    assert len(queue.launcher.launched) == 1


def test_cloud_landing_claim_recovers_a_reused_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue = Queue(tmp_path, monkeypatch)
    first = queue.start()
    config = _concurrency.ConcurrencyConfig({}, tmp_path / "slots")
    monkeypatch.setattr(_concurrency, "load_config", lambda: config)
    with Store(db=str(tmp_path / "jobs.sqlite")) as store:
        _store.write_meta(store, "job", {
            "land_claimed_at": queue.clock(),
            "land_owner": {"host": socket.gethostname().casefold(), "pid": first.owner["pid"],
                           "started_at": first.owner["started_at"]},
        })
        assert not _cloud_land._claim(store, "job", "land")
        queue.processes[first.owner["pid"]] = "reused"
        assert _cloud_land._claim(store, "job", "land")
