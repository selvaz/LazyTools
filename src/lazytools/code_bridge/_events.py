"""Bridge event computation shared by ``wait-any`` and the ``bridge_events`` MCP tool.

``iter_events`` is the single implementation of "what happened on the watched jobs since
the last look". ``wait-any`` calls it in its polling loop with a live :class:`WatchState`;
:func:`poll` wraps it for bounded callers (MCP), carrying the same state between calls in
an opaque, validated cursor string.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import math
import time
import zlib
from collections.abc import Collection, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lazytools.code_bridge import _cloud, _cloud_land, _jobs, _registry, _store

#: Event kinds ``wait-any --events`` accepts.
WAIT_EVENTS = frozenset({"done", "failed", "approval", "landed", "interrupted"})
#: Event kinds :func:`poll` reports: ``cancelled`` is an ``interrupted`` job an operator stopped.
EVENT_KINDS = WAIT_EVENTS | {"cancelled"}

_TERMINAL_EVENT = {"done": "done", "failed": "failed", "partial": "failed", "interrupted": "interrupted"}
_CANCEL_ERRORS = ("cancelled by operator", "cloud stop requested by operator")

_CURSOR_PREFIX = "ev1"
_CURSOR_MAX_CHARS = 4_000_000
_CURSOR_MAX_BYTES = 32_000_000


class CursorError(ValueError):
    """A cursor that was not produced by :func:`poll` (truncated, edited, or another format)."""


@dataclass
class WatchState:
    """What one event stream has already looked at and reported.

    ``seen_jobs`` maps a job id to the event kind reported for it (or, for history, the
    one it already stood for); a job in it gets no further terminal event, except that a
    caller which does not finish cloud jobs itself is still told once when a done cloud
    job's landing (or review posting) later fails or succeeds. ``seen_tickets`` maps each
    reported approval ticket id to its job id.
    """

    watched: list[str]
    history: set[str] = field(default_factory=set)
    follow: bool = False
    started_at: float = 0.0
    seen_jobs: dict[str, str] = field(default_factory=dict)
    seen_tickets: dict[str, str] = field(default_factory=dict)


def job_event(job: dict[str, Any]) -> str | None:
    """The event a stored job record stands for, or ``None`` while it is unfinished.

    Only persisted state counts (status and landing/review metadata), so every reader of
    the same record gets the same answer, whoever ran the finisher.
    """
    status = str(job.get("status") or "")
    if status == "done" and _cloud.is_cloud(job):
        if job.get("land") and job.get("landed_at"):
            return "landed"
        land_failed = job.get("land_error") and not job.get("landed_at")
        review_failed = job.get("review_error") and not (job.get("review_posted_at") or job.get("review_comment_url"))
        if land_failed or review_failed:
            return "failed"
    return _TERMINAL_EVENT.get(status)


def wait_event(job: dict[str, Any], event: str, approval: dict[str, Any] | None = None) -> dict[str, Any]:
    # kind/job_id/task_id/detail are shared with `dispatch --json` events.
    if approval is not None:
        command = " ".join(str(approval.get("command") or approval.get("summary") or "").split())[:200]
        reason = " ".join(str(approval.get("reason") or "").split())[:160]
        detail = f"{approval['approval_id']} {command} | {reason}"
    elif event == "landed":
        detail = str(job.get("pr_url") or "")
    else:
        error = job.get("error")
        if event == "failed" and job.get("status") == "done":
            error = job.get("land_error") or job.get("review_error")
        detail = " ".join(str(error or job.get("status") or "").split())
    return {
        "event": event,
        "job_id": str(job["job_id"]),
        "state": job.get("status"),
        "approval": approval,
        "pr_url": job.get("pr_url"),
        "kind": event,
        "task_id": job.get("task_id"),
        "detail": detail,
    }


def _ticket_args(db_path: Path | None, registry_db: Path | None) -> argparse.Namespace:
    return argparse.Namespace(db=str(db_path) if db_path is not None else None, registry_db=registry_db)


def _decided_tickets(
    store: Any, job: dict[str, Any], skip: Collection[str], fallback_registry: Path | None, legacy: list[dict[str, Any]],
) -> list[Any]:
    """A job's tickets that are no longer pending and not in ``skip``."""
    from lazybridge.ext.approval import ApprovalTicket

    job_id = str(job["job_id"])
    tickets: list[ApprovalTicket] = []
    ids = [str(ticket_id) for ticket_id in job.get("registry_ticket_ids") or [] if str(ticket_id) not in skip]
    if ids:
        registry = _registry.Registry.for_job(store, job_id, fallback_registry)
        tickets.extend(ticket for ticket_id in ids if (ticket := registry.inspect(ticket_id)) is not None)
    for raw in legacy:
        if raw.get("task_id") == job_id and str(raw.get("approval_id") or "") not in skip:
            tickets.append(ApprovalTicket.model_validate(raw))
    return [ticket for ticket in tickets if ticket.status != "pending"]


def _legacy_tickets(store: Any) -> list[dict[str, Any]]:
    return [raw for _key, raw in store.items(prefix=_store.APPROVAL_PREFIX) if isinstance(raw, dict)]


def _cancelled(store: Any, job: dict[str, Any]) -> bool:
    if str(job.get("error") or "").startswith(_CANCEL_ERRORS):
        return True
    meta = _store.read_meta(store, str(job["job_id"])) or {}
    return any(isinstance(item, dict) and item.get("kind") == "cancel" for item in meta.get("manual_interventions") or [])


def iter_events(
    state: WatchState,
    *,
    db_path: Path | None,
    registry_db: Path | None,
    kinds: Collection[str],
    finish: bool,
    interval: float = 0.25,
    deadline: float | None = None,
    include_decided: bool = False,
) -> Iterator[dict[str, Any]]:
    """Yield the new events on the watched jobs, approvals first, then completions in order.

    Updates ``state`` as it goes, so an event is reported once per state. Events whose kind
    is not in ``kinds`` are recorded as seen but not yielded. ``finish`` lets this call run
    the cloud finisher (landing, review) and record a reported landing in the job metadata,
    as ``wait-any`` does; without it the computation only reads. ``include_decided`` also
    reports tickets that were already decided before this call first saw them.
    """
    from lazytools.code_bridge import cli  # deferred: cli imports this module

    store = _store.build_store(db_path)
    registry = _store.build_job_registry(store)
    if state.follow:  # pick up every job created after this stream began, even one already finished
        for job in _jobs.list_jobs(store, all_jobs=True):
            job_id = str(job["job_id"])
            if job_id not in state.watched and job_id not in state.history:
                state.watched.append(job_id)
    jobs = {job_id: _jobs.find_job(store, registry, job_id) for job_id in state.watched}

    # A ticket is reported once per stream, even if it remains pending.
    ticket_args = _ticket_args(db_path, registry_db)
    tickets = cli._pending_tickets(ticket_args, store, jobs=(job for job in jobs.values() if job is not None))
    if include_decided:
        legacy = _legacy_tickets(store)
        fallback = cli._selected_registry_db(ticket_args)
        for found in jobs.values():
            if found is not None:
                tickets.extend(_decided_tickets(store, found, state.seen_tickets, fallback, legacy))
        tickets = sorted({ticket.approval_id: ticket for ticket in tickets}.values(), key=lambda t: t.created_at)
    for ticket in tickets:
        job_ref = _registry.job_ref(ticket)
        ticket_job = jobs.get(job_ref)
        if ticket_job is None or ticket.approval_id in state.seen_tickets:
            continue
        state.seen_tickets[ticket.approval_id] = job_ref
        detail = cli._request_detail(ticket.prompt)
        approval = {
            "approval_id": ticket.approval_id,
            "command": detail.get("command"),
            "summary": detail.get("summary") or ticket.prompt.splitlines()[0],
            "reason": detail.get("reason"),
        }
        if include_decided:
            approval["status"] = ticket.status
        if "approval" in kinds:
            yield wait_event(ticket_job, "approval", approval)

    candidates: list[tuple[float, dict[str, Any], str]] = []
    for job_id, current in jobs.items():
        if current is None:
            continue
        prior = state.seen_jobs.get(job_id)
        # Without the finisher, a reported cloud job can still land or fail to later: look again.
        if prior is not None and (finish or prior == "landed" or not _cloud.is_cloud(current)):
            continue
        job = current
        if finish and _cloud.is_cloud(job) and (
            job.get("status") not in cli._TERMINAL or _cloud_land.needs_finishing(job)
        ):
            _cloud_land.process_job(
                store,
                registry,
                job,
                db_path=db_path,
                poll_interval=interval,
                deadline=deadline,
                nonblocking_poll=True,
            )
            job = _jobs.find_job(store, registry, job_id) or job
        event = job_event(job)
        # A landing an earlier stream already delivered is, to this one, just a done job.
        if event == "landed" and float(job["landed_at"]) < state.started_at and job.get("landed_event_reported_at"):
            event = "done"
        # After the first report, only a new landing outcome of a done job is news.
        if prior is not None and (event == prior or event not in ("landed", "failed") or job.get("status") != "done"):
            continue
        if event == "interrupted" and "cancelled" in kinds and _cancelled(store, job):
            event = "cancelled"
        if event:
            meta = _store.read_meta(store, job_id) or {}
            candidates.append((float(meta.get("completed_at") or meta.get("updated_at") or 0), job, event))
    for _at, job, event in sorted(candidates, key=lambda item: item[0]):
        job_id = str(job["job_id"])
        state.seen_jobs[job_id] = event
        if event in kinds:
            if event == "landed" and finish:
                meta = _store.read_meta(store, job_id) or {}
                meta["landed_event_reported_at"] = time.time()
                _store.write_meta(store, job_id, meta)
            yield wait_event(job, event)


# --------------------------------------------------------------------------- #
# cursor-based polling (bounded callers such as MCP)
# --------------------------------------------------------------------------- #


def encode_cursor(state: WatchState) -> str:
    body = json.dumps(
        {"t": state.started_at, "j": state.seen_jobs, "k": state.seen_tickets}, sort_keys=True, separators=(",", ":"),
    ).encode()
    packed = base64.urlsafe_b64encode(zlib.compress(body, 9)).rstrip(b"=").decode("ascii")
    return f"{_CURSOR_PREFIX}.{packed}.{_checksum(packed)}"


def _checksum(packed: str) -> str:
    digest = hashlib.sha256(f"{_CURSOR_PREFIX}.{packed}".encode("ascii")).digest()[:12]
    return base64.urlsafe_b64encode(digest).decode("ascii")


def decode_cursor(cursor: str) -> WatchState:
    """Validate a cursor from :func:`poll`; anything else raises :class:`CursorError`."""
    if not isinstance(cursor, str) or not cursor or len(cursor) > _CURSOR_MAX_CHARS:
        raise CursorError("malformed cursor")
    parts = cursor.split(".")
    if len(parts) != 3 or parts[0] != _CURSOR_PREFIX or not parts[1].isascii() or not parts[2].isascii():
        raise CursorError("malformed cursor")
    if not hmac.compare_digest(_checksum(parts[1]), parts[2]):
        raise CursorError("malformed cursor: checksum mismatch")
    try:
        raw = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        inflater = zlib.decompressobj()
        body = inflater.decompress(raw, _CURSOR_MAX_BYTES)
        if inflater.unconsumed_tail or not inflater.eof:
            raise CursorError("malformed cursor")
        payload = json.loads(body)
    except (binascii.Error, zlib.error, ValueError) as exc:
        raise CursorError("malformed cursor") from exc
    if not isinstance(payload, dict) or set(payload) != {"t", "j", "k"}:
        raise CursorError("malformed cursor")
    started_at, seen_jobs, seen_tickets = payload["t"], payload["j"], payload["k"]
    if isinstance(started_at, bool) or not isinstance(started_at, (int, float)) or not math.isfinite(started_at):
        raise CursorError("malformed cursor")
    for mapping in (seen_jobs, seen_tickets):
        if not isinstance(mapping, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in mapping.items()
        ):
            raise CursorError("malformed cursor")
    return WatchState(watched=[], follow=True, started_at=float(started_at),
                      seen_jobs=dict(seen_jobs), seen_tickets=dict(seen_tickets))


def _start_state(db_path: Path | None, registry_db: Path | None) -> WatchState:
    """The "from now" state: like ``wait-any --follow``'s startup snapshot."""
    from lazytools.code_bridge import cli  # deferred: cli imports this module

    state = WatchState(watched=[], follow=True, started_at=time.time())
    store = _store.build_store(db_path)
    jobs = _jobs.list_jobs(store, all_jobs=True)
    for job in jobs:
        if str(job.get("status") or "") in cli._TERMINAL:
            state.seen_jobs[str(job["job_id"])] = job_event(job) or str(job["status"])
    # Decisions made before the stream began are history; open tickets are reported.
    legacy = _legacy_tickets(store)
    fallback = cli._selected_registry_db(_ticket_args(db_path, registry_db))
    for job in jobs:
        for ticket in _decided_tickets(store, job, (), fallback, legacy):
            state.seen_tickets[ticket.approval_id] = str(job["job_id"])
    return state


def poll(
    cursor: str = "", *, db_path: Path | None = None, registry_db: Path | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Return the events since ``cursor`` and the cursor to pass next time.

    An empty cursor starts "from now": jobs already finished are history and are not
    replayed, while open approval tickets are reported, as ``wait-any --all-active
    --loop --follow`` does at startup. Any event that happens between two calls -- a job
    that starts and finishes, a ticket created and decided -- is returned by the next call
    with the previous cursor. A cursor this function did not produce raises
    :class:`CursorError`. The computation only reads the stores.
    """
    state = decode_cursor(cursor) if cursor else _start_state(db_path, registry_db)
    store = _store.build_store(db_path)
    by_id = {str(job["job_id"]): job for job in _jobs.list_jobs(store, all_jobs=True)}
    # Settled jobs (reported, and not a cloud job whose landing may still change) are history:
    # they are not looked at again, and their ticket ids need not be remembered.
    state.history = {
        job_id for job_id, kind in state.seen_jobs.items()
        if job_id in by_id and (kind == "landed" or not _cloud.is_cloud(by_id[job_id]))
    }
    events = list(iter_events(
        state, db_path=db_path, registry_db=registry_db, kinds=EVENT_KINDS, finish=False, include_decided=True,
    ))
    known = set(by_id) | set(state.watched)
    state.seen_jobs = {job_id: kind for job_id, kind in state.seen_jobs.items() if job_id in known}
    watched = set(state.watched)
    state.seen_tickets = {ticket: job_id for ticket, job_id in state.seen_tickets.items() if job_id in watched}
    return events, encode_cursor(state)


__all__ = [
    "EVENT_KINDS",
    "WAIT_EVENTS",
    "CursorError",
    "WatchState",
    "decode_cursor",
    "encode_cursor",
    "iter_events",
    "job_event",
    "poll",
    "wait_event",
]
