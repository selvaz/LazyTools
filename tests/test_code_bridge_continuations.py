"""Exercise timeout recovery without starting a coding runtime."""

import asyncio
import json
import subprocess

import pytest
from lazybridge import Envelope

import _code_bridge_fakes as fakes
from lazytools.code_bridge import _jobs, _store, cli


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


@pytest.mark.parametrize("raised", [False, True])
def test_commit_then_timeout_resumes_same_thread_and_finishes(workspace, monkeypatch, capsys, raised):
    calls = []
    commits = []

    async def turn(engine, prompt, name):
        engine.finish_turn()
        calls.append((engine, prompt, engine.handle))
        git(workspace, "commit", "--allow-empty", "-m", f"step {len(calls)}")
        commits.append(git(workspace, "rev-parse", "HEAD"))
        if len(calls) == 1:
            if raised:
                raise TimeoutError("turn deadline")
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


def test_wall_cap_cancels_turn_after_progress(workspace, monkeypatch):
    async def turn(engine, *args):
        engine.finish_turn()
        git(workspace, "commit", "--allow-empty", "-m", "step")
        await asyncio.sleep(20)

    monkeypatch.setattr(_jobs, "_run_agent", turn)
    result = run(workspace, timeout=10, max_wall=2)
    assert result.status == "partial"
    assert "max wall" in result.error
    assert row(workspace, result)["continuations"] == 0


def test_wait_crosses_checkpoint(monkeypatch, capsys):
    states = iter([{"status": "checkpoint"}, {"status": "running"}, {"status": "done"}, {"status": "done", "result": "finished"}])
    monkeypatch.setattr(_store, "build_store", lambda *a: None)
    monkeypatch.setattr(_store, "build_job_registry", lambda *a: None)
    monkeypatch.setattr(_jobs, "find_job", lambda *a: next(states))
    monkeypatch.setattr(cli, "_detached_pid", lambda *a: None)
    monkeypatch.setattr(cli.time, "sleep", lambda *a: None)
    assert cli.main(["wait", "job"]) == 0
    assert "finished" in capsys.readouterr().out


@pytest.mark.parametrize("flags", [["--max-continuations", "-1"], ["--max-wall", "0"], ["--max-wall", "nan"]])
def test_invalid_limits_refused_before_detach(workspace, monkeypatch, flags):
    monkeypatch.setattr(cli, "_spawn_detached", lambda *a: pytest.fail("spawned"))
    assert cli.main(["run", "--detach", "--engine", "codex", "--cwd", str(workspace),
                     "--root", str(workspace.parent), "--task", "x", *flags]) == 2
