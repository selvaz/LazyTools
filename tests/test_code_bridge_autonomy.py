from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from lazytools.code_bridge import _store, cli


@pytest.fixture
def autonomy_db(tmp_path):
    """A real SQLite Store shaped like one day of bridge job history."""
    path = tmp_path / "code-bridge.sqlite"
    store = _store.build_store(path)
    registry = _store.build_job_registry(store)
    stamp = datetime(2026, 11, 10, 12, tzinfo=UTC).timestamp()
    for job_id, status in (("auto-1", "done"), ("auto-2", "failed"), ("helped", "done")):
        registry.write(job_id, "fixture task", tool_name="codex", status=status)
        _store.write_meta(store, job_id, {"job_id": job_id, "created_at": stamp - 60, "updated_at": stamp})
    _store.record_intervention(store, "helped", "resync", "provider recovered")
    _store.record_intervention(store, "helped", "apply")
    registry.write("running", "not finished", tool_name="codex", status="running")
    _store.write_meta(store, "running", {"job_id": "running", "updated_at": stamp})
    return path


def test_autonomy_sample_day_matches_hand_count(autonomy_db, capsys):
    assert cli.main(
        ["autonomy", "--since", "2026-11-10", "--until", "2026-11-11", "--json", "--db", str(autonomy_db)]
    ) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "since": "2026-11-10",
        "until": "2026-11-11",
        "finished_jobs": 3,
        "autonomous_jobs": 2,
        "autonomy_percent": pytest.approx(66.6666666667),
        "target_percent": 90.0,
        "target_met": False,
        "intervention_causes": {"apply": 1, "resync": 1},
    }


def test_autonomy_week_and_human_output(autonomy_db, capsys):
    assert cli.main(["autonomy", "--week", "2026-W46", "--db", str(autonomy_db)]) == 0
    output = capsys.readouterr().out
    assert "Autonomy: 2/3 finished jobs (66.7%); target 90.0%" in output
    assert "apply: 1" in output
    assert "resync: 1" in output


def test_manual_approval_records_reason(tmp_path):
    db = tmp_path / "bridge.sqlite"
    store = _store.build_store(db)
    _store.write_meta(store, "job-1", {"job_id": "job-1"})
    ticket = _store.build_approval_queue(store).create_ticket(task_id="job-1", prompt="approve?")
    assert cli.main(["approve", ticket.approval_id, "--reason", "reviewed", "--db", str(db)]) == 0
    assert _store.read_meta(store, "job-1")["manual_interventions"][0]["reason"] == "reviewed"


def test_approval_for_non_bridge_task_does_not_create_meta(tmp_path):
    db = tmp_path / "bridge.sqlite"
    store = _store.build_store(db)
    ticket = _store.build_approval_queue(store).create_ticket(task_id="foreign-task", prompt="approve?")
    assert cli.main(["approve", ticket.approval_id, "--db", str(db)]) == 0
    assert _store.read_meta(store, "foreign-task") is None


def test_intervention_record_failure_only_warns(monkeypatch, capsys):
    def fail(*_args, **_kwargs):
        raise RuntimeError("busy")

    monkeypatch.setattr(_store, "record_intervention", fail)
    cli._record_intervention(object(), "job-1", "apply")
    assert "warning: action succeeded" in capsys.readouterr().err


def test_stale_meta_writer_preserves_concurrent_intervention(tmp_path):
    store = _store.build_store(tmp_path / "bridge.sqlite")
    _store.write_meta(store, "job-1", {"job_id": "job-1", "status_detail": "old"})
    stale = _store.read_meta(store, "job-1")
    _store.record_intervention(store, "job-1", "apply")
    stale["status_detail"] = "new"
    _store.write_meta(store, "job-1", stale)

    meta = _store.read_meta(store, "job-1")
    assert meta["status_detail"] == "new"
    assert [item["kind"] for item in meta["manual_interventions"]] == ["apply"]


def test_report_uses_stable_completion_time_after_later_metadata_update(tmp_path, capsys):
    db = tmp_path / "bridge.sqlite"
    store = _store.build_store(db)
    registry = _store.build_job_registry(store)
    completed = datetime(2026, 11, 10, 23, tzinfo=UTC).timestamp()
    registry.write("job-1", "task", tool_name="codex", status="done")
    _store.write_meta(store, "job-1", {
        "job_id": "job-1", "completed_at": completed,
        "updated_at": datetime(2026, 11, 12, tzinfo=UTC).timestamp(),
    })

    assert cli.main([
        "autonomy", "--since", "2026-11-10", "--until", "2026-11-11",
        "--json", "--db", str(db),
    ]) == 0
    assert json.loads(capsys.readouterr().out)["finished_jobs"] == 1


def test_relaunch_marks_recent_matching_failed_job(tmp_path):
    store = _store.build_store(tmp_path / "bridge.sqlite")
    registry = _store.build_job_registry(store)
    now = datetime(2026, 11, 10, 12, tzinfo=UTC).timestamp()
    registry.write("old", "same brief", tool_name="codex", status="failed")
    _store.write_meta(store, "old", {
        "job_id": "old", "cwd": "/repo", "completed_at": now - 60,
        "brief_hash": _store.brief_hash("same brief"),
    })

    assert _store.record_relaunch(store, "new", "same brief", "/repo", now=now) == "old"
    assert _store.read_meta(store, "old")["manual_interventions"][0]["kind"] == "relaunch"


def test_relaunch_ignores_old_or_different_jobs(tmp_path):
    store = _store.build_store(tmp_path / "bridge.sqlite")
    registry = _store.build_job_registry(store)
    now = datetime(2026, 11, 10, 12, tzinfo=UTC).timestamp()
    registry.write("old", "same brief", tool_name="codex", status="failed")
    _store.write_meta(store, "old", {"job_id": "old", "cwd": "/repo", "completed_at": now - 86401})
    assert _store.record_relaunch(store, "new", "same brief", "/repo", now=now) is None
    assert not _store.read_meta(store, "old").get("manual_interventions")
