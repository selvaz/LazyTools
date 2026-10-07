"""Unit tests for ``lazytools.code_bridge`` — fake engines, no real Codex/Claude calls.

Covers: job lifecycle, lock refusal + stale reclaim, an approval ticket
filed -> approved -> job continues, filed -> rejected -> job fails, TTL
expiry -> job fails, session persistence/reuse, and cwd-outside-root
refusal.
"""

from __future__ import annotations

import threading
import time

import pytest

import _code_bridge_fakes as fakes
from lazytools.code_bridge import _jobs, _store
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


def test_lock_is_keyed_on_the_git_repo_root_not_the_literal_cwd(tmp_path, monkeypatch):
    """Two cwds inside the SAME repo (root + a subdirectory) must share one lock."""
    _env(monkeypatch, tmp_path)
    repo_root = tmp_path / "repo"
    (repo_root / "sub" / "dir").mkdir(parents=True)
    (repo_root / ".git").mkdir()

    script = fakes.install(monkeypatch)
    script.request_approval = True  # keep the job parked on a ticket so it's still "running"
    db_path = tmp_path / "store.sqlite"

    thread, _box = _run_in_thread(
        engine_name="codex",
        cwd=str(repo_root / "sub" / "dir"),
        task="x",
        root=str(tmp_path),
        db_path=db_path,
    )
    try:
        store = _store.build_store(db_path)
        queue = _store.build_approval_queue(store)
        _wait_for_one_ticket(queue)  # job is live and holding the repo-root lock

        with pytest.raises(LockHeld):
            _jobs.run_job(
                engine_name="codex", cwd=str(repo_root), task="y", root=str(tmp_path), db_path=db_path
            )

        assert queue.reject_ticket(
            queue.list_pending_tickets()[0].approval_id, actor="test", channel="test", reason="done"
        )
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


def _wait_for_one_ticket(queue, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        tickets = queue.list_pending_tickets()
        if tickets:
            return tickets[0]
        time.sleep(0.02)
    raise AssertionError("no approval ticket appeared in time")


def test_approval_ticket_approved_lets_the_job_continue(tmp_path, repo, monkeypatch):
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_TICKET_TTL", "30")
    script = fakes.install(monkeypatch)
    script.request_approval = True
    script.text = "continued after approval"
    db_path = tmp_path / "store.sqlite"

    thread, box = _run_in_thread(
        engine_name="codex", cwd=str(repo), task="do a risky thing", root=str(tmp_path), db_path=db_path
    )
    try:
        store = _store.build_store(db_path)
        queue = _store.build_approval_queue(store)
        ticket = _wait_for_one_ticket(queue)
        assert ticket.task_id  # scoped to the job
        assert queue.approve_ticket(ticket.approval_id, actor="test", channel="test")
        thread.join(timeout=10)
    finally:
        assert not thread.is_alive()

    assert "exception" not in box
    result = box["result"]
    assert result.status == "done"
    assert result.text == "continued after approval"


def test_approval_ticket_rejected_fails_the_job(tmp_path, repo, monkeypatch):
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_TICKET_TTL", "30")
    script = fakes.install(monkeypatch)
    script.request_approval = True
    db_path = tmp_path / "store.sqlite"

    thread, box = _run_in_thread(
        engine_name="codex", cwd=str(repo), task="do a risky thing", root=str(tmp_path), db_path=db_path
    )
    try:
        store = _store.build_store(db_path)
        queue = _store.build_approval_queue(store)
        ticket = _wait_for_one_ticket(queue)
        assert queue.reject_ticket(ticket.approval_id, actor="test", channel="test", reason="no")
        thread.join(timeout=10)
    finally:
        assert not thread.is_alive()

    result = box["result"]
    assert result.status == "failed"
    assert "denied" in result.error


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
