from __future__ import annotations

import json
from pathlib import Path

import pytest

from lazytools.code_bridge import _store, cli
from lazytools.code_bridge.cli import main


def _job(db, job_id: str, status: str):
    store = _store.build_store(db)
    _store.build_job_registry(store).write(job_id, "task", tool_name="codex", status=status)
    _store.write_meta(store, job_id, {"job_id": job_id, "engine": "codex", "where": "local", "cwd": "."})
    return store


@pytest.mark.parametrize("configured", [None, ":memory:"])
def test_legacy_approval_commands_do_not_create_projects_store(tmp_path, monkeypatch, capsys, configured):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("LAZYCEO_CEO_STORE_DB", raising=False)
    monkeypatch.delenv("LAZYTOOLS_PROJECTS_STORE_DB", raising=False)
    if configured is not None:
        monkeypatch.setenv("LAZYTOOLS_PROJECTS_STORE_DB", configured)
    db = tmp_path / "jobs.sqlite"
    job = "legacy-approval"
    queue = _store.build_approval_queue(_job(db, job, "awaiting_approval"))
    first = queue.create_ticket(task_id=job, prompt="First")
    second = queue.create_ticket(task_id=job, prompt="Second")
    db_args = ["--db", str(db)]

    assert main(["pending", "--json", *db_args]) == 0
    assert {row["approval_id"] for row in json.loads(capsys.readouterr().out)} == {first.approval_id, second.approval_id}
    assert main(["wait-any", job, "--events", "approval", "--timeout", "0", *db_args]) == 0
    assert main(["approve", first.approval_id, *db_args]) == 0
    assert main(["reject", second.approval_id, "--reason", "Test decision", *db_args]) == 0
    assert queue.get_ticket(first.approval_id).status == "approved"
    assert queue.get_ticket(second.approval_id).status == "rejected"
    assert not (tmp_path / ".lazytools").exists()


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
        "kind": event,
        "task_id": None,
        "detail": status,
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


def test_wait_any_all_active_loop_follows_jobs_started_later(tmp_path, capsys, monkeypatch):
    db = tmp_path / "jobs.sqlite"
    store = _job(db, "first-job", "running")
    _job(db, "old-done-job", "done")  # finished before the wait began: not followed
    polls = 0

    def advance(_interval):
        nonlocal polls
        polls += 1
        registry = _store.build_job_registry(store)
        if polls == 1:
            _job(db, "late-job", "running")
            _store.build_approval_queue(store).create_ticket(task_id="late-job", prompt="late approval")
        elif polls == 2:
            registry.write("first-job", "task", tool_name="codex", status="done")
        elif polls == 3:
            # Started and finished between two polls: found by its creation time.
            _job(db, "flash-job", "done")
            _store.write_meta(store, "flash-job", {"job_id": "flash-job", "where": "local",
                                                   "created_at": cli.time.time()})
        elif polls == 4:
            registry.write("late-job", "task", tool_name="codex", status="failed")

    monkeypatch.setattr(cli.time, "sleep", advance)
    # --follow keeps the stream open; it ends on the timeout (exit 3).
    assert main(["wait-any", "--all-active", "--loop", "--follow", "--timeout", "1", "--db", str(db)]) == 3
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(row["kind"], row["job_id"]) for row in rows] == [
        ("approval", "late-job"), ("done", "first-job"), ("done", "flash-job"), ("failed", "late-job"),
    ]
    assert rows[0]["detail"].startswith(rows[0]["approval"]["approval_id"] + " ")


def test_wait_any_all_active_loop_waits_for_a_first_job(tmp_path, capsys, monkeypatch):
    db = tmp_path / "jobs.sqlite"
    store = _job(db, "old-done-job", "done")
    statuses = iter(["running", "done"])
    monkeypatch.setattr(cli.time, "sleep", lambda _interval: _store.build_job_registry(store).write(
        "new-job", "task", tool_name="codex", status=next(statuses, "done")))
    assert main(["wait-any", "--all-active", "--loop", "--follow", "--timeout", "1", "--db", str(db)]) == 3
    assert [json.loads(line)["job_id"] for line in capsys.readouterr().out.splitlines()] == ["new-job"]
    assert main(["wait-any", "--all-active", "--db", str(db)]) == 2  # without --loop it is still a snapshot


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

    # A persisted landing is delivered once, rather than being rediscovered
    # by every future wait-any invocation.
    assert main(["wait-any", "cloud-landed", "--events", "landed", "--timeout", "0", "--db", str(db)]) == 3
    assert capsys.readouterr().out == ""


def test_wait_any_ignores_old_landing_that_was_already_reported(tmp_path, capsys):
    db = tmp_path / "jobs.sqlite"
    store = _job(db, "old-landing", "done")
    _store.write_meta(store, "old-landing", {
        "job_id": "old-landing", "where": "cloud", "land": True,
        "landed_at": 100, "landed_event_reported_at": 101, "pr_url": "https://example.test/pull/1",
    })

    assert main(["wait-any", "old-landing", "--events", "landed", "--timeout", "0", "--db", str(db)]) == 3
    assert capsys.readouterr().out == ""


def test_wait_any_reports_landing_created_after_wait_started(tmp_path, capsys, monkeypatch):
    db = tmp_path / "jobs.sqlite"
    store = _job(db, "new-landing", "done")
    _store.write_meta(store, "new-landing", {
        "job_id": "new-landing", "where": "cloud", "land": True,
        "landed_at": 200, "landed_event_reported_at": 150, "pr_url": "https://example.test/pull/2",
    })
    monkeypatch.setattr(cli.time, "time", lambda: 175)

    assert main(["wait-any", "new-landing", "--events", "landed", "--db", str(db)]) == 0
    assert json.loads(capsys.readouterr().out)["event"] == "landed"


def test_wait_any_does_not_treat_running_job_pr_as_new_landing(tmp_path, capsys):
    db = tmp_path / "jobs.sqlite"
    store = _job(db, "running-with-pr", "running")
    _store.write_meta(store, "running-with-pr", {
        "job_id": "running-with-pr",
        "engine": "codex",
        "where": "cloud",
        "land": True,
        "pr_url": "https://example.test/pull/old",
    })

    assert main(["wait-any", "running-with-pr", "--events", "landed", "--timeout", "0", "--db", str(db)]) == 3
    assert capsys.readouterr().out == ""


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


def test_wait_any_follow_picks_up_jobs_started_later(tmp_path, capsys, monkeypatch):
    db = tmp_path / "jobs.sqlite"
    _store.build_store(db)  # empty store: --follow may start with no active job
    polls = {"n": 0}

    def later(_interval):
        polls["n"] += 1
        if polls["n"] == 1:
            store = _job(db, "late-job", "awaiting_approval")
            _store.build_approval_queue(store).create_ticket(task_id="late-job", prompt="Allow git add?")
        elif polls["n"] == 2:
            _store.build_job_registry(_store.build_store(db)).write("late-job", "task", tool_name="codex", status="done")

    monkeypatch.setattr(cli.time, "sleep", later)
    code = main(["wait-any", "--all-active", "--loop", "--follow", "--events", "approval,done",
                 "--interval", "0", "--timeout", "0.5", "--db", str(db)])
    events = [json.loads(line)["event"] for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert code == 3  # --follow only stops on the timeout
    assert events == ["approval", "done"]


def test_wait_any_follow_requires_all_active_and_loop(tmp_path, capsys):
    db = tmp_path / "jobs.sqlite"
    _job(db, "a-job", "running")
    assert main(["wait-any", "--all-active", "--follow", "--db", str(db)]) == 2
    assert "--follow needs --all-active and --loop" in capsys.readouterr().err


def test_wait_any_follow_reports_a_job_that_finished_between_polls(tmp_path, capsys, monkeypatch):
    db = tmp_path / "jobs.sqlite"
    _job(db, "old-job", "done")  # history: not replayed
    polls = {"n": 0}

    def quick_job(_interval):
        polls["n"] += 1
        if polls["n"] == 1:
            _job(db, "quick-job", "done")  # starts and ends before the next poll

    monkeypatch.setattr(cli.time, "sleep", quick_job)
    main(["wait-any", "--all-active", "--loop", "--follow", "--events", "done",
          "--interval", "0", "--timeout", "0.5", "--db", str(db)])
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert [(event["event"], event["job_id"]) for event in events] == [("done", "quick-job")]
