"""Exercise timeout recovery without starting a coding runtime."""

import asyncio
import json
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from lazybridge import Envelope

import _code_bridge_fakes as fakes
from lazytools.code_bridge import _concurrency, _jobs, _lockfile, _progress, _store, cli


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("LAZYBRIDGE_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    fakes.install(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Bridge Test")
    git(repo, "config", "user.email", "bridge@example.invalid")
    git(repo, "commit", "--allow-empty", "-m", "initial")
    return repo


def run(repo, **kwargs):
    return _jobs.run_job(engine_name="codex", cwd=str(repo), root=str(repo.parent),
                         task="Done-when: tests pass", db_path=repo.parent / "jobs.sqlite", **kwargs)


def row(repo, result):
    store = _store.build_store(repo.parent / "jobs.sqlite")
    return _jobs.find_job(store, _store.build_job_registry(store), result.job_id)


@pytest.fixture
def wall_clock(monkeypatch):
    # Git startup latency is unrelated to the timeout behavior under test.
    # Keep asyncio's real timer, but control the bridge's remaining budget.
    now = [0.0]
    clock = SimpleNamespace(time=time.time, monotonic=lambda: now[0], sleep=time.sleep)
    monkeypatch.setattr(_jobs, "time", clock)
    monkeypatch.setattr(_concurrency, "time", clock)
    return now


@pytest.mark.parametrize("raised", [False, "timeout", "durable"])
def test_commit_then_timeout_resumes_same_thread_and_finishes(workspace, monkeypatch, capsys, raised):
    calls = []
    commits = []

    async def turn(engine, prompt, name):
        engine.finish_turn()
        calls.append((engine, prompt, engine.handle))
        git(workspace, "commit", "--allow-empty", "-m", f"step {len(calls)}")
        commits.append(git(workspace, "rev-parse", "HEAD"))
        if len(calls) == 1:
            if raised == "timeout":
                raise TimeoutError("turn deadline")
            if raised == "durable":
                raise RuntimeError(f"Turn timed out on durable thread {engine.handle}; whether it was committed is unknown")
            return Envelope.error_envelope(RuntimeError(f"Turn timed out on durable thread {engine.handle}; whether it was committed is unknown"))
        return Envelope(payload="finished")

    monkeypatch.setattr(_jobs, "_run_agent", turn)
    result = run(workspace)
    assert result.status == "done"
    assert calls[0][0] is calls[1][0]
    assert calls[0][2] == calls[1][2]
    assert calls[1][1] == _jobs.CONTINUATION_PROMPT
    record = row(workspace, result)
    assert record["continuations"] == 1
    assert [t["outcome"] for t in record["turns"]] == ["checkpoint", "done"]
    assert [t["commits"] for t in record["turns"]] == [[c] for c in commits]
    assert commits[0] in result.result_path.read_text(encoding="utf-8")
    db = str(workspace.parent / "jobs.sqlite")
    assert cli.main(["status", result.job_id, "--db", db]) == 0
    assert commits[1] in capsys.readouterr().out
    assert cli.main(["result", result.job_id, "--json", "--db", db]) == 0
    assert json.loads(capsys.readouterr().out)["continuations"] == 1


def test_timeout_without_commit_fails_no_progress(workspace, monkeypatch):
    async def turn(engine, *args):
        engine.finish_turn()
        (workspace / "dirty.txt").write_text("uncommitted")
        raise TimeoutError("turn deadline")

    monkeypatch.setattr(_jobs, "_run_agent", turn)
    result = run(workspace)
    assert result.status == "failed"
    assert result.error.startswith("no progress:")
    assert row(workspace, result)["continuations"] == 0


def test_progress_each_turn_exhausts_budget_as_partial(workspace, monkeypatch):
    async def turn(engine, *args):
        engine.finish_turn()
        git(workspace, "commit", "--allow-empty", "-m", "step")
        raise TimeoutError("turn deadline")

    monkeypatch.setattr(_jobs, "_run_agent", turn)
    result = run(workspace, max_continuations=2)
    assert result.status == "partial"
    assert "exhausted" in result.error
    assert len(row(workspace, result)["turns"]) == 3
    assert row(workspace, result)["continuations"] == 2
    assert cli.main(["wait", result.job_id, "--db", str(workspace.parent / "jobs.sqlite")]) == 1


def test_push_only_progress_resumes(workspace, monkeypatch):
    git(workspace, "branch", "-m", "feat/progress")
    remote = workspace.parent / "remote.git"
    git(workspace, "init", "--bare", str(remote))
    git(workspace, "remote", "add", "origin", str(remote))
    calls = []

    async def turn(engine, *args):
        engine.finish_turn()
        calls.append(1)
        if len(calls) == 1:
            git(workspace, "push", "origin", "HEAD:refs/heads/feat/progress")
            raise TimeoutError("turn deadline")
        return Envelope(payload="finished")

    monkeypatch.setattr(_jobs, "_run_agent", turn)
    result = run(workspace)
    assert result.status == "done"
    checkpoint = row(workspace, result)["turns"][0]
    assert checkpoint["commits"] == []
    assert checkpoint["pushed_refs"][0]["ref"] == "refs/heads/feat/progress"


def test_wall_cap_cancels_turn_after_progress(workspace, monkeypatch, wall_clock):
    async def turn(engine, *args):
        engine.finish_turn()
        git(workspace, "commit", "--allow-empty", "-m", "step")
        await asyncio.sleep(20)

    monkeypatch.setattr(_jobs, "_run_agent", turn)
    result = run(workspace, timeout=10, max_wall=2)
    assert result.status == "partial"
    assert "max wall" in result.error
    assert row(workspace, result)["continuations"] == 0


def test_wall_cap_keeps_earlier_checkpoint_when_latest_turn_has_no_commit(workspace, monkeypatch, wall_clock):
    calls = []

    async def turn(engine, *args):
        engine.finish_turn()
        calls.append(1)
        if len(calls) == 1:
            git(workspace, "commit", "--allow-empty", "-m", "step")
            wall_clock[0] = 2.0
            raise TimeoutError("turn deadline")
        await asyncio.sleep(20)

    monkeypatch.setattr(_jobs, "_run_agent", turn)
    result = run(workspace, timeout=10, max_wall=3)
    assert result.status == "partial"
    assert "max wall" in result.error
    assert row(workspace, result)["continuations"] == 1


def test_wall_cap_during_approval_closes_ticket(workspace, monkeypatch, wall_clock):
    async def turn(engine, *args):
        engine.finish_turn()
        store = _store.build_store(workspace.parent / "jobs.sqlite")
        job = _jobs.list_jobs(store)[0]
        _store.build_approval_queue(store).create_ticket(task_id=job["job_id"], prompt="waiting")
        await asyncio.sleep(20)

    monkeypatch.setattr(_jobs, "_run_agent", turn)
    result = run(workspace, timeout=10, max_wall=2)
    assert result.status == "failed"
    assert result.error.startswith("no progress:")
    tickets = [raw for _, raw in _store.build_store(workspace.parent / "jobs.sqlite").items(prefix=_store.APPROVAL_PREFIX)]
    assert tickets[0]["status"] == "rejected"


def test_wait_crosses_checkpoint(monkeypatch, capsys):
    states = iter([{"status": "checkpoint"}, {"status": "running"}, {"status": "done"}, {"status": "done", "result": "finished"}])
    monkeypatch.setattr(_store, "build_store", lambda *a: None)
    monkeypatch.setattr(_store, "build_job_registry", lambda *a: None)
    monkeypatch.setattr(_jobs, "find_job", lambda *a: next(states))
    monkeypatch.setattr(cli, "_detached_pid", lambda *a: None)
    monkeypatch.setattr(cli.time, "sleep", lambda *a: None)
    assert cli.main(["wait", "job"]) == 0
    assert "finished" in capsys.readouterr().out


@pytest.mark.parametrize("flags", [["--max-continuations", "-1"], ["--max-continuations", "11"], ["--max-wall", "0"], ["--max-wall", "nan"]])
def test_invalid_limits_refused_before_detach(workspace, monkeypatch, flags):
    monkeypatch.setattr(cli, "_spawn_detached", lambda *a: pytest.fail("spawned"))
    assert cli.main(["run", "--detach", "--engine", "codex", "--cwd", str(workspace),
                     "--root", str(workspace.parent), "--task", "x", *flags]) == 2


def test_slot_wait_respects_total_wall_cap(tmp_path, monkeypatch):
    slot = _concurrency.EngineSlot("codex", 1, tmp_path)
    now = [0.0]

    def busy(*args):
        raise _concurrency.EngineBusy("codex", 1, [])

    monkeypatch.setattr(_concurrency.EngineSlot, "try_acquire", busy)
    monkeypatch.setattr(_concurrency.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(_concurrency.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    with pytest.raises(ValueError, match="max-wall"):
        slot.acquire("job", wait=True, deadline=2)
    assert now[0] == 2


def test_detached_child_receives_recovery_and_routine_options(tmp_path):
    parser = cli.build_parser()
    args = parser.parse_args(["run", "--engine", "codex", "--cwd", str(tmp_path), "--task", "x",
                              "--max-continuations", "2", "--max-wall", "600", "--scratch-root", "E:/Scratch",
                              "--no-routine-auto"])
    child = parser.parse_args(cli._child_argv(args, "job")[3:])
    assert child.max_continuations == 2
    assert child.max_wall == 600
    assert child.scratch_root == "E:/Scratch"
    assert child.routine_auto is False


def test_unrelated_remote_commits_do_not_count_as_job_progress(workspace, monkeypatch):
    git(workspace, "branch", "-m", "feat/own")
    remote = workspace.parent / "remote.git"
    git(workspace, "init", "--bare", str(remote))
    git(workspace, "remote", "add", "origin", str(remote))
    git(workspace, "push", "origin", "HEAD:refs/heads/feat/own")

    async def turn(engine, *args):
        engine.finish_turn()
        tree = git(workspace, "rev-parse", "HEAD^{tree}")
        commit = git(workspace, "commit-tree", tree, "-p", "HEAD", "-m", "unrelated")
        git(workspace, "push", "origin", f"{commit}:refs/heads/feat/other")
        raise TimeoutError("turn deadline")

    monkeypatch.setattr(_jobs, "_run_agent", turn)
    result = run(workspace)
    assert result.status == "failed"
    assert result.error.startswith("no progress:")
    assert row(workspace, result)["turns"][0]["pushed_refs"] == []
    assert row(workspace, result)["turns"][0]["commits"] == []


def test_git_evidence_probes_run_off_the_event_loop(workspace, monkeypatch):
    main_thread = threading.get_ident()
    observed = []
    for name in ("snapshot", "changes"):
        original = getattr(_progress, name)

        def checked(*args, _original=original, **kwargs):
            observed.append(threading.get_ident())
            assert threading.get_ident() != main_thread
            return _original(*args, **kwargs)

        monkeypatch.setattr(_progress, name, checked)
    assert run(workspace).status == "done"
    assert len(observed) == 3


def test_timed_out_engine_subprocess_is_dead_before_continuation(workspace, monkeypatch):
    processes = []
    calls = []

    async def turn(engine, *args):
        engine.finish_turn()
        calls.append(1)
        if len(calls) == 1:
            process = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(60)")
            processes.append(process)
            git(workspace, "commit", "--allow-empty", "-m", "step")
            return Envelope.error_envelope(TimeoutError("turn deadline"))
        assert processes[0].returncode is not None
        assert await processes[0].wait() is not None
        return Envelope(payload="finished")

    monkeypatch.setattr(_jobs, "_run_agent", turn)
    result = run(workspace)
    assert result.status == "done"
    assert len(calls) == 2
    assert processes[0].returncode is not None


def test_late_cancellation_ticket_is_closed_before_continuation(workspace, monkeypatch):
    store = _store.build_store(workspace.parent / "jobs.sqlite")
    queue = _store.build_approval_queue(store)
    sweeps = []
    closed = _jobs.close_pending_tickets
    calls = []
    late = []

    def close(*args):
        sweeps.append(1)
        closed(*args)

    async def turn(engine, *args):
        engine.finish_turn()
        calls.append(1)
        if len(calls) == 1:
            job_id = _jobs.list_jobs(store)[0]["job_id"]
            ready = asyncio.Event()

            async def late_callback():
                ready.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    assert len(sweeps) == 1
                    late.append(queue.create_ticket(task_id=job_id, prompt="late callback ticket"))

            callback = asyncio.create_task(late_callback())
            await ready.wait()
            assert not callback.done()
            git(workspace, "commit", "--allow-empty", "-m", "step")
            raise TimeoutError("turn deadline")
        assert len(sweeps) >= 2
        assert queue.get_ticket(late[0].approval_id).status == "rejected"
        return Envelope(payload="finished")

    monkeypatch.setattr(_jobs, "close_pending_tickets", close)
    monkeypatch.setattr(_jobs, "_run_agent", turn)
    result = run(workspace)
    assert result.status == "done"
    assert len(late) == 1
    assert _jobs.pending_ticket_ids(store, result.job_id) == []


def test_cleanup_failure_prevents_automatic_continuation(workspace, monkeypatch):
    original = _jobs.TurnCleanup
    calls = []

    class IncompleteCleanup(original):
        async def __aexit__(self, *args):
            await super().__aexit__(*args)
            self.error = "subprocess termination could not be verified"

    async def turn(engine, *args):
        engine.finish_turn()
        calls.append(1)
        git(workspace, "commit", "--allow-empty", "-m", "step")
        raise TimeoutError("turn deadline")

    monkeypatch.setattr(_jobs, "TurnCleanup", IncompleteCleanup)
    monkeypatch.setattr(_jobs, "_run_agent", turn)
    result = run(workspace)
    assert result.status == "partial"
    assert "termination could not be verified" in result.error
    assert len(calls) == 1


def test_wait_reports_dead_checkpoint_owner_without_pid_file(monkeypatch, capsys):
    job = {"job_id": "job", "status": "checkpoint", "pid": 999999}
    monkeypatch.setattr(_store, "build_store", lambda *a: None)
    monkeypatch.setattr(_store, "build_job_registry", lambda *a: None)
    monkeypatch.setattr(_jobs, "find_job", lambda *a: job)
    monkeypatch.setattr(cli, "_detached_pid", lambda *a: None)
    monkeypatch.setattr(_lockfile, "_pid_alive", lambda pid: False)
    assert cli.main(["wait", "job", "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "died"
    assert result["last_status"] == "checkpoint"


@pytest.mark.parametrize("enabled", [True, False])
def test_routine_rule_count_is_logged_and_visible_in_status(workspace, capsys, enabled):
    result = run(workspace, routine_auto=enabled)
    expected = "routine-auto: enabled; 13 active rules" if enabled else "routine-auto: disabled; 0 active rules"
    assert expected in capsys.readouterr().err
    assert cli.main(["status", result.job_id, "--db", str(workspace.parent / "jobs.sqlite")]) == 0
    assert expected in capsys.readouterr().out
