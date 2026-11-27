"""Native conversation ids on bridge jobs, and the review independence check (fake engines)."""

from __future__ import annotations

import json
import os

import pytest
from lazybridge import Envelope

import _code_bridge_fakes as fakes
from lazytools.code_bridge import _jobs, _store, _task, cli


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("LAZYBRIDGE_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    script = fakes.install(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    return script, tmp_path, repo, tmp_path / "store.sqlite"


def run(bridge, engine="claude", **kwargs):
    _, root, repo, db = bridge
    return _jobs.run_job(engine_name=engine, cwd=str(repo), root=str(root), db_path=db, task="Run", **kwargs)


def result_json(bridge, job_id, capsys):
    capsys.readouterr()
    cli.main(["result", job_id, "--db", str(bridge[3]), "--json"])
    return json.loads(capsys.readouterr().out)


def cli_run_argv(bridge, *extra):
    _, root, repo, db = bridge
    return [
        "run", "--engine", "claude", "--cwd", str(repo), "--root", str(root), "--db", str(db), "--task", "Review",
        "--no-brief-check", "--no-freshness-check", *extra,
    ]  # fmt: skip


@pytest.mark.parametrize("engine", ["claude", "codex"])
@pytest.mark.parametrize("mode", ["ok", "error"])
def test_result_json_shows_native_id(bridge, capsys, engine, mode):
    bridge[0].mode = mode
    job = run(bridge, engine)
    payload = result_json(bridge, job.job_id, capsys)
    assert payload["native_id"] == f"{engine}-id-1"
    assert "review_of" not in payload and "writer_native_id" not in payload


def test_review_records_both_ids(bridge, capsys):
    writer = run(bridge, session_name="writer")
    assert cli.main(cli_run_argv(bridge, "--review-of", writer.job_id[:8], "--session", "reviewer")) == 0
    review_id = capsys.readouterr().out.splitlines()[0]
    payload = result_json(bridge, review_id, capsys)
    assert payload["status"] == "done"
    assert payload["review_of"] == writer.job_id
    assert payload["writer_native_id"] == "claude-id-1"
    assert payload["native_id"] == "claude-id-2"


def test_review_resuming_the_writer_conversation_is_refused_before_the_engine(bridge, capsys):
    writer = run(bridge, session_name="writer")
    review = run(bridge, session_name="writer", review_of=writer.job_id, intentional_review=True)
    assert review.status == "failed"
    assert "not independent" in review.error and "claude-id-1" in review.error
    assert len(bridge[0].engines) == 1 and len(bridge[0].prompts) == 1
    payload = result_json(bridge, review.job_id, capsys)
    assert payload["native_id"] == payload["writer_native_id"] == "claude-id-1"


@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_engine_reporting_the_writer_id_is_refused_after_the_turn(bridge, capsys, monkeypatch, engine):
    writer = run(bridge, engine)

    async def turn(fake, prompt, name, tools=None):
        fake._set(f"{engine}-id-1")
        return Envelope(task=prompt, payload="looks good")

    monkeypatch.setattr(_jobs, "_run_agent", turn)
    review = run(bridge, engine, review_of=writer.job_id, intentional_review=True)
    assert review.status == "failed" and review.text == ""
    assert "not independent" in review.error
    payload = result_json(bridge, review.job_id, capsys)
    assert payload["native_id"] == payload["writer_native_id"] == f"{engine}-id-1"
    assert payload.get("result") != "looks good"


def test_writer_record_without_native_id_falls_back_to_its_turns(bridge, capsys):
    writer = run(bridge, session_name="writer")
    store = _store.build_store(bridge[3])
    meta = _store.read_meta(store, writer.job_id)
    del meta["native_id"]  # as recorded before the field existed
    _store.write_meta(store, writer.job_id, meta)
    review = run(bridge, session_name="writer", review_of=writer.job_id, intentional_review=True)
    assert review.status == "failed" and "not independent" in review.error


def test_a_retry_attempt_resumes_the_writer_conversation_of_its_task(bridge, monkeypatch):
    from lazytools.connectors.projects import ProjectsTools
    from lazytools.projects.task_service import CompletionEvidence, ProjectTaskService
    from test_projects_task_service import OPERATOR, PROJECT, admit, claim, placement, revision, task

    path = os.environ["LAZYTOOLS_PROJECTS_STORE_DB"]
    projects = ProjectsTools(store_db=path, allow_write=True)
    projects.projects_create(PROJECT, "Bridge test", "Resume a writer across attempts")
    admit(projects, [task("local"), task("other")])
    service = ProjectTaskService(path)
    monkeypatch.setenv(_task.PRINCIPAL_ENV, OPERATOR.model_dump_json())
    bridge[0].mode = "error"  # each attempt's engine fails, as a crash would
    results = []
    for number in (1, 2):
        placement(service, "local")
        receipt = claim(service, "local", key=f"dispatch-{number}", principal=OPERATOR)
        assert receipt["attempt"] == number
        result = run(bridge, "codex", session_name="dispatch-attempt-1", task_id=receipt["task_id"],
                     claim_token=receipt["claim_token"], launch_mode="legacy")
        results.append(result)
        reference = f"evidence-{result.job_id}"
        service.record_completion_evidence(OPERATOR, receipt["task_id"], number, CompletionEvidence(
            reference=reference, job_ref=result.job_id, executor=receipt["executor"],
            engine_family=receipt["engine_family"], state="blocked", outcome="task_failure"))
        service.complete(OPERATOR, receipt["task_id"], receipt["claim_token"], reference, "blocked", reference,
                         "task_failure")
        service.prepare_attempt(OPERATOR, receipt["task_id"], revision(service, "local"), "retry after engine crash")
    assert [result.error.splitlines()[0] for result in results] == ["turn failed"] * 2  # no binding refusal
    store = _store.build_store(bridge[3])
    assert [_store.read_meta(store, result.job_id)["native_id"] for result in results] == ["codex-id-1"] * 2
    assert bridge[0].engines[1].kwargs["thread_id"] == "codex-id-1"


def test_unknown_writer_job_is_refused_without_a_record(bridge):
    with pytest.raises(ValueError, match="no job found"):
        run(bridge, review_of="nope")
    assert _jobs.list_jobs(_store.build_store(bridge[3]), all_jobs=True) == []


def test_detached_review_child_records_the_writer(bridge, capsys, monkeypatch):
    writer = run(bridge)
    spawned = []
    monkeypatch.setattr(cli, "_spawn_detached", lambda argv, log: spawned.append(argv) or 1)
    assert cli.main(cli_run_argv(bridge, "--review-of", writer.job_id, "--detach")) == 0
    argv = spawned[0]
    assert cli.main(argv[argv.index("run") :]) == 0  # what the child process would run
    payload = result_json(bridge, argv[argv.index("--job-id") + 1], capsys)
    assert payload["review_of"] == writer.job_id
    assert payload["writer_native_id"] == "claude-id-1"
    assert payload["native_id"] == "claude-id-2"
