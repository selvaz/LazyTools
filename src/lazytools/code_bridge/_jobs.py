"""Job lifecycle: run one coding-engine job to completion in THIS process.

``run_job`` is the whole synchronous contract the CLI's ``run`` subcommand
needs: resolve and lock the ``cwd``, record the job, run the engine in the
foreground (Claude Code backgrounds the whole process via its Bash tool's
``run_in_background``, so this function does not need to), and persist the
result -- to the job record and to a result file -- before returning.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from lazytools.code_bridge import _concurrency, _engines, _progress, _store, _tempdir
from lazytools.code_bridge._approvals import BridgeGate
from lazytools.code_bridge._lockfile import JobLock
from lazytools.code_bridge._policy import CODING_RULES

ENGINES: tuple[str, ...] = ("codex", "claude")
DEFAULT_MAX_CONTINUATIONS = 3
CONTINUATION_PROMPT = "continue from the last commit; re-read the brief's Done-when; do not redo committed steps"

#: Longer than lazybridge's own 2h ApprovalQueue default: a ticket filed
#: overnight should still be answerable in the morning without extra
#: configuration. Override with LAZYTOOLS_CODE_BRIDGE_TICKET_TTL (seconds).
DEFAULT_TICKET_TTL = timedelta(hours=4)
TICKET_TTL_ENV = "LAZYTOOLS_CODE_BRIDGE_TICKET_TTL"


def ticket_ttl() -> timedelta:
    raw = os.environ.get(TICKET_TTL_ENV)
    if not raw:
        return DEFAULT_TICKET_TTL
    try:
        seconds = float(raw)
    except ValueError as exc:
        raise ValueError(f"{TICKET_TTL_ENV} is not a number: {raw!r}") from exc
    if seconds <= 0:
        raise ValueError(f"{TICKET_TTL_ENV} must be positive, got {raw!r}")
    return timedelta(seconds=seconds)


#: How often ``StoreApprovalChannel.ask()`` re-reads the ticket from the
#: Store. The 2.0s library default is fine in production; tests override it
#: (LAZYTOOLS_CODE_BRIDGE_POLL_SECONDS) so an approve/reject/TTL-expiry test
#: does not have to wait seconds for the poll loop to notice.
POLL_SECONDS_ENV = "LAZYTOOLS_CODE_BRIDGE_POLL_SECONDS"
DEFAULT_POLL_SECONDS = 2.0


def poll_seconds() -> float:
    raw = os.environ.get(POLL_SECONDS_ENV)
    if not raw:
        return DEFAULT_POLL_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{POLL_SECONDS_ENV} is not a number: {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{POLL_SECONDS_ENV} must be positive, got {raw!r}")
    return value


class _StatusTrackingChannel:
    """Wraps an approval ``Channel`` so the job record reflects a pending ticket.

    ``TieredGate`` only ever sees this wrapper, never the inner
    ``StoreApprovalChannel`` directly, so every ``ask()`` -- "session" and
    "ask" tier alike -- flips the job's status to ``awaiting_approval``
    while a human has not yet answered, and back once they have (approved,
    rejected, or the ticket expired).
    """

    def __init__(self, inner: Any, on_waiting: Callable[[], None], on_resumed: Callable[[], None]) -> None:
        self._inner = inner
        self._on_waiting = on_waiting
        self._on_resumed = on_resumed
        self.name = getattr(inner, "name", "store-approval-queue")

    async def ask(self, prompt: str) -> bool:
        self._on_waiting()
        try:
            return await self._inner.ask(prompt)
        finally:
            self._on_resumed()


def new_job_id() -> str:
    return uuid.uuid4().hex


@dataclass
class RunResult:
    job_id: str
    status: str  # "done" | "partial" | "failed" | "interrupted"
    text: str
    error: str | None
    #: ``None`` only when persisting the result file itself failed (disk
    #: full, permissions, ...) -- the job record still exists and reports
    #: accurately, there is just no file to point to.
    result_path: Path | None


def _write_result(db_path: Path | None, job_id: str, text: str) -> Path:
    path = _store.results_dir(db_path) / f"{job_id}.txt"
    path.write_text(text, encoding="utf-8")
    return path


def _git_repo_root(path: Path) -> Path:
    """The git worktree/repo root containing ``path``, else ``path`` itself.

    The lock protects "one job touching this repository's working tree and
    git index at a time" -- keying it on the literal ``--cwd`` argument
    instead would let two jobs pointed at the ROOT and at a SUBDIRECTORY of
    the very same repository run concurrently and race on the same `.git`
    index, despite the CLI explicitly allowing `--cwd` to name a
    subdirectory. Found by Codex review.
    """
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return path


def _owns_process_group() -> bool:
    """True only when this process leads its own session (setsid / start_new_session),
    so its process group holds nothing but the job and its descendants. A shell
    pipeline leader also has pgid == pid but shares the group with its peers."""
    if sys.platform == "win32":
        return False
    try:
        return os.getsid(0) == os.getpid()
    except OSError:
        return False


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )


def _git_head(path: Path) -> str | None:
    result = _git(path, "rev-parse", "--verify", "--quiet", "HEAD")
    if result.returncode == 1:  # unborn branch
        return None
    result.check_returncode()
    return result.stdout.strip() or None


def _head_at_start(path: Path) -> tuple[str | None, str | None]:
    try:
        return _git_head(path), None
    except Exception as exc:
        if isinstance(exc, subprocess.CalledProcessError) and "not a git repository" in (exc.stderr or "").lower():
            return None, None
        reason = " ".join(str(exc).split()) or type(exc).__name__
        return None, reason


def workspace_evidence(path: Path, start: str | None, *, start_error: str | None = None) -> tuple[str, dict[str, Any]]:
    """Best-effort git evidence; a broken/missing git must never hide the job error."""
    data: dict[str, Any] = {"head": None, "commits_since_start": None, "uncommitted": None}
    if start_error is not None:
        return f"workspace: unavailable (start snapshot failed: {start_error})", data
    try:
        repo = _git_repo_root(path)
        probe = _git(repo, "rev-parse", "--is-inside-work-tree")
        if probe.returncode and "not a git repository" in probe.stderr.lower():
            return "workspace: not a git repo", data
        probe.check_returncode()
        head = _git_head(repo)
        data["head"] = head
        status = _git(repo, "status", "--porcelain=v1", "-uall")
        status.check_returncode()
        data["uncommitted"] = len(status.stdout.splitlines())
        if head == start:
            data["commits_since_start"] = 0
            summary = "HEAD unchanged"
        else:
            count = _git(repo, "rev-list", "--count", f"{start}..HEAD" if start else "HEAD")
            count.check_returncode()
            data["commits_since_start"] = int(count.stdout.strip())
            summary = f"HEAD {start[:7] if start else 'none'} -> {head[:7] if head else 'none'} ({data['commits_since_start']} commit(s) not reachable from start)"
        return f"workspace: {summary}; {data['uncommitted']} uncommitted path(s)", data
    except Exception as exc:
        reason = " ".join(str(exc).split()) or type(exc).__name__
        return f"workspace: unavailable ({reason})", data


def _with_workspace(
    store: Any,
    job_id: str,
    error: str,
    *,
    path: Path | None = None,
    start: str | None = None,
    start_error: str | None = None,
) -> str:
    meta = _store.read_meta(store, job_id) or {}
    cwd = path or (Path(meta["cwd"]) if meta.get("cwd") else None)
    if cwd is None:
        suffix, data = (
            "workspace: unavailable (job has no cwd)",
            {"head": None, "commits_since_start": None, "uncommitted": None},
        )
    else:
        suffix, data = workspace_evidence(
            cwd,
            start if path is not None else meta.get("head_at_start"),
            start_error=start_error if path is not None else meta.get("head_at_start_error"),
        )
    meta["workspace_at_end"] = data
    meta["updated_at"] = time.time()
    _store.write_meta(store, job_id, meta)
    return f"{error}\n{suffix}"


def interrupt_job(store: Any, registry: Any, job: dict[str, Any], error: str, *, db_path: Path | None = None) -> str:
    """Persist an interruption and close this job's still-open approval tickets."""
    job_id = str(job["job_id"])
    error = _with_workspace(store, job_id, error)
    try:
        _write_result(db_path, job_id, f"[interrupted] {error}")
    except OSError:
        pass  # the durable record still reports the interruption
    registry.write(
        job_id,
        str(job.get("objective") or ""),
        tool_name=str(job.get("kind") or "unknown"),
        status="interrupted",
        error=error,
    )
    close_pending_tickets(store, job_id, error)
    return error


def pending_ticket_ids(store: Any, job_id: str) -> list[str]:
    """Ids of this job's tickets still recorded as ``pending`` -- expired-by-time ones included.

    Reads the raw records rather than ``list_pending_tickets``: that has a
    100-ticket cap and hides a ticket whose TTL ran out, which is still
    ``"pending"`` on disk until someone transitions it.
    """
    return [
        key.removeprefix(_store.APPROVAL_PREFIX)
        for key, raw in store.items(prefix=_store.APPROVAL_PREFIX)
        if isinstance(raw, dict) and raw.get("task_id") == job_id and raw.get("status") == "pending"
    ]


def close_pending_tickets(store: Any, job_id: str, reason: str) -> None:
    """Reject this job's pending tickets; a ticket already past its TTL is marked expired.

    ``reject_ticket`` refuses a ticket whose TTL ran out (it is no longer
    actionable) yet leaves it ``"pending"`` on disk -- ``expire_ticket``
    closes those, so "no pending tickets left" is reachable. May raise if the
    queue's Store fails; callers decide whether that is fatal.
    """
    queue = _store.build_approval_queue(store)
    for approval_id in pending_ticket_ids(store, job_id):
        if not queue.reject_ticket(approval_id, actor="operator", channel="cli", reason=reason):
            queue.expire_ticket(approval_id)


async def _run_agent(engine: Any, task: str, agent_name: str) -> Any:
    from lazybridge import Agent

    return await Agent(engine, name=agent_name).run(task)


def run_job(
    *,
    engine_name: str,
    cwd: str,
    task: str,
    session_name: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    root: str | None = None,
    db_path: Path | None = None,
    on_job_id: Callable[[str], None] | None = None,
    job_id: str | None = None,
    routing: dict[str, Any] | None = None,
    timeout: float = _engines.DEFAULT_TIMEOUT,
    max_continuations: int = DEFAULT_MAX_CONTINUATIONS,
    max_wall: float | None = None,
    wait_for_slot: bool = False,
    on_slot_wait: Callable[[_concurrency.EngineBusy], None] | None = None,
) -> RunResult:
    """Run one job to completion. Raises before any job is recorded for a
    bad engine name or a ``cwd`` outside the confinement root; raises
    :class:`~lazytools.code_bridge._lockfile.LockHeld` if another live
    process already owns this ``cwd``, and
    :class:`~lazytools.code_bridge._concurrency.EngineBusy` if the engine is
    at its machine-wide concurrency limit (unless ``wait_for_slot``, which
    blocks until a slot frees up -- holding this repository's lock, so the
    queued job keeps its place). Every other failure (engine error,
    denied action, expired approval) is captured into the job record and
    returned as ``status="failed"`` rather than raised, so the CLI can
    report it and exit non-zero without a traceback.
    """
    if engine_name not in ENGINES:
        raise ValueError(f"engine must be one of {ENGINES}, got {engine_name!r}")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("--timeout must be a finite number greater than 0")
    timeout = float(timeout)
    if type(max_continuations) is not int or max_continuations < 0:
        raise ValueError("--max-continuations must be an integer >= 0")
    if max_wall is not None and (not math.isfinite(max_wall) or max_wall <= 0):
        raise ValueError("--max-wall must be a finite number greater than 0")
    wall_started = time.monotonic()
    resolved_cwd = _engines.resolve_cwd(cwd, root)

    # A detached launch (`run --detach`) picks the id in the parent so it can
    # report it before the child exists; every other caller gets a fresh one.
    # A caller-chosen id becomes a file name (<id>.txt, .log, .pid): only a
    # plain token is accepted, never a path. Found by review.
    if job_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job_id):
        raise ValueError(f"job id must be 1-64 letters, digits, '-' or '_', got {job_id!r}")
    job_id = job_id or new_job_id()
    if on_job_id is not None:
        on_job_id(job_id)

    store = _store.build_store(db_path)
    registry = _store.build_job_registry(store)
    queue = _store.build_approval_queue(store)
    # A caller-chosen id must be new: writing its records would otherwise
    # silently replace an earlier job's history.
    if _store.read_meta(store, job_id) is not None:
        raise ValueError(f"job id {job_id!r} already exists -- refusing to overwrite its record")

    lock = JobLock(_store.locks_dir(db_path), _git_repo_root(resolved_cwd))
    stale_job_id = lock.acquire(job_id)  # raises LockHeld -- job_id was reported but nothing else is recorded

    # Engine slot AFTER the repo lock: the repo lock never waits (it raises),
    # so a job queued here cannot deadlock against one holding a slot. Taken
    # before anything is recorded, like the repo lock: a refused or
    # interrupted wait leaves no job record behind.
    store_path = (db_path or _store.default_db_path()).expanduser().resolve()
    try:
        slot_config = _concurrency.load_config()
        slot = _concurrency.EngineSlot(
            engine_name, slot_config.limits.get(engine_name), slot_config.slot_dir, db_path=store_path
        )
        slot.acquire(job_id, wait=wait_for_slot, on_wait=on_slot_wait)
    except BaseException:
        lock.release()
        raise

    # Everything from here on holds the lock; a single `finally` at the
    # bottom covers the whole rest of the function, INCLUDING the initial
    # bookkeeping writes just below -- not only the engine run further down.
    # A narrower `try` starting after those writes would leave the lock
    # stuck held forever if `_mark_interrupted`/`registry.write`/
    # `write_meta` ever raised and the caller is a long-lived process that
    # catches the exception and keeps running (the CLI itself always exits
    # right after, reclaiming on pid-death, but `run_job` is a public
    # function other callers may embed). Found by Codex review.
    head_at_start = None
    head_at_start_error = None
    temp_dir: Path | None = None
    try:
        # Defined FIRST, before anything below that could itself raise:
        # the `except BaseException` handler further down calls this to
        # record the failure, and it must exist regardless of whether the
        # failure happened during the bookkeeping writes right below it or
        # during the engine run further down.
        def _set_status(status: str, *, result: str | None = None, error: str | None = None) -> None:
            meta = _store.read_meta(store, job_id) or {}
            meta["updated_at"] = time.time()
            _store.write_meta(store, job_id, meta)
            registry.write(job_id, task, tool_name=engine_name, status=status, result=result, error=error)

        def _finish(status: str, text: str, error: str | None) -> RunResult:
            if status != "done":
                error = _with_workspace(
                    store,
                    job_id,
                    error or "unknown error",
                    path=lock.cwd,
                    start=head_at_start,
                    start_error=head_at_start_error,
                )
            # The result FILE is persisted BEFORE the job record is marked
            # terminal, not after: marking "done" first and writing the file
            # second would let a disk-full/permissions failure on the write
            # leave a job record claiming success with no result file to
            # back it up (and an uncaught exception out of a function whose
            # whole contract is "never raises for an engine-side failure").
            # Found by Codex review.
            body = text if status == "done" else f"[{status}] {error}"
            history = _progress.history_text((_store.read_meta(store, job_id) or {}).get("turns", []))
            if history:
                body += f"\n\n{history}"
            try:
                result_path: Path | None = _write_result(db_path, job_id, body)
            except OSError as exc:
                write_error = f"result could not be persisted: {type(exc).__name__}: {exc}"
                write_error = _with_workspace(
                    store, job_id, write_error, path=lock.cwd, start=head_at_start, start_error=head_at_start_error
                )
                _set_status("failed", error=write_error)
                return RunResult(job_id, "failed", "", write_error, None)
            _set_status(status, result=text if status == "done" else None, error=error)
            return RunResult(job_id, status, text if status == "done" else "", error, result_path)

        head_at_start, head_at_start_error = _head_at_start(lock.cwd)
        if stale_job_id is not None:
            _mark_interrupted(store, registry, stale_job_id, db_path=db_path)
        # Stale engine slots: mark each dead job interrupted in ITS Store, and
        # only then delete the slot file -- the file is the only record that
        # the job died. A bookkeeping failure keeps the file for a later try.
        slot.discard_stale(
            [
                path
                for path, stale_meta in slot.stale
                if _mark_slot_interrupted(stale_meta, store, registry, current_db=store_path, db_path=db_path)
            ]
        )

        # The job's own temp dir: the ONLY extra writable root it gets, and
        # what TEMP/TMP/TMPDIR point at for the engine. None (with the reason
        # recorded) when the OS temp base is unsafe -- fail closed.
        temp_dir, temp_refused = _tempdir.create_job_temp_dir(job_id, cwd=resolved_cwd, db_path=store_path)

        now = time.time()
        registry.write(job_id, task, tool_name=engine_name, status="running")
        _store.write_meta(
            store,
            job_id,
            {
                "job_id": job_id,
                "engine": engine_name,
                "cwd": str(resolved_cwd),
                "session_name": session_name,
                "model": model,
                "effort": effort,
                "timeout": timeout,
                "max_continuations": max_continuations,
                "max_wall": max_wall,
                "continuations": 0,
                "turns": [],
                "head_at_start": head_at_start,
                **({"head_at_start_error": head_at_start_error} if head_at_start_error is not None else {}),
                "pid": os.getpid(),
                "owns_process_group": _owns_process_group(),
                "created_at": now,
                "updated_at": now,
                **({"routing": routing} if routing is not None else {}),
                "temp_dir": str(temp_dir) if temp_dir is not None else None,
                **({"temp_dir_refused": temp_refused} if temp_refused is not None else {}),
            },
        )
        temp_arg = str(temp_dir) if temp_dir is not None else None

        channel = _StatusTrackingChannel(
            _store_channel(queue, job_id),
            on_waiting=lambda: _set_status("awaiting_approval"),
            on_resumed=lambda: _set_status("running"),
        )
        gate = BridgeGate(channel=channel, rules=CODING_RULES)

        from lazybridge.engines.sessions import default_session_registry

        session_registry = default_session_registry()

        if engine_name == "codex":
            thread_id = session_registry.resolve("codex", resolved_cwd, session_name) if session_name else None
            engine = _engines.build_codex_engine(
                cwd=str(resolved_cwd),
                gate=gate,
                model=model,
                effort=effort,
                thread_id=thread_id,
                session_alias=session_name,
                session_registry=session_registry,
                timeout=timeout,
                temp_dir=temp_arg,
            )
            agent_name = "code-bridge-codex"
        else:
            session_id = session_registry.resolve("claude", resolved_cwd, session_name) if session_name else None
            engine = _engines.build_claude_engine(
                cwd=str(resolved_cwd),
                gate=gate,
                model=model,
                effort=effort,
                session_id=session_id,
                session_alias=session_name,
                session_registry=session_registry,
                timeout=timeout,
                temp_dir=temp_arg,
            )
            agent_name = "code-bridge-claude"

        # The engine subprocess inherits this process's environment: point its
        # TEMP/TMP/TMPDIR at the job's own temp dir for exactly this run.
        async def run_turns() -> RunResult:
            from lazybridge import Envelope

            prompt = task
            turns: list[dict[str, Any]] = []
            for turn in range(max_continuations + 1):
                before = _progress.snapshot(lock.cwd)
                remaining = None if max_wall is None else max_wall - (time.monotonic() - wall_started)
                if remaining is not None and remaining <= 0:
                    return _finish("partial" if any(t["progress"] for t in turns) else "failed", "", "max wall reached")
                _set_status("running")
                meta = _store.read_meta(store, job_id) or {}
                meta["continuations"] = turn
                _store.write_meta(store, job_id, meta)
                started = time.time()
                wall_expired = False
                try:
                    async with asyncio.timeout(min(timeout, remaining) if remaining is not None else timeout):
                        env = await _run_agent(engine, prompt, agent_name)
                except TimeoutError as exc:
                    wall_expired = remaining is not None and remaining <= timeout
                    env = Envelope.error_envelope(exc)
                evidence = _progress.changes(lock.cwd, before, _progress.snapshot(lock.cwd))
                timed_out = not env.ok and env.error is not None and (
                    env.error.type == "TimeoutError" or "Turn timed out on durable thread" in env.error.message
                )
                turns.append({
                    "turn": turn, "started_at": started, "ended_at": time.time(),
                    "outcome": "done" if env.ok else "checkpoint" if timed_out and evidence["progress"] else "failed",
                    "thread_id": getattr(engine, "thread_id", None) or getattr(engine, "session_id", None),
                    **evidence,
                })
                meta = _store.read_meta(store, job_id) or {}
                meta["turns"] = turns
                _store.write_meta(store, job_id, meta)
                if env.ok:
                    return _finish("done", env.text(), None)
                message = env.error.message if env.error else "unknown error"
                if not timed_out:
                    return _finish("failed", "", message)
                if not evidence["progress"]:
                    return _finish("failed", "", f"no progress: {message or 'turn timed out'}")
                _set_status("checkpoint")
                if wall_expired or (max_wall is not None and time.monotonic() - wall_started >= max_wall):
                    return _finish("partial", "", "max wall reached after progress")
                if turn == max_continuations:
                    return _finish("partial", "", "automatic continuations exhausted after progress")
                if not turns[-1]["thread_id"]:
                    return _finish("partial", "", "progress recorded, but no durable thread is available to resume")
                prompt = CONTINUATION_PROMPT
            raise AssertionError("unreachable")

        with _tempdir.temp_env(temp_dir):
            return asyncio.run(run_turns())
    except BaseException as exc:
        # BaseException, not Exception: Ctrl+C (KeyboardInterrupt) and a
        # cancelled asyncio task (CancelledError) both derive from
        # BaseException, not Exception -- an `except Exception` here would
        # let them unwind straight past this handler, skip the status
        # write below, and leave the job stuck at "running"/
        # "awaiting_approval" forever (the next run against the same cwd
        # reclaims the LOCK, but nothing re-examines this job's own record
        # unless the reclaim path finds a DIFFERENT job_id still holding
        # it). Found by Codex review.
        interrupted = isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError))
        status = "interrupted" if interrupted else "failed"
        error = f"{type(exc).__name__}: {exc}"
        finished = _finish(status, "", error)
        if interrupted:
            raise  # propagate the signal rather than reporting a fabricated RunResult; `finally` below still releases the lock
        return finished
    finally:
        _tempdir.remove_job_temp_dir(temp_dir)  # best effort, never raises
        slot.release()
        lock.release()


def _mark_slot_interrupted(
    stale: dict[str, Any], store: Any, registry: Any, *, current_db: Path, db_path: Path | None
) -> bool:
    """Mark a reclaimed engine slot's job interrupted in the Store IT recorded into.

    Slots are machine-wide, so the dead job may belong to a different
    ``--db``. Returns ``True`` only when the bookkeeping is COMPLETE -- the
    job is marked interrupted (or already terminal) AND it has no pending
    tickets left -- or there is nothing to do (no job id, or its Store no
    longer exists). Only then may the stale slot file be deleted. ``False``
    on any failure, which never fails THIS job; the slot file stays and the
    next launch retries, ticket rejection included.
    """
    job_id = stale.get("job_id")
    if not job_id:
        return True
    owner = stale.get("db_path")
    try:
        if not owner or Path(owner) == current_db:
            target_store, target_registry, target_db = store, registry, db_path
        else:
            other = Path(owner)
            if not other.is_file():
                return True
            target_store = _store.build_store(other)
            target_registry, target_db = _store.build_job_registry(target_store), other
        _mark_interrupted(target_store, target_registry, str(job_id), db_path=target_db)
        return not pending_ticket_ids(target_store, str(job_id))
    except Exception:  # bookkeeping for ANOTHER job must never fail this one
        return False


def _mark_interrupted(store: Any, registry: Any, old_job_id: str, *, db_path: Path | None = None) -> None:
    """Best-effort: a reclaimed lock means that job's process died without
    ever updating its own record, which would otherwise sit at ``running``/
    ``awaiting_approval`` forever.

    Closing the job's pending tickets is PART of this bookkeeping, and is
    retried even when the record already says ``interrupted``/``failed``/
    ``done``: ``interrupt_job`` persists the status before it rejects the
    tickets, so a rejection that failed once would otherwise never be
    retried -- the job would look finished while a human could still be
    asked to approve an action for a process that no longer exists. Found by
    review, round 3."""
    old = registry.find(old_job_id)
    if old is None or old.get("status") in ("done", "partial", "failed", "interrupted"):
        close_pending_tickets(store, old_job_id, "the process running this job appears to have died")
        return
    interrupt_job(
        store, registry, old, "lock reclaimed: the process running this job appears to have died", db_path=db_path
    )


def _store_channel(queue: Any, job_id: str) -> Any:
    from lazybridge.ext.approval import StoreApprovalChannel

    return StoreApprovalChannel(queue, task_id=job_id, ttl=ticket_ttl(), poll_seconds=poll_seconds())


def find_job(store: Any, registry: Any, job_id: str) -> dict[str, Any] | None:
    """One job's record, merged with this bridge's own metadata."""
    job = registry.find(job_id)
    if job is None:
        return None
    if job.get("job_id") != job_id:
        matches = [row for row in _store.all_job_records(store) if str(row.get("job_id", "")).startswith(job_id)]
        if len(matches) != 1:
            return None
        job = matches[0]
    meta = _store.read_meta(store, str(job.get("job_id", ""))) or {}
    return {**meta, **job}


def list_jobs(store: Any, *, all_jobs: bool = False) -> list[dict[str, Any]]:
    """Every job record, merged with metadata, newest-created first unless ``all_jobs``.

    Without ``all_jobs``, only ``running``/``awaiting_approval`` rows are
    returned -- the "what's active right now" view a polling Claude Code
    session actually wants; pass ``all_jobs=True`` for the full history.
    """
    rows = []
    for job in _store.all_job_records(store):
        meta = _store.read_meta(store, str(job.get("job_id", ""))) or {}
        rows.append({**meta, **job})
    if not all_jobs:
        rows = [r for r in rows if r.get("status") in ("running", "checkpoint", "awaiting_approval")]
    rows.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    return rows


__all__ = [
    "DEFAULT_TICKET_TTL",
    "ENGINES",
    "RunResult",
    "find_job",
    "list_jobs",
    "new_job_id",
    "run_job",
    "ticket_ttl",
]
