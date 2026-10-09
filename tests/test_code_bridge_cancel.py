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
    monkeypatch.setattr(cli, "_kill_process_tree", lambda pid: pytest.fail("killed terminal job"))
    monkeypatch.setattr(_lockfile, "_pid_alive", lambda pid: pytest.fail("checked terminal pid"))
    assert cli.main(["cancel", "cancel-this", "--db", str(db)]) == 0
    assert f"already {status}" in capsys.readouterr().out
    assert _jobs.find_job(store, registry, "cancel-this-job") == before


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

    def kill(candidate):
        killed.append(candidate)
        alive.remove(candidate)

    monkeypatch.setattr(cli, "_kill_process_tree", kill)
    assert cli.main(["cancel", "cancel-this", "--reason", "stop now", "--db", str(db)]) == 0
    assert killed == [pid]
    row = _jobs.find_job(store, registry, "cancel-this-job")
    assert row["status"] == "interrupted"
    assert row["error"] == "cancelled by operator: stop now\nworkspace: not a git repo"
    assert row["workspace_at_end"] == {"head": None, "new_commits": None, "uncommitted": None}
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
    monkeypatch.setattr(cli, "_kill_process_tree", lambda pid: pytest.fail("killed unrelated pid"))
    assert cli.main(["cancel", "cancel-this", "--db", str(db)]) == 1
    assert "refusing to kill" in capsys.readouterr().err
    assert _jobs.find_job(store, registry, "cancel-this-job")["status"] == "running"


def test_cancel_dead_pid_still_marks_interrupted(tmp_path, monkeypatch):
    db, _, store, registry = job_record(tmp_path)
    monkeypatch.setattr(_lockfile, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(cli, "_kill_process_tree", lambda pid: pytest.fail("killed dead pid"))
    assert cli.main(["cancel", "cancel-this", "--db", str(db)]) == 0
    assert _jobs.find_job(store, registry, "cancel-this-job")["status"] == "interrupted"


def test_cancel_kill_failure_preserves_running_record(tmp_path, monkeypatch):
    db, _, store, registry = job_record(tmp_path)
    monkeypatch.setattr(_lockfile, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(cli, "_process_matches_bridge", lambda pid: True)

    def fail_kill(pid):
        raise OSError("access denied")

    monkeypatch.setattr(cli, "_kill_process_tree", fail_kill)
    assert cli.main(["cancel", "cancel-this", "--db", str(db)]) == 1
    assert _jobs.find_job(store, registry, "cancel-this-job")["status"] == "running"


def test_cancel_missing_or_ambiguous_job_cannot_kill(tmp_path, monkeypatch):
    db, _, _, registry = job_record(tmp_path)
    registry.write("cancel-that-job", "x", tool_name="codex", status="running")
    monkeypatch.setattr(cli, "_kill_process_tree", lambda pid: pytest.fail("killed"))
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
    monkeypatch.setattr(cli, "os", SimpleNamespace(name="nt"))
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: calls.append((argv, kwargs)))
    cli._kill_process_tree(4242)
    assert calls[0][0] == ["taskkill", "/PID", "4242", "/T", "/F"]
    assert calls[0][1]["timeout"] == 30 and calls[0][1]["check"] is True


@pytest.mark.parametrize("group", [4242, 123])
def test_posix_killer_targets_private_group_or_pid_fallback(monkeypatch, group):
    calls = []
    monkeypatch.setattr(cli, "signal", SimpleNamespace(SIGKILL=9))
    monkeypatch.setattr(
        cli,
        "os",
        SimpleNamespace(
            name="posix",
            getpgid=lambda pid: group,
            killpg=lambda pgid, sig: calls.append(("group", pgid, sig)),
            kill=lambda pid, sig: calls.append(("pid", pid, sig)),
        ),
    )
    cli._kill_process_tree(4242)
    assert calls == [("group" if group == 4242 else "pid", 4242, 9)]


@pytest.mark.parametrize(
    "already_private,setsid_fails,expected",
    [
        (True, False, []),
        (False, False, ["setsid"]),
        (False, True, ["setsid", (0, 0)]),
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
            getpgrp=lambda: 4242 if already_private else 123,
            setsid=setsid,
            setpgid=lambda pid, group: calls.append((pid, group)),
        ),
    )
    cli._isolate_process_group()
    assert calls == expected
