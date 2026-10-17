"""Per-engine machine-wide concurrency limit, and the Codex sandbox's temp root."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

import pytest

import _code_bridge_fakes as fakes
from lazytools.code_bridge import _concurrency, _engines, _jobs, _lockfile, _store, cli
from lazytools.code_bridge._concurrency import EngineBusy, EngineSlot


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LAZYBRIDGE_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_POLL_SECONDS", "0.05")
    monkeypatch.setattr(_concurrency, "WAIT_POLL_SECONDS", 0.05)
    script = fakes.install(monkeypatch)
    repos = []
    for name in ("repo-a", "repo-b"):
        path = tmp_path / name
        (path / ".git").mkdir(parents=True)
        repos.append(path)
    return script, repos, tmp_path / "store.sqlite"


def hold(db: Path, engine: str, job_id: str, limit: int = 1) -> EngineSlot:
    """A slot held by THIS (live) process, standing in for another running job."""
    slot = EngineSlot(_store.locks_dir(db), engine, limit)
    slot.try_acquire(job_id)
    return slot


def run(repo: Path, db: Path, engine: str = "codex", **kwargs):
    return _jobs.run_job(engine_name=engine, cwd=str(repo), task="t", root=str(repo.parent), db_path=db, **kwargs)


# --------------------------------------------------------------------------- #
# run refuses / queues
# --------------------------------------------------------------------------- #


def test_second_codex_run_is_refused_naming_the_running_job(env):
    _, (_, repo_b), db = env
    holder = hold(db, "codex", "job-already-running")
    try:
        with pytest.raises(EngineBusy) as exc_info:
            run(repo_b, db)
        assert "job-already-running" in str(exc_info.value)
        assert "--wait-for-slot" in str(exc_info.value)
        # Nothing was recorded for the refused job, and its repo lock was released.
        assert _jobs.list_jobs(_store.build_store(db), all_jobs=True) == []
        assert run(repo_b, db, engine="claude").status == "done"
    finally:
        holder.release()
    assert run(repo_b, db).status == "done"


def test_claude_is_unlimited_by_default(env):
    _, (repo_a, _), db = env
    holder = hold(db, "codex", "codex-job")
    try:
        assert run(repo_a, db, engine="claude").status == "done"
        assert _concurrency.engine_at_limit(_store.locks_dir(db), "claude") == []
    finally:
        holder.release()


def test_wait_for_slot_queues_until_the_running_job_ends(env):
    _, (_, repo_b), db = env
    holder = hold(db, "codex", "job-ahead")
    waiting = threading.Event()
    box: dict = {}

    def target():
        box["result"] = run(repo_b, db, wait_for_slot=True, on_slot_wait=lambda busy: waiting.set())

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    try:
        assert waiting.wait(10)
        time.sleep(0.2)
        assert thread.is_alive()
        assert _jobs.list_jobs(_store.build_store(db), all_jobs=True) == []  # queued, not started
    finally:
        holder.release()
    thread.join(10)
    assert not thread.is_alive()
    assert box["result"].status == "done"


def test_stale_slot_from_a_dead_process_is_reclaimed_and_its_job_marked_interrupted(env, monkeypatch):
    _, (_, repo_b), db = env
    dead_pid = 4_000_000_000 % (2**31)
    real_alive = _lockfile._pid_alive
    monkeypatch.setattr(_lockfile, "_pid_alive", lambda pid: pid != dead_pid and real_alive(pid))
    store = _store.build_store(db)
    _store.build_job_registry(store).write("dead-job", "old", tool_name="codex", status="running")
    _store.write_meta(store, "dead-job", {"job_id": "dead-job", "engine": "codex", "cwd": str(repo_b)})
    lock_dir = _store.locks_dir(db)
    lock_dir.mkdir(parents=True, exist_ok=True)
    (lock_dir / "engine-codex-0.lock").write_text(json.dumps({"pid": dead_pid, "job_id": "dead-job"}))

    assert _concurrency.engine_at_limit(lock_dir, "codex") == []
    assert run(repo_b, db).status == "done"
    old = _store.build_job_registry(_store.build_store(db)).find("dead-job")
    assert old["status"] == "interrupted"


def test_configured_limit_of_two_allows_two_codex_jobs(env, tmp_path, monkeypatch):
    _, (_, repo_b), db = env
    config = tmp_path / "code-bridge.toml"
    config.write_text("[concurrency]\ncodex = 2\n", encoding="utf-8")
    monkeypatch.setenv(_concurrency.CONFIG_ENV, str(config))
    holder = hold(db, "codex", "first", limit=2)
    try:
        assert run(repo_b, db).status == "done"
    finally:
        holder.release()


def test_slot_is_released_after_a_failed_job(env):
    script, (repo_a, repo_b), db = env
    script.mode = "error"
    assert run(repo_a, db).status == "failed"
    script.mode = "ok"
    assert run(repo_b, db).status == "done"


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #


def test_limits_default_and_config(tmp_path):
    missing = tmp_path / "none.toml"
    assert _concurrency.load_limits(missing) == {"codex": 1, "claude": None}
    path = tmp_path / "c.toml"
    path.write_text("[concurrency]\ncodex = 0\nclaude = 2\n", encoding="utf-8")
    assert _concurrency.load_limits(path) == {"codex": None, "claude": 2}


@pytest.mark.parametrize("body", ["[concurrency]\ncodex = -1\n", "[concurrency]\ncodex = 'x'\n",
                                  "[concurrency]\ngemini = 1\n", "[concurrency]\ncodex = true\n", "not toml ["])
def test_bad_limits_are_rejected(tmp_path, body):
    path = tmp_path / "c.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError):
        _concurrency.load_limits(path)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _run_args(repo: Path, db: Path, *extra: str) -> list[str]:
    return ["run", "--engine", "codex", "--cwd", str(repo), "--root", str(repo.parent), "--task", "t",
            "--db", str(db), *extra]


def test_cli_run_refuses_with_a_clear_message(env, capsys):
    _, (_, repo_b), db = env
    holder = hold(db, "codex", "busy-job-id")
    try:
        assert cli.main(_run_args(repo_b, db)) == 2
    finally:
        holder.release()
    err = capsys.readouterr().err
    assert "busy-job-id" in err and "concurrency limit" in err


def test_cli_detach_refuses_early_and_queues_with_wait_for_slot(env, monkeypatch, capsys):
    _, (_, repo_b), db = env
    spawned = []
    monkeypatch.setattr(cli, "_spawn_detached", lambda argv, log: spawned.append(argv) or 4321)
    holder = hold(db, "codex", "busy-job-id")
    try:
        assert cli.main(_run_args(repo_b, db, "--detach")) == 2
        assert "busy-job-id" in capsys.readouterr().err and spawned == []
        assert cli.main(_run_args(repo_b, db, "--detach", "--wait-for-slot")) == 0
    finally:
        holder.release()
    assert "--wait-for-slot" in spawned[0]


def test_cli_bad_config_is_a_clean_error(env, tmp_path, monkeypatch, capsys):
    _, (_, repo_b), db = env
    config = tmp_path / "bad.toml"
    config.write_text("[concurrency]\ncodex = -3\n", encoding="utf-8")
    monkeypatch.setenv(_concurrency.CONFIG_ENV, str(config))
    assert cli.main(_run_args(repo_b, db)) == 2
    assert "codex must be an integer" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Codex sandbox: temp dir writable, network untouched
# --------------------------------------------------------------------------- #


def test_codex_sandbox_gets_the_os_temp_dir_as_a_writable_root(env, tmp_path):
    script, (repo_a, _), db = env
    run(repo_a, db)
    codex_policy = script.kwargs["config"].codex
    assert codex_policy.sandbox == "workspace-write"
    assert codex_policy.approval_policy == "on-request"
    roots = codex_policy.writable_roots
    assert str(Path(tempfile.gettempdir()).resolve()) in roots
    # pytest's own tmp_path lives under the temp dir, so a job's tests can use it.
    assert any(tmp_path.resolve().is_relative_to(Path(root)) for root in roots)
    # Only the temp directories were added -- never a drive root.
    assert set(roots) == set(_engines.temp_writable_roots())
    assert all(Path(root) != Path(Path(root).anchor) for root in roots)


def test_temp_roots_include_distinct_temp_and_tmp_env(tmp_path, monkeypatch):
    other = tmp_path / "other-temp"
    other.mkdir()
    monkeypatch.setenv("TMP", str(other))
    roots = _engines.temp_writable_roots()
    assert str(other.resolve()) in roots
    assert len({os.path.normcase(r) for r in roots}) == len(roots)


def test_codex_sandbox_has_no_network_setting():
    """There is no network switch in the Codex policy the bridge sends: the
    sandbox keeps Codex's default (no network), so writes to GitHub must escalate."""
    from lazybridge.engines.coding import CodexPolicy

    assert "network" not in " ".join(CodexPolicy.__dataclass_fields__)
