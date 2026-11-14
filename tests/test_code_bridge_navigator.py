from __future__ import annotations

import json
import sys
from dataclasses import replace

from lazytools.code_bridge._navigator import (
    CheckResult,
    Finding,
    Head,
    Navigator,
    Review,
    exit_status,
    resync_and_land_cloud_job,
    run_reviewer_command,
)


class FakeBackend:
    def __init__(self, heads, reviews, check_codes=(0,)):
        self.heads = iter(heads)
        self.reviews = iter(reviews)
        self.check_codes = iter(check_codes)
        self.saved = []
        self.fixed = []
        self.landed = []

    def resync_and_land(self, job_id):
        self.landed.append(job_id)
        return next(self.heads)

    def review(self, head):
        return next(self.reviews)

    def relaunch_fix(self, head, brief):
        self.fixed.append((head, brief))
        return f"fix-{len(self.fixed)}"

    def run_check(self, command):
        return CheckResult(command, next(self.check_codes))

    def save(self, record):
        self.saved.append(replace(record, reviewed_heads=list(record.reviewed_heads), checks=list(record.checks)))


def test_poll_failed_job_is_resynced_landed_reviewed_and_checked_by_fake_provider():
    backend = FakeBackend([Head("abc", "codex", 42)], [Review("claude")])
    result = Navigator(backend, checks=("pytest contract",)).run("poll-failed-job")

    assert result.status == "merge-ready" and exit_status(result) == 0
    assert backend.landed == ["poll-failed-job"]
    assert result.reviewed_heads == ["abc"]
    assert [check.command for check in result.checks] == ["pytest contract"]


def test_review_fix_review_stops_at_round_cap_and_records_escalation():
    backend = FakeBackend(
        [Head(f"head-{n}", "codex", 42) for n in range(3)],
        [Review("claude", (Finding("P2", f"finding {n}"),)) for n in range(3)],
    )
    result = Navigator(backend, checks=("check",)).run("job")

    assert result.status == "round-cap" and exit_status(result) == 1
    assert result.rounds == 3 and len(backend.fixed) == 2
    assert result.escalation and result.escalation["reason"] == "review round cap reached (3)"
    assert all("same PR" in brief for _, brief in backend.fixed)


def test_p0_or_p1_escalates_to_owner_without_relaunching():
    backend = FakeBackend(
        [Head("abc", "claude", 9)],
        [Review("codex", (Finding("P1", "credentials exposed"), Finding("P2", "rename")))],
    )
    result = Navigator(backend, checks=("check",)).run("job")

    assert result.status == "escalated" and result.escalation
    assert result.escalation["findings"] == ["P1: credentials exposed"]
    assert not backend.fixed


def test_each_new_head_gets_opposite_model_review_and_checks_gate_close():
    backend = FakeBackend(
        [Head("one", "codex", 7), Head("two", "codex", 7)],
        [Review("claude", (Finding("P2", "add test"),)), Review("claude")],
        check_codes=(0, 1, 0),
    )
    result = Navigator(backend, checks=("unit", "lint", "never-run")).run("job")

    assert result.reviewed_heads == ["one", "two"]
    assert result.status == "checks-failed" and exit_status(result) == 1
    assert [c.command for c in result.checks] == ["unit", "lint"]


def test_same_model_review_is_rejected():
    backend = FakeBackend([Head("abc", "codex", 1)], [Review("codex")])
    result = Navigator(backend, checks=("check",)).run("job")
    assert result.status == "failed"
    assert result.error == "independent review used the author model"


def test_fix_must_land_on_same_pr():
    backend = FakeBackend(
        [Head("one", "codex", 1), Head("two", "codex", 2)],
        [Review("claude", (Finding("P2", "fix"),))],
    )
    result = Navigator(backend, checks=("check",)).run("job")
    assert result.status == "failed"
    assert result.error == "fix moved from PR #1 to PR #2"


def test_configured_reviewer_command_receives_every_head(tmp_path):
    script = tmp_path / "reviewer.py"
    script.write_text(
        "import json, os\n"
        "print(json.dumps({'reviewer_model': 'claude', 'findings': "
        "[{'severity': 'P2', 'message': os.environ['LAZYTOOLS_REVIEW_HEAD']}]}))\n",
        encoding="utf-8",
    )
    review = run_reviewer_command(f'"{sys.executable}" "{script}"', Head("deadbeef", "codex", 12), cwd=tmp_path)
    assert json.loads(json.dumps(review.findings[0].message)) == "deadbeef"
    assert review.reviewer_model == "claude"


def test_cloud_transition_resyncs_legacy_poll_failure_then_lands(monkeypatch):
    job = {
        "job_id": "deadbeef",
        "where": "cloud",
        "status": "failed",
        "last_poll_error": "temporary transport error",
        "cloud_handle": "task-1",
        "engine": "codex",
    }
    calls = []

    monkeypatch.setattr("lazytools.code_bridge._jobs.find_job", lambda store, registry, ref: job)

    def resync(store, registry, value):
        calls.append("resync")
        return {**value, "status": "done"}

    monkeypatch.setattr("lazytools.code_bridge._cloud.resync", resync)
    monkeypatch.setattr(
        "lazytools.code_bridge._cloud_land.land_job",
        lambda store, registry, value, db_path=None: calls.append("land"),
    )
    monkeypatch.setattr(
        "lazytools.code_bridge._cloud._merged",
        lambda store, registry, job_id: {**job, "pushed_sha": "abc123", "pr_number": 8},
    )

    head = resync_and_land_cloud_job(object(), object(), "deadbeef")
    assert calls == ["resync", "land"]
    assert head == Head("abc123", "codex", 8)
