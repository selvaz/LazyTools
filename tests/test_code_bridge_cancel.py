"""Operator cancellation tests: fake killers, durable records and real lock reclaim."""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

import _code_bridge_fakes as fakes
from lazytools.code_bridge import _jobs, _lockfile, _store, cli


def job_record(tmp_path, status="running", pid=4242):
    db = tmp_path / "store.sqlite"
    repo = tmp_path / "repo"
    repo.mkdir()
    store = _store.build_store(db)
    registry = _store.build_job_registry(store)
    registry.write("cancel-this-job", "x", tool_name="codex", status=status)
    _store.write_meta(store, "cancel-this-job", {"cwd": str(repo), "pid": pid, "head_at_start": None})
    return db, repo, store, registry


@pytest.mark.parametrize("status", ["done", "failed", "interrupted"])
def test_cancel_terminal_is_noop(tmp_path, monkeypatch, capsys, status):
    db, _, store, registry = job_record(tmp_path, status)
    before = _jobs.find_job(store, registry, "cancel-this-job")
    monkeypatch.setattr(cli, "_kill_process_tree", lambda pid, **_: pytest.fail("killed terminal job"))
    monkeypatch.setattr(_lockfile, "_pid_alive", lambda pid: pytest.fail("checked terminal pid"))
    assert cli.main(["cancel", "cancel-this", "--db", str(db)]) == 0
    assert f"already {status}" in capsys.readouterr().out
    assert _jobs.find_job(store, registry, "cancel-this-job") == before


@pytest.mark.parametrize("status", ["done", "failed", "interrupted"])
@pytest.mark.parametrize("already_dead", [False, True])
def test_cancel_preserves_terminal_record_written_during_process_exit(
    tmp_path, monkeypatch, capsys, status, already_dead
):
    db, _, store, registry = job_record(tmp_path)
    ticket = _store.build_approval_queue(store).create_ticket(task_id="cancel-this-job", prompt="pending")
    alive = True
    terminal = {}
    killed = []

    def finish():
        nonlocal alive
        registry.write(
            "cancel-this-job",
            "x",
            tool_name="codex",
            status=status,
            result="finished work" if status == "done" else None,
            error="original failure" if status != "done" else None,
        )
        terminal.update(_jobs.find_job(store, registry, "cancel-this-job"))
        alive = False

    def pid_alive(pid):
        if already_dead and alive:
            finish()
        return alive

    def kill(pid, **_):
        killed.append(pid)
        finish()

    monkeypatch.setattr(_lockfile, "_pid_alive", pid_alive)
    monkeypatch.setattr(cli, "_process_matches_bridge", lambda pid: True)
    monkeypatch.setattr(cli, "_kill_process_tree", kill)
    monkeypatch.setattr(_jobs, "interrupt_job", lambda *a, **kw: pytest.fail("overwrote terminal record"))
    assert cli.main(["cancel", "cancel-this", "--db", str(db)]) == 0
    assert capsys.readouterr().out.strip() == f"job cancel-this-job already {status}"
    assert _jobs.find_job(store, registry, "cancel-this-job") == terminal
    assert killed == ([] if already_dead else [4242])
    assert _store.build_approval_queue(store).get_ticket(ticket.approval_id).status == "pending"


@pytest.mark.parametrize("selector", ["startup-job", "startup-"])
@pytest.mark.parametrize("child_writes_record", [False, True])
@pytest.mark.parametrize("has_meta", [False, True])
def test_cancel_detached_startup_persists_interruption(
    tmp_path, monkeypatch, capsys, selector, child_writes_record, has_meta
):
    db = tmp_path / "store.sqlite"
    store = _store.build_store(db)
    registry = _store.build_job_registry(store)
    (_store.results_dir(db) / "startup-job.pid").write_text("4242", encoding="utf-8")
    if has_meta:
        _store.write_meta(store, "startup-job", {"cwd": str(tmp_path), "engine": "codex", "head_at_start": None})
    queue = _store.build_approval_queue(store)
    ticket = queue.create_ticket(task_id="startup-job", prompt="startup ticket")
    alive = True
    checked = []
    killed = []

    def identity(pid):
        checked.append(pid)
        return True

    def kill(pid, **_):
        nonlocal alive
        killed.append(pid)
        if child_writes_record:
            registry.write("startup-job", "child task", tool_name="claude", status="running")
        alive = False

    monkeypatch.setattr(_lockfile, "_pid_alive", lambda pid: alive)
    monkeypatch.setattr(cli, "_process_matches_bridge", identity)
    monkeypatch.setattr(cli, "_kill_process_tree", kill)
    assert cli.main(["cancel", selector, "--reason", "stop startup", "--db", str(db)]) == 0
    assert checked == killed == [4242]
    row = _jobs.find_job(store, registry, "startup-job")
    assert row["status"] == "interrupted"
    assert row["job_id"] == "startup-job"
    assert row["pid"] == 4242
    assert row["error"].startswith("cancelled by operator during startup: stop startup\nworkspace: ")
    assert row["kind"] == ("claude" if child_writes_record else "codex" if has_meta else "unknown")
    assert row["objective"] == ("child task" if child_writes_record else "")
    assert queue.get_ticket(ticket.approval_id).status == "rejected"
    assert row["error"] in (_store.results_dir(db) / "startup-job.txt").read_text(encoding="utf-8")
    before = row.copy()
    assert cli.main(["cancel", selector, "--db", str(db)]) == 0
    assert _jobs.find_job(store, registry, "startup-job") == before


@pytest.mark.parametrize("owns", [True, False, None])
def test_cancel_group_kills_only_a_recorded_owned_group(tmp_path, monkeypatch, owns):
    db = tmp_path / "store.sqlite"
    store = _store.build_store(db)
    registry = _store.build_job_registry(store)
    registry.write("owned-job", "task", tool_name="codex", status="running")
    meta = {"cwd": str(tmp_path), "pid": 4242, "head_at_start": None}
    if owns is not None:
        meta["owns_process_group"] = owns
    _store.write_meta(store, "owned-job", meta)
    alive = True
    calls = []

    def kill(pid, **kwargs):
        nonlocal alive
        calls.append((pid, kwargs))
        alive = False

    monkeypatch.setattr(_lockfile, "_pid_alive", lambda pid: alive)
    monkeypatch.setattr(cli, "_process_matches_bridge", lambda pid: True)
    monkeypatch.setattr(cli, "_kill_process_tree", kill)
    assert cli.main(["cancel", "owned-job", "--db", str(db)]) == 0
    assert calls == [(4242, {"owns_group": bool(owns)})]


def test_cancel_startup_refuses_unrelated_pid_and_ambiguous_pid_files(tmp_path, monkeypatch):
    db = tmp_path / "store.sqlite"
    (_store.results_dir(db) / "startup-job.pid").write_text("4242", encoding="utf-8")
    monkeypatch.setattr(_lockfile, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(cli, "_process_matches_bridge", lambda pid: False)
    monkeypatch.setattr(cli, "_kill_process_tree", lambda pid, **_: pytest.fail("killed"))
    assert cli.main(["cancel", "startup-", "--db", str(db)]) == 1
    store = _store.build_store(db)
    assert _jobs.list_jobs(store, all_jobs=True) == []
    (_store.results_dir(db) / "startup-other.pid").write_text("4343", encoding="utf-8")
    assert cli.main(["cancel", "startup-", "--db", str(db)]) == 1
    assert _jobs.list_jobs(store, all_jobs=True) == []


@pytest.mark.parametrize("detached", [False, True])
def test_cancel_kills_rejects_tickets_and_next_run_reclaims_lock(tmp_path, monkeypatch, capsys, detached):
    db, repo, store, registry = job_record(tmp_path)
    pid = 4343 if detached else 4242
    if detached:
        (_store.results_dir(db) / "cancel-this-job.pid").write_text(str(pid), encoding="utf-8")
    queue = _store.build_approval_queue(store)
    # Also exercise the approval queue's default 100-ticket cap.
    tickets = [queue.create_ticket(task_id="cancel-this-job", prompt="approve?") for _ in range(101)]
    other = queue.create_ticket(task_id="another-job", prompt="other")
    answered = queue.create_ticket(task_id="cancel-this-job", prompt="already answered")
    queue.approve_ticket(answered.approval_id, actor="test", channel="test")
    lock = _lockfile.JobLock(_store.locks_dir(db), repo)
    lock.acquire("cancel-this-job")
    payload = json.loads(lock.path.read_text())
    payload["pid"] = pid
    lock.path.write_text(json.dumps(payload), encoding="utf-8")
    alive = {pid}
    killed = []
    monkeypatch.setattr(_lockfile, "_pid_alive", lambda candidate: candidate in alive)
    monkeypatch.setattr(cli, "_process_matches_bridge", lambda candidate: None)

    def kill(candidate, **_):
        killed.append(candidate)
        alive.remove(candidate)

    monkeypatch.setattr(cli, "_kill_process_tree", kill)
    assert cli.main(["cancel", "cancel-this", "--reason", "stop now", "--db", str(db)]) == 0
    assert killed == [pid]
    row = _jobs.find_job(store, registry, "cancel-this-job")
    assert row["status"] == "interrupted"
    assert row["error"] == "cancelled by operator: stop now\nworkspace: not a git repo"
    assert row["workspace_at_end"] == {"head": None, "commits_since_start": None, "uncommitted": None}
    assert all(queue.get_ticket(ticket.approval_id).status == "rejected" for ticket in tickets)
    assert queue.get_ticket(tickets[0].approval_id).reason.startswith("cancelled by operator: stop now")
    assert queue.get_ticket(other.approval_id).status == "pending"
    assert queue.get_ticket(answered.approval_id).status == "approved"
    assert lock.path.exists()  # the killed owner could not release it
    monkeypatch.setenv("LAZYBRIDGE_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    fakes.install(monkeypatch)
    result = _jobs.run_job(engine_name="codex", cwd=str(repo), root=str(tmp_path), task="next", db_path=db)
    assert result.status == "done"
    assert not lock.path.exists()
    assert _jobs.find_job(store, registry, "cancel-this-job")["error"] == row["error"]
    assert cli.main(["cancel", "cancel-this", "--db", str(db)]) == 0
    assert killed == [pid]


def test_cancel_refuses_reused_pid(tmp_path, monkeypatch, capsys):
    db, _, store, registry = job_record(tmp_path)
    monkeypatch.setattr(_lockfile, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(cli, "_process_matches_bridge", lambda pid: False)
    monkeypatch.setattr(cli, "_kill_process_tree", lambda pid, **_: pytest.fail("killed unrelated pid"))
    assert cli.main(["cancel", "cancel-this", "--db", str(db)]) == 1
    assert "refusing to kill" in capsys.readouterr().err
    assert _jobs.find_job(store, registry, "cancel-this-job")["status"] == "running"


def test_cancel_dead_pid_still_marks_interrupted(tmp_path, monkeypatch):
    db, _, store, registry = job_record(tmp_path)
    monkeypatch.setattr(_lockfile, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(cli, "_kill_process_tree", lambda pid, **_: pytest.fail("killed dead pid"))
    assert cli.main(["cancel", "cancel-this", "--db", str(db)]) == 0
    assert _jobs.find_job(store, registry, "cancel-this-job")["status"] == "interrupted"


def test_cancel_kill_failure_preserves_running_record(tmp_path, monkeypatch):
    db, _, store, registry = job_record(tmp_path)
    monkeypatch.setattr(_lockfile, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(cli, "_process_matches_bridge", lambda pid: True)

    def fail_kill(pid, **_):
        raise OSError("access denied")

    monkeypatch.setattr(cli, "_kill_process_tree", fail_kill)
    assert cli.main(["cancel", "cancel-this", "--db", str(db)]) == 1
    assert _jobs.find_job(store, registry, "cancel-this-job")["status"] == "running"


def test_cancel_missing_or_ambiguous_job_cannot_kill(tmp_path, monkeypatch):
    db, _, _, registry = job_record(tmp_path)
    registry.write("cancel-that-job", "x", tool_name="codex", status="running")
    monkeypatch.setattr(cli, "_kill_process_tree", lambda pid, **_: pytest.fail("killed"))
    assert cli.main(["cancel", "cancel-", "--db", str(db)]) == 1
    assert cli.main(["cancel", "missing", "--db", str(db)]) == 1


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["python.exe", "-m", "lazytools.code_bridge", "run"], True),
        (["python", "/bin/lazytools-code-bridge", "run"], True),
        (["python", "-m", "unrelated"], False),
        (["other.exe", "-m", "lazytools.code_bridge"], False),
    ],
)
def test_process_identity_check(monkeypatch, argv, expected):
    monkeypatch.setitem(
        sys.modules,
        "psutil",
        SimpleNamespace(
            Process=lambda pid: SimpleNamespace(cmdline=lambda: argv),
            Error=RuntimeError,
        ),
    )
    assert cli._process_matches_bridge(4242) is expected


def test_identity_check_without_psutil_uses_proc(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    monkeypatch.setattr(cli.Path, "read_bytes", lambda path: b"python\0-m\0lazytools.code_bridge\0run\0")
    assert cli._process_matches_bridge(4242) is True


def test_windows_killer_requests_tree(monkeypatch):
    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="win32"))
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: calls.append((argv, kwargs)))
    cli._kill_process_tree(4242)
    assert calls[0][0] == ["taskkill", "/PID", "4242", "/T", "/F"]
    assert calls[0][1]["timeout"] == 30 and calls[0][1]["check"] is True


@pytest.mark.parametrize(
    "group,owns_group,expected",
    [(4242, True, "group"), (123, True, "pid"), (4242, False, "pid")],
)
def test_posix_killer_targets_private_group_or_pid_fallback(monkeypatch, group, owns_group, expected):
    calls = []
    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(cli, "signal", SimpleNamespace(SIGKILL=9))
    monkeypatch.setattr(
        cli,
        "os",
        SimpleNamespace(
            getpgid=lambda pid: group,
            killpg=lambda pgid, sig: calls.append(("group", pgid, sig)),
            kill=lambda pid, sig: calls.append(("pid", pid, sig)),
        ),
    )
    # owns_group=False is a shell pipeline leader: pgid == pid but peers share it.
    cli._kill_process_tree(4242, owns_group=owns_group)
    assert calls == [(expected, 4242, 9)]


@pytest.mark.parametrize(
    "already_private,setsid_fails,expected",
    [
        (True, False, []),
        (False, False, ["setsid"]),
        (False, True, ["setsid"]),
    ],
)
def test_posix_run_owns_group_and_detached_child_keeps_existing_session(
    monkeypatch, already_private, setsid_fails, expected
):
    calls = []

    def setsid():
        calls.append("setsid")
        if setsid_fails:
            raise PermissionError("group leader")

    monkeypatch.setattr(
        cli,
        "os",
        SimpleNamespace(
            getpid=lambda: 4242,
            getsid=lambda pid: 4242 if already_private else 123,
            setsid=setsid,
            setpgid=lambda pid, group: calls.append((pid, group)),
        ),
    )
    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="linux"))
    cli._isolate_process_group()
    assert calls == expected
