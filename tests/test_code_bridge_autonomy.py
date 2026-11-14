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
