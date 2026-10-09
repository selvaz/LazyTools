"""Timeout forwarding and best-effort workspace evidence with fake coding engines."""

import subprocess

import pytest

import _code_bridge_fakes as fakes
from lazytools.code_bridge import _engines, _jobs, _store, cli


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("LAZYBRIDGE_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init")
    git(path, "config", "user.name", "Bridge Test")
    git(path, "config", "user.email", "bridge@example.invalid")
    (path / "tracked.txt").write_text("initial", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-m", "initial")
    return path


@pytest.mark.parametrize("engine", ["codex", "claude"])
def test_timeout_cli_to_builder_and_status_meta(tmp_path, repo, monkeypatch, capsys, engine):
    script = fakes.install(monkeypatch)
    db = tmp_path / "store.sqlite"
    assert (
        cli.main(
            [
                "run",
                "--engine",
                engine,
                "--cwd",
                str(repo),
                "--root",
                str(tmp_path),
                "--task",
                "x",
                "--timeout",
                "72.5",
                "--db",
                str(db),
            ]
        )
        == 0
    )
    job_id = capsys.readouterr().out.splitlines()[0]
    assert script.kwargs["request_timeout"] == 72.5
    assert cli.main(["status", job_id, "--json", "--db", str(db)]) == 0
    import json

    meta = json.loads(capsys.readouterr().out)
    assert meta["timeout"] == 72.5
    assert meta["head_at_start"] == git(repo, "rev-parse", "HEAD")


def test_timeout_default_and_child_argv(tmp_path, repo, monkeypatch, capsys):
    parser = cli.build_parser()
    args = parser.parse_args(["run", "--engine", "codex", "--cwd", str(repo), "--task", "x"])
    assert args.timeout == _engines.DEFAULT_TIMEOUT
    seen = []
    monkeypatch.setattr(cli, "_spawn_detached", lambda argv, log: seen.append(argv) or 4242)
    assert (
        cli.main(
            [
                "run",
                "--detach",
                "--engine",
                "codex",
                "--cwd",
                str(repo),
                "--root",
                str(tmp_path),
                "--task",
                "x",
                "--timeout",
                "99.25",
                "--db",
                str(tmp_path / "store.sqlite"),
            ]
        )
        == 0
    )
    argv = seen[0]
    assert argv[argv.index("--timeout") + 1] == "99.25"
    child = parser.parse_args(argv[3:])
    assert child.timeout == 99.25


@pytest.mark.parametrize("detach", [False, True])
@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf"])
def test_invalid_timeout_refused_in_parent(tmp_path, monkeypatch, capsys, detach, timeout):
    monkeypatch.setattr(cli, "_spawn_detached", lambda *a: pytest.fail("spawned"))
    monkeypatch.setattr(_jobs, "run_job", lambda **kw: pytest.fail("ran job"))
    args = ["run", "--engine", "codex", "--cwd", str(tmp_path), "--task", "x", "--timeout", timeout]
    assert cli.main(args + (["--detach"] if detach else [])) == 2
    assert "--timeout" in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["error", "raise", "interrupt", "write_failure"])
def test_unfinished_job_has_unchanged_head_dirty_evidence(tmp_path, repo, monkeypatch, mode):
    script = fakes.install(monkeypatch)
    script.mode = "raise" if mode == "interrupt" else mode
    if mode == "interrupt":
        script.exception_cls = KeyboardInterrupt
    if mode == "write_failure":

        def fail_write(*args):
            raise OSError("disk full")

        monkeypatch.setattr(_jobs, "_write_result", fail_write)
    (repo / "tracked.txt").write_text("dirty", encoding="utf-8")
    (repo / "untracked.txt").write_text("new", encoding="utf-8")
    db = tmp_path / "store.sqlite"
    kwargs = dict(engine_name="codex", cwd=str(repo), task="x", root=str(tmp_path), db_path=db)
    if mode == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            _jobs.run_job(**kwargs)
    else:
        assert _jobs.run_job(**kwargs).status == "failed"
    store = _store.build_store(db)
    row = _jobs.list_jobs(store, all_jobs=True)[0]
    assert row["error"].endswith("workspace: HEAD unchanged; 2 uncommitted path(s)")
    assert row["workspace_at_end"] == {
        "head": row["head_at_start"],
        "new_commits": 0,
        "uncommitted": 2,
    }


def test_new_commits_are_captured_on_engine_failure(tmp_path, repo, monkeypatch):
    fakes.install(monkeypatch)
    start = git(repo, "rev-parse", "HEAD")
    sub = repo / "sub"
    sub.mkdir()

    async def fail_after_commit(*args):
        for i in range(2):
            (repo / "tracked.txt").write_text(str(i), encoding="utf-8")
            git(repo, "add", ".")
            git(repo, "commit", "-m", f"new {i}")
        raise RuntimeError("engine died")

    monkeypatch.setattr(_jobs, "_run_agent", fail_after_commit)
    db = tmp_path / "store.sqlite"
    result = _jobs.run_job(engine_name="codex", cwd=str(sub), task="x", root=str(tmp_path), db_path=db)
    now = git(repo, "rev-parse", "HEAD")
    assert result.error.endswith(f"workspace: HEAD {start[:7]} -> {now[:7]} (2 new commit(s)); 0 uncommitted path(s)")
    row = _jobs.list_jobs(_store.build_store(db), all_jobs=True)[0]
    assert row["head_at_start"] == start
    assert row["workspace_at_end"] == {"head": now, "new_commits": 2, "uncommitted": 0}
    assert result.error in result.result_path.read_text(encoding="utf-8")


def test_non_git_and_git_failure_never_mask_engine_error(tmp_path, monkeypatch):
    monkeypatch.setenv("LAZYBRIDGE_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    script = fakes.install(monkeypatch)
    script.mode = "error"
    cwd = tmp_path / "plain"
    cwd.mkdir()
    db = tmp_path / "store.sqlite"
    kwargs = dict(engine_name="codex", cwd=str(cwd), task="x", root=str(tmp_path), db_path=db)
    result = _jobs.run_job(**kwargs)
    assert "turn failed" in result.error
    assert result.error.endswith("workspace: not a git repo")

    def broken_git(*args):
        raise subprocess.TimeoutExpired("git", 10)

    monkeypatch.setattr(_jobs, "_git", broken_git)
    result = _jobs.run_job(**kwargs)
    assert "turn failed" in result.error
    assert "workspace: unavailable (" in result.error
    row = _jobs.find_job(_store.build_store(db), _store.build_job_registry(_store.build_store(db)), result.job_id)
    assert row["head_at_start"] is None
    assert row["workspace_at_end"] == {"head": None, "new_commits": None, "uncommitted": None}


def test_unborn_git_head(tmp_path):
    git(tmp_path, "init")
    assert _jobs._head_at_start(tmp_path) == (None, None)
    suffix, data = _jobs.workspace_evidence(tmp_path, None)
    assert suffix == "workspace: HEAD unchanged; 0 uncommitted path(s)"
    assert data == {"head": None, "new_commits": 0, "uncommitted": 0}


@pytest.mark.parametrize("mode", ["error", "raise", "interrupt", "write_failure"])
def test_failed_start_snapshot_is_not_treated_as_unborn_head(tmp_path, repo, monkeypatch, mode):
    script = fakes.install(monkeypatch)
    script.mode = "raise" if mode == "interrupt" else mode
    if mode == "interrupt":
        script.exception_cls = KeyboardInterrupt
    if mode == "write_failure":

        def fail_write(*args):
            raise OSError("disk full")

        monkeypatch.setattr(_jobs, "_write_result", fail_write)
    real_git = _jobs._git
    commands = []

    def transient_git_failure(path, *args):
        commands.append(args)
        assert args[0] != "rev-list", "counted all historical commits after start snapshot failed"
        if len(commands) == 1:
            raise subprocess.TimeoutExpired("git", 10)
        return real_git(path, *args)

    monkeypatch.setattr(_jobs, "_git", transient_git_failure)
    db = tmp_path / "store.sqlite"
    kwargs = dict(engine_name="codex", cwd=str(repo), task="x", root=str(tmp_path), db_path=db)
    if mode == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            _jobs.run_job(**kwargs)
    else:
        assert _jobs.run_job(**kwargs).status == "failed"
    store = _store.build_store(db)
    row = _jobs.list_jobs(store, all_jobs=True)[0]
    assert row["head_at_start"] is None
    reason = row["head_at_start_error"]
    assert "timed out" in reason
    suffix = f"workspace: unavailable (start snapshot failed: {reason})"
    assert row["error"].endswith(suffix)
    assert row["workspace_at_end"]["new_commits"] is None
    # Git is healthy again, but the missing baseline still prevents a commit count.
    assert _jobs.workspace_evidence(repo, None, start_error=reason)[0] == suffix
    assert _jobs._with_workspace(store, row["job_id"], "cancelled").endswith(suffix)


def test_interrupt_during_initial_snapshot_still_finishes_job(tmp_path, monkeypatch):
    def interrupted_snapshot(path):
        raise KeyboardInterrupt("snapshot interrupted")

    monkeypatch.setattr(_jobs, "_head_at_start", interrupted_snapshot)
    db = tmp_path / "store.sqlite"
    with pytest.raises(KeyboardInterrupt):
        _jobs.run_job(engine_name="codex", cwd=str(tmp_path), root=str(tmp_path), task="x", db_path=db)
    row = _jobs.list_jobs(_store.build_store(db), all_jobs=True)[0]
    assert row["status"] == "interrupted"
    assert "snapshot interrupted" in row["error"]
    assert row["error"].endswith("workspace: not a git repo")
