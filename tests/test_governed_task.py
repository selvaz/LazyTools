"""Exercise the NC-7 runner with fake engines, GitHub, and a local Git remote."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

import pytest
from lazybridge import Store

from _code_bridge_fakes import Script
from _receipt_demo_fakes import install
from lazytools.code_bridge import _cloud, _store, cli
from lazytools.projects.plan_edit import _board_key
from lazytools.projects.task_service import ProjectTaskService

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "nc-7-subtraction"
BRANCH = "trial/subtraction"


def python_check(code):
    return f'"{sys.executable}" -c "{code}"'


@pytest.fixture
def trial(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    runner = importlib.import_module("governed_task")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "0")
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_POLL_SECONDS", "0.05")
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_TICKET_TTL", "20")
    monkeypatch.setenv("LAZYTOOLS_PROJECTS_STORE_DB", str(tmp_path / "ambient-projects.sqlite"))
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_DB", str(tmp_path / "ambient-bridge.sqlite"))
    monkeypatch.setenv("LAZYTOOLS_RECEIPT_ISSUER_KEY", str(tmp_path / "ambient-issuer.pem"))
    seed = tmp_path / "seed"
    seed.mkdir()
    git = _cloud._git
    git(seed, "init", "--initial-branch=main")
    git(seed, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "Base")

    fake_state = tmp_path / "fakes"
    fake_state.mkdir()
    state_parent = tmp_path / "trial state"
    state = state_parent / f"task-{PROJECT}"
    ledger = tmp_path / "ledger.jsonl"
    brief = tmp_path / "brief.md"
    brief.write_text("# Brief: Subtraction\n\nAdd subtraction and test a negative result.", encoding="utf-8")
    review = tmp_path / "review.md"
    review.write_text("Check that the test uses 2 minus 5.", encoding="utf-8")
    script = Script()

    def setup(*, accepted=True, native_approval=False):
        script.request_approval = native_approval
        checkout = install(stack, fake_state, seed, "main", "subtraction.py", "test_subtraction.py",
                           writer_script=script, writer_commit=True, review_accepted=accepted)
        return checkout

    def invoke(command, *args):
        return runner.main([command, "--project", PROJECT, "--state-dir", str(state_parent), *args])

    def start(checkout, *args):
        return invoke("start", "--repo", str(checkout), "--branch", BRANCH, "--brief", f"@{brief}",
                      "--review-brief", f"@{review}", "--ledger", str(ledger), *args)

    with ExitStack() as stack:
        yield start, invoke, setup, script, state, ledger, fake_state
    assert not list(tmp_path.glob("ambient-*"))


def test_governed_task_publication_and_ledger(trial, capsys):
    start, invoke, setup, script, state, ledger, fake_state = trial
    checkout = setup(native_approval=True)
    git = _cloud._git
    base = git(checkout, "rev-parse", "HEAD")
    git(checkout, "switch", "-c", "unrelated")
    git(checkout, "commit", "--allow-empty", "-m", "Unrelated work")
    options = ["--db", str(state / "bridge.sqlite"), "--registry-db", str(state / "registry.sqlite")]
    gh_state = fake_state / "fake-github.json"

    def writer_operator():
        pythonpath = os.pathsep.join(filter(None, (str(ROOT / "src"), os.environ.get("PYTHONPATH"))))
        env = {**os.environ, "PYTHONPATH": pythonpath}
        command = [sys.executable, "-m", "lazytools.code_bridge"]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if (state / "registry.sqlite").is_file():
                output = subprocess.check_output([*command, "pending", "--json", *options], env=env, text=True, timeout=10)
                tickets = json.loads(output)
                if tickets:
                    ticket, = tickets
                    assert ticket["task_id"] == f"{PROJECT}/writer"
                    subprocess.run([*command, "approve", ticket["approval_id"], *options], env=env, check=True,
                                   capture_output=True, timeout=10)
                    return
            time.sleep(0.05)
        pytest.fail("No writer approval appeared in the isolated registry")

    with ThreadPoolExecutor() as pool:
        approval = pool.submit(writer_operator)
        assert start(checkout) == 0
        approval.result()
    trace = capsys.readouterr().out
    assert all(f"STEP {step}:" in trace for step in range(1, 5))
    assert "lazytools-code-bridge pending" in trace and "lazytools-code-bridge approve TICKET" in trace
    assert all(option in trace for option in options)
    head = git(checkout, "rev-parse", "HEAD")
    assert git(checkout, "rev-parse", "HEAD^") == base
    assert git(checkout, "branch", "--show-current") == BRANCH
    assert "Check that the test uses 2 minus 5." in script.prompts[1]
    assert "Add subtraction and test a negative result." in script.prompts[0]
    assert not gh_state.exists()
    with Store(db=str(state / "projects.sqlite")) as project_store:
        _, review = project_store.read(_board_key(PROJECT))["tasks"]
        assert review["dependency_ids"] == ["writer"]
        writer_job, review_job = project_store.read("governed-task:jobs")["ids"]
    with Store(db=str(state / "bridge.sqlite")) as job_store:
        writer_meta = _store.read_meta(job_store, writer_job)
        review_meta = _store.read_meta(job_store, review_job)
        assert writer_meta and review_meta
        assert (writer_meta["engine"], writer_meta["model"], writer_meta["effort"]) == ("codex", "gpt-6.2-sol", "high")
        assert (review_meta["engine"], review_meta["profile"]) == ("claude", "read")
        assert review_meta["routing"]["provider"] == "claude_code"

    assert invoke("publish", "--resume") == 1
    assert not gh_state.exists()
    capsys.readouterr()
    assert invoke("publish") == 0
    trace = capsys.readouterr().out
    assert f"example/publication {BRANCH} A={head} into main" in trace
    approve_line = next(line for line in trace.splitlines() if line.startswith("lazytools-code-bridge approve "))
    ticket = approve_line.split()[2]
    assert all(option in approve_line for option in options)
    assert not gh_state.exists()
    assert cli.main(["approve", ticket, *options, "--actor", "test-operator"]) == 0

    assert invoke("publish") == 0
    assert "is approved. Run publish --resume" in capsys.readouterr().out
    assert not gh_state.exists()
    assert invoke("publish", "--resume") == 0
    trace = capsys.readouterr().out
    assert "live valid" in trace and "returned stored receipt" in trace
    gh = json.loads(gh_state.read_text(encoding="utf-8"))
    pr, = gh["prs"]
    assert pr["isDraft"] and pr["headRefOid"] == head and pr["url"] in trace
    assert len(gh["comments"]) == 1
    with Store(db=str(state / "projects.sqlite")) as project_store:
        receipt_id = project_store.read("governed-task:live-verified-receipt")
    assert receipt_id in trace

    ledger.write_text('{"task":"baseline","flow":"manual"}\n', encoding="utf-8")
    assert invoke("ledger", "--manual", "2", "--stop-real", "Stop one https://example.invalid/one",
                  "--stop-real", "Stop two https://example.invalid/two", "--stop-false", "False stop",
                  "--reconstruct-seconds", "20", "--notes", "Trial note") == 0
    assert invoke("ledger") == 0
    baseline, row = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    seconds = sum(turn["ended_at"] - turn["started_at"] for meta in (writer_meta, review_meta) for turn in meta["turns"])
    wall = datetime.fromisoformat(pr["createdAt"]).timestamp() - writer_meta["created_at"]
    assert baseline == {"task": "baseline", "flow": "manual"}
    assert row == {
        "task": f"{PROJECT}: Subtraction", "flow": "unified", "wall_minutes": round(max(0, wall) / 60, 4),
        "agent_minutes": round(seconds / 60, 4), "manual_interventions": 4,
        "stops_real": ["Stop one https://example.invalid/one", "Stop two https://example.invalid/two"],
        "stops_false": ["False stop"], "reconstruct_seconds": 20,
        "receipt": {"id": receipt_id, "verification": "live valid"}, "notes": "Trial note",
    }


def test_governed_task_reuses_branch_and_stops_on_review_rejection(trial, capsys):
    start, invoke, setup, _, state, ledger, fake_state = trial
    checkout = setup(accepted=False)
    git = _cloud._git
    git(checkout, "switch", "-c", BRANCH)
    git(checkout, "commit", "--allow-empty", "-m", "Earlier branch work")
    earlier = git(checkout, "rev-parse", "HEAD")
    git(checkout, "switch", "main")

    assert start(checkout, "--engine", "claude", "--model", "claude-sonnet-5-5", "--effort", "medium") == 1
    trace = capsys.readouterr()
    assert "rejected" in trace.out and "A required check failed." in trace.err
    assert git(checkout, "rev-parse", "HEAD^") == earlier
    with Store(db=str(state / "projects.sqlite")) as project_store:
        writer_job, _ = project_store.read("governed-task:jobs")["ids"]
    with Store(db=str(state / "bridge.sqlite")) as job_store:
        writer = _store.read_meta(job_store, writer_job)
        assert writer and (writer["engine"], writer["model"], writer["effort"]) == ("claude", "claude-sonnet-5-5", "medium")
    assert invoke("publish") == 1
    assert not (fake_state / "fake-github.json").exists()
    assert invoke("ledger", "--stop-real", "A required check failed") == 0
    row = json.loads(ledger.read_text(encoding="utf-8"))
    assert row["wall_minutes"] is None and row["receipt"] is None
    assert row["stops_real"] == ["A required check failed"]


def test_checks_from_brief_run_in_order_on_writer_commit(trial):
    start, _, setup, script, state, _, _ = trial
    checkout = setup()
    first = python_check("import subprocess; print(subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True))")
    second = python_check("from subtraction import sub; assert sub(2, 5) == -3; print('negative result | OK')")
    brief = state.parent.parent / "checks.md"
    brief.write_text(f"# Subtraction\n\n## Checks\n\n```text\n{first}\n{second}\n```\n", encoding="utf-8")

    assert start(checkout, "--brief", f"@{brief}") == 0
    head = _cloud._git(checkout, "rev-parse", "HEAD")
    logs = sorted(state.glob("checks-*/*.log"))
    assert [log.read_text().strip() for log in logs] == [head, "negative result | OK"]
    assert f"| 0 | {head} |" in script.prompts[1]
    assert "| 0 | negative result &#124; OK |" in script.prompts[1]


@pytest.mark.parametrize("failure", ["exit", "timeout", "missing"])
def test_required_check_failure_stops_before_review(trial, capsys, monkeypatch, failure):
    start, _, setup, script, state, _, _ = trial
    checkout = setup()
    brief = state.parent.parent / "checks.md"
    brief.write_text("# Subtraction\n\n## Checks\n```\nshould-never-run\n```\n", encoding="utf-8")
    if failure == "exit":
        command = python_check("import sys; print('check error', file=sys.stderr); sys.exit(7)")
        expected = "check error"
    elif failure == "timeout":
        monkeypatch.setattr(importlib.import_module("governed_task"), "CHECK_TIMEOUT", 1)
        command = python_check("import time; time.sleep(30)")
        expected = "Timed out after 1 seconds."
    else:
        command = "missing-governed-check"
        expected = "launch error"

    assert start(checkout, "--brief", f"@{brief}", "--check", command, "--check", python_check("print('ran second')")) == 1
    error = capsys.readouterr().err
    assert expected in error and "Required checks failed" in error and "should-never-run" not in error
    assert len(script.prompts) == 1
    first, second = sorted(state.glob("checks-*/*.log"))
    assert str(first) in error
    assert second.read_text().strip() == "ran second"


def test_review_receives_bounded_copies_of_external_specs(trial, monkeypatch):
    start, _, setup, script, state, _, _ = trial
    checkout = setup()
    first = state.parent.parent / "external spec.md"
    first.write_text("A1. Keep the negative result.\n```\n", encoding="utf-8")
    second = first.with_name("large spec.md")
    second.write_text("OK" * 32 + "omitted text", encoding="utf-8")
    monkeypatch.setattr(importlib.import_module("governed_task"), "SPEC_MAX_BYTES", 64)

    assert start(checkout, "--spec", str(first), "--spec", str(second)) == 0
    review = script.prompts[1]
    assert f"Path: {first.as_posix()}" in review and "A1. Keep the negative result.\n```" in review
    assert f"Path: {second.as_posix()}" in review and "OK" * 32 in review
    assert "Attachment truncated after 64 bytes." in review
    assert "omitted text" not in review


def test_review_judges_the_code_and_uses_runner_evidence(trial, monkeypatch):
    start, _, setup, script, _, _, _ = trial
    checkout = setup()
    runner = importlib.import_module("governed_task")
    run_job = runner.run_job

    def multi_commit_writer(*args, **kwargs):
        result = run_job(*args, **kwargs)
        if not kwargs.get("review_of"):
            _cloud._git(checkout, "commit", "--allow-empty", "-m", "Second writer commit")
        return result

    monkeypatch.setattr(runner, "run_job", multi_commit_writer)
    assert start(checkout) == 0
    head = _cloud._git(checkout, "rev-parse", "HEAD")
    assert script.prompts[1].startswith(f"Review commit {head}")
    assert "Judge correctness, security and spec coverage." in script.prompts[1]
    assert "Use the attached runner check results instead of running checks." in script.prompts[1]
    assert "writer-process preferences are not review blockers unless they hide risk" in script.prompts[1]


def test_rereview_uses_legacy_state_and_current_head(trial):
    start, invoke, setup, script, state, _, _ = trial
    accepted = False
    checkout = setup(accepted=lambda: accepted)
    assert start(checkout, "--engine", "claude", "--model", "claude-sonnet-5-5") == 1
    git = _cloud._git
    original = git(checkout, "rev-parse", "HEAD")
    git(checkout, "commit", "--allow-empty", "-m", "Advance review target")
    head = git(checkout, "rev-parse", "HEAD")
    with Store(db=str(state / "projects.sqlite")) as project_store:
        run = project_store.read("governed-task:run")
        project_store.write("governed-task:run", {k: run[k] for k in ("repo", "branch", "base", "title", "ledger")})
        previous = project_store.read("governed-task:jobs")["ids"]

    accepted = True
    spec = state.parent.parent / "spec.md"
    spec.write_text("A1. Return a negative number.", encoding="utf-8")
    brief = spec.with_name("check-brief.md")
    command = python_check("print('fresh gate')")
    brief.write_text(f"# Gate\n\n## Checks\n```\n{command}\n```\n", encoding="utf-8")
    assert invoke("review", "--brief", f"@{brief}", "--spec", str(spec)) == 0
    assert len(script.prompts) == 3
    assert script.prompts[2].startswith(f"Review commit {head}")
    assert "Add subtraction and test a negative result." in script.prompts[2]
    assert "| 0 | fresh gate |" in script.prompts[2] and "A1. Return a negative number." in script.prompts[2]
    with Store(db=str(state / "projects.sqlite")) as project_store:
        writer, reviewer = project_store.read(_board_key(PROJECT))["tasks"]
        latest = project_store.read("governed-task:jobs")["ids"]
    assert latest[0] == previous[0] and latest[1] != previous[1]
    assert len(writer["dispatches"]) == 1
    assert [d["receipt"]["attempt"] for d in reviewer["dispatches"]] == [1, 2]
    assert reviewer["dispatches"][1]["receipt"]["engine_family"] == "codex"
    assert reviewer["dispatches"][1]["completion"]["result"]["reviewed_sha"] == head
    publication = ProjectTaskService(str(state / "projects.sqlite")).publication_review(f"{PROJECT}/writer")
    assert publication.writer.evidence.artifact_ref == original and publication.reviewer is None
    assert invoke("publish") == 1


def test_rework_appends_rejection_and_reviews_new_writer_attempt(trial):
    start, invoke, setup, script, state, _, _ = trial
    accepted = False
    checkout = setup(accepted=lambda: accepted)
    check = python_check("print('retained gate')")
    assert start(checkout, "--check", check) == 1
    original = _cloud._git(checkout, "rev-parse", "HEAD")
    accepted = True
    brief = state.parent.parent / "fix.md"
    brief.write_text("Address the rejection on the same branch.", encoding="utf-8")

    assert invoke("rework", "--brief", f"@{brief}") == 0
    head = _cloud._git(checkout, "rev-parse", "HEAD")
    assert head != original
    assert "Address the rejection on the same branch." in script.prompts[2]
    assert script.prompts[2].endswith("Previous review reason\nA required check failed.")
    assert script.prompts[3].startswith(f"Review commit {head}")
    assert "| 0 | retained gate |" in script.prompts[3]
    publication = ProjectTaskService(str(state / "projects.sqlite")).publication_review(f"{PROJECT}/writer")
    assert publication.writer.attempt == publication.reviewer.attempt == 2
    assert publication.independence == "opposite-engine"
    assert publication.writer.evidence.artifact_ref == publication.reviewer.evidence.reviewed_sha == head


def test_review_recovers_after_check_failure_and_rechecks_before_dispatch(trial):
    start, invoke, setup, script, state, _, _ = trial
    checkout = setup()
    fail = python_check("print('gate failed'); raise SystemExit(7)")
    assert start(checkout, "--check", fail) == 1
    assert invoke("review") == 1
    assert len(script.prompts) == 1
    assert invoke("review", "--check", python_check("print('gate passed')")) == 0
    assert len(script.prompts) == 2
    assert "| 0 | gate passed |" in script.prompts[1]
    assert len(list(state.glob("checks-*/*.log"))) == 3
