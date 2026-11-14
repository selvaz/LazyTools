"""Merge-ready orchestration for an already submitted cloud job.

The navigator deliberately stops before merge.  Its small backend protocol keeps
provider and command execution at the edge and makes the state machine testable
without network access, credentials, or real processes.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

DEFAULT_MAX_ROUNDS = 3
BLOCKING_SEVERITIES = frozenset({"P0", "P1"})
DEFAULT_CHECKS = (
    "pytest tests -k code_bridge",
    "ruff check",
    "mypy src/lazytools/code_bridge",
)


@dataclass(frozen=True)
class Finding:
    severity: str
    message: str


@dataclass(frozen=True)
class Head:
    sha: str
    author_model: str
    pr_number: int


@dataclass(frozen=True)
class Review:
    reviewer_model: str
    findings: tuple[Finding, ...] = ()


@dataclass(frozen=True)
class CheckResult:
    command: str
    returncode: int
    output: str = ""


@dataclass
class NavigatorRecord:
    job_id: str
    status: str = "running"
    rounds: int = 0
    reviewed_heads: list[str] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    escalation: dict[str, object] | None = None
    error: str | None = None
    updated_at: float = field(default_factory=time.time)


class Backend(Protocol):
    """Provider boundary used by :class:`Navigator`."""

    def resync_and_land(self, job_id: str) -> Head:
        """Recover polling when needed, land the job, and return its PR head."""

    def review(self, head: Head) -> Review:
        """Request and wait for an independent review of exactly ``head.sha``."""

    def relaunch_fix(self, head: Head, brief: str) -> str:
        """Relaunch a fix on ``head.pr_number`` and return the new cloud job id."""

    def run_check(self, command: str) -> CheckResult:
        """Run one task-contract check in the checked-out PR workspace."""

    def save(self, record: NavigatorRecord) -> None:
        """Persist the audit record after every externally visible transition."""


class Navigator:
    """Drive cloud work to review-clean and checks-green, never to merge."""

    def __init__(self, backend: Backend, *, checks: tuple[str, ...], max_rounds: int = DEFAULT_MAX_ROUNDS):
        if max_rounds < 1:
            raise ValueError("max_rounds must be at least 1")
        if not checks:
            raise ValueError("at least one required check is needed")
        self.backend = backend
        self.checks = checks
        self.max_rounds = max_rounds

    def run(self, job_id: str) -> NavigatorRecord:
        record = NavigatorRecord(job_id=job_id)
        self.backend.save(record)
        current_job = job_id
        pr_number: int | None = None
        while record.rounds < self.max_rounds:
            head = self.backend.resync_and_land(current_job)
            if pr_number is None:
                pr_number = head.pr_number
            elif head.pr_number != pr_number:
                return self._fail(record, f"fix moved from PR #{pr_number} to PR #{head.pr_number}")
            if head.sha in record.reviewed_heads:
                return self._fail(record, f"head {head.sha} was already reviewed but did not advance")
            review = self.backend.review(head)
            record.rounds += 1
            record.reviewed_heads.append(head.sha)
            record.updated_at = time.time()
            if review.reviewer_model == head.author_model:
                return self._fail(record, "independent review used the author model")
            severe = [finding for finding in review.findings if finding.severity.upper() in BLOCKING_SEVERITIES]
            if severe:
                record.status = "escalated"
                record.escalation = {
                    "reason": "P0/P1 review finding",
                    "head": head.sha,
                    "pr_number": head.pr_number,
                    "findings": [f"{f.severity.upper()}: {f.message}" for f in severe],
                }
                record.updated_at = time.time()
                self.backend.save(record)
                return record
            if not review.findings:
                return self._checks(record)
            if record.rounds >= self.max_rounds:
                record.status = "round-cap"
                record.escalation = {
                    "reason": f"review round cap reached ({self.max_rounds})",
                    "head": head.sha,
                    "pr_number": head.pr_number,
                    "findings": [f"{f.severity}: {f.message}" for f in review.findings],
                }
                record.updated_at = time.time()
                self.backend.save(record)
                return record
            brief = self._fix_brief(head, review, record.rounds)
            current_job = self.backend.relaunch_fix(head, brief)
            self.backend.save(record)
        return self._fail(record, "review loop ended unexpectedly")

    def _checks(self, record: NavigatorRecord) -> NavigatorRecord:
        for command in self.checks:
            result = self.backend.run_check(command)
            record.checks.append(result)
            record.updated_at = time.time()
            self.backend.save(record)
            if result.returncode:
                record.status = "checks-failed"
                record.error = f"required check failed: {command}"
                self.backend.save(record)
                return record
        record.status = "merge-ready"
        record.updated_at = time.time()
        self.backend.save(record)
        return record

    def _fail(self, record: NavigatorRecord, message: str) -> NavigatorRecord:
        record.status = "failed"
        record.error = message
        record.updated_at = time.time()
        self.backend.save(record)
        return record

    @staticmethod
    def _fix_brief(head: Head, review: Review, round_number: int) -> str:
        findings = "\n".join(f"- {f.severity}: {f.message}" for f in review.findings)
        return (
            f"Fix review findings on PR #{head.pr_number}, head {head.sha}.\n"
            f"This is review round {round_number}. Keep using the same PR; do not merge or push to main.\n\n"
            f"Findings:\n{findings}\n\nRun the task contract checks and report the new head."
        )


def exit_status(record: NavigatorRecord) -> int:
    """Stable process status: zero only when the PR is merge-ready."""
    return 0 if record.status == "merge-ready" else 1


def run_reviewer_command(command: str, head: Head, *, cwd: Path) -> Review:
    """Run a configured reviewer without a shell and parse its JSON contract.

    The command receives the immutable review target through environment
    variables and must print one JSON object containing ``reviewer_model`` and
    a ``findings`` list.  This boundary is also convenient for local wrappers
    around hosted review services.
    """
    argv = shlex.split(command, posix=os.name != "nt")
    if not argv:
        raise ValueError("reviewer command cannot be empty")
    env = {
        **os.environ,
        "LAZYTOOLS_REVIEW_HEAD": head.sha,
        "LAZYTOOLS_REVIEW_PR": str(head.pr_number),
        "LAZYTOOLS_AUTHOR_MODEL": head.author_model,
    }
    completed = subprocess.run(
        argv, cwd=cwd, env=env, text=True, capture_output=True, check=False, timeout=3600
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"reviewer command exited {completed.returncode}: {detail}")
    try:
        payload = json.loads(completed.stdout)
        model = str(payload["reviewer_model"]).strip()
        raw_findings = payload.get("findings", [])
        findings = tuple(Finding(str(item["severity"]).upper(), str(item["message"])) for item in raw_findings)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("reviewer command did not emit the required JSON object") from exc
    if not model:
        raise ValueError("reviewer_model cannot be empty")
    return Review(model, findings)


def resync_and_land_cloud_job(
    store: Any, registry: Any, job_id: str, *, db_path: Path | None = None
) -> Head:
    """Recover (when necessary) and land one cloud job in a single operation.

    This is the production implementation of the backend's first transition.
    Legacy bridges made a provider transport error terminal; those records are
    resynced before ordinary refresh/landing.  No merge is performed.
    """
    from lazytools.code_bridge import _cloud, _cloud_land, _jobs

    job = _jobs.find_job(store, registry, job_id)
    if job is None:
        raise _cloud.CloudError(f"no job found matching {job_id!r}")
    if not _cloud.is_cloud(job):
        raise _cloud.CloudError(f"job {job_id!r} is not a cloud job")
    if job.get("status") == "failed" and job.get("last_poll_error") and job.get("cloud_handle"):
        job = _cloud.resync(store, registry, job)
    elif job.get("status") in _cloud.ACTIVE_STATUSES:
        job = _cloud.refresh(store, registry, job)
    if job.get("status") != "done":
        raise _cloud.CloudError(f"job {job_id!r} is {job.get('status')}, not ready to land")
    _cloud_land.land_job(store, registry, job, db_path=db_path)
    landed = _cloud._merged(store, registry, str(job["job_id"]))
    sha = str(landed.get("pushed_sha") or landed.get("result_commit") or "")
    pr_number = landed.get("pr_number")
    if not sha or not isinstance(pr_number, int):
        raise _cloud.CloudError("landed job has no pushed head or pull request")
    author_model = str(landed.get("model") or landed.get("engine") or "")
    return Head(sha=sha, author_model=author_model, pr_number=pr_number)


__all__ = [
    "BLOCKING_SEVERITIES", "DEFAULT_CHECKS", "DEFAULT_MAX_ROUNDS", "Backend", "CheckResult", "Finding", "Head",
    "Navigator", "NavigatorRecord", "Review", "exit_status", "resync_and_land_cloud_job", "run_reviewer_command",
]
