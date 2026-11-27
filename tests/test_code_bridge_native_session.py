"""Native conversation ids on bridge jobs, and the review independence check (fake engines)."""

from __future__ import annotations

import json
import os
import subprocess

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


@pytest.fixture
def dispatch_queue(tmp_path):
    from lazytools.connectors.projects import ProjectsTools
    from lazytools.projects.task_service import ProjectTaskService
    from test_projects_task_service import PROJECT, Clock

    path = str(tmp_path / "dispatch-queue.sqlite")
    projects = ProjectsTools(store_db=path, allow_write=True)
    projects.projects_create(PROJECT, "Bridge test", "Recover an orphaned attempt")
    return projects, ProjectTaskService(path, lease_seconds=60, clock=Clock())


def test_expired_local_intent_retries_once_and_keeps_its_continuation(dispatch_queue, tmp_path, monkeypatch):
    from lazytools.code_bridge import _dispatch, _dispatch_launch
    from lazytools.projects.task_service import AttemptContinuation
    from test_projects_task_service import OPERATOR, PROJECT, admit, claim, placement, revision, task

    projects, service = dispatch_queue
    admit(projects, [task("local"), task("other", dependency_ids=["local"])])
    task_id = f"{PROJECT}/local"
    continuation = AttemptContinuation(preserved_branch="dispatch-attempt/queue-test/local/1", preserved_commit="a" * 40)
    service.prepare_attempt(OPERATOR, task_id, revision(service, "local"), "continue work", continuation=continuation)
    placement(service, "local")
    first = claim(service, "local", principal=OPERATOR)
    launcher = _dispatch_launch.BridgeLauncher(str(tmp_path / "jobs.sqlite"), OPERATOR, PROJECT)
    launches, events = [], []
    monkeypatch.setattr(launcher, "launch", lambda task, receipt: launches.append((task, receipt)) or "missing-job")

    def run_pass():
        return _dispatch.Dispatcher(service, OPERATOR, launcher, PROJECT, clock=service.clock,
                                    emit=events.append).pass_once()

    run_pass()
    assert launches == [] and events == []
    service.clock.now = first["lease_expires_at"]
    run_pass()
    retried_task, retry = launches[0]
    assert retry["attempt"] == first["attempt"] + 1
    assert retried_task["preserved_commit"] == continuation.preserved_commit
    assert f"reconciled {task_id} absent" in events

    # The retry also dies before submit. The budget survives dispatcher restart.
    service.clock.now = retry["lease_expires_at"]
    run_pass()
    state = _dispatch._tasks(service, PROJECT)[0]
    assert state.status == "failed" and len(launches) == 1
    assert [old["state"] for old in state.data["dispatches"]] == ["absent", "absent"]


@pytest.mark.parametrize("outcome", ["adopted", "legacy-adopted", "absent", "recorded-absent", "unavailable"])
def test_expired_cloud_submission_reconciles_provider_outcome(dispatch_queue, tmp_path, monkeypatch, outcome):
    from lazytools.code_bridge import _cloud, _cloud_stop, _dispatch, _dispatch_launch
    from test_projects_task_service import OPERATOR, PROJECT, admit, claim, placement, task

    projects, service = dispatch_queue
    admit(projects, [task("cloud", requested_placement="cloud"), task("other", dependency_ids=["cloud"])])
    placement(service, "cloud", "cloud")
    first = claim(service, "cloud", principal=OPERATOR)
    task_id, token, attempt = first["task_id"], first["claim_token"], first["attempt"]
    service.begin_submit(OPERATOR, task_id, token)
    job_id = _dispatch_launch.cloud_job_id(token)
    if outcome in ("absent", "recorded-absent"):
        # An older dispatcher could acknowledge the bridge record without a provider handle.
        service.record_submit(OPERATOR, task_id, token, job_id)
    db = tmp_path / "jobs.sqlite"
    label = _cloud.dispatch_label(task_id, attempt, job_id)
    brief = "Produce cloud" if outcome == "legacy-adopted" else f"Produce cloud\n\n{label}"
    created_at = service.clock()
    with _store.build_store(db) as store:
        meta = {
            "job_id": job_id, "task_id": task_id, "where": "cloud", "engine": "codex",
            "env_id": "cloud-env", "cloud_handle": None, "objective": brief,
            "created_at": created_at, "brief_hash": _store.brief_hash(brief),
        }
        _cloud._write(store, _store.build_job_registry(store), job_id, meta, status="submitting")
        if outcome == "recorded-absent":
            # A restart between writing the absence evidence and closing the bridge job.
            _store.write_meta(store, job_id, {**meta, "cloud_submission_absent": True})

    lookups = []

    def cli_list(argv, **_kwargs):
        assert argv[:6] == ["codex", "cloud", "list", "--json", "--env", "cloud-env"]
        lookups.append(argv[6:])
        if outcome == "unavailable":
            return subprocess.CompletedProcess(argv, 1, "", "provider unavailable")
        page = {"tasks": [{"id": "task_matching"}]} if argv[6:] else {
            "tasks": [{"id": "task_other"}], "cursor": "page-two",
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(page), "")

    def details(method, url, token, *, account_id):
        assert (method, token, account_id) == ("GET", "fake-token", "fake-account")
        prompt = _cloud.CLOUD_PREAMBLE + brief + _cloud.CLOUD_REPORT_SUFFIX
        if outcome != "adopted" or url.endswith("task_other"):
            prompt = prompt.replace(label, _cloud.dispatch_label(task_id, attempt + 1, job_id))
        submitted_at = created_at - 10 if url.endswith("task_other") else created_at + 1
        return {
            "task": {"created_at": submitted_at},
            "current_user_turn": {"input_items": [{"role": "user", "content": [
                {"content_type": "text", "text": prompt},
            ]}]},
        }

    monkeypatch.setattr(_cloud, "_codex_argv", lambda: ["codex"])
    monkeypatch.setattr(_cloud_stop.subprocess, "run", cli_list)
    monkeypatch.setattr(_cloud_stop, "_credentials", lambda _engine: ("fake-token", "fake-account"))
    monkeypatch.setattr(_cloud_stop, "_request", details)
    launcher = _dispatch_launch.BridgeLauncher(str(db), OPERATOR, PROJECT)
    launches, events = [], []
    monkeypatch.setattr(launcher, "launch_cloud", lambda task, receipt, job_id: launches.append(receipt) or job_id)

    def run_pass():
        _dispatch.Dispatcher(service, OPERATOR, launcher, PROJECT, clock=service.clock, emit=events.append).pass_once()

    run_pass()
    assert not lookups and not launches
    service.clock.now = first["lease_expires_at"]
    run_pass()
    assert lookups == {"recorded-absent": [], "unavailable": [[]]}.get(outcome, [[], ["--cursor", "page-two"]])
    state = _dispatch._tasks(service, PROJECT)[0]
    job = launcher.job(job_id)
    assert job is not None
    if outcome in ("adopted", "legacy-adopted"):
        assert job["cloud_handle"] == "task_matching" and job["status"] == "running"
        assert state.dispatch["job_ref"] == job_id and state.status == "claimed" and not launches
        assert f"reconciled {task_id} adopted" in events
    elif outcome in ("absent", "recorded-absent"):
        assert job["status"] == "interrupted" and launcher.cloud_free("codex")
        assert state.data["dispatches"][0]["state"] == "absent"
        assert launches[0]["attempt"] == attempt + 1
        assert f"reconciled {task_id} absent" in events
    else:
        assert job["status"] == "submitting" and state.status == "claimed" and not launches
        assert any("provider unavailable" in event for event in events)


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
