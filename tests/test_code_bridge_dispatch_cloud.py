"""Cloud placement in `dispatch`: a fake cloud launcher against the real task service."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from lazybridge import Store

from lazytools.code_bridge import _dispatch
from lazytools.code_bridge._dispatch import BridgeLauncher, Dispatcher, cloud_job_id
from lazytools.connectors.projects import ProjectsTools
from lazytools.projects.plan_edit import _board_key
from lazytools.projects.task_service import ProjectTaskService, TaskPrincipal

PROJECT = "cloudp"
PRINCIPAL = TaskPrincipal(actor="code-bridge-dispatch", role="orchestrator", projects=(PROJECT,))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True).stdout.strip()


def _repo(path: Path) -> str:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "user.email", "test@example.invalid")
    (path / "ok.py").write_text("raise SystemExit(0)\n")
    (path / "bad.py").write_text("print('assertion failed'); raise SystemExit(1)\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "Landed work")
    return _git(path, "rev-parse", "HEAD")


class FakeCloudLauncher:
    def __init__(self, checkout: Path, sha: str) -> None:
        self.checkout, self.sha = checkout, sha
        self.jobs: dict[str, dict[str, Any]] = {}
        self.launches: list[tuple[str, str, str]] = []
        self.free = True
        self.cancelled: list[str] = []

    def launch(self, task: dict[str, Any], claim: dict[str, Any]) -> str:
        raise AssertionError("a cloud task must not take the local launch path")

    def job(self, job_ref: str) -> dict[str, Any] | None:
        return self.jobs.get(job_ref)

    def find(self, task_id: str, attempt: int) -> dict[str, Any] | None:
        return None

    def cloud_free(self, engine_family: str) -> bool:
        return self.free

    def launch_cloud(self, task: dict[str, Any], claim: dict[str, Any], job_id: str) -> str:
        self.launches.append((claim["task_id"], claim["engine_family"], job_id))
        self.jobs[job_id] = {"job_id": job_id, "where": "cloud", "status": "running", "land": True}
        return job_id

    def cloud_job(self, job_ref: str) -> dict[str, Any] | None:
        return self.jobs.get(job_ref)

    def cloud_checkout(self, task_id: str, job: dict[str, Any]) -> Path:
        assert job["pushed_sha"] == self.sha
        return self.checkout

    def cancel(self, job_ref: str) -> bool:
        self.cancelled.append(job_ref)
        return True

    def land(self, job_id: str) -> None:
        self.jobs[job_id].update(status="done", landed_at=1.0, pr_url="https://github.com/org/repo/pull/7",
                                 pushed_sha=self.sha, landed_branch="cloud/abc")


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProjectTaskService:
    monkeypatch.delenv("LAZYTOOLS_PROJECTS_STORE_DB", raising=False)
    monkeypatch.delenv("LAZYCEO_CEO_STORE_DB", raising=False)
    path = str(tmp_path / "projects.sqlite")
    projects = ProjectsTools(store_db=path, allow_write=True)
    projects.projects_create(PROJECT, "Cloud dispatch", "Dispatch a task to a cloud agent")
    return ProjectTaskService(path, lease_seconds=600)


def admit(service: ProjectTaskService, check: str | None) -> None:
    projects = ProjectsTools(store_db=str(service.path), allow_write=True)
    fields: dict[str, Any] = {"checks": [check]} if check else {}
    tasks = [{"text": "Produce A", "task_id": "A", "tier": "basic", "repo": "org/repo",
              "requested_placement": "cloud", "acceptance_criteria": ["A exists"], **fields},
             # A local follow-up; these dispatchers have no local router, so it is never placed.
             {"text": "Produce B", "task_id": "B", "tier": "basic", "dependency_ids": ["A"],
              "acceptance_criteria": ["B exists"]}]
    args = (PROJECT, "Artifacts are visible", "2027-01-01T00:00:00Z", tasks)
    assert projects.projects_review_plan(*args)["ok"]
    assert projects.projects_promote(*args).startswith("promoted")


def board_task(service: ProjectTaskService) -> dict[str, Any]:
    with Store(db=str(service.path)) as store:
        return dict(store.read(_board_key(PROJECT))["tasks"][0])


def check(script: str) -> str:
    return f'"{sys.executable}" {script}'


def dispatcher(service: ProjectTaskService, launcher: FakeCloudLauncher, events: list[str]) -> Dispatcher:
    return Dispatcher(service, PRINCIPAL, launcher, PROJECT, emit=events.append,  # type: ignore[arg-type]
                      cloud_placer=lambda _task, _tier: ("codex", "env-123"))


@pytest.fixture
def landed(tmp_path: Path) -> tuple[Path, str]:
    checkout = tmp_path / "landed"
    return checkout, _repo(checkout)


def launched(service: ProjectTaskService, launcher: FakeCloudLauncher, events: list[str]) -> str:
    dispatcher(service, launcher, events).pass_once()
    assert len(launcher.launches) == 1, events
    task_id, engine, job_id = launcher.launches[0]
    assert (task_id, engine) == (f"{PROJECT}/A", "codex")
    data = board_task(service)
    assert data["placement_evidence"]["cloud"]["evidence"]["environment_id"] == "env-123"
    assert data["dispatches"][-1]["state"] == "submitted" and data["dispatches"][-1]["job_ref"] == job_id
    return job_id


def test_landed_cloud_job_with_green_checks_is_accepted(service: ProjectTaskService, landed: tuple[Path, str]) -> None:
    admit(service, check("ok.py"))
    launcher, events = FakeCloudLauncher(*landed), []  # type: FakeCloudLauncher, list[str]
    job_id = launched(service, launcher, events)
    launcher.land(job_id)
    dispatcher(service, launcher, events).pass_once()
    data = board_task(service)
    assert data["status"] == "done", events
    assert data["dispatches"][-1]["completion"]["result"]["artifact_ref"] == landed[1]
    assert f"done {PROJECT}/A" in events


def test_landed_cloud_job_with_a_red_check_fails(service: ProjectTaskService, landed: tuple[Path, str]) -> None:
    admit(service, check("bad.py"))
    launcher, events = FakeCloudLauncher(*landed), []  # type: FakeCloudLauncher, list[str]
    launcher.land(launched(service, launcher, events))
    dispatcher(service, launcher, events).pass_once()
    data = board_task(service)
    assert data["status"] == "failed"
    assert "check failed" in data["error"] and "assertion failed" in data["error"]


def test_cloud_job_that_did_not_land_fails(service: ProjectTaskService, landed: tuple[Path, str]) -> None:
    admit(service, check("ok.py"))
    launcher, events = FakeCloudLauncher(*landed), []  # type: FakeCloudLauncher, list[str]
    job_id = launched(service, launcher, events)
    dispatcher(service, launcher, events).pass_once()
    assert board_task(service)["status"] == "claimed"  # still running: nothing to settle
    launcher.jobs[job_id].update(status="done", land_failure="base branch moved")
    dispatcher(service, launcher, events).pass_once()
    data = board_task(service)
    assert data["status"] == "failed"
    assert data["error"] == "cloud job did not land: base branch moved"


def test_failed_cloud_job_fails_with_its_error(service: ProjectTaskService, landed: tuple[Path, str]) -> None:
    admit(service, None)
    launcher, events = FakeCloudLauncher(*landed), []  # type: FakeCloudLauncher, list[str]
    job_id = launched(service, launcher, events)
    launcher.jobs[job_id].update(status="failed", error="cloud task error: tests broke")
    dispatcher(service, launcher, events).pass_once()
    assert board_task(service)["error"] == "cloud job failed: cloud task error: tests broke"


def test_restart_while_a_cloud_job_runs_settles_on_the_next_pass(
    service: ProjectTaskService, landed: tuple[Path, str],
) -> None:
    admit(service, check("ok.py"))
    launcher, events = FakeCloudLauncher(*landed), []  # type: FakeCloudLauncher, list[str]
    job_id = launched(service, launcher, events)
    launcher.land(job_id)
    restarted = dispatcher(service, launcher, events)  # a new process: no in-memory state
    restarted.pass_once()
    assert board_task(service)["status"] == "done"
    assert len(launcher.launches) == 1


def test_restart_between_submit_and_its_acknowledgement_adopts_the_job(
    service: ProjectTaskService, landed: tuple[Path, str],
) -> None:
    admit(service, None)
    launcher, events = FakeCloudLauncher(*landed), []  # type: FakeCloudLauncher, list[str]
    first = dispatcher(service, launcher, events)
    first._place(_dispatch._tasks(service, PROJECT))
    summary = service.next_ready(PRINCIPAL, PROJECT)["tasks"][0]
    claim = service.claim(PRINCIPAL, summary["task_id"], summary["revision"], "dispatch:A:1")["claim"]
    service.begin_submit(PRINCIPAL, summary["task_id"], claim["claim_token"])
    job_id = cloud_job_id(claim["claim_token"])
    launcher.launch_cloud(summary, claim, job_id)  # the dispatcher died before record_submit
    launcher.land(job_id)
    dispatcher(service, launcher, events).pass_once()
    data = board_task(service)
    assert data["status"] == "done", events
    assert data["dispatches"][-1]["job_ref"] == job_id


def test_restart_after_lease_expiry_settles_recorded_accepted_evidence(
    service: ProjectTaskService, landed: tuple[Path, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    admit(service, check("ok.py"))
    launcher, events = FakeCloudLauncher(*landed), []  # type: FakeCloudLauncher, list[str]
    launcher.land(launched(service, launcher, events))

    def die(*_args: Any, **_kwargs: Any) -> None:
        raise KeyboardInterrupt("dispatcher stopped after recording evidence")

    monkeypatch.setattr(service, "complete", die)
    with pytest.raises(KeyboardInterrupt):
        dispatcher(service, launcher, events).pass_once()
    monkeypatch.undo()
    data = board_task(service)
    assert data["status"] == "claimed"
    assert [record["state"] for record in data["dispatches"][-1]["evidence"].values()] == ["accepted"]

    later = time.time() + 3600  # the dispatcher comes back after the lease has expired
    service.clock = lambda: later
    restarted = Dispatcher(service, PRINCIPAL, launcher, PROJECT, emit=events.append,  # type: ignore[arg-type]
                           cloud_placer=lambda _task, _tier: ("codex", "env-123"), clock=lambda: later)
    restarted.pass_once()
    data = board_task(service)
    assert data["status"] == "failed", events
    assert data["error"] == "claim lease expired"
    assert data["dispatches"][-1]["completion"]["result"]["state"] == "blocked"
    restarted.pass_once()  # the dependant is no longer waiting on a claimed task
    assert f"blocked {PROJECT}/B" in events
    assert len(launcher.launches) == 1


def test_cloud_task_is_routed_while_local_slots_are_full(
    service: ProjectTaskService, landed: tuple[Path, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    def choose(tier: str, *, respect_slots: bool = True, **_kwargs: Any) -> Any:
        # Every engine is at its machine-wide local limit: only a slot-blind pick finds one.
        return SimpleNamespace(decision=SimpleNamespace(provider=None if respect_slots else "codex"), readings={})

    monkeypatch.setattr(_dispatch._routing, "choose", choose)
    monkeypatch.setattr(_dispatch._cloud, "load_config", lambda: {})
    monkeypatch.setattr(_dispatch._cloud_place, "decide_for_repo",
                        lambda *_args, **_kwargs: SimpleNamespace(where="cloud", reason=""))
    monkeypatch.setattr(_dispatch._cloud, "repo_root", lambda repo: repo)
    monkeypatch.setattr(_dispatch._cloud, "repo_slug", lambda _root: "org/repo")
    monkeypatch.setattr(_dispatch._cloud, "provider",
                        lambda _engine: SimpleNamespace(env_for=lambda *_args: "env-123"))
    router, cloud_placer = _dispatch._routers(None, {"org/repo": landed[0]})
    assert router("basic") is None  # local placement honours the full slots
    admit(service, None)
    launcher, events = FakeCloudLauncher(*landed), []  # type: FakeCloudLauncher, list[str]
    Dispatcher(service, PRINCIPAL, launcher, PROJECT, emit=events.append, router=router,  # type: ignore[arg-type]
               capacity=lambda _engine: 1, router_with_engine=True, cloud_placer=cloud_placer).pass_once()
    assert [(task_id, engine) for task_id, engine, _job in launcher.launches] == [(f"{PROJECT}/A", "codex")], events
    assert board_task(service)["placement_evidence"]["cloud"]["evidence"]["environment_id"] == "env-123"


def test_full_cloud_slots_leave_the_task_unclaimed(service: ProjectTaskService, landed: tuple[Path, str]) -> None:
    admit(service, None)
    launcher, events = FakeCloudLauncher(*landed), []  # type: FakeCloudLauncher, list[str]
    launcher.free = False
    dispatcher(service, launcher, events).pass_once()
    assert not launcher.launches
    assert board_task(service)["status"] == "todo"
    assert f"waiting {PROJECT}/A cloud concurrency limit reached" in events


def test_cloud_launch_failure_without_a_job_fails_the_task(
    service: ProjectTaskService, landed: tuple[Path, str],
) -> None:
    admit(service, None)
    launcher, events = FakeCloudLauncher(*landed), []  # type: FakeCloudLauncher, list[str]

    def refuse(*_args: Any) -> str:
        raise RuntimeError("error: branch 'main' does not exist on the 'origin' remote")

    launcher.launch_cloud = refuse  # type: ignore[method-assign]
    dispatcher(service, launcher, events).pass_once()
    data = board_task(service)
    assert data["status"] == "failed"
    assert "does not exist on the 'origin' remote" in data["error"]


def test_bridge_launcher_reuses_the_cloud_run_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"
    _repo(source)
    launcher = BridgeLauncher("jobs.sqlite", PRINCIPAL, PROJECT, repo_roots={"org/repo": source})
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        if argv[0] == "git":
            answer = {"get-url": "https://github.com/org/repo.git\n", "--show-toplevel": f"{source}\n"}
            return subprocess.CompletedProcess(argv, 0, answer.get(argv[-1], answer.get(argv[-2], "main\n")), "")
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "abc\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    job_id = cloud_job_id("token")
    claim = {"task_id": f"{PROJECT}/A", "engine_family": "claude_code", "claim_token": "token"}
    assert launcher.launch_cloud({"text": "Do A", "repo": "org/repo", "tier": "basic"}, claim, job_id) == job_id
    argv = seen[0]
    assert argv[1:5] == ["-m", "lazytools.code_bridge", "run", "--where"]
    for flag, value in (("--where", "cloud"), ("--engine", "claude"), ("--branch", "main"),
                        ("--task-id", f"{PROJECT}/A"), ("--job-id", job_id), ("--cwd", str(source)),
                        ("--db", "jobs.sqlite")):
        assert argv[argv.index(flag) + 1] == value
    assert {"--allow-cloud", "--open-pr", "--land"} <= set(argv)


def test_cloud_checkout_clones_the_landed_branch_at_the_pushed_commit(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    _repo(origin)
    source = tmp_path / "source"
    _git(tmp_path, "clone", "-q", str(origin), str(source))
    _git(origin, "checkout", "-qb", "cloud/abc")
    (origin / "work.txt").write_text("cloud work\n")
    _git(origin, "add", "work.txt")
    _git(origin, "commit", "-qm", "Cloud work")
    sha = _git(origin, "rev-parse", "HEAD")
    launcher = BridgeLauncher(None, PRINCIPAL, PROJECT, worktree_root=tmp_path / "trees")
    checkout = launcher.cloud_checkout(f"{PROJECT}/A", {"repo_root": str(source), "landed_branch": "cloud/abc",
                                                        "pushed_sha": sha})
    assert _git(checkout, "rev-parse", "HEAD") == sha
    assert (checkout / "work.txt").read_text() == "cloud work\n"


def _other_repo(path: Path) -> Path:
    _repo(path)
    _git(path, "remote", "add", "origin", "https://github.com/org/other.git")
    return path


@pytest.mark.parametrize("mapped", [True, False])
def test_cloud_placement_refuses_a_repository_other_than_the_tasks(
    service: ProjectTaskService, tmp_path: Path, landed: tuple[Path, str], monkeypatch: pytest.MonkeyPatch,
    mapped: bool,
) -> None:
    monkeypatch.setattr(_dispatch._routing, "choose", lambda *_args, **_kwargs: SimpleNamespace(
        decision=SimpleNamespace(provider="codex"), readings={}))
    monkeypatch.setattr(_dispatch._cloud, "load_config", lambda: {})
    monkeypatch.setattr(_dispatch._cloud_place, "decide_for_repo",
                        lambda *_args, **_kwargs: SimpleNamespace(where="cloud", reason=""))
    monkeypatch.setattr(_dispatch._cloud, "provider",
                        lambda _engine: SimpleNamespace(env_for=lambda *_args: "env-123"))
    # Mapped to a checkout of org/other, or not mapped while the dispatcher runs inside org/other.
    other = _other_repo(tmp_path / "other")
    monkeypatch.chdir(other)
    _router, cloud_placer = _dispatch._routers(None, {"org/repo": other} if mapped else {})
    admit(service, None)
    launcher, events = FakeCloudLauncher(*landed), []  # type: FakeCloudLauncher, list[str]
    first = Dispatcher(service, PRINCIPAL, launcher, PROJECT, emit=events.append,  # type: ignore[arg-type]
                       cloud_placer=cloud_placer)
    first.pass_once()
    first.pass_once()
    assert not launcher.launches
    data = board_task(service)
    assert "cloud" not in data.get("placement_evidence", {})
    refused = [line for line in events if line.startswith(f"failed {PROJECT}/A cloud placement refused")]
    assert len(refused) == 1, events
    assert ("org/other" if mapped else "no --repo-root mapping") in refused[0]
    assert first.run(once=False) == 0  # nothing left to wait for


def test_cloud_launch_with_a_mismatched_repository_fails_and_submits_nothing(
    service: ProjectTaskService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = _other_repo(tmp_path / "other")

    class Launcher(BridgeLauncher):
        def cloud_free(self, engine_family: str) -> bool:
            return True

        def job(self, job_ref: str) -> dict[str, Any] | None:
            return None

    bridge_calls: list[list[str]] = []
    real_run = subprocess.run

    def run(argv: list[str], *args: Any, **kwargs: Any) -> Any:
        if argv[0] != "git":
            bridge_calls.append(argv)
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    admit(service, None)
    events: list[str] = []
    # Placement evidence recorded earlier (by a placer without the check); the launch checks again.
    Dispatcher(service, PRINCIPAL, Launcher(None, PRINCIPAL, PROJECT, repo_roots={"org/repo": other}), PROJECT,
               emit=events.append, cloud_placer=lambda _task, _tier: ("codex", "env-123")).pass_once()
    data = board_task(service)
    assert data["status"] == "failed", events
    assert "Cloud repository differs from the task claim" in data["error"] and "org/other" in data["error"]
    assert not data["dispatches"][-1].get("job_ref")
    assert bridge_calls == []


def test_cloud_acceptance_checkout_never_shares_or_deletes_a_local_checkout(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    _repo(origin)
    source = tmp_path / "source"
    _git(tmp_path, "clone", "-q", str(origin), str(source))
    sha = _git(origin, "rev-parse", "HEAD")
    launcher = BridgeLauncher(None, PRINCIPAL, PROJECT, repo_roots={"org/repo": source},
                              worktree_root=tmp_path / "trees")
    local = launcher._checkout({"task_id": f"{PROJECT}/A-cloud", "repo": "org/repo"})
    (local / "wip.txt").write_text("uncommitted local work\n")
    job = {"repo_root": str(source), "landed_branch": "main", "pushed_sha": sha, "task_attempt": 1}
    first = launcher.cloud_checkout(f"{PROJECT}/A", job)
    second = launcher.cloud_checkout(f"{PROJECT}/A", job)
    assert len({local, first, second}) == 3
    assert first.parent == second.parent == tmp_path / "trees" / PROJECT / ".cloud-accept"
    launcher.remove_cloud_checkout(first)
    launcher.remove_cloud_checkout(second)
    launcher.remove_cloud_checkout(local)  # not a path this launcher allocated: left alone
    assert not first.exists() and not second.exists()
    assert (local / ".git").is_dir() and (local / "wip.txt").read_text() == "uncommitted local work\n"


def test_cloud_run_accepts_a_task_id_label_but_not_a_claim_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from lazytools.code_bridge import cli

    def place(args: Any) -> None:
        args.where = "cloud"

    seen: list[str] = []
    monkeypatch.setattr(cli, "_configure_run_root", lambda _args: None)
    monkeypatch.setattr(cli, "_resolve_placement", place)
    monkeypatch.setattr(cli, "_cmd_run_cloud", lambda args: seen.append(args.task_id) or 0)
    base = ["run", "--where", "cloud", "--allow-cloud", "--engine", "codex", "--cwd", ".", "--task", "x",
            "--task-id", "p/A"]
    assert cli._cmd_run(cli.build_parser().parse_args(base)) == 0
    assert seen == ["p/A"]
    assert cli._cmd_run(cli.build_parser().parse_args([*base, "--claim-token", "t"])) == 2
