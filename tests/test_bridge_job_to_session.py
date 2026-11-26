from __future__ import annotations

import subprocess
import sys

import pytest
from lazybridge.ext.viz.replay import list_sessions, load_session_events, reconstruct_graph
from tools import bridge_job_to_session as bridge

from lazytools.code_bridge import _store


def test_export_finished_job_for_replay(tmp_path):
    db = tmp_path / "jobs.sqlite"
    output = tmp_path / "replay.sqlite"
    store = _store.build_store(db)
    _store.build_job_registry(store).write("job123", "Do work", tool_name="codex", status="done", result="Finished")
    _store.write_meta(store, "job123", {
        "created_at": 10.0, "completed_at": 30.0,
        "turns": [{"turn": 0, "started_at": 12.0, "ended_at": 20.0, "outcome": "done"}],
    })
    queue = _store.build_approval_queue(store)
    ticket = queue.create_ticket(task_id="job123", prompt="Allow action?")
    queue.approve_ticket(ticket.approval_id, actor="owner", channel="test")
    store.close()

    result = subprocess.run(
        [sys.executable, "tools/bridge_job_to_session.py", "job123", "--db", str(db), "--output", str(output)],
        check=True, capture_output=True, text=True,
    )
    assert "Rejected event types: {}" in result.stdout
    sessions = list_sessions(str(output))
    assert len(sessions) == 1
    events = load_session_events(str(output), sessions[0]["session_id"])
    assert {event["event_type"] for event in events} == {"agent_start", "loop_step", "hil_decision", "agent_finish"}
    assert next(event for event in events if event["event_type"] == "hil_decision")["status"] == "approved"
    assert next(event for event in events if event["event_type"] == "agent_finish")["result"] == "Finished"
    assert reconstruct_graph(events)["nodes"][0]["name"] == "code-bridge-codex"


def test_reject_unfinished_and_report_normalization_failure(tmp_path, monkeypatch):
    db = tmp_path / "jobs.sqlite"
    store = _store.build_store(db)
    registry = _store.build_job_registry(store)
    registry.write("job", "Work", tool_name="codex", status="running")
    with pytest.raises(ValueError, match="not finished"):
        bridge.export_job("job", db, tmp_path / "out.sqlite")
    registry.write("job", "Work", tool_name="codex", status="done")
    store.close()

    def reject(_payload):
        raise ValueError("unsupported")

    monkeypatch.setattr(bridge, "normalise_event", reject)
    _, rejected = bridge.export_job("job", db, tmp_path / "out.sqlite")
    assert rejected == {"agent_start": 1, "agent_finish": 1}
