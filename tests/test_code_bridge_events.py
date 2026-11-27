"""Unit tests of the shared bridge event poll (``_events.poll``) on a temporary store."""

from __future__ import annotations

import json

import pytest

from lazytools.code_bridge import _cloud, _cloud_land, _events, _store
from lazytools.code_bridge.cli import main


def _job(db, job_id: str, status: str, **meta):
    store = _store.build_store(db)
    _store.build_job_registry(store).write(job_id, "task", tool_name="codex", status=status)
    _store.write_meta(store, job_id, {"job_id": job_id, "engine": "codex", "where": "local", "cwd": ".", **meta})
    return store


def _kinds(events):
    return [(event["kind"], event["job_id"]) for event in events]


def test_empty_cursor_starts_from_now(tmp_path):
    db = tmp_path / "jobs.sqlite"
    _job(db, "old-done", "done")
    store = _job(db, "old-decided", "running")
    queue = _store.build_approval_queue(store)
    decided = queue.create_ticket(task_id="old-decided", prompt="decided before")
    queue.approve_ticket(decided.approval_id, actor="owner", channel="cli")
    open_ticket = queue.create_ticket(task_id="old-decided", prompt="still open")

    events, cursor = _events.poll("", db_path=db)
    # Finished jobs and past decisions are history; an open ticket is reported, as wait-any does.
    assert [(e["kind"], e["approval"]["approval_id"]) for e in events] == [("approval", open_ticket.approval_id)]
    assert events[0]["approval"]["status"] == "pending"

    events, _cursor = _events.poll(cursor, db_path=db)
    assert events == []


def test_job_started_and_finished_between_calls_is_not_lost(tmp_path):
    db = tmp_path / "jobs.sqlite"
    _store.build_store(db)
    _events_list, cursor = _events.poll("", db_path=db)
    _job(db, "quick-job", "done")

    events, cursor = _events.poll(cursor, db_path=db)
    assert _kinds(events) == [("done", "quick-job")]
    assert events[0]["detail"] == "done" and events[0]["task_id"] is None
    events, _cursor = _events.poll(cursor, db_path=db)
    assert events == []


def test_ticket_created_and_decided_between_calls_is_reported(tmp_path):
    db = tmp_path / "jobs.sqlite"
    store = _job(db, "running-job", "running")
    _events_list, cursor = _events.poll("", db_path=db)
    queue = _store.build_approval_queue(store)
    ticket = queue.create_ticket(task_id="running-job", prompt="Allow git push?")
    queue.reject_ticket(ticket.approval_id, actor="owner", channel="cli", reason="no")

    events, cursor = _events.poll(cursor, db_path=db)
    assert [(e["kind"], e["approval"]["approval_id"], e["approval"]["status"]) for e in events] == [
        ("approval", ticket.approval_id, "rejected")
    ]
    events, _cursor = _events.poll(cursor, db_path=db)
    assert events == []


@pytest.mark.parametrize(
    ("status", "meta", "kind"),
    [
        ("done", {}, "done"),
        ("failed", {}, "failed"),
        ("partial", {}, "failed"),
        ("interrupted", {}, "interrupted"),
        ("interrupted", {"manual_interventions": [{"kind": "cancel", "at": 1}]}, "cancelled"),
    ],
)
def test_terminal_kinds(tmp_path, status, meta, kind):
    db = tmp_path / "jobs.sqlite"
    store = _job(db, "a-job", "running")
    _events_list, cursor = _events.poll("", db_path=db)
    _store.build_job_registry(store).write("a-job", "task", tool_name="codex", status=status)
    if meta:
        _store.write_meta(store, "a-job", {"job_id": "a-job", "where": "local", **meta})
    events, _cursor = _events.poll(cursor, db_path=db)
    assert _kinds(events) == [(kind, "a-job")]


def test_cancel_error_text_is_cancelled(tmp_path):
    db = tmp_path / "jobs.sqlite"
    store = _job(db, "stopped", "running")
    _events_list, cursor = _events.poll("", db_path=db)
    _store.build_job_registry(store).write(
        "stopped", "task", tool_name="codex", status="interrupted", error="cancelled by operator: stale",
    )
    events, _cursor = _events.poll(cursor, db_path=db)
    assert _kinds(events) == [("cancelled", "stopped")]


def test_cloud_landing_after_done_is_reported_once(tmp_path):
    db = tmp_path / "jobs.sqlite"
    store = _job(db, "cloud-job", "running", where="cloud", land=True)
    _events_list, cursor = _events.poll("", db_path=db)
    _store.build_job_registry(store).write("cloud-job", "task", tool_name="codex", status="done")
    events, cursor = _events.poll(cursor, db_path=db)
    assert _kinds(events) == [("done", "cloud-job")]

    meta = _store.read_meta(store, "cloud-job") or {}
    _store.write_meta(store, "cloud-job", {**meta, "landed_at": 4e9, "pr_url": "https://example.test/pull/7"})
    events, cursor = _events.poll(cursor, db_path=db)
    assert _kinds(events) == [("landed", "cloud-job")] and events[0]["detail"].endswith("/7")
    # Read-only: the poll does not mark the landing reported in the store.
    assert "landed_event_reported_at" not in (_store.read_meta(store, "cloud-job") or {})
    events, _cursor = _events.poll(cursor, db_path=db)
    assert events == []


def test_landing_failure_after_done_is_reported_once(tmp_path, capsys):
    db = tmp_path / "jobs.sqlite"
    store = _job(db, "cloud-job", "running", where="cloud", land=True)
    _events_list, cursor = _events.poll("", db_path=db)
    _store.build_job_registry(store).write("cloud-job", "task", tool_name="codex", status="done")
    events, cursor = _events.poll(cursor, db_path=db)
    assert _kinds(events) == [("done", "cloud-job")]

    meta = _store.read_meta(store, "cloud-job") or {}
    _store.write_meta(store, "cloud-job", {**meta, "land_error": "push rejected", "land_failed_at": 4e9})
    events, cursor = _events.poll(cursor, db_path=db)
    assert _kinds(events) == [("failed", "cloud-job")]
    assert events[0]["state"] == "done" and events[0]["detail"] == "push rejected"
    events, cursor = _events.poll(cursor, db_path=db)
    assert events == []
    # A scheduler retrying the same landing is not a new outcome.
    _store.write_meta(store, "cloud-job", {**meta, "land_error": "push rejected", "land_failed_at": 4e9 + 60})
    events, cursor = _events.poll(cursor, db_path=db)
    assert events == []

    _store.write_meta(store, "cloud-job", {**meta, "land_error": None, "landed_at": 4e9 + 120})
    events, _cursor = _events.poll(cursor, db_path=db)
    assert _kinds(events) == [("landed", "cloud-job")]


def test_wait_any_and_poll_report_the_same_stored_landing_failure(tmp_path, capsys):
    db = tmp_path / "jobs.sqlite"
    _store.build_store(db)
    _events_list, cursor = _events.poll("", db_path=db)
    # No owed landing (submitted without --land): neither reader runs the finisher.
    _job(db, "cloud-job", "done", where="cloud", land_error="push rejected", land_failed_at=100)
    polled, _cursor = _events.poll(cursor, db_path=db)
    assert main(["wait-any", "cloud-job", "--db", str(db)]) == 0
    assert json.loads(capsys.readouterr().out) == polled[0]
    assert polled[0]["kind"] == "failed"


def test_landing_failure_before_the_cursor_is_history(tmp_path):
    db = tmp_path / "jobs.sqlite"
    _job(db, "old-failure", "done", where="cloud", land=True, land_error="push rejected", land_failed_at=100)
    events, cursor = _events.poll("", db_path=db)
    assert events == []
    assert _events.poll(cursor, db_path=db)[0] == []


def test_review_posting_failure_is_persisted_and_reported(tmp_path, monkeypatch):
    db = tmp_path / "jobs.sqlite"
    store = _job(db, "review-job", "running", where="cloud", review={"of": "other"})
    _events_list, cursor = _events.poll("", db_path=db)
    registry = _store.build_job_registry(store)
    registry.write("review-job", "task", tool_name="codex", status="done")
    events, cursor = _events.poll(cursor, db_path=db)
    assert _kinds(events) == [("done", "review-job")]

    monkeypatch.setattr(_cloud_land, "review_text", lambda job: "")
    with pytest.raises(_cloud.CloudError):
        _cloud_land.post_review(store, registry, {"job_id": "review-job"})
    events, cursor = _events.poll(cursor, db_path=db)
    assert _kinds(events) == [("failed", "review-job")] and "REVIEW.md" in events[0]["detail"]
    assert _events.poll(cursor, db_path=db)[0] == []


def test_landing_before_the_cursor_is_history(tmp_path):
    db = tmp_path / "jobs.sqlite"
    _job(db, "old-landing", "done", where="cloud", land=True, landed_at=100)
    events, cursor = _events.poll("", db_path=db)
    assert events == []
    assert _events.poll(cursor, db_path=db)[0] == []


@pytest.mark.parametrize("cursor", [
    "garbage", "ev1.abc", "ev1..", "ev2.eJyrrgUAAXUA-Q.AAAAAAAAAAAAAAAA", "ev1.eJyrrgUAAXUA-Q.AAAAAAAAAAAAAAAA",
])
def test_malformed_cursor_is_refused(tmp_path, cursor):
    with pytest.raises(_events.CursorError):
        _events.poll(cursor, db_path=tmp_path / "jobs.sqlite")


def test_tampered_cursor_is_refused(tmp_path):
    db = tmp_path / "jobs.sqlite"
    _store.build_store(db)
    _events_list, cursor = _events.poll("", db_path=db)
    prefix, packed, check = cursor.split(".")
    flipped = ("A" if packed[-1] != "A" else "B")
    with pytest.raises(_events.CursorError):
        _events.poll(f"{prefix}.{packed[:-1]}{flipped}.{check}", db_path=db)


def test_cursor_round_trip_and_pruning(tmp_path):
    db = tmp_path / "jobs.sqlite"
    _job(db, "kept", "done")
    _events_list, cursor = _events.poll("", db_path=db)
    state = _events.decode_cursor(cursor)
    assert state.seen_jobs == {"kept": "done"}
    assert _events.decode_cursor(_events.encode_cursor(state)).seen_jobs == {"kept": "done"}

    store = _store.build_store(db)
    store.delete(f"{_store.JOB_PREFIX}kept")
    _events_list, cursor = _events.poll(cursor, db_path=db)
    assert _events.decode_cursor(cursor).seen_jobs == {}
