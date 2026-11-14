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
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

DEFAULT_MAX_ROUNDS = 3
BLOCKING_SEVERITIES = frozenset({"P0", "P1"})
KNOWN_SEVERITIES = frozenset({"P0", "P1", "P2", "P3"})
REVIEW_MARKER = "<!-- independent-review v1 -->"
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
            author = head.author_model.strip().casefold()
            reviewer = review.reviewer_model.strip().casefold()
            if not author:
                return self._fail(record, "author model is unknown; independence cannot be verified")
            if reviewer == author:
                return self._fail(record, "independent review used the author model")
            severe = [finding for finding in review.findings
                      if finding.severity.upper() in BLOCKING_SEVERITIES
                      or finding.severity.upper() not in KNOWN_SEVERITIES]
            if severe:
                record.status = "escalated"
                record.escalation = {
                    "reason": "blocking or unrecognised review severity",
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
    return 0 if record.status == "merge-ready" else (3 if record.status in {"escalated", "round-cap"} else 1)


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
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("reviewer command did not emit the required JSON object") from exc
    if "findings" not in payload or not isinstance(payload["findings"], list):
        raise ValueError("reviewer output must contain a findings list")
    try:
        findings = tuple(Finding(str(item["severity"]).upper(), str(item["message"]))
                         for item in payload["findings"])
    except (KeyError, TypeError) as exc:
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
        waited = _cloud.wait_job(lambda: store, str(job["job_id"]), timeout=_cloud.DEFAULT_CLOUD_WAIT_TIMEOUT,
                                 interval=5.0)
        if waited is not None:
            job = waited
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


class ProductionBackend:
    """GitHub/cloud-backed navigator boundary, isolated in temporary clones/worktrees."""

    def __init__(self, *, repo: str, pr_number: int, reviewer_cmd: str, fix_engine: str,
                 db_path: Path | None = None):
        from lazytools.code_bridge import _gh
        self.repo, self.pr_number, self.reviewer_cmd = repo, pr_number, reviewer_cmd
        self.fix_engine, self.db_path = fix_engine, db_path
        self._tmp = tempfile.TemporaryDirectory(prefix="lazytools-navigate-")
        self.root = Path(self._tmp.name) / "repo"
        _gh.run_gh("repo", "clone", repo, str(self.root), "--", "--no-checkout")
        self._head: Head | None = None
        self._check_dir: Path | None = None

    def resync_and_land(self, job_id: str) -> Head:
        from lazytools.code_bridge import _gh, _store
        if job_id:
            store = _store.build_store(self.db_path)
            head = resync_and_land_cloud_job(store, _store.build_job_registry(store), job_id, db_path=self.db_path)
        else:
            view = _gh.pr_view(self.repo, self.pr_number)
            sha = str(view.get("headRefOid") or "")
            if not sha:
                raise RuntimeError(f"PR #{self.pr_number} has no head SHA")
            head = Head(sha, self.fix_engine, self.pr_number)
        self._head, self._check_dir = head, None
        return head

    def review(self, head: Head) -> Review:
        from lazytools.code_bridge import _gh
        review = run_reviewer_command(self.reviewer_cmd, head, cwd=self.root)
        comments = _gh._json("api", f"repos/{self.repo}/issues/{self.pr_number}/comments")
        if not any(REVIEW_MARKER in str(c.get("body") or "") for c in comments if isinstance(c, dict)):
            raise RuntimeError(f"reviewer did not post {REVIEW_MARKER}")
        inline = _gh._json("api", f"repos/{self.repo}/pulls/{self.pr_number}/comments")
        findings = list(review.findings)
        for item in inline if isinstance(inline, list) else []:
            if not isinstance(item, dict) or str(item.get("commit_id") or "") != head.sha:
                continue
            login = str((item.get("user") or {}).get("login") or "").casefold()
            if "codex" not in login:
                continue
            body = str(item.get("body") or "").strip()
            severity = body.split(":", 1)[0].strip().upper()
            findings.append(Finding(severity, body.split(":", 1)[-1].strip()))
        return Review(review.reviewer_model, tuple(findings))

    def relaunch_fix(self, head: Head, brief: str) -> str:
        from lazytools.code_bridge import _cloud
        job = _cloud.submit_job(engine=self.fix_engine, cwd=self.root, task=brief, pr=head.pr_number,
                                land=True, db_path=self.db_path, approvals=("push",))
        return str(job["job_id"])

    def run_check(self, command: str) -> CheckResult:
        if self._head is None:
            raise RuntimeError("cannot check before resolving a PR head")
        if self._check_dir is None:
            self._check_dir = Path(self._tmp.name) / "check"
            subprocess.run(["git", "-C", str(self.root), "fetch", "origin", self._head.sha], check=True,
                           capture_output=True, text=True)
            subprocess.run(["git", "-C", str(self.root), "worktree", "add", "--detach",
                            str(self._check_dir), self._head.sha], check=True, capture_output=True, text=True)
        proc = subprocess.run(command, cwd=self._check_dir, shell=True, text=True, capture_output=True)
        return CheckResult(command, proc.returncode, proc.stdout + proc.stderr)

    def save(self, record: NavigatorRecord) -> None:
        (Path(self._tmp.name) / "navigator.json").write_text(
            json.dumps(record, default=lambda value: value.__dict__, indent=2), encoding="utf-8")


def add_navigate_parser(subparsers: Any) -> None:
    """Register the navigator as a self-contained CLI command."""
    parser = subparsers.add_parser("navigate", help="Review/fix a PR until merge-ready or escalated.")
    parser.add_argument("--repo", required=True, metavar="OWNER/REPO")
    parser.add_argument("--pr", required=True, type=int, metavar="N")
    parser.add_argument("--reviewer-cmd", required=True)
    parser.add_argument("--checks", action="append", required=True, metavar="CMD")
    parser.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS)
    parser.add_argument("--fix-engine", choices=("codex", "claude"), default="codex")
    parser.add_argument("--db", default=None)
    parser.set_defaults(func=_cmd_navigate)


def _cmd_navigate(args: Any) -> int:
    try:
        backend = ProductionBackend(repo=args.repo, pr_number=args.pr, reviewer_cmd=args.reviewer_cmd,
                                    fix_engine=args.fix_engine,
                                    db_path=Path(args.db).expanduser() if args.db else None)
        record = Navigator(backend, checks=tuple(args.checks), max_rounds=args.max_rounds).run("")
        print(json.dumps(record, default=lambda value: value.__dict__))
        return exit_status(record)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


__all__ = [
    "BLOCKING_SEVERITIES", "DEFAULT_CHECKS", "DEFAULT_MAX_ROUNDS", "Backend", "CheckResult", "Finding", "Head",
    "Navigator", "NavigatorRecord", "ProductionBackend", "Review", "add_navigate_parser", "exit_status", "resync_and_land_cloud_job", "run_reviewer_command",
]
