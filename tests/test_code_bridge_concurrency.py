"""Per-engine machine-wide concurrency limit, and the per-job temp directory."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

import _code_bridge_fakes as fakes
from lazytools.code_bridge import _concurrency, _jobs, _lockfile, _store, _tempdir, cli
from lazytools.code_bridge._concurrency import EngineBusy, EngineSlot

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LAZYBRIDGE_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_POLL_SECONDS", "0.05")
    monkeypatch.setattr(_concurrency, "WAIT_POLL_SECONDS", 0.05)
    script = fakes.install(monkeypatch)
    repos = []
    for name in ("repo-a", "repo-b"):
        path = tmp_path / "work" / name
        (path / ".git").mkdir(parents=True)
        repos.append(path)
    return script, repos, tmp_path / "work" / "store.sqlite"


def configure(**limits: int) -> None:
    """Append limit keys to this test's config (conftest leaves it inside [concurrency])."""
    with open(os.environ[_concurrency.CONFIG_ENV], "a", encoding="utf-8") as fh:
        for engine, value in limits.items():
            fh.write(f"{engine} = {value}\n")


def slot_dir() -> Path:
    return _concurrency.load_config().slot_dir


def hold(engine: str, job_id: str, limit: int | None = 1, db: Path | None = None) -> EngineSlot:
    """A slot held by THIS (live) process, standing in for another running job."""
    slot = EngineSlot(engine, limit, slot_dir(), db_path=db)
    slot.try_acquire(job_id)
    return slot


def write_stale_slot(engine: str, job_id: str, db: Path | None) -> Path:
    directory = slot_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{engine}-{job_id}.slot"
    payload = {"pid": DEAD_PID, "job_id": job_id, "engine": engine, "db_path": str(db) if db else None}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


DEAD_PID = 1_999_999_999


@pytest.fixture
def dead_pid(monkeypatch):
    real_alive = _lockfile._pid_alive
    monkeypatch.setattr(_lockfile, "_pid_alive", lambda pid: pid != DEAD_PID and real_alive(pid))


def run(repo: Path, db: Path, engine: str = "codex", **kwargs):
    return _jobs.run_job(engine_name=engine, cwd=str(repo), task="t", root=str(repo.parent), db_path=db, **kwargs)


# --------------------------------------------------------------------------- #
# run refuses / queues
# --------------------------------------------------------------------------- #


def test_second_codex_run_is_refused_naming_the_running_job(env):
    _, (_, repo_b), db = env
    holder = hold("codex", "job-already-running")
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
    holder = hold("codex", "codex-job")
    other = hold("claude", "claude-job", limit=None)
    try:
        assert run(repo_a, db, engine="claude").status == "done"
        assert _concurrency.engine_at_limit("claude") == []
    finally:
        holder.release()
        other.release()


def test_wait_for_slot_queues_until_the_running_job_ends(env):
    _, (_, repo_b), db = env
    holder = hold("codex", "job-ahead")
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


def test_slot_is_released_after_a_failed_job(env):
    script, (repo_a, repo_b), db = env
    script.mode = "error"
    assert run(repo_a, db).status == "failed"
    script.mode = "ok"
    assert run(repo_b, db).status == "done"
    assert list(slot_dir().glob("codex-*.slot")) == []


# --------------------------------------------------------------------------- #
# P2-5: simultaneous stale reclaim
# --------------------------------------------------------------------------- #


def test_stale_slot_is_reclaimed_and_its_job_marked_interrupted(env, dead_pid):
    _, (_, repo_b), db = env
    store = _store.build_store(db)
    _store.build_job_registry(store).write("dead-job", "old", tool_name="codex", status="running")
    _store.write_meta(store, "dead-job", {"job_id": "dead-job", "engine": "codex", "cwd": str(repo_b)})
    write_stale_slot("codex", "dead-job", db.resolve())

    assert _concurrency.engine_at_limit("codex") == []
    assert run(repo_b, db).status == "done"
    assert _store.build_job_registry(_store.build_store(db)).find("dead-job")["status"] == "interrupted"


def test_stale_slot_from_another_store_is_marked_interrupted_in_that_store(env, tmp_path, dead_pid):
    _, (_, repo_b), db = env
    other_db = tmp_path / "other" / "store.sqlite"
    other = _store.build_store(other_db)
    _store.build_job_registry(other).write("foreign-dead", "old", tool_name="codex", status="running")
    _store.write_meta(other, "foreign-dead", {"job_id": "foreign-dead", "engine": "codex", "cwd": str(repo_b)})
    write_stale_slot("codex", "foreign-dead", other_db.resolve())

    assert run(repo_b, db).status == "done"
    assert _store.build_job_registry(_store.build_store(other_db)).find("foreign-dead")["status"] == "interrupted"
    assert _store.build_job_registry(_store.build_store(db)).find("foreign-dead") is None


def test_threads_racing_to_reclaim_the_same_stale_slot_exactly_one_wins(env, dead_pid):
    write_stale_slot("codex", "dead-job", None)
    contenders = 8
    barrier = threading.Barrier(contenders)
    won: list[EngineSlot] = []
    lost: list[str] = []
    lock = threading.Lock()

    def contend(i: int) -> None:
        slot = EngineSlot("codex", 1, slot_dir())
        barrier.wait()
        try:
            slot.try_acquire(f"job-{i}")
        except EngineBusy:
            with lock:
                lost.append(f"job-{i}")
        else:
            with lock:
                won.append(slot)

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(contenders)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    try:
        assert len(won) == 1 and len(lost) == contenders - 1
        (winner,) = won
        assert [meta["job_id"] for meta in winner.reclaimed] == ["dead-job"]  # handed to the winner only
        winner.discard_stale([path for path, _ in winner.stale])
        assert len(list(slot_dir().glob("codex-*.slot"))) == 1
    finally:
        for slot in won:
            slot.release()


_CONTENDER = """
import os, sys, time
from pathlib import Path
from lazytools.code_bridge._concurrency import EngineBusy, EngineSlot
go, slot_dir, name = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
while not go.exists():
    time.sleep(0.005)
slot = EngineSlot("codex", 1, slot_dir)
try:
    slot.try_acquire(name)
except EngineBusy:
    print("lost", flush=True)
    sys.exit(0)
print("won", flush=True)
time.sleep(2.0)  # hold it, live, while the others try
slot.release()
"""


def test_processes_racing_for_one_slot_exactly_one_wins(env, tmp_path):
    go = tmp_path / "go"
    env_vars = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), os.environ.get("PYTHONPATH", "")])}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _CONTENDER, str(go), str(slot_dir()), f"proc-{i}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env_vars,
        )
        for i in range(4)
    ]
    time.sleep(1.0)  # let every interpreter start and reach the spin on `go`
    go.write_text("go", encoding="utf-8")
    outcomes = []
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err
        outcomes.append(out.strip())
    assert sorted(outcomes) == ["lost", "lost", "lost", "won"]


# --------------------------------------------------------------------------- #
# P2-6: lowering the limit / jobs started while unlimited
# --------------------------------------------------------------------------- #


def test_lowering_the_limit_counts_every_live_slot(env):
    _, (_, repo_b), db = env
    configure(codex=2)
    first = hold("codex", "job-in-slot-0", limit=2)
    second = hold("codex", "job-in-slot-1", limit=2)
    first.release()  # only the job in the "second" slot remains
    # Lower the limit to 1 (rewrite: TOML forbids a duplicate key).
    path = Path(os.environ[_concurrency.CONFIG_ENV])
    path.write_text(f"[concurrency]\nslot_dir = '{slot_dir()}'\ncodex = 1\n", encoding="utf-8")
    try:
        with pytest.raises(EngineBusy) as exc_info:
            run(repo_b, db)
        assert "job-in-slot-1" in str(exc_info.value)
    finally:
        second.release()


def test_a_job_started_while_unlimited_counts_once_a_limit_is_set(env):
    _, (_, repo_b), db = env
    path = Path(os.environ[_concurrency.CONFIG_ENV])
    original = path.read_text(encoding="utf-8")
    path.write_text(original + "codex = 0\n", encoding="utf-8")
    unlimited_job = hold("codex", "started-unlimited", limit=None)
    path.write_text(original + "codex = 1\n", encoding="utf-8")
    try:
        with pytest.raises(EngineBusy) as exc_info:
            run(repo_b, db)
        assert "started-unlimited" in str(exc_info.value)
    finally:
        unlimited_job.release()


def test_refused_launch_keeps_a_dead_jobs_slot_until_its_bookkeeping_runs(env, dead_pid):
    """Round-2 review: limit 2 with two jobs; one dies; the limit drops to 1; a
    launch WITHOUT --wait-for-slot is refused. The refusal must not throw away
    the dead job's slot (its only death record) without marking it interrupted."""
    _, (_, repo_b), db = env
    configure(codex=2)
    live = hold("codex", "live-job", limit=2, db=db.resolve())
    # The second job "was killed": its slot remains, owned by a dead pid, and
    # its record still says awaiting_approval with an open ticket.
    store = _store.build_store(db)
    _store.build_job_registry(store).write("killed-job", "old", tool_name="codex", status="awaiting_approval")
    _store.write_meta(store, "killed-job", {"job_id": "killed-job", "engine": "codex", "cwd": str(repo_b)})
    queue = _store.build_approval_queue(store)
    ticket = queue.create_ticket(task_id="killed-job", prompt="[TieredGate] agent asks to run command 'codex-shell'")
    stale_path = write_stale_slot("codex", "killed-job", db.resolve())
    path = Path(os.environ[_concurrency.CONFIG_ENV])
    path.write_text(f"[concurrency]\nslot_dir = '{slot_dir()}'\ncodex = 1\n", encoding="utf-8")
    try:
        with pytest.raises(EngineBusy) as exc_info:
            run(repo_b, db)
        assert "live-job" in str(exc_info.value)
        # Refused: the dead job's slot file -- and so its death record -- is still there.
        assert stale_path.exists()
        assert json.loads(stale_path.read_text(encoding="utf-8"))["job_id"] == "killed-job"
    finally:
        live.release()
    # The next successful launch does the bookkeeping, and only then drops the file.
    assert run(repo_b, db).status == "done"
    registry = _store.build_job_registry(_store.build_store(db))
    assert registry.find("killed-job")["status"] == "interrupted"
    assert queue.get_ticket(ticket.approval_id).status != "pending"
    assert not stale_path.exists()


def _killed_job_with_ticket(db: Path, repo: Path, job_id: str = "killed-job", ttl=None):
    from datetime import timedelta

    store = _store.build_store(db)
    _store.build_job_registry(store).write(job_id, "old", tool_name="codex", status="awaiting_approval")
    _store.write_meta(store, job_id, {"job_id": job_id, "engine": "codex", "cwd": str(repo)})
    queue = _store.build_approval_queue(store)
    ticket = queue.create_ticket(
        task_id=job_id, prompt="[TieredGate] agent asks to run command 'codex-shell'", ttl=ttl or timedelta(hours=1)
    )
    return queue, ticket, write_stale_slot("codex", job_id, db.resolve())


def test_ticket_rejection_failing_once_is_retried_before_the_slot_is_removed(env, dead_pid, monkeypatch):
    """Round-3 review (P2): interrupt_job persists `interrupted` BEFORE rejecting
    tickets. A one-time reject failure must not let the next attempt see an
    `interrupted` job, call the bookkeeping complete and drop the slot while
    the ticket is still pending."""
    from lazybridge.ext.approval import ApprovalQueue

    _, (repo_a, repo_b), db = env
    queue, ticket, stale_path = _killed_job_with_ticket(db, repo_b)

    real_reject = ApprovalQueue.reject_ticket
    calls = {"n": 0}

    def flaky_reject(self, approval_id, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("store busy, once")
        return real_reject(self, approval_id, **kwargs)

    monkeypatch.setattr(ApprovalQueue, "reject_ticket", flaky_reject)
    real_discard = EngineSlot.discard_stale
    seen_at_discard: list = []

    def recording_discard(self, paths):
        if stale_path in paths:
            seen_at_discard.append(queue.get_ticket(ticket.approval_id).status)
        return real_discard(self, paths)

    monkeypatch.setattr(EngineSlot, "discard_stale", recording_discard)

    # First launch: status persisted, rejection fails -> NOT complete: slot kept.
    assert run(repo_a, db).status == "done"
    registry = _store.build_job_registry(_store.build_store(db))
    assert registry.find("killed-job")["status"] == "interrupted"
    assert queue.get_ticket(ticket.approval_id).status == "pending"
    assert stale_path.exists() and seen_at_discard == []

    # Second launch: the job is ALREADY interrupted, the rejection is retried,
    # and only after it succeeded is the slot file removed.
    assert run(repo_a, db).status == "done"
    assert queue.get_ticket(ticket.approval_id).status == "rejected"
    assert seen_at_discard == ["rejected"]
    assert not stale_path.exists()


def test_an_expired_but_pending_ticket_does_not_pin_the_slot_forever(env, dead_pid, monkeypatch):
    """reject_ticket refuses a ticket past its TTL (still "pending" on disk);
    bookkeeping expires it instead, so the slot can be released."""
    from datetime import timedelta

    _, (repo_a, repo_b), db = env
    queue, ticket, stale_path = _killed_job_with_ticket(db, repo_b, ttl=timedelta(milliseconds=50))
    time.sleep(0.2)
    assert queue.get_ticket(ticket.approval_id).status == "pending"  # expired by time only
    assert run(repo_a, db).status == "done"
    assert queue.get_ticket(ticket.approval_id).status == "expired"
    assert not stale_path.exists()


def test_stale_slot_is_kept_when_its_bookkeeping_fails(env, dead_pid, monkeypatch):
    _, (_, repo_b), db = env
    stale_path = write_stale_slot("codex", "unlucky-job", db.resolve())

    def failing(*args, **kwargs):
        raise OSError("store locked")

    monkeypatch.setattr(_jobs, "_mark_interrupted", failing)
    assert run(repo_b, db).status == "done"  # never fails THIS job
    assert stale_path.exists()  # kept for a later launch to retry


# --------------------------------------------------------------------------- #
# P2-7: one machine-wide slot directory, whatever --db says
# --------------------------------------------------------------------------- #


def test_two_different_db_directories_share_the_limit(env, tmp_path):
    _, (repo_a, repo_b), _ = env
    db_one = tmp_path / "store-one" / "bridge.sqlite"
    db_two = tmp_path / "store-two" / "bridge.sqlite"
    started = threading.Event()
    release = threading.Event()
    script = env[0]
    script.request_approval = True  # park the first job on a ticket so it stays live

    box: dict = {}
    thread = threading.Thread(target=lambda: box.setdefault("r", run(repo_a, db_one)), daemon=True)
    thread.start()
    try:
        queue = _store.build_approval_queue(_store.build_store(db_one))
        deadline = time.monotonic() + 10
        while not queue.list_pending_tickets():
            assert time.monotonic() < deadline and thread.is_alive()
            time.sleep(0.02)
        started.set()
        with pytest.raises(EngineBusy):
            run(repo_b, db_two)
    finally:
        ticket = queue.list_pending_tickets()[0]
        queue.reject_ticket(ticket.approval_id, actor="test", channel="test", reason="done")
        release.set()
        thread.join(10)
    assert started.is_set() and not thread.is_alive()


def test_slot_dir_does_not_depend_on_the_db(monkeypatch, tmp_path):
    monkeypatch.setenv(_concurrency.CONFIG_ENV, str(tmp_path / "missing.toml"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    expected = _concurrency.default_slot_dir()
    assert _concurrency.load_config().slot_dir == expected
    assert expected.name == "engine-slots"
    assert "sqlite" not in str(expected)


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #


def test_limits_default_and_config(tmp_path):
    missing = tmp_path / "none.toml"
    assert _concurrency.load_limits(missing) == {"codex": 1, "claude": None}
    path = tmp_path / "c.toml"
    path.write_text(f"[concurrency]\ncodex = 0\nclaude = 2\nslot_dir = '{tmp_path / 's'}'\n", encoding="utf-8")
    config = _concurrency.load_config(path)
    assert config.limits == {"codex": None, "claude": 2}
    assert config.slot_dir == tmp_path / "s"


@pytest.mark.parametrize(
    "body",
    [
        "[concurrency]\ncodex = -1\n",
        "[concurrency]\ncodex = 'x'\n",
        "[concurrency]\ngemini = 1\n",
        "[concurrency]\ncodex = true\n",
        "[concurrency]\nslot_dir = 3\n",
        "[concurrency]\nslot_dir = ''\n",
        "not toml [",
    ],
)
def test_bad_limits_are_rejected(tmp_path, body):
    path = tmp_path / "c.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError):
        _concurrency.load_config(path)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _run_args(repo: Path, db: Path, *extra: str) -> list[str]:
    return ["run", "--engine", "codex", "--cwd", str(repo), "--root", str(repo.parent), "--task", "t",
            "--db", str(db), *extra]  # fmt: skip


def test_cli_run_refuses_with_a_clear_message(env, capsys):
    _, (_, repo_b), db = env
    holder = hold("codex", "busy-job-id")
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
    holder = hold("codex", "busy-job-id")
    try:
        assert cli.main(_run_args(repo_b, db, "--detach")) == 2
        assert "busy-job-id" in capsys.readouterr().err and spawned == []
        assert cli.main(_run_args(repo_b, db, "--detach", "--wait-for-slot")) == 0
    finally:
        holder.release()
    assert "--wait-for-slot" in spawned[0]


def test_cli_bad_config_is_a_clean_error(env, monkeypatch, capsys):
    _, (_, repo_b), db = env
    configure(codex=-3)
    assert cli.main(_run_args(repo_b, db)) == 2
    assert "codex must be an integer" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# P1-3: a dedicated per-job temp directory, fail closed on an unsafe base
# --------------------------------------------------------------------------- #


@pytest.fixture
def os_temp(tmp_path, monkeypatch):
    """A safe OS temp base, sibling of the repos (never their ancestor)."""
    base = tmp_path / "ostemp" / "Temp"
    base.mkdir(parents=True)
    set_temp(monkeypatch, base)
    return base


def set_temp(monkeypatch, value, *, names=("TMPDIR", "TEMP", "TMP")) -> None:
    for name in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.delenv(name, raising=False)
    for name in names:
        monkeypatch.setenv(name, str(value))
    monkeypatch.setattr(tempfile, "tempdir", str(value))


def test_normal_temp_dir_adds_only_the_per_job_subdir(env, os_temp):
    script, (repo_a, _), db = env
    result = run(repo_a, db)
    job_dir = os_temp.resolve() / "lazytools-bridge" / result.job_id
    codex_policy = script.kwargs["config"].codex
    assert codex_policy.writable_roots == (str(job_dir),)
    assert codex_policy.sandbox == "workspace-write" and codex_policy.approval_policy == "on-request"
    meta = _store.read_meta(_store.build_store(db), result.job_id)
    assert meta["temp_dir"] == str(job_dir)
    assert not job_dir.exists()  # removed at job end


def test_engine_subprocess_env_carries_the_job_temp_dir(env, os_temp, monkeypatch):
    _, (repo_a, _), db = env
    seen: dict = {}
    before = {name: os.environ.get(name) for name in ("TEMP", "TMP", "TMPDIR")}

    async def recording_agent(engine, task, agent_name):
        from lazybridge import Envelope

        seen["env"] = {name: os.environ.get(name) for name in ("TEMP", "TMP", "TMPDIR")}
        seen["exists"] = Path(os.environ["TEMP"]).is_dir()
        # A real child process inherits it -- which is how the engine subprocess sees it.
        child = subprocess.run(
            [sys.executable, "-c", "import os, tempfile; print(os.environ['TMP']); print(tempfile.gettempdir())"],
            capture_output=True,
            text=True,
            check=True,
        )
        seen["child"] = child.stdout.split()
        return Envelope(task=task, payload="ok")

    monkeypatch.setattr(_jobs, "_run_agent", recording_agent)
    result = run(repo_a, db)
    job_dir = str(os_temp.resolve() / "lazytools-bridge" / result.job_id)
    assert seen["env"] == {"TEMP": job_dir, "TMP": job_dir, "TMPDIR": job_dir}
    assert seen["exists"]
    assert [os.path.normcase(p) for p in seen["child"]] == [os.path.normcase(job_dir)] * 2
    assert {name: os.environ.get(name) for name in ("TEMP", "TMP", "TMPDIR")} == before  # restored


@pytest.mark.parametrize("failure", [RuntimeError("engine blew up"), KeyboardInterrupt(), "cancelled"])
def test_temp_env_is_restored_when_the_engine_raises_or_is_cancelled(env, os_temp, monkeypatch, failure):
    import asyncio

    _, (repo_a, _), db = env
    before = {name: os.environ.get(name) for name in ("TEMP", "TMP", "TMPDIR")}
    inside: dict = {}

    async def failing_agent(engine, task, agent_name):
        inside["TEMP"] = os.environ["TEMP"]
        if failure == "cancelled":
            raise asyncio.CancelledError()
        raise failure

    monkeypatch.setattr(_jobs, "_run_agent", failing_agent)
    if isinstance(failure, RuntimeError):
        assert run(repo_a, db).status == "failed"
    else:
        with pytest.raises((KeyboardInterrupt, asyncio.CancelledError)):
            run(repo_a, db)
    assert "lazytools-bridge" in inside["TEMP"]
    assert {name: os.environ.get(name) for name in ("TEMP", "TMP", "TMPDIR")} == before
    assert not Path(inside["TEMP"]).exists()  # removed on the failure path too


def _short_path(path: Path) -> str:
    import ctypes

    buffer = ctypes.create_unicode_buffer(1024)
    length = ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer))  # type: ignore[attr-defined]
    return buffer.value if length else str(path)


@pytest.mark.skipif(sys.platform != "win32", reason="8.3 short names and case folding are Windows behaviour")
def test_temp_base_validation_sees_through_short_names_and_case(tmp_path, monkeypatch):
    long_dir = tmp_path / "Long Directory Name" / "Projects"
    cwd = long_dir / "repo"
    cwd.mkdir(parents=True)
    db = tmp_path / "db-home" / "store.sqlite"
    short = _short_path(tmp_path / "Long Directory Name")
    if "~" not in short:
        pytest.skip("8.3 short names are disabled on this volume")
    # A short-name spelling of the cwd's ancestor is still its ancestor.
    reason = _tempdir.unsafe_reason(Path(short), cwd=cwd, db_path=db)
    assert reason is not None and "ancestor of the job cwd" in reason
    # So is a differently-cased spelling.
    upper = Path(str(tmp_path / "Long Directory Name").upper())
    reason = _tempdir.unsafe_reason(upper, cwd=cwd, db_path=db)
    assert reason is not None and "ancestor of the job cwd" in reason
    # And a forbidden system path given in another case.
    windows = tmp_path / "fakewin" / "Windows"
    (windows / "Temp").mkdir(parents=True)
    monkeypatch.setenv("SystemRoot", str(windows))
    reason = _tempdir.unsafe_reason(Path(str(windows / "Temp").lower()), cwd=cwd, db_path=db)
    assert reason is not None and "SystemRoot" in reason


def test_claude_gets_the_job_temp_dir_as_a_file_root_and_in_its_instructions(env, os_temp):
    script, (repo_a, _), db = env
    result = run(repo_a, db, engine="claude")
    job_dir = str(os_temp.resolve() / "lazytools-bridge" / result.job_id)
    assert script.kwargs["file_roots"] == [str(repo_a.resolve()), job_dir]
    assert job_dir in script.kwargs["system"]
    assert "Write and Edit tools" in script.kwargs["system"]


def test_temp_set_to_a_drive_root_adds_no_root(env, tmp_path, monkeypatch):
    script, (repo_a, _), db = env
    set_temp(monkeypatch, Path(tmp_path.anchor), names=("TEMP",))
    result = run(repo_a, db)
    assert result.status == "done"
    assert script.kwargs["config"].codex.writable_roots is None
    meta = _store.read_meta(_store.build_store(db), result.job_id)
    assert meta["temp_dir"] is None and "root" in meta["temp_dir_refused"]


def test_tmp_set_to_the_repos_parent_is_rejected(env, os_temp, monkeypatch):
    script, (repo_a, _), db = env
    monkeypatch.setenv("TMP", str(repo_a.parent))  # TEMP/TMPDIR stay safe: any unsafe value refuses
    result = run(repo_a, db)
    assert script.kwargs["config"].codex.writable_roots is None
    meta = _store.read_meta(_store.build_store(db), result.job_id)
    assert "ancestor of the job cwd" in meta["temp_dir_refused"]
    assert not (os_temp / "lazytools-bridge").exists()


def test_cleanup_failure_does_not_fail_the_job(env, os_temp, monkeypatch):
    _, (repo_a, _), db = env

    def boom(*args, **kwargs):
        raise OSError("locked by antivirus")

    monkeypatch.setattr(_tempdir.shutil, "rmtree", boom)
    assert run(repo_a, db).status == "done"


@pytest.mark.parametrize("label", ["drive_root", "short", "windows", "programdata", "home", "cwd_ancestor", "db_ancestor"])
def test_unsafe_reason_cases(label, tmp_path, monkeypatch):
    cwd = tmp_path / "a" / "b" / "repo"
    db = tmp_path / "x" / "y" / "store.sqlite"
    windows = tmp_path / "fakewin" / "Windows"
    programdata = tmp_path / "fakewin" / "ProgramData"
    monkeypatch.setenv("SystemRoot", str(windows))
    monkeypatch.setenv("WINDIR", str(windows))
    monkeypatch.setenv("ProgramData", str(programdata))
    monkeypatch.setenv("ALLUSERSPROFILE", str(programdata))
    base = {
        "drive_root": Path(tmp_path.anchor),
        "short": Path(tmp_path.anchor) / "Temp",
        "windows": windows / "Temp",
        "programdata": programdata,
        "home": Path.home().resolve(),
        "cwd_ancestor": tmp_path / "a",
        "db_ancestor": tmp_path / "x" / "y",
    }[label]
    assert _tempdir.unsafe_reason(base, cwd=cwd, db_path=db) is not None
    assert _tempdir.unsafe_reason(tmp_path / "ok" / "Temp", cwd=cwd, db_path=db) is None


@pytest.mark.skipif(sys.platform != "win32", reason="UNC paths are a Windows concept")
def test_unc_share_root_is_unsafe(tmp_path):
    share = Path("\\\\server\\share\\")
    assert _tempdir.unsafe_reason(share, cwd=tmp_path / "r", db_path=tmp_path / "d") is not None


def test_codex_sandbox_has_no_network_setting():
    """There is no network switch in the Codex policy the bridge sends: the
    sandbox keeps Codex's default (no network), so writes to GitHub must escalate."""
    from lazybridge.engines.coding import CodexPolicy

    assert "network" not in " ".join(CodexPolicy.__dataclass_fields__)
