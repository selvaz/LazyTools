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
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from lazybridge.policy import PolicyError, policy_scope

from lazytools.code_bridge import (
    _concurrency,
    _credentials,
    _engines,
    _launch,
    _progress,
    _registry,
    _store,
    _task,
    _tempdir,
)
from lazytools.code_bridge._approvals import BridgeGate
from lazytools.code_bridge._lockfile import JobLock
from lazytools.code_bridge._policy import NATIVE_READ_RULES
from lazytools.code_bridge._review_tools import review_tools
from lazytools.code_bridge._turn_cleanup import TurnCleanup

ENGINES: tuple[str, ...] = ("codex", "claude")
DEFAULT_MAX_CONTINUATIONS = 3
MAX_CONTINUATIONS = 10
CONTINUATION_PROMPT = "continue from the last commit; re-read the brief's Done-when; do not redo committed steps"
COMMIT_CONTINUATION_PROMPT = (
    "The checkout still has uncommitted changes. Commit the completed work now. "
    "Respect any rejection reason you received for an earlier commit request, and do not add AI attribution lines."
)

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


def new_job_id() -> str:
    return uuid.uuid4().hex


def detach_binding(
    launch: _launch.Launch, engine: str, cwd: Path, task: str, session: str | None, claim: _task.Claim | None = None,
    *, registry_db: Path | None = None,
) -> dict[str, Any]:
    return {
        **launch.binding(), "engine": engine, "cwd": str(cwd),
        "brief_hash": _store.brief_hash(task), "session": session,
        **(claim.binding() if claim else {}),
        **({"registry_db": str(registry_db)} if registry_db is not None else {}),
    }


def record_detached_refusal(db_path: Path | None, job_id: str | None, task: str, reason: str) -> None:
    if job_id is None or not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return
    store = _store.build_store(db_path)
    binding = store.read(_launch.DETACH_PREFIX + job_id)
    registry = _store.build_job_registry(store)
    if not isinstance(binding, dict) or _store.read_meta(store, job_id) or registry.find(job_id):
        return
    now = time.time()
    _store.write_meta(store, job_id, {
        **binding, "job_id": job_id, "created_at": now, "updated_at": now, "completed_at": now,
    })
    _write_result(db_path, job_id, f"[failed] {reason}")
    registry.write(job_id, task, tool_name=binding["engine"], status="failed", error=reason)


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


def _uncommitted_count(path: Path) -> int | None:
    """Return the number of dirty paths, or ``None`` when git cannot say."""
    try:
        result = _git(_git_repo_root(path), "status", "--porcelain=v1", "-uall")
        result.check_returncode()
        return len(result.stdout.splitlines())
    except Exception:
        return None


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


def interrupt_job(
    store: Any, registry: Any, job: dict[str, Any], error: str, *,
    db_path: Path | None = None, registry_db: Path | None = None,
) -> str:
    """Persist an interruption and close this job's still-open approval tickets."""
    job_id = str(job["job_id"])
    error = _with_workspace(store, job_id, error)
    meta = _store.read_meta(store, job_id) or {}
    _store.mark_completion(meta, "interrupted")
    _store.write_meta(store, job_id, meta)
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
    try:
        _registry.Registry.for_job(store, job_id, registry_db).revoke_coder_hand(job_id)
    finally:
        close_pending_tickets(store, job_id, error, registry_db=registry_db)
    return error


def pending_ticket_ids(store: Any, job_id: str) -> list[str]:
    """Ids of this job's tickets still recorded as ``pending`` -- expired-by-time ones included.

    Reads the raw records rather than ``list_pending_tickets``: that has a
    100-ticket cap and hides a ticket whose TTL ran out, which is still
    ``"pending"`` on disk until someone transitions it.
    """
    return [ticket.approval_id for ticket in _registry.Registry.for_job(store, job_id).open_for_job(job_id)]


def close_pending_tickets(store: Any, job_id: str, reason: str, *, registry_db: Path | None = None) -> None:
    """Reject this job's pending tickets; a ticket already past its TTL is marked expired.

    ``reject_ticket`` refuses a ticket whose TTL ran out (it is no longer
    actionable) yet leaves it ``"pending"`` on disk -- ``expire_ticket``
    closes those, so "no pending tickets left" is reachable. May raise if the
    queue's Store fails; callers decide whether that is fatal.
    """
    registry = _registry.Registry.for_job(store, job_id, registry_db)
    for ticket in registry.open_for_job(job_id):
        registry.close(ticket, reason)


async def _run_agent(
    engine: Any, task: str, agent_name: str, *, tools: list[Callable[[str], str]] | None = None,
) -> Any:
    from lazybridge import Agent

    return await Agent(engine, name=agent_name, tools=list(tools) if tools else None).run(task)


@_credentials.scrub_job_environment()
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
    registry_db: Path | None = None,
    on_job_id: Callable[[str], None] | None = None,
    job_id: str | None = None,
    routing: dict[str, Any] | None = None,
    timeout: float = _engines.DEFAULT_TIMEOUT,
    max_continuations: int = DEFAULT_MAX_CONTINUATIONS,
    max_wall: float | None = None,
    wait_for_slot: bool = False,
    on_slot_wait: Callable[[_concurrency.EngineBusy], None] | None = None,
    profile: str = "default",
    network: str | None = None,
    launch_mode: str = "auto",
    intentional_review: bool = False,
    detached_binding: bool = False,
    task_id: str | None = None,
    claim_token: str | None = None,
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
    if type(max_continuations) is not int or not 0 <= max_continuations <= MAX_CONTINUATIONS:
        raise ValueError(f"--max-continuations must be an integer from 0 to {MAX_CONTINUATIONS}")
    if max_wall is not None and (not math.isfinite(max_wall) or max_wall <= 0):
        raise ValueError("--max-wall must be a finite number greater than 0")
    wall_started = time.monotonic()
    registry_db = _registry.isolated_path(db_path, registry_db)
    resolved_cwd = _engines.resolve_cwd(cwd, root)
    claim = _task.load(task_id, claim_token, state="intent")
    resolver = claim.launch if claim else _launch.resolve_launch
    launch = resolver(engine_name, resolved_cwd, profile, network, launch_mode, review=intentional_review)
    legacy = launch.mode == "ungoverned-legacy"
    can_write = bool(launch.policy.tool_classes & {"edit", "shell"})
    writer = legacy and can_write

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
    if detached_binding:
        _launch.check_binding(
            store.read(_launch.DETACH_PREFIX + job_id),
            detach_binding(launch, engine_name, resolved_cwd, task, session_name, claim, registry_db=registry_db),
        )
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
    approval_registry = _registry.Registry(store, registry_db)
    try:
        slot_config = _concurrency.load_config()
        slot = _concurrency.EngineSlot(
            engine_name, slot_config.limits.get(engine_name), slot_config.slot_dir, db_path=store_path
        )
        slot.acquire(job_id, wait=wait_for_slot, on_wait=on_slot_wait,
                     deadline=None if max_wall is None else wall_started + max_wall)
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
            _store.mark_completion(meta, status)
            _store.write_meta(store, job_id, meta)
            registry.write(job_id, task, tool_name=engine_name, status=status, result=result, error=error)

        def _finish(status: str, text: str, error: str | None) -> RunResult:
            approval_registry.revoke_coder_hand(job_id)
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
            turns = (_store.read_meta(store, job_id) or {}).get("turns", [])
            history = _progress.history_text(turns) if len(turns) > 1 or any(t["progress"] for t in turns) or status != "done" else ""
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
        temp_refused = None
        if "edit" in launch.policy.tool_classes and launch.mode == "ungoverned-legacy":
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
                "approval_profile": profile,
                **launch.metadata(),
                **(claim.binding() if claim else {}),
                "registry_ticket_ids": [],
                **({"registry_db": str(registry_db)} if registry_db is not None else {}),
                "continuations": 0,
                "turns": [],
                "head_at_start": head_at_start,
                **({"head_at_start_error": head_at_start_error} if head_at_start_error is not None else {}),
                "pid": os.getpid(),
                "owns_process_group": _owns_process_group(),
                "created_at": now,
                "updated_at": now,
                "brief_hash": _store.brief_hash(task),
                **({"routing": routing} if routing is not None else {}),
                "temp_dir": str(temp_dir) if temp_dir is not None else None,
                **({"temp_dir_refused": temp_refused} if temp_refused is not None else {}),
            },
        )
        try:
            _store.record_relaunch(store, job_id, task, str(resolved_cwd), now=now)
        except Exception as exc:
            print(f"warning: job started but its relaunch intervention could not be recorded: {exc}", file=sys.stderr)
        temp_arg = str(temp_dir) if temp_dir is not None else None
        print(f"local launch: {launch.mode}; profile={launch.policy.profile}; network={launch.policy.network}",
              file=sys.stderr)

        def validate_policy() -> None:
            if claim:
                claim.recheck(job_id)
            current = resolver(engine_name, resolved_cwd, profile, network, launch_mode, review=intentional_review)
            _launch.check_binding(launch.binding(), current.binding())

        scoped_reads = (
            review_tools(resolved_cwd, network=launch.policy.network, validate=validate_policy)
            if legacy and launch.policy.profile == "read" and "read" in launch.policy.tool_classes else None
        )
        hand = None
        if writer:
            # A successful final turn may receive one additional turn to
            # commit a dirty checkout, independently of the timeout recovery
            # budget configured by --max-continuations.
            lifetime = min(max_wall or math.inf, timeout * (max_continuations + 2))
            # An expiry also covers a worker that dies before it can revoke.
            expires = datetime.now(UTC) + timedelta(seconds=min(lifetime + 60, 7 * 24 * 3600))
            hand = approval_registry.copy_coder_hand(
                job_id, claim.task_id if claim else job_id, resolved_cwd, temp_dir, expires,
                network=launch.policy.network,
            )
        channel = _registry.ApprovalChannel(
            approval_registry, job_id, launch.policy.revision, launch.policy.digest,
            ticket_ttl(), poll_seconds(),
            on_waiting=lambda: _set_status("awaiting_approval"),
            on_resumed=lambda: _set_status("running"),
            validate=validate_policy,
            task_id=claim.task_id if claim else None,
            actor=claim.principal.actor if claim else "local-bridge",
            attempt=claim.receipt["attempt"] if claim else 1,
            fenced_consume=partial(claim.consume_approval, registry=approval_registry) if claim else None,
            hand=hand,
            fenced_queue=partial(claim.card_queue, job_id, approval_registry) if claim else None,
        )
        gate = BridgeGate(channel=channel, rules=NATIVE_READ_RULES if legacy else (), cwd=str(resolved_cwd), temp_dir=temp_arg,
                          effective_policy=launch.policy, legacy_callbacks=legacy,
                          writer=writer)

        from lazybridge.engines.sessions import default_session_registry

        session_registry = default_session_registry()
        native_id = session_registry.resolve(engine_name, resolved_cwd, session_name) if session_name else None
        native_binding = {**launch.binding(), "cwd": str(resolved_cwd)}
        if claim:
            if (session_name and intentional_review and legacy and launch.policy.profile == "read"
                    and launch.policy.network == "off" and claim.receipt["review_of"]):
                # The transcript spans review rounds. The new claim still fences the job and every tool call.
                native_binding.update(task_id=claim.task_id, task_executor=claim.principal.actor,
                                      task_review_of=claim.receipt["review_of"],
                                      task_branch=claim.receipt["assigned_branch"])
            else:
                native_binding.update(claim.binding())
        _launch.check_resume(store, engine_name, native_id, native_binding)
        if native_id and engine_name == "claude" and launch.mode == "governed":
            raise PolicyError("Governed Claude resume requires the original SDK client; use a new session alias")

        if engine_name == "codex":
            engine = _engines.build_codex_engine(
                cwd=str(resolved_cwd),
                gate=gate,
                model=model,
                effort=effort,
                thread_id=native_id,
                session_alias=session_name,
                session_registry=session_registry,
                timeout=timeout,
                temp_dir=temp_arg,
                launch=launch,
                state_dir=store_path.parent / "codex-policy-state",
            )
            agent_name = "code-bridge-codex"
        else:
            engine = _engines.build_claude_engine(
                cwd=str(resolved_cwd),
                gate=gate,
                model=model,
                effort=effort,
                session_id=native_id,
                session_alias=session_name,
                session_registry=session_registry,
                timeout=timeout,
                temp_dir=temp_arg,
                launch=launch,
            )
            agent_name = "code-bridge-claude"

        if claim:
            claim.service.begin_submit(claim.principal, claim.task_id, claim.token)
            claim.service.record_submit(claim.principal, claim.task_id, claim.token, job_id)

        # The engine subprocess inherits this process's environment: point its
        # TEMP/TMP/TMPDIR at the job's own temp dir for exactly this run.
        async def run_turns() -> RunResult:
            from lazybridge import Envelope

            prompt = task
            turns: list[dict[str, Any]] = []
            commit_continuation = False
            before = await asyncio.to_thread(_progress.snapshot, lock.cwd)
            job_branch = before.get("branch")
            for turn in range(max_continuations + 2):
                validate_policy()
                if turn:
                    before = await asyncio.to_thread(_progress.snapshot, lock.cwd, job_branch)
                    close_pending_tickets(store, job_id, "continuation preflight: previous turn is closed")
                    if pending_ticket_ids(store, job_id):
                        raise RuntimeError("continuation preflight left pending approval tickets")
                remaining = None if max_wall is None else max_wall - (time.monotonic() - wall_started)
                if remaining is not None and remaining <= 0:
                    return _finish("partial" if any(t["progress"] for t in turns) else "failed", "", "max wall reached")
                _set_status("running")
                meta = _store.read_meta(store, job_id) or {}
                meta["continuations"] = turn
                _store.write_meta(store, job_id, meta)
                started = time.time()
                wall_expired = False
                turn_limit = min(timeout, remaining) if remaining is not None else timeout
                lease_left = claim.receipt["lease_expires_at"] - time.time() if claim else math.inf
                deadline = asyncio.timeout(min(turn_limit, lease_left))
                gate.accepting = channel.accepting = True
                async with TurnCleanup() as cleanup:
                    try:
                        async with deadline:
                            with policy_scope(launch.policy if launch.mode == "governed" else None):
                                if scoped_reads:
                                    env = await _run_agent(engine, prompt, agent_name, tools=scoped_reads)
                                else:
                                    env = await _run_agent(engine, prompt, agent_name)
                    except TimeoutError as exc:
                        wall_expired = deadline.expired() and remaining is not None and remaining <= timeout
                        if deadline.expired() and lease_left <= turn_limit:
                            env = Envelope.error_envelope(PolicyError("Task claim lease expired during the turn"))
                        else:
                            env = Envelope.error_envelope(exc)
                    except Exception as exc:
                        env = Envelope.error_envelope(exc)
                    finally:
                        gate.accepting = channel.accepting = False
                        close_pending_tickets(store, job_id, "turn ended; approval no longer actionable")
                reported_model = env.metadata.model if env.metadata.model_resolved else None
                if reported_model:
                    meta = _store.read_meta(store, job_id) or {}
                    if meta.get("model") is None:
                        meta["model"] = reported_model
                        _store.write_meta(store, job_id, meta)
                # A cancellation callback may have filed a ticket after the
                # first sweep. All producers are now quiescent: sweep again.
                close_pending_tickets(store, job_id, "turn cleanup completed; approval no longer actionable")
                if pending_ticket_ids(store, job_id):
                    raise RuntimeError("turn cleanup left pending approval tickets")
                after = await asyncio.to_thread(_progress.snapshot, lock.cwd, job_branch)
                evidence = await asyncio.to_thread(_progress.changes, lock.cwd, before, after)
                timed_out = not env.ok and env.error is not None and (
                    env.error.type == "TimeoutError" or "Turn timed out on durable thread" in env.error.message
                )
                handle = getattr(engine, "thread_id", None) or getattr(engine, "session_id", None)
                if handle:
                    _launch.bind_native(store, engine_name, handle, native_binding)
                turns.append({
                    "turn": turn, "started_at": started, "ended_at": time.time(),
                    "outcome": "done" if env.ok else "checkpoint" if timed_out and evidence["progress"] else "failed",
                    "thread_id": handle,
                    **evidence,
                    "cleanup_error": cleanup.error,
                })
                meta = _store.read_meta(store, job_id) or {}
                meta["turns"] = turns
                _store.write_meta(store, job_id, meta)
                if cleanup.error:
                    return _finish("partial" if any(t["progress"] for t in turns) else "failed", "", cleanup.error)
                if env.ok:
                    uncommitted = await asyncio.to_thread(_uncommitted_count, lock.cwd)
                    if uncommitted and can_write:
                        if commit_continuation:
                            return _finish(
                                "partial", "", f"job ended with {uncommitted} uncommitted path(s) "
                                "after the commit continuation",
                            )
                        if not turns[-1]["thread_id"]:
                            return _finish(
                                "partial", "", f"job ended with {uncommitted} uncommitted path(s); "
                                "no durable thread is available for the commit continuation",
                            )
                        commit_continuation = True
                        _set_status("checkpoint")
                        prompt = COMMIT_CONTINUATION_PROMPT
                        continue
                    return _finish("done", env.text(), None)
                message = env.error.message if env.error else "unknown error"
                if not timed_out:
                    return _finish("failed", "", message)
                if wall_expired or (max_wall is not None and time.monotonic() - wall_started >= max_wall):
                    progressed = any(t["progress"] for t in turns)
                    return _finish("partial" if progressed else "failed", "",
                                   "max wall reached after progress" if progressed else "no progress: max wall reached")
                if not evidence["progress"]:
                    return _finish("failed", "", f"no progress: {message or 'turn timed out'}")
                if commit_continuation:
                    uncommitted = await asyncio.to_thread(_uncommitted_count, lock.cwd)
                    detail = f"; {uncommitted} uncommitted path(s) remain" if uncommitted is not None else ""
                    return _finish("partial", "", f"commit continuation timed out{detail}")
                _set_status("checkpoint")
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
        try:
            approval_registry.revoke_coder_hand(job_id)
        finally:
            _tempdir.remove_job_temp_dir(temp_dir)
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
        _registry.Registry.for_job(store, old_job_id).revoke_coder_hand(old_job_id)
        return
    interrupt_job(
        store, registry, old, "lock reclaimed: the process running this job appears to have died", db_path=db_path
    )


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
