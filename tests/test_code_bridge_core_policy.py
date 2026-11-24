"""Offline acceptance at the local bridge entry points."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from lazybridge import Envelope
from lazybridge.engines.codex.app_server import codex_executable
from lazybridge.engines.codex.policy_launch import runtime_revision
from lazybridge.engines.coding import ApprovalRequest
from lazybridge.ext.approval import ApprovalQueue
from lazybridge.policy import current_policy_context, load_profiles
from lazybridge.policy.codex_admission import LAUNCH_PATH, REQUIRED_PROBES

import _code_bridge_fakes as fakes
from lazytools.code_bridge import _jobs, _launch, _lockfile, _registry, _store, _task, cli
from lazytools.code_bridge._mcp import CodeBridgeTools
from lazytools.connectors.projects import ProjectsTools
from lazytools.projects.task_service import ProjectTaskService
from lazytools.projects.task_store import TaskServiceError
from test_code_bridge_continuations import git
from test_projects_task_service import OPERATOR, PROJECT, WORKER, admit, claim, placement, task


def command(*args):
    proc = subprocess.run(
        [sys.executable, "-m", "lazytools.code_bridge", *args],
        capture_output=True, encoding="utf-8", timeout=45, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def record(job_id):
    store = _store.build_store()
    return _jobs.find_job(store, _store.build_job_registry(store), job_id)


def test_legacy_writer_retains_readonly_commands_and_routine_pytest(tmp_path, monkeypatch):
    fakes.install(monkeypatch)
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    scratch = tmp_path / "scratch"

    async def turn(engine, prompt, name):
        gate = engine.kwargs["config"].approval_gate
        for command in ("git status", "git diff", "git log -1",
                        f"'{Path(sys.executable).as_posix()}' -m pytest tests --basetemp {scratch.as_posix()}/run"):
            request = ApprovalRequest(provider="claude-code", kind="tool", name="Bash",
                                      arguments={"command": command}, cwd=str(repo))
            assert (await gate(request)).action == "allow", command
        return Envelope(payload="done")

    monkeypatch.setattr(_jobs, "_run_agent", turn)
    result = _jobs.run_job(engine_name="claude", cwd=str(repo), root=str(tmp_path), task="Write",
                           scratch_root=str(scratch))
    assert result.status == "done", result.error
    ticket_id, = record(result.job_id)["registry_ticket_ids"]
    assert ApprovalQueue(_registry.registry_store()).get_ticket(ticket_id).request.payload["rule_id"] == "pytest-scratch"


def test_legacy_writer_retains_branch_session_grants(tmp_path, monkeypatch):
    fakes.install(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "symbolic-ref", "HEAD", "refs/heads/feat/legacy")
    prompts = []

    async def turn(engine, prompt, name):
        gate = engine.kwargs["config"].approval_gate

        async def approve(prompt):
            prompts.append(prompt)
            return True

        monkeypatch.setattr(gate.channel, "ask", approve)

        async def request(command):
            return await gate(ApprovalRequest(provider="claude-code", kind="tool", name="Bash",
                                              arguments={"command": command}, cwd=str(repo)))

        for command in ("git push origin feat/legacy", "gh pr create --head feat/legacy"):
            assert (await request(command)).action == "allow_session"
            assert (await request(command)).action == "allow"
        assert (await request("git push origin main")).action == "allow"
        assert (await request("git push origin main")).action == "allow"
        return Envelope(payload="done")

    monkeypatch.setattr(_jobs, "_run_agent", turn)
    result = _jobs.run_job(engine_name="claude", cwd=str(repo), root=str(tmp_path), task="Write", routine_auto=False)
    assert result.status == "done", result.error
    assert len(prompts) == 4 and all("git push origin main" in prompt for prompt in prompts[-2:])


@pytest.fixture
def subprocess_bridge(tmp_path, monkeypatch):
    # Keep the production CLI, MCP subprocess, detach worker, and SQLite paths.
    # Python imports this fixture in the subprocess to replace only the agents.
    (tmp_path / "sitecustomize.py").write_text(
        "import os\n"
        "from types import SimpleNamespace\n"
        "from _code_bridge_fakes import install\n"
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


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_cli_detach_uses_typed_registry_across_processes(subprocess_bridge, monkeypatch, decision):
    repo = subprocess_bridge
    monkeypatch.setenv("BRIDGE_TEST_APPROVAL", "1")
    code, out, err = command(
        "run", "--detach", "--engine", "codex", "--cwd", str(repo), "--root", str(repo.parent),
        "--task", "Wait for one approval", "--profile", "default", "--json",
    )
    assert code == 0, err
    launch = json.loads(out)
    assert (launch["profile"], launch["launch_mode"], launch["network"]) == ("edit-and-run", "ungoverned-legacy", "on")
    job = launch["job_id"]
    deadline = time.monotonic() + 15
    pending = []
    while not pending and time.monotonic() < deadline:
        code, out, err = command("pending", "--json")
        assert code == 0, err
        pending = json.loads(out)
    assert pending, Path(launch["log"]).read_text(encoding="utf-8")
    ticket_id = pending[0]["approval_id"]
    assert [(row["job_id"], row["approval_id"]) for row in pending] == [(job, ticket_id)]
    code, out, err = command(decision, ticket_id, "--reason", "Test operator decision")
    assert code == 0, (out, err)
    code, out, err = command("wait", job, "--timeout", "15")
    assert code == (0 if decision == "approve" else 1), (out, err)
    metadata = record(job)
    assert metadata["registry_ticket_ids"] == [ticket_id]
    ticket = ApprovalQueue(_registry.registry_store()).get_ticket(ticket_id)
    assert ticket.request.digest == pending[0]["action_digest"]
    assert ticket.request.payload["policy_digest"] == metadata["policy_digest"]
    assert ticket.status == ("approved" if decision == "approve" else "rejected")
    assert "Test operator decision" in ticket.decision.reason
    assert (ticket.consumed_at is not None) == (decision == "approve")
    assert _store.build_approval_queue(_store.build_store()).get_ticket(ticket_id) is None


def test_cancel_closes_typed_ticket(tmp_path, monkeypatch):
    store = _store.build_store()
    job_id = "cancel-native"
    _store.build_job_registry(store).write(job_id, "Run", tool_name="codex", status="awaiting_approval")
    pid = 12345
    _store.write_meta(store, job_id, {"pid": pid, "cwd": str(tmp_path)})
    channel = _registry.ApprovalChannel(
        _registry.Registry(store), job_id, "revision", "digest", timedelta(minutes=1), 0.01,
        lambda: None, lambda: None, lambda: None,
    )
    ticket, _ = channel._file(
        ApprovalRequest(provider="codex", kind="command", name="shell", arguments={"command": "git status"}),
        "Approval requested",
    )
    alive = {pid}
    monkeypatch.setattr(_lockfile, "_pid_alive", alive.__contains__)
    monkeypatch.setattr(cli, "_process_matches_bridge", lambda _: True)
    monkeypatch.setattr(cli, "_kill_process_tree", lambda pid, **kwargs: alive.remove(pid))
    assert cli.main(["cancel", job_id, "--reason", "Cancelled by the test operator"]) == 0
    assert record(job_id)["status"] == "interrupted"
    assert record(job_id)["registry_ticket_ids"] == [ticket.approval_id]
    decided = ApprovalQueue(_registry.registry_store()).get_ticket(ticket.approval_id)
    assert decided.decision.verdict == "rejected"
    assert decided.consumed_at is None
    assert cli.main(["approve", ticket.approval_id]) == 1


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


def test_claimed_approval_consumes_only_for_its_dispatch(monkeypatch):
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
    registry = _registry.Registry(_store.build_store())
    channel = _registry.ApprovalChannel(
        registry, job_id, "revision", "digest", timedelta(minutes=1), 0.01,
        lambda: None, lambda: None, lambda: binding.recheck(job_id), task_id=task_id, actor=WORKER.actor,
        attempt=receipt["attempt"], fenced_consume=binding.consume_approval,
    )
    ticket, executor = channel._file(
        ApprovalRequest(provider="codex", kind="command", name="shell", arguments={"command": "git status"}),
        "Approval requested",
    )
    assert registry.decide(ticket, "approved", "operator", "Confirmed")
    with pytest.raises(TaskServiceError, match="no longer belongs"):
        binding.consume_approval("wrong-job", ticket.approval_id, executor)
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
        launch_mode="legacy", intentional_review=True, routine_auto=True, scratch_root=str(tmp_path),
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
