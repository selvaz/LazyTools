from __future__ import annotations

import json

import pytest

from lazytools.code_bridge import _store, cli
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


def test_wait_any_reports_running_job_approval_once(tmp_path, capsys, monkeypatch):
    db = tmp_path / "jobs.sqlite"
    store = _job(db, "running-approval", "running")
    ticket = _store.build_approval_queue(store).create_ticket(task_id="running-approval", prompt="needs approval")
    sleeps = 0

    def finish_on_second_poll(_interval):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            _store.build_job_registry(store).write("running-approval", "task", tool_name="codex", status="done")

    monkeypatch.setattr(cli.time, "sleep", finish_on_second_poll)
    assert main(["wait-any", "running-approval", "--loop", "--db", str(db)]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row["event"] for row in rows] == ["approval", "done"]
    assert sum(row.get("approval", {}).get("approval_id") == ticket.approval_id for row in rows if row["approval"]) == 1


def test_wait_any_orders_multiple_completions_by_completed_at(tmp_path, capsys):
    db = tmp_path / "jobs.sqlite"
    late = _job(db, "late-job", "done")
    _store.write_meta(late, "late-job", {"job_id": "late-job", "where": "local", "completed_at": 20})
    early = _job(db, "early-job", "done")
    _store.write_meta(early, "early-job", {"job_id": "early-job", "where": "local", "completed_at": 10})
    assert main(["wait-any", "late-job", "early-job", "--loop", "--db", str(db)]) == 0
    assert [json.loads(line)["job_id"] for line in capsys.readouterr().out.splitlines()] == ["early-job", "late-job"]


def test_wait_any_all_active_and_event_filter(tmp_path, capsys):
    db = tmp_path / "jobs.sqlite"
    store = _job(db, "active-job", "running")
    _store.build_approval_queue(store).create_ticket(task_id="active-job", prompt="approve me")
    assert main(["wait-any", "--all-active", "--events", "approval", "--db", str(db)]) == 0
    assert json.loads(capsys.readouterr().out)["job_id"] == "active-job"

    _job(db, "finished-job", "done")
    assert main(["wait-any", "finished-job", "--events", "approval", "--timeout", "0", "--db", str(db)]) == 3
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("action", ["land-failed", "review-failed"])
def test_wait_any_reports_cloud_finishing_failure(tmp_path, capsys, monkeypatch, action):
    db = tmp_path / "jobs.sqlite"
    store = _job(db, "cloud-failure", "done")
    _store.write_meta(store, "cloud-failure", {"job_id": "cloud-failure", "where": "cloud", "land": True})
    monkeypatch.setattr(cli._cloud_land, "needs_finishing", lambda job: True)
    monkeypatch.setattr(cli._cloud_land, "process_job", lambda *args, **kwargs: {"action": action})
    assert main(["wait-any", "cloud-failure", "--db", str(db)]) == 0
    assert json.loads(capsys.readouterr().out)["event"] == "failed"


def test_wait_any_recognizes_persisted_cloud_landing(tmp_path, capsys):
    db = tmp_path / "jobs.sqlite"
    store = _job(db, "cloud-landed", "done")
    _store.write_meta(store, "cloud-landed", {"job_id": "cloud-landed", "where": "cloud", "land": True,
                                                  "landed_at": 123, "pr_url": "https://example.test/pull/1"})
    assert main(["wait-any", "cloud-landed", "--events", "landed", "--db", str(db)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["event"] == "landed" and payload["pr_url"].endswith("/1")


def test_jobs_json_and_encoding_safe_human_output(tmp_path, capsys, monkeypatch):
    db = tmp_path / "jobs.sqlite"
    store = _job(db, "unicode-job", "done")
    _store.build_job_registry(store).write("unicode-job", "snowman ☃", tool_name="codex", status="done")
    assert main(["jobs", "--all", "--json", "--db", str(db)]) == 0
    assert json.loads(capsys.readouterr().out)["objective"] == "snowman ☃"

    printed = []
    monkeypatch.setattr(cli.sys, "stdout", type("AsciiOut", (), {"encoding": "ascii"})())
    monkeypatch.setattr("builtins.print", lambda value, **kwargs: printed.append(value))
    cli._safe_print("snowman ☃")
    assert printed == [r"snowman \u2603"]
