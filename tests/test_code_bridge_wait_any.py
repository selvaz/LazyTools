from __future__ import annotations

import json

import pytest

from lazytools.code_bridge import _store
from lazytools.code_bridge.cli import main


def _job(db, job_id: str, status: str):
    store = _store.build_store(db)
    _store.build_job_registry(store).write(job_id, "task", tool_name="codex", status=status)
    _store.write_meta(store, job_id, {"job_id": job_id, "engine": "codex", "where": "local", "cwd": "."})
    return store


@pytest.mark.parametrize(
    ("status", "event"),
    [("done", "done"), ("failed", "failed"), ("partial", "failed"), ("interrupted", "interrupted")],
)
def test_wait_any_reports_terminal_events(tmp_path, capsys, status, event):
    db = tmp_path / "jobs.sqlite"
    _job(db, "a-full-job-id", status)

    assert main(["wait-any", "a-full", "--db", str(db), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "event": event,
        "job_id": "a-full-job-id",
        "state": status,
        "approval": None,
        "pr_url": None,
    }


def test_wait_any_times_out_and_validates_usage(tmp_path, capsys):
    db = tmp_path / "jobs.sqlite"
    _job(db, "running-job", "running")
    assert main(["wait-any", "running-job", "--timeout", "0", "--db", str(db)]) == 3
    assert main(["wait-any", "--all-active", "running-job", "--db", str(db)]) == 2
    assert "but not both" in capsys.readouterr().err


def test_wait_any_approval_is_deduplicated_in_loop(tmp_path, capsys):
    db = tmp_path / "jobs.sqlite"
    store = _job(db, "approval-job", "done")
    queue = _store.build_approval_queue(store)
    ticket = queue.create_ticket(
        task_id="approval-job",
        prompt='agent asks\n  arguments: {"command":"git push","reason":"publish?"}',
    )

    assert main(["wait-any", "approval-job", "--loop", "--db", str(db)]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row["event"] for row in rows] == ["approval", "done"]
    assert rows[0]["approval"]["approval_id"] == ticket.approval_id
    assert rows[0]["approval"]["command"] == "git push"
