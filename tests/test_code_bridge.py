"""Unit tests for ``lazytools.code_bridge`` — fake engines, no real Codex/Claude calls.

Covers: job lifecycle, lock refusal + stale reclaim, an approval ticket
filed -> approved -> job continues, filed -> rejected -> job fails, TTL
expiry -> job fails, session persistence/reuse, and cwd-outside-root
refusal.
"""

from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace

import pytest
from lazybridge import Envelope
from lazybridge.engines.codex.app_server import CodexAppServerClient
from lazybridge.ext.approval import ApprovalQueue

import _code_bridge_fakes as fakes
from lazytools.code_bridge import _engines, _jobs, _registry, _store, cli
from lazytools.code_bridge._lockfile import JobLock, LockHeld


def _env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Isolate every process-wide file this package touches, per test."""
    monkeypatch.setenv("LAZYBRIDGE_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_POLL_SECONDS", "0.05")


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    return path


# --------------------------------------------------------------------------- #
# job lifecycle
# --------------------------------------------------------------------------- #


def test_job_runs_to_done_and_persists_result(tmp_path, repo, monkeypatch):
    _env(monkeypatch, tmp_path)
    script = fakes.install(monkeypatch)
    script.text = "all good"
    db_path = tmp_path / "store.sqlite"

    seen_job_ids = []
    result = _jobs.run_job(
        engine_name="codex",
        cwd=str(repo),
        task="do a thing",
        root=str(tmp_path),
        db_path=db_path,
        on_job_id=seen_job_ids.append,
    )

    assert result.status == "done"
    assert result.text == "all good"
    assert seen_job_ids == [result.job_id]
    assert result.result_path.read_text(encoding="utf-8") == "all good"

    store = _store.build_store(db_path)
    rows = _jobs.list_jobs(store, all_jobs=True)
    assert len(rows) == 1
    assert rows[0]["status"] == "done"
    assert rows[0]["engine"] == "codex"
    assert rows[0]["cwd"] == str(repo.resolve())
    assert rows[0]["heartbeat_at"] == rows[0]["turns"][-1]["ended_at"]


def test_codex_activity_refreshes_heartbeat_during_a_turn(tmp_path, repo, monkeypatch):
    _env(monkeypatch, tmp_path)
    fakes.install(monkeypatch)
    db_path = tmp_path / "store.sqlite"
    job_id = "heartbeat-test"
    now = time.time()
    monkeypatch.setattr(_jobs, "time", SimpleNamespace(time=lambda: now, monotonic=time.monotonic))
    engine_events = []

    async def run_transport(_client, *, progress):
        nonlocal now
        with _store.build_store(db_path) as store:
            assert _store.read_meta(store, job_id)["heartbeat_at"] == now
            now += 300
            progress["on_activity"]()
            assert _store.read_meta(store, job_id)["heartbeat_at"] == now

    async def run_agent(agent, _prompt):
        def activity():
            engine_events.append(now)

        progress = {"on_activity": activity}
        await agent.engine.kwargs["client"].run(progress=progress)
        assert progress["on_activity"] is activity
        return Envelope(payload="done")

    monkeypatch.setattr(CodexAppServerClient, "run", run_transport)
    monkeypatch.setattr(fakes.FakeAgent, "run", run_agent)

    result = _jobs.run_job(engine_name="codex", cwd=str(repo), task="do a thing",
                           root=str(tmp_path), db_path=db_path, job_id=job_id)

    assert result.status == "done"
    assert engine_events == [now]


def test_claude_tool_calls_are_recorded_until_their_results_arrive(tmp_path, repo, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, ToolResultBlock, ToolUseBlock, UserMessage

    _env(monkeypatch, tmp_path)
    fakes.install(monkeypatch)
    db_path = tmp_path / "store.sqlite"
    job_id = "tool-heartbeat-test"
    now = time.time()
    monkeypatch.setattr(_jobs, "time", SimpleNamespace(time=lambda: now, monotonic=time.monotonic))
    options = SimpleNamespace(on_session_id=None)

    async def run_agent(agent, _prompt):
        nonlocal now
        client = agent.engine.kwargs["client"]
        # Tool calls come from the engine's own events, not a custom SDK transport.
        assert client._transport_factory is None and client._query_factory is None
        client._report_session_id(options, AssistantMessage(
            content=[ToolUseBlock(id="toolu_1", name="Bash", input={"command": "pytest"})], model="m"))
        with _store.build_store(db_path) as store:
            meta = _store.read_meta(store, job_id)
            assert meta["pid"] == os.getpid() and "engine_pid" not in meta
            assert meta["tool_calls"] == {"toolu_1": {"name": "Bash", "started_at": now}}
            now += 1200
            client._report_session_id(options, UserMessage(content=[ToolResultBlock(tool_use_id="toolu_1")]))
            meta = _store.read_meta(store, job_id)
            assert meta["tool_calls"] == {} and meta["heartbeat_at"] == now
            client._report_session_id(options, AssistantMessage(
                content=[ToolUseBlock(id="toolu_2", name="Bash", input={})], model="m"))
            client._report_session_id(options, ResultMessage(
                subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s"))
            assert _store.read_meta(store, job_id)["tool_calls"] == {}
        return Envelope(payload="done")

    monkeypatch.setattr(fakes.FakeAgent, "run", run_agent)

    result = _jobs.run_job(engine_name="claude", cwd=str(repo), task="do a thing",
                           root=str(tmp_path), db_path=db_path, job_id=job_id)

    assert result.status == "done", result


def test_governed_claude_launch_with_release_report_is_admitted(tmp_path, monkeypatch):
    """The heartbeat client passes LazyBridge's governed admission check (no custom transport)."""
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
    from lazybridge.engines.claude_code import sdk_client

    folder = tmp_path / "work"
    folder.mkdir()
    binary = tmp_path / "bin" / "claude.exe"
    binary.parent.mkdir()
    binary.write_text("", encoding="utf-8")
    report = tmp_path / "release-report.json"
    report.write_text("{}", encoding="utf-8")
    seen = {}

    def compile_claude(policy, *, release_report, runtime_revision):
        seen.update(release_report=release_report, runtime_revision=runtime_revision)
        return {"eligible": True, "ineligibility_reasons": []}

    monkeypatch.setattr(SubprocessCLITransport, "_find_cli", lambda _self: str(binary))
    monkeypatch.setattr(sdk_client, "runtime_revision", lambda _binary: "rev-1")
    monkeypatch.setattr(sdk_client, "compile_claude", compile_claude)
    client = _engines._claude_client(policy_release_report=report, on_activity=lambda **_: None)
    options = SimpleNamespace(effective_policy=SimpleNamespace(folder=folder.resolve()))

    assert client._require_policy_evidence(options) == binary.resolve()
    assert seen == {"release_report": client._policy_release_report, "runtime_revision": "rev-1"}


@pytest.mark.parametrize(("engine", "reported_model"), [
    ("codex", "gpt-reported-by-app-server"),
    ("claude", "claude-reported-by-sdk"),
])
def test_job_persists_engine_reported_model_when_none_was_requested(
    tmp_path, repo, monkeypatch, engine, reported_model
):
    _env(monkeypatch, tmp_path)
    script = fakes.install(monkeypatch)
    script.reported_model = reported_model
    db_path = tmp_path / "store.sqlite"

    result = _jobs.run_job(
        engine_name=engine, cwd=str(repo), task="do a thing", root=str(tmp_path), db_path=db_path
    )

    meta = _store.read_meta(_store.build_store(db_path), result.job_id)
    assert meta["model"] == reported_model


def test_job_keeps_explicit_model_when_engine_reports_a_different_model(tmp_path, repo, monkeypatch):
    _env(monkeypatch, tmp_path)
    script = fakes.install(monkeypatch)
    script.reported_model = "engine-reported-model"
    db_path = tmp_path / "store.sqlite"

    result = _jobs.run_job(
        engine_name="codex", cwd=str(repo), task="do a thing", root=str(tmp_path), db_path=db_path,
        model="explicit-model",
    )

    meta = _store.read_meta(_store.build_store(db_path), result.job_id)
    assert meta["model"] == "explicit-model"


def test_job_failure_is_recorded_not_raised(tmp_path, repo, monkeypatch):
    _env(monkeypatch, tmp_path)
    script = fakes.install(monkeypatch)
    script.mode = "error"
    db_path = tmp_path / "store.sqlite"

    result = _jobs.run_job(
        engine_name="claude", cwd=str(repo), task="do a thing", root=str(tmp_path), db_path=db_path
    )

    assert result.status == "failed"
    assert result.error
    assert "[failed]" in result.result_path.read_text(encoding="utf-8")


def test_lock_is_keyed_on_the_git_repo_root_not_the_literal_cwd(tmp_path, monkeypatch, provision_bridge_approver):
    """Two cwds inside the SAME repo (root + a subdirectory) must share one lock."""
    _env(monkeypatch, tmp_path)
    repo_root = tmp_path / "repo"
    (repo_root / "sub" / "dir").mkdir(parents=True)
    (repo_root / ".git").mkdir()

    script = fakes.install(monkeypatch)
    script.request_approval = True  # keep the job parked on a ticket so it's still "running"
    db_path = tmp_path / "store.sqlite"
    registry_path = db_path.with_suffix(".registry.sqlite")
    provision_bridge_approver(registry_path)

    thread, _box = _run_in_thread(
        engine_name="codex",
        cwd=str(repo_root / "sub" / "dir"),
        task="x",
        root=str(tmp_path),
        db_path=db_path,
    )
    try:
        queue = ApprovalQueue(_registry.registry_store(registry_path))
        ticket = _wait_for_one_ticket(queue, thread=thread, box=_box)  # job holds the repo-root lock

        with pytest.raises(LockHeld):
            _jobs.run_job(
                engine_name="codex", cwd=str(repo_root), task="y", root=str(tmp_path), db_path=db_path
            )

        assert cli.main(["reject", ticket.approval_id, "--db", str(db_path), "--reason", "done"]) == 0
        thread.join(timeout=10)
    finally:
        assert not thread.is_alive()


def test_result_write_failure_does_not_record_the_job_as_done(tmp_path, repo, monkeypatch):
    import lazytools.code_bridge._jobs as jobs_mod

    _env(monkeypatch, tmp_path)
    script = fakes.install(monkeypatch)
    script.text = "would have succeeded"
    db_path = tmp_path / "store.sqlite"

    def _boom(db_path, job_id, text):
        raise OSError("disk full")

    monkeypatch.setattr(jobs_mod, "_write_result", _boom)

    result = _jobs.run_job(engine_name="codex", cwd=str(repo), task="x", root=str(tmp_path), db_path=db_path)

    assert result.status == "failed"
    assert result.result_path is None
    assert "disk full" in result.error

    store = _store.build_store(db_path)
    row = _jobs.find_job(store, _store.build_job_registry(store), result.job_id)
    assert row["status"] == "failed"  # never "done" with a missing result file


def test_keyboard_interrupt_marks_the_job_interrupted_and_propagates(tmp_path, repo, monkeypatch):
    _env(monkeypatch, tmp_path)
    script = fakes.install(monkeypatch)
    script.mode = "raise"
    script.exception_cls = KeyboardInterrupt
    db_path = tmp_path / "store.sqlite"

    with pytest.raises(KeyboardInterrupt):
        _jobs.run_job(engine_name="codex", cwd=str(repo), task="x", root=str(tmp_path), db_path=db_path)

    store = _store.build_store(db_path)
    rows = _jobs.list_jobs(store, all_jobs=True)
    assert len(rows) == 1
    assert rows[0]["status"] == "interrupted"

    # The lock was still released despite the re-raise -- a later run in
    # the same cwd must not be refused.
    lock = JobLock(_store.locks_dir(db_path), repo.resolve())
    lock.acquire("next-job")
    lock.release()


def test_cwd_outside_root_is_refused_before_any_job_is_recorded(tmp_path, repo, monkeypatch):
    _env(monkeypatch, tmp_path)
    fakes.install(monkeypatch)
    outside = tmp_path.parent / "definitely-outside"
    outside.mkdir(exist_ok=True)
    db_path = tmp_path / "store.sqlite"

    with pytest.raises(ValueError):
        _jobs.run_job(engine_name="codex", cwd=str(outside), task="x", root=str(tmp_path), db_path=db_path)

    store = _store.build_store(db_path)
    assert _jobs.list_jobs(store, all_jobs=True) == []


# --------------------------------------------------------------------------- #
# lock refusal + stale reclaim
# --------------------------------------------------------------------------- #


def test_second_run_in_same_cwd_is_refused_while_first_is_live(tmp_path, repo):
    lock_dir = tmp_path / "locks"
    first = JobLock(lock_dir, repo.resolve())
    first.acquire("job-a")

    second = JobLock(lock_dir, repo.resolve())
    with pytest.raises(LockHeld) as exc_info:
        second.acquire("job-b")
    assert "job-a" in str(exc_info.value)

    first.release()
    # Now that the holder released, a fresh acquire succeeds.
    second.acquire("job-b")
    second.release()


def test_stale_lock_from_a_dead_pid_is_reclaimed(tmp_path, repo, monkeypatch):
    import lazytools.code_bridge._lockfile as lockfile_mod

    lock_dir = tmp_path / "locks"
    stale = JobLock(lock_dir, repo.resolve())
    stale.acquire("dead-job")
    # Simulate the holder having died: nobody's pid passes the liveness
    # check now, regardless of what is actually in the lock file.
    monkeypatch.setattr(lockfile_mod, "_pid_alive", lambda pid: False)

    fresh = JobLock(lock_dir, repo.resolve())
    fresh.acquire("new-job")  # must not raise

    import json

    on_disk = json.loads(fresh.path.read_text())
    assert on_disk["job_id"] == "new-job"
    fresh.release()


def test_reclaiming_a_stale_lock_marks_the_old_job_interrupted(tmp_path, repo, monkeypatch):
    import lazytools.code_bridge._lockfile as lockfile_mod

    _env(monkeypatch, tmp_path)
    db_path = tmp_path / "store.sqlite"
    store = _store.build_store(db_path)
    registry = _store.build_job_registry(store)
    registry.write("dead-job-id", "an old task", tool_name="codex", status="running")

    lock = JobLock(_store.locks_dir(db_path), repo.resolve())
    lock.acquire("dead-job-id")
    monkeypatch.setattr(lockfile_mod, "_pid_alive", lambda pid: False)

    fakes.install(monkeypatch)
    result = _jobs.run_job(engine_name="codex", cwd=str(repo), task="new task", root=str(tmp_path), db_path=db_path)
    assert result.status == "done"

    old = registry.find("dead-job-id")
    assert old is not None
    assert old["status"] == "interrupted"


# --------------------------------------------------------------------------- #
# approval tickets
# --------------------------------------------------------------------------- #


def _run_in_thread(**kwargs):
    box: dict = {}

    def worker() -> None:
        try:
            box["result"] = _jobs.run_job(**kwargs)
        except Exception as exc:  # pragma: no cover - surfaced via assertion below
            box["exception"] = exc

    thread = threading.Thread(target=worker)
    thread.start()
    return thread, box


def _wait_for_one_ticket(queue, timeout: float = 30.0, thread=None, box=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        tickets = queue.list_pending_tickets()
        if tickets:
            return tickets[0]
        if thread is not None and not thread.is_alive():
            # The job finished without ever filing a ticket: say how, instead
            # of waiting out the timeout and failing with no clue.
            raise AssertionError(f"job ended before any ticket appeared: {box!r}")
        time.sleep(0.02)
    raise AssertionError(f"no approval ticket appeared in time (job box: {box!r})")


def test_approval_ticket_approved_lets_the_job_continue(tmp_path, repo, monkeypatch, provision_bridge_approver):
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_TICKET_TTL", "30")
    script = fakes.install(monkeypatch)
    script.request_approval = True
    script.text = "continued after approval"
    db_path = tmp_path / "store.sqlite"
    registry_path = db_path.with_suffix(".registry.sqlite")
    provision_bridge_approver(registry_path)

    thread, box = _run_in_thread(
        engine_name="codex", cwd=str(repo), task="do a risky thing", root=str(tmp_path), db_path=db_path
    )
    try:
        queue = ApprovalQueue(_registry.registry_store(registry_path))
        ticket = _wait_for_one_ticket(queue, thread=thread, box=box)
        assert ticket.task_id  # scoped to the job
        assert cli.main(["approve", ticket.approval_id, "--db", str(db_path)]) == 0
        thread.join(timeout=10)
    finally:
        assert not thread.is_alive()

    assert "exception" not in box
    result = box["result"]
    assert result.status == "done"
    assert result.text == "continued after approval"


def test_approval_ticket_rejected_fails_the_job(tmp_path, repo, monkeypatch, provision_bridge_approver):
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_TICKET_TTL", "30")
    script = fakes.install(monkeypatch)
    script.request_approval = True
    db_path = tmp_path / "store.sqlite"
    registry_path = db_path.with_suffix(".registry.sqlite")
    provision_bridge_approver(registry_path)

    thread, box = _run_in_thread(
        engine_name="codex", cwd=str(repo), task="do a risky thing", root=str(tmp_path), db_path=db_path
    )
    try:
        queue = ApprovalQueue(_registry.registry_store(registry_path))
        ticket = _wait_for_one_ticket(queue, thread=thread, box=box)
        assert cli.main(["reject", ticket.approval_id, "--db", str(db_path), "--reason", "no"]) == 0
        thread.join(timeout=10)
    finally:
        assert not thread.is_alive()

    result = box["result"]
    assert result.status == "failed"
    assert "denied" in result.error
    meta = _store.read_meta(_store.build_store(db_path), result.job_id)
    ticket = ApprovalQueue(_registry.registry_store(db_path.with_suffix(".registry.sqlite"))).get_ticket(
        meta["registry_ticket_ids"][0]
    )
    assert ticket.status == "rejected"


def test_approval_ticket_ttl_expiry_fails_the_job_cleanly(tmp_path, repo, monkeypatch):
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_TICKET_TTL", "0.2")
    script = fakes.install(monkeypatch)
    script.request_approval = True
    db_path = tmp_path / "store.sqlite"

    # No one ever answers: the ticket must expire and the job must fail,
    # not hang.
    result = _jobs.run_job(
        engine_name="codex", cwd=str(repo), task="do a risky thing", root=str(tmp_path), db_path=db_path
    )

    assert result.status == "failed"
    assert "denied" in result.error
    meta = _store.read_meta(_store.build_store(db_path), result.job_id)
    ticket = ApprovalQueue(_registry.registry_store(db_path.with_suffix(".registry.sqlite"))).get_ticket(
        meta["registry_ticket_ids"][0]
    )
    assert ticket.status == "expired"


# --------------------------------------------------------------------------- #
# session persistence / reuse
# --------------------------------------------------------------------------- #


def test_session_name_resumes_the_same_native_session_on_the_next_run(tmp_path, repo, monkeypatch):
    _env(monkeypatch, tmp_path)
    script = fakes.install(monkeypatch)
    db_path = tmp_path / "store.sqlite"

    first = _jobs.run_job(
        engine_name="codex",
        cwd=str(repo),
        task="first turn",
        session_name="my-session",
        root=str(tmp_path),
        db_path=db_path,
    )
    assert first.status == "done"
    first_handle = script.engines[0].handle
    assert first_handle is not None

    second = _jobs.run_job(
        engine_name="codex",
        cwd=str(repo),
        task="second turn",
        session_name="my-session",
        root=str(tmp_path),
        db_path=db_path,
    )
    assert second.status == "done"
    # The second engine was constructed with the FIRST run's native id
    # already resolved from the session registry -- a real resume, not a
    # fresh thread.
    assert script.engines[1].kwargs.get("thread_id") == first_handle


def test_both_engines_get_web_access(tmp_path, repo, monkeypatch):
    """Jobs may look things up while they work: Claude gets WebSearch/WebFetch,
    Codex gets live web search set explicitly (not inherited from the user's
    Codex config)."""
    _env(monkeypatch, tmp_path)
    script = fakes.install(monkeypatch)
    db_path = tmp_path / "store.sqlite"

    _jobs.run_job(engine_name="claude", cwd=str(repo), task="x", root=str(tmp_path), db_path=db_path)
    assert script.kwargs["web"] is True

    _jobs.run_job(engine_name="codex", cwd=str(repo), task="y", root=str(tmp_path), db_path=db_path)
    assert script.kwargs["config"].codex.web_search == "live"
    assert script.kwargs["config"].codex.sandbox == "workspace-write"
