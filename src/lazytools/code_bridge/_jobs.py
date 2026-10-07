"""Job lifecycle: run one coding-engine job to completion in THIS process.

``run_job`` is the whole synchronous contract the CLI's ``run`` subcommand
needs: resolve and lock the ``cwd``, record the job, run the engine in the
foreground (Claude Code backgrounds the whole process via its Bash tool's
``run_in_background``, so this function does not need to), and persist the
result -- to the job record and to a result file -- before returning.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from lazytools.code_bridge import _engines, _store
from lazytools.code_bridge._lockfile import JobLock
from lazytools.code_bridge._policy import CODING_RULES

ENGINES: tuple[str, ...] = ("codex", "claude")

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
    status: str  # "done" | "failed" | "interrupted"
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
) -> RunResult:
    """Run one job to completion. Raises before any job is recorded for a
    bad engine name or a ``cwd`` outside the confinement root; raises
    :class:`~lazytools.code_bridge._lockfile.LockHeld` if another live
    process already owns this ``cwd``. Every other failure (engine error,
    denied action, expired approval) is captured into the job record and
    returned as ``status="failed"`` rather than raised, so the CLI can
    report it and exit non-zero without a traceback.
    """
    if engine_name not in ENGINES:
        raise ValueError(f"engine must be one of {ENGINES}, got {engine_name!r}")
    resolved_cwd = _engines.resolve_cwd(cwd, root)

    job_id = new_job_id()
    if on_job_id is not None:
        on_job_id(job_id)

    store = _store.build_store(db_path)
    registry = _store.build_job_registry(store)
    queue = _store.build_approval_queue(store)

    lock = JobLock(_store.locks_dir(db_path), _git_repo_root(resolved_cwd))
    stale_job_id = lock.acquire(job_id)  # raises LockHeld -- job_id was reported but nothing else is recorded

    # Everything from here on holds the lock; a single `finally` at the
    # bottom covers the whole rest of the function, INCLUDING the initial
    # bookkeeping writes just below -- not only the engine run further down.
    # A narrower `try` starting after those writes would leave the lock
    # stuck held forever if `_mark_interrupted`/`registry.write`/
    # `write_meta` ever raised and the caller is a long-lived process that
    # catches the exception and keeps running (the CLI itself always exits
    # right after, reclaiming on pid-death, but `run_job` is a public
    # function other callers may embed). Found by Codex review.
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
            # The result FILE is persisted BEFORE the job record is marked
            # terminal, not after: marking "done" first and writing the file
            # second would let a disk-full/permissions failure on the write
            # leave a job record claiming success with no result file to
            # back it up (and an uncaught exception out of a function whose
            # whole contract is "never raises for an engine-side failure").
            # Found by Codex review.
            body = text if status == "done" else f"[{status}] {error}"
            try:
                result_path: Path | None = _write_result(db_path, job_id, body)
            except OSError as exc:
                write_error = f"result could not be persisted: {type(exc).__name__}: {exc}"
                _set_status("failed", error=write_error)
                return RunResult(job_id, "failed", "", write_error, None)
            _set_status(status, result=text if status == "done" else None, error=error)
            return RunResult(job_id, status, text if status == "done" else "", error, result_path)

        if stale_job_id is not None:
            _mark_interrupted(registry, stale_job_id)

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
                "pid": os.getpid(),
                "created_at": now,
                "updated_at": now,
            },
        )

        channel = _StatusTrackingChannel(
            _store_channel(queue, job_id),
            on_waiting=lambda: _set_status("awaiting_approval"),
            on_resumed=lambda: _set_status("running"),
        )
        from lazybridge.ext.approval import TieredGate

        gate = TieredGate(channel=channel, rules=CODING_RULES)

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
            )
            agent_name = "code-bridge-claude"

        env = asyncio.run(_run_agent(engine, task, agent_name))
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
        lock.release()

    if not env.ok:
        message = env.error.message if env.error else "unknown error"
        return _finish("failed", "", message)

    return _finish("done", env.text(), None)


def _mark_interrupted(registry: Any, old_job_id: str) -> None:
    """Best-effort: a reclaimed lock means that job's process died without
    ever updating its own record, which would otherwise sit at ``running``/
    ``awaiting_approval`` forever. No-op if the old record is gone."""
    old = registry.find(old_job_id)
    if old is None:
        return
    registry.write(
        old_job_id,
        str(old.get("objective") or ""),
        tool_name=str(old.get("kind") or "unknown"),
        status="interrupted",
        error="lock reclaimed: the process running this job appears to have died",
    )


def _store_channel(queue: Any, job_id: str) -> Any:
    from lazybridge.ext.approval import StoreApprovalChannel

    return StoreApprovalChannel(queue, task_id=job_id, ttl=ticket_ttl(), poll_seconds=poll_seconds())


def find_job(store: Any, registry: Any, job_id: str) -> dict[str, Any] | None:
    """One job's record, merged with this bridge's own metadata."""
    job = registry.find(job_id)
    if job is None:
        return None
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
        rows = [r for r in rows if r.get("status") in ("running", "awaiting_approval")]
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
