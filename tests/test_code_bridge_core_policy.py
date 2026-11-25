"""Offline acceptance at the local bridge entry points."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import pytest
from lazybridge import Envelope
from lazybridge.engines.codex.app_server import codex_executable
from lazybridge.engines.codex.policy_launch import runtime_revision
from lazybridge.engines.coding import ApprovalRequest
from lazybridge.engines.sessions import default_session_registry
from lazybridge.ext.approval import ApprovalQueue, ApprovalTarget, TypedApprovalRequest
from lazybridge.policy import current_policy_context, load_profiles
from lazybridge.policy.codex_admission import LAUNCH_PATH, REQUIRED_PROBES

import _code_bridge_fakes as fakes
from lazytools.code_bridge import _credentials, _jobs, _launch, _lockfile, _registry, _review_tools, _store, _task, cli
from lazytools.code_bridge._mcp import CodeBridgeTools
from lazytools.connectors.projects import ProjectsTools
from lazytools.projects.task_service import ProjectTaskService
from lazytools.projects.task_store import TaskServiceError
from test_code_bridge_continuations import git
from test_projects_task_service import OPERATOR, PROJECT, WORKER, admit, claim, placement, task


@pytest.fixture(autouse=True)
def credential_home(tmp_path, monkeypatch):
    monkeypatch.setattr(_credentials, "credential_directory", lambda: tmp_path / "operator-credentials")


def command(*args):
    proc = subprocess.run(
        [sys.executable, "-m", "lazytools.code_bridge", *args],
        capture_output=True, encoding="utf-8", timeout=45, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def record(job_id):
    store = _store.build_store()
    return _jobs.find_job(store, _store.build_job_registry(store), job_id)


@pytest.fixture
def subprocess_bridge(tmp_path, monkeypatch):
    # Keep the production CLI, MCP subprocess, detach worker, and SQLite paths.
    # Python imports this fixture in the subprocess to replace only the agents.
    (tmp_path / "sitecustomize.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "from types import SimpleNamespace\n"
        "from _code_bridge_fakes import install\n"
        "from lazytools.code_bridge import _credentials\n"
        "_credentials.credential_directory = lambda: Path(__file__).parent / 'operator-credentials'\n"
        "script = install(SimpleNamespace(setattr=setattr))\n"
        "script.request_approval = os.getenv('BRIDGE_TEST_APPROVAL') == '1'\n",
        encoding="utf-8",
    )
    tests = Path(__file__).resolve().parent
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(map(str, (tmp_path, tests, tests.parent / "src"))))
    monkeypatch.setenv("LAZYBRIDGE_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_POLL_SECONDS", "0.025")
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_TICKET_TTL", "30")
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


@pytest.mark.parametrize(("mode", "decision"), [
    ("default", "approve"),
    ("explicit-default", "reject"),
    ("sibling", "approve"),
    ("sibling", "reject"),
    ("override", "approve"),
])
def test_cli_detach_uses_typed_registry_across_processes(subprocess_bridge, monkeypatch, mode, decision):
    repo = subprocess_bridge
    monkeypatch.setenv("BRIDGE_TEST_APPROVAL", "1")
    monkeypatch.chdir(repo.parent)
    shared = Path(os.environ["LAZYTOOLS_PROJECTS_STORE_DB"])
    isolated = mode in {"sibling", "override"}
    db = _store.default_db_path() if not isolated else Path("job files/demo.db")
    db_flags = [] if mode == "default" else ["--db", str(db)]
    override = Path("approval files/custom.sqlite")
    registry_flags = ["--registry-db", str(override)] if mode == "override" else []
    expected_registry = shared
    if mode == "sibling":
        expected_registry = db.with_suffix(".registry.sqlite").resolve()
    elif mode == "override":
        expected_registry = override.resolve()
    code, out, err = command("approver-credential", "init", *db_flags, *registry_flags)
    assert code == 0, err
    code, out, err = command(
        "run", "--detach", "--engine", "codex", "--cwd", str(repo), "--root", str(repo.parent),
        "--task", "Wait for one approval", "--profile", "default", "--json", *db_flags, *registry_flags,
    )
    assert code == 0, err
    launch = json.loads(out)
    assert (launch["profile"], launch["launch_mode"], launch["network"]) == ("edit-and-run", "ungoverned-legacy", "on")
    job = launch["job_id"]
    if isolated:
        assert launch["registry_db"] == str(expected_registry)
    else:
        assert "registry_db" not in launch
    deadline = time.monotonic() + 15
    pending = []
    while not pending and time.monotonic() < deadline:
        code, out, err = command("pending", "--json", *db_flags)
        assert code == 0, err
        pending = json.loads(out)
    assert pending, Path(launch["log"]).read_text(encoding="utf-8")
    ticket_id = pending[0]["approval_id"]
    assert [(row["job_id"], row["approval_id"]) for row in pending] == [(job, ticket_id)]
    if isolated:
        assert not shared.exists()
        code, out, err = command("status", job, "--json", *db_flags, *registry_flags)
        assert code == 0, err
        assert json.loads(out)["registry_db"] == str(expected_registry)
        code, out, err = command("wait-any", job, "--timeout", "0", *db_flags)
        assert code == 0, err
        assert json.loads(out)["approval"]["approval_id"] == ticket_id
    if registry_flags:
        assert not db.with_suffix(".registry.sqlite").exists()
        code, out, err = command("pending", "--json", *db_flags, *registry_flags)
        assert code == 0, err
        assert json.loads(out) == pending
    code, out, err = command(decision, ticket_id, "--reason", "Test operator decision", *db_flags)
    assert code == 0, (out, err)
    code, out, err = command("wait", job, "--timeout", "15", *db_flags, *registry_flags)
    assert code == (0 if decision == "approve" else 1), (out, err)
    store = _store.build_store(db)
    metadata = _store.read_meta(store, job)
    assert metadata["registry_ticket_ids"] == [ticket_id]
    assert metadata.get("registry_db") == launch.get("registry_db")
    assert expected_registry.is_file()
    ticket = ApprovalQueue(_registry.registry_store(expected_registry)).get_ticket(ticket_id)
    assert ticket.request.digest == pending[0]["action_digest"]
    assert ticket.request.payload["policy_digest"] == metadata["policy_digest"]
    assert ticket.status == ("approved" if decision == "approve" else "rejected")
    assert ticket.decision.decider == "owner"
    assert "Test operator decision" in ticket.decision.reason
    assert (ticket.consumed_at is not None) == (decision == "approve")
    assert _store.build_approval_queue(store).get_ticket(ticket_id) is None


def test_custom_job_store_finds_pre_isolation_shared_tickets(tmp_path, monkeypatch, capsys):
    shared = tmp_path / "projects.sqlite"
    monkeypatch.setenv("LAZYTOOLS_PROJECTS_STORE_DB", str(shared))
    db = tmp_path / "demo.db"
    sibling = db.with_suffix(".registry.sqlite")
    store = _store.build_store(db)
    jobs = _store.build_job_registry(store)
    old_job, isolated_job = "pre-isolation", "isolated"
    for job_id in (old_job, isolated_job):
        jobs.write(job_id, "Run", tool_name="codex", status="awaiting_approval")
    _store.write_meta(store, isolated_job, {"registry_db": str(sibling)})

    def file_ticket(job_id, registry):
        channel = _registry.ApprovalChannel(
            registry, job_id, "revision", "digest", timedelta(minutes=1), 0.01,
            lambda: None, lambda: None, lambda: None,
        )
        ticket, _ = channel._submit(
            ApprovalRequest(provider="codex", kind="command", name="shell", arguments={"command": "git status"}),
            "Approval requested",
        )
        return ticket.approval_id

    shared_registry = _registry.Registry(store, shared)
    first = file_ticket(old_job, shared_registry)
    second = file_ticket(old_job, shared_registry)
    isolated = file_ticket(isolated_job, _registry.Registry(store, sibling))
    misplaced = file_ticket(isolated_job, shared_registry)
    other_store = _store.build_store(tmp_path / "other.db")
    _store.build_job_registry(other_store).write("other-job", "Run", tool_name="codex", status="awaiting_approval")
    unrelated = file_ticket("other-job", _registry.Registry(other_store, shared))
    assert "registry_db" not in _store.read_meta(store, old_job)

    flags = ["--db", str(db)]
    assert cli.main(["pending", "--json", *flags]) == 0
    assert {row["approval_id"] for row in json.loads(capsys.readouterr().out)} == {first, second, isolated}
    assert cli.main(["wait-any", old_job, "--events", "approval", "--timeout", "0", *flags]) == 0
    assert json.loads(capsys.readouterr().out)["approval"]["approval_id"] in {first, second}
    assert cli.main(["approver-credential", "init", "--registry-db", str(shared)]) == 0
    assert cli.main(["approve", first, *flags]) == 0
    assert cli.main(["reject", second, "--reason", "Test decision", *flags]) == 0
    assert cli.main(["approve", misplaced, *flags]) == 1
    assert cli.main(["reject", unrelated, "--reason", "Test decision", *flags]) == 1

    shared_queue = shared_registry.queue
    assert shared_queue.get_ticket(first).status == "approved"
    assert shared_queue.get_ticket(second).status == "rejected"
    assert shared_queue.get_ticket(misplaced).status == "pending"
    assert shared_queue.get_ticket(unrelated).status == "pending"


def test_typed_bridge_credentials_are_required_and_bound_to_the_registry(tmp_path, capsys):
    path = tmp_path / "approvals.sqlite"
    store = _store.build_store()
    registry = _registry.Registry(store, path)
    ticket = registry.queue.create_typed_ticket(TypedApprovalRequest.create(
        task_id="job", attempt=1, action="native_command", target=ApprovalTarget(),
        requested_by="worker", policy_revision="test", payload={"registry": "code-bridge"},
    ), operator_only=True)
    flags = ["--registry-db", str(path)]
    assert cli.main(["approve", ticket.approval_id, *flags]) == 1
    assert "credential is missing" in capsys.readouterr().err
    assert registry.queue.get_ticket(ticket.approval_id).status == "pending"

    assert cli.main(["approver-credential", "init", *flags]) == 0
    secret = _credentials.load(path)
    assert secret not in capsys.readouterr().out
    assert cli.main(["approver-credential", "init", *flags]) == 0
    assert "already initialized" in capsys.readouterr().out
    assert _credentials.load(path) == secret
    if os.name != "nt":
        assert _credentials._path(path).stat().st_mode & 0o777 == 0o600

    other = _registry.Registry(store, tmp_path / "other.sqlite")
    assert other.initialize_credential()
    _credentials._path(path).write_text(_credentials.load(other.path), encoding="ascii")
    assert cli.main(["approve", ticket.approval_id, *flags]) == 1
    assert "Invalid owner approver credential" in capsys.readouterr().err
    assert registry.queue.get_ticket(ticket.approval_id).status == "pending"


def test_job_environment_scrubs_owner_credential(tmp_path, monkeypatch):
    fakes.install(monkeypatch)
    monkeypatch.setenv(_credentials.CREDENTIAL_ENV, "worker-must-not-inherit-this")

    async def turn(*args):
        child = subprocess.run(
            [sys.executable, "-c", "import os, sys; sys.exit(sys.argv[1] in os.environ)", _credentials.CREDENTIAL_ENV],
            check=False,
        )
        assert child.returncode == 0
        return Envelope(payload="done")

    monkeypatch.setattr(_jobs, "_run_agent", turn)
    result = _jobs.run_job(engine_name="codex", cwd=str(tmp_path), root=str(tmp_path), task="Check environment")
    assert result.status == "done", result.error
    assert os.environ[_credentials.CREDENTIAL_ENV] == "worker-must-not-inherit-this"


def test_operator_commands_do_not_create_missing_registries(tmp_path, capsys):
    shared = Path(os.environ["LAZYTOOLS_PROJECTS_STORE_DB"])
    db = tmp_path / "demo.db"
    sibling = tmp_path / "demo.registry.sqlite"
    override = tmp_path / "missing" / "override.sqlite"
    both = tmp_path / "both.sqlite"
    _store.write_meta(_store.build_store(db), "job", {"registry_db": str(override)})
    for flags in (
        [], ["--db", str(db)], ["--db", str(db), "--registry-db", str(override)],
        ["--db", str(both), "--registry-db", str(both)],
    ):
        assert cli.main(["pending", "--json", *flags]) == 0
        assert json.loads(capsys.readouterr().out) == []
        assert cli.main(["approve", "missing-ticket", *flags]) == 1
        assert cli.main(["reject", "missing-ticket", "--reason", "Test", *flags]) == 1
        capsys.readouterr()
    assert not shared.exists()
    assert not sibling.exists()
    assert not override.parent.exists()
    assert not both.exists()


@pytest.mark.parametrize("isolated", [False, True])
def test_cancel_closes_typed_ticket(tmp_path, monkeypatch, isolated):
    db = tmp_path / "demo.sqlite" if isolated else None
    registry_db = tmp_path / "custom.sqlite" if isolated else None
    db_flags = ["--db", str(db)] if db else []
    store = _store.build_store(db)
    registry = _registry.Registry(store, registry_db)
    job_id = "cancel-native"
    _store.build_job_registry(store).write(job_id, "Run", tool_name="codex", status="awaiting_approval")
    pid = 12345
    _store.write_meta(store, job_id, {
        "pid": pid, "cwd": str(tmp_path), **({"registry_db": str(registry_db)} if registry_db else {}),
    })
    channel = _registry.ApprovalChannel(
        registry, job_id, "revision", "digest", timedelta(minutes=1), 0.01,
        lambda: None, lambda: None, lambda: None,
    )
    ticket, _ = channel._submit(
        ApprovalRequest(provider="codex", kind="command", name="shell", arguments={"command": "git status"}),
        "Approval requested",
    )
    alive = {pid}
    monkeypatch.setattr(_lockfile, "_pid_alive", alive.__contains__)
    monkeypatch.setattr(cli, "_process_matches_bridge", lambda _: True)
    monkeypatch.setattr(cli, "_kill_process_tree", lambda pid, **kwargs: alive.remove(pid))
    assert cli.main(["cancel", job_id, "--reason", "Cancelled by the test operator", *db_flags]) == 0
    metadata = _jobs.find_job(store, _store.build_job_registry(store), job_id)
    assert metadata["status"] == "interrupted"
    assert metadata["registry_ticket_ids"] == [ticket.approval_id]
    decided = registry.queue.get_ticket(ticket.approval_id)
    assert decided.status == "expired"
    assert decided.decision is None
    assert decided.consumed_at is None
    assert cli.main(["approve", ticket.approval_id, *db_flags]) == 1


def test_mcp_submits_and_worker_validates_claim(subprocess_bridge):
    repo = subprocess_bridge
    path = os.environ["LAZYTOOLS_PROJECTS_STORE_DB"]
    projects = ProjectsTools(store_db=path, allow_write=True)
    projects.projects_create(PROJECT, "Bridge test", "Exercise local launch")
    admit(projects, [task("local", requested_profile="read", requested_network=False), task("other")])
    service = ProjectTaskService(path)
    placement(service, "local")
    receipt = claim(service, "local")
    bridge = CodeBridgeTools(str(repo.parent), task_principal=WORKER.model_dump_json(), projects_db=path)
    kwargs = {
        "task": receipt["text"], "cwd": str(repo), "engine": "codex", "where": "local",
        "task_id": receipt["task_id"], "launch_mode": "legacy",
    }
    refused = bridge.code_bridge_submit(**kwargs, claim_token="stale")
    assert not refused["ok"]
    refused = bridge.code_bridge_submit(**kwargs, profile="edit-and-run", claim_token=receipt["claim_token"])
    assert not refused["ok"] and "profile differs" in refused["error"]
    refused = bridge.code_bridge_submit(**kwargs, network="on", claim_token=receipt["claim_token"])
    assert not refused["ok"] and "exceeds the task claim" in refused["error"]
    submitted = bridge.code_bridge_submit(**kwargs, claim_token=receipt["claim_token"])
    assert submitted["ok"], submitted
    assert submitted["profile"] == "read"
    job = submitted["job_id"]
    code, out, err = command("wait", job, "--timeout", "15")
    assert code == 0, (out, err)
    assert service.dispatch_status(OPERATOR, receipt["task_id"])["job_ref"] == job
    metadata = record(job)
    assert metadata["task_id"] == receipt["task_id"]
    assert metadata["task_preflight_ref"] == receipt["preflight_ref"]
    assert "claim_token" not in metadata


@pytest.mark.parametrize("isolated", [False, True])
def test_claimed_approval_consumes_only_for_its_dispatch(tmp_path, monkeypatch, isolated):
    path = os.environ["LAZYTOOLS_PROJECTS_STORE_DB"]
    projects = ProjectsTools(store_db=path, allow_write=True)
    projects.projects_create(PROJECT, "Bridge test", "Exercise the approval fence")
    admit(projects, [task("local"), task("other")])
    service = ProjectTaskService(path)
    placement(service, "local")
    receipt = claim(service, "local")
    task_id, token = receipt["task_id"], receipt["claim_token"]
    monkeypatch.setenv(_task.PRINCIPAL_ENV, WORKER.model_dump_json())
    binding = _task.load(task_id, token, state="intent")
    job_id = "claimed-native"
    service.begin_submit(WORKER, task_id, token)
    service.record_submit(WORKER, task_id, token, job_id)
    registry = _registry.Registry(_store.build_store(), tmp_path / "registry.sqlite" if isolated else None)
    consume = partial(binding.consume_approval, registry=registry)
    channel = _registry.ApprovalChannel(
        registry, job_id, "revision", "digest", timedelta(minutes=1), 0.01,
        lambda: None, lambda: None, lambda: binding.recheck(job_id), task_id=task_id, actor=WORKER.actor,
        attempt=receipt["attempt"], fenced_consume=consume,
    )
    ticket, executor = channel._submit(
        ApprovalRequest(provider="codex", kind="command", name="shell", arguments={"command": "git status"}),
        "Approval requested",
    )
    assert registry.initialize_credential()
    assert registry.decide(ticket, "approved", "operator", "Confirmed")
    with pytest.raises(TaskServiceError, match="no longer belongs"):
        consume("wrong-job", ticket.approval_id, executor)
    assert not channel._consume(ticket, executor.model_copy(update={"digest": "wrong-digest"}))
    assert channel._consume(ticket, executor)
    assert not channel._consume(ticket, executor)


def test_detached_worker_refuses_changed_policy_before_start(tmp_path, monkeypatch, capsys):
    profiles = write_profiles(tmp_path, monkeypatch)
    script = fakes.install(monkeypatch)
    child = []
    monkeypatch.setattr(cli, "_spawn_detached", lambda argv, log: child.extend(argv[3:]) or 123)
    assert cli.main([
        "run", "--where", "local", "--engine", "codex", "--cwd", str(tmp_path),
        "--root", str(tmp_path), "--task", "original", "--detach", "--json",
    ]) == 0
    rewrite_revision(profiles)
    assert cli.main(child) == 2
    captured = capsys.readouterr()
    assert "launch binding changed" in captured.err
    job = record(json.loads(captured.out.splitlines()[0])["job_id"])
    assert job["status"] == "failed" and job["policy_revision"] == "original"
    assert script.engines == []


def write_profiles(tmp_path, monkeypatch):
    path = tmp_path / "profiles.toml"
    path.write_text(
        'version = 1\nrevision = "original"\n'
        '[profiles.read]\nallowed_tool_classes = ["read"]\n'
        '[profiles.edit]\nallowed_tool_classes = ["read", "edit"]\n'
        '[profiles.edit-and-run]\nallowed_tool_classes = ["read", "edit", "shell"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_PROFILES", str(path))
    return path


def rewrite_revision(profiles):
    profiles.write_text(profiles.read_text(encoding="utf-8").replace('"original"', '"revised"'), encoding="utf-8")


def test_revision_change_refuses_continuation_and_native_resume(tmp_path, monkeypatch):
    profiles = write_profiles(tmp_path, monkeypatch)
    monkeypatch.setenv("LAZYBRIDGE_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    script = fakes.install(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Bridge Test")
    git(repo, "config", "user.email", "bridge@example.invalid")
    git(repo, "commit", "--allow-empty", "-m", "Initial")
    calls = []

    async def turn(engine, prompt, name):
        calls.append(prompt)
        engine.finish_turn()
        git(repo, "commit", "--allow-empty", "-m", "Progress")
        rewrite_revision(profiles)
        raise TimeoutError(" turn timed out")

    monkeypatch.setattr(_jobs, "_run_agent", turn)
    kwargs = dict(engine_name="codex", cwd=str(repo), root=str(tmp_path), task="Run", session_name="writer")
    result = _jobs.run_job(**kwargs)
    assert "launch binding changed" in result.error
    assert len(record(result.job_id)["turns"]) == 1
    resumed = _jobs.run_job(**kwargs)
    assert "launch binding changed" in resumed.error
    assert calls == ["Run"] and len(script.engines) == 1


@pytest.mark.parametrize(("engine", "id_arg"), [("claude", "session_id"), ("codex", "thread_id")])
def test_legacy_resume_binds_preexisting_alias(tmp_path, monkeypatch, engine, id_arg):
    monkeypatch.setenv("LAZYBRIDGE_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    script = fakes.install(monkeypatch)
    default_session_registry().bind(engine, tmp_path, "writer", "native-before-pr")
    kwargs = dict(engine_name=engine, cwd=str(tmp_path), root=str(tmp_path), task="Run", session_name="writer")

    result = _jobs.run_job(**kwargs)
    assert result.status == "done", result.error
    assert script.kwargs[id_arg] == "native-before-pr"
    assert _jobs.run_job(**kwargs).status == "done"
    widened = _jobs.run_job(**kwargs, network="off")
    assert widened.status == "failed" and "launch binding changed" in widened.error
    assert len(script.engines) == 2


def test_governed_resume_refuses_unbound_handle(tmp_path):
    store = _store.build_store(tmp_path / "jobs.sqlite")
    binding = {"launch_mode": "governed"}
    with pytest.raises(ValueError, match="No bridge policy binding"):
        _launch.check_resume(store, "codex", "unbound", binding)


def test_release_receipt_matches_runtime_and_policy(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    document = load_profiles()
    report = {
        "schema": 1, "launch_path": LAUNCH_PATH, "runtime_revision": runtime_revision(codex_executable()),
        "policy_version": document.version, "policy_revision": document.revision,
        "checked_at": datetime.now(UTC).isoformat(),
        "matrix": [{"profile": "read", "network": "off", "status": "green", "tool_classes": ["read"],
                    "probes": {probe: {"passed": True} for probe in REQUIRED_PROBES}}],
    }
    path = tmp_path / "release.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_CODEX_RELEASE_REPORT", str(path))
    script = fakes.install(monkeypatch)

    async def turn(engine, prompt, name):
        from lazybridge import Envelope

        assert current_policy_context().effective_policy.profile == "read"
        return Envelope(payload="governed")

    monkeypatch.setattr(_jobs, "_run_agent", turn)
    result = _jobs.run_job(engine_name="codex", cwd=str(repo), root=str(tmp_path), task="Read", profile="review")
    assert result.status == "done"
    assert record(result.job_id)["launch_mode"] == "governed"
    assert record(result.job_id)["release_receipt"]
    assert script.kwargs["client"].policy_release_report == path
    for key in ("runtime_revision", "policy_revision"):
        path.write_text(json.dumps({**report, key: "stale"}), encoding="utf-8")
        with pytest.raises(ValueError, match="Governed local launch refused"):
            _launch.resolve_launch("codex", repo, "review", None, "auto")


@pytest.mark.parametrize("engine", ["codex", "claude"])
def test_intentional_review_cannot_borrow_writer_options(tmp_path, monkeypatch, engine):
    script = fakes.install(monkeypatch)
    script.request_approval = True
    script.approval_overrides = {"kind": "tool", "name": "Write", "arguments": {"file_path": "forbidden.txt"}}
    result = _jobs.run_job(
        engine_name=engine, cwd=str(tmp_path), root=str(tmp_path), task="Review", profile="edit-and-run",
        launch_mode="legacy", intentional_review=True,
    )
    meta = record(result.job_id)
    assert result.status == "failed" and "barred" in result.error
    assert meta["profile"] == "read" and meta["registry_ticket_ids"] == [] and meta["temp_dir"] is None
    kwargs = script.kwargs
    if engine == "claude":
        assert kwargs["config"].claude.extra_tools == ()
        assert {"Write", "Edit", "Bash"} <= set(kwargs["config"].claude.disallowed_tools)
        assert not kwargs["web"]
    else:
        assert kwargs["config"].codex.sandbox == "read-only"
        assert kwargs["config"].codex.approval_policy == "never"
        assert kwargs["config"].codex.writable_roots == ()


@pytest.mark.parametrize("engine", ["codex", "claude"])
def test_review_tools_read_real_diffs_and_workspace_files(tmp_path, monkeypatch, engine):
    script = fakes.install(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Bridge Test")
    git(repo, "config", "user.email", "bridge@example.invalid")
    git(repo, "remote", "add", "origin", "https://github.com/example/review.git")
    file = repo / "example.txt"
    file.write_text("before\n", encoding="utf-8")
    git(repo, "add", "example.txt")
    git(repo, "commit", "-m", "Initial review fixture")
    file.write_text("after\n", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("outside", encoding="utf-8")
    native_run = _review_tools._run
    gh_calls = []

    def run(cwd, executable, args, env):
        if executable == "gh":
            gh_calls.append(args)
            return "PR read"
        return native_run(cwd, executable, args, env)

    async def turn(agent, prompt):
        review_command, review_read_file = agent.tools
        assert "+after" in review_command("git diff")
        assert "Initial review fixture" in review_command("git log -1")
        assert review_read_file("example.txt") == "after\n"
        assert review_command("gh pr view 1") == "PR read"
        assert review_command("gh pr diff 1") == "PR read"
        for denied in ("git push origin main", "git checkout -- example.txt", "python -m pytest",
                       "gh pr diff 1 --repo other/repo", "gh pr view https://github.com/other/repo/pull/1"):
            with pytest.raises(ValueError):
                review_command(denied)
        with pytest.raises(ValueError):
            review_read_file("../outside.txt")
        return Envelope(payload="done")

    monkeypatch.setattr(_review_tools, "_run", run)
    monkeypatch.setattr(fakes.FakeAgent, "run", turn)
    result = _jobs.run_job(engine_name=engine, cwd=str(repo), root=str(tmp_path), task="Review", profile="review")
    assert result.status == "done", result.error
    assert all(args[-2:] == ["--repo", "github.com/example/review"] for args in gh_calls)
    config = script.kwargs["config"]
    if engine == "claude":
        assert config.claude.preapprove_application_tools
    else:
        assert config.codex.preapprove_dynamic_tools and config.codex.sandbox == "read-only"


def test_offline_review_denies_github_reads(tmp_path):
    review_command, _ = _review_tools.review_tools(tmp_path, network="off", validate=lambda: None)
    with pytest.raises(ValueError, match="require network"):
        review_command("gh pr view 1")
