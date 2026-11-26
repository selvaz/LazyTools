"""Restart-safe dispatch of a projects task graph through bridge jobs."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from lazytools.code_bridge import _jobs, _routing, _store
from lazytools.connectors.projects.tools import _resolve_store_db
from lazytools.projects.plan_edit import _board_key
from lazytools.projects.task_fields import task_id as board_task_id
from lazytools.projects.task_service import CompletionEvidence, PlacementEvidence, ProjectTaskService, TaskPrincipal
from lazytools.projects.task_store import TaskServiceError, transaction

TERMINAL = frozenset({"done", "partial", "failed", "interrupted"})
# Engine crashes (not task failures): retried once with a fresh attempt.
COMMIT_EXCLUDES = ("*.log", "**/__pycache__", ".pytest_cache", "**/.pytest_cache")
RETRYABLE_FAILURES = ("App Server exited", "outcome is unknown", "3221226505", "0xC0000409",
                      "no new commit since the task started")


@dataclass(frozen=True)
class Task:
    task_id: str
    revision: str
    status: str
    data: dict[str, Any]
    dispatch: dict[str, Any] | None


class Launcher(Protocol):
    def launch(self, task: dict[str, Any], claim: dict[str, Any]) -> str: ...
    def job(self, job_ref: str) -> dict[str, Any] | None: ...
    def find(self, task_id: str, attempt: int) -> dict[str, Any] | None: ...


def _tasks(service: ProjectTaskService, project_id: str) -> list[Task]:
    with transaction(service.path) as store:
        board = store.read(_board_key(project_id), {"tasks": []})
        return [Task(f"{project_id}/{board_task_id(data, index)}", data.get("revision", "0"),
                     data.get("status", "todo"), data,
                     data.get("dispatches", [None])[-1] if data.get("dispatches") else None)
                for index, data in enumerate(board["tasks"])]


class BridgeLauncher:
    """Launch the existing CLI and inspect its durable job records."""
    def __init__(self, db: str | None, principal: TaskPrincipal, project_id: str, *,
                 repo_roots: dict[str, Path] | None = None, worktree_root: Path | None = None,
                 registry_db: str | None = None, launch_mode: str | None = None):
        self.db = db
        self.registry_db = registry_db
        self.launch_mode = launch_mode
        self.conflicts: dict[str, str] = {}
        self.principal = principal
        self.project_id = project_id
        self.repo_roots = repo_roots or {}
        self.worktree_root = worktree_root

    @staticmethod
    def _git(repo: Path, *args: str) -> str:
        result = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
        return result.stdout.strip()

    def _checkout(self, task: dict[str, Any]) -> Path:
        repo = self.repo_roots.get(str(task.get("repo"))) or Path.cwd()
        task_name = str(task["task_id"]).rsplit("/", 1)[-1]
        safe_project = re.sub(r"[^A-Za-z0-9_.-]+", "-", self.project_id)
        safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "-", task_name)
        root = self.worktree_root or repo.parent / "_dispatch"
        checkout = root / safe_project / safe_task
        if checkout.exists():
            return checkout
        checkout.parent.mkdir(parents=True, exist_ok=True)
        dependencies = task.get("dependencies", [])
        commits = [str(item["artifact_ref"]) for item in dependencies if item.get("artifact_ref")]
        assigned = task.get("assigned_branch")
        branch = assigned or f"dispatch/{safe_project}/{safe_task}"
        branch_exists = subprocess.run(
            ["git", "-C", str(repo), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            check=False,
        ).returncode == 0
        base = commits[0] if commits else (f"origin/{branch}" if branch_exists else None)
        if not base:
            symbolic = subprocess.run(
                ["git", "-C", str(repo), "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
                text=True, capture_output=True, check=False,
            ).stdout.strip()
            base = symbolic.removeprefix("refs/remotes/") if symbolic else "HEAD"
        # A local clone, not a linked worktree: its .git lives inside the checkout, so a
        # sandboxed agent can commit there (a linked worktree keeps its index and merge
        # state under the main repository, outside the sandbox).
        self._git(repo, "clone", "--quiet", "--local", "--no-checkout", str(repo), str(checkout))
        origin = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"],
                                text=True, capture_output=True, check=False).stdout.strip()
        if origin:
            self._git(checkout, "remote", "set-url", "origin", origin)  # the claim checks the repo slug
        self._git(checkout, "remote", "add", "dispatch-source", str(repo))
        for sibling in sorted(path for path in checkout.parent.iterdir() if path != checkout and (path / ".git").is_dir()):
            subprocess.run(["git", "-C", str(checkout), "fetch", "--quiet", str(sibling), "+HEAD:refs/dispatch/" + sibling.name],
                           text=True, capture_output=True, check=False)
        self._git(checkout, "checkout", "--quiet", "-B", str(branch), str(base))
        self._git(checkout, "config", "dispatch.branch", str(branch))  # publication checks it
        for index, commit in enumerate(commits[1:], start=1):
            result = subprocess.run(
                ["git", "-C", str(checkout), "merge", "--no-edit", commit],
                text=True, capture_output=True, check=False,
            )
            if result.returncode:
                unmerged = self._git(checkout, "diff", "--name-only", "--diff-filter=U")
                if index == len(commits) - 1 and unmerged:
                    # Last dependency: leave the conflicted merge for the task's agent to
                    # resolve and conclude with an ordinary commit (that resolution is the
                    # task's first piece of work, so the base is set before the merge).
                    self._git(checkout, "tag", "-f", "dispatch-base", "HEAD")
                    self.conflicts[str(task["task_id"])] = commit
                    return checkout
                subprocess.run(
                    ["git", "-C", str(checkout), "merge", "--abort"],
                    text=True, capture_output=True, check=False,
                )
                shutil.rmtree(checkout, ignore_errors=True)
                detail = (result.stderr or result.stdout).strip().splitlines()
                raise RuntimeError("dependency merge conflict" if unmerged else
                                   f"dependency merge failed: {detail[-1] if detail else 'git merge error'}")
        # Set after dependency merges: the task must add its own commits on top.
        self._git(checkout, "tag", "-f", "dispatch-base", "HEAD")
        return checkout

    def launch(self, task: dict[str, Any], claim: dict[str, Any]) -> str:
        cwd = self._checkout({**task, "task_id": claim["task_id"]})
        text = str(task["text"])
        if conflict := self.conflicts.pop(str(claim["task_id"]), None):
            text = (f"This checkout is in the middle of merging dependency commit {conflict} and has "
                    "conflicts (see `git status`). First resolve every conflict so the changes of both "
                    "sides are kept, run the tests, then `git add` the files and `git commit` to "
                    "conclude the merge. Then do the task below.\n\n" + text)
        command = [sys.executable, "-m", "lazytools.code_bridge", "run", "--cwd", str(cwd),
                   "--root", str(cwd),
                   "--task", text, "--tier", task.get("tier") or "thinking",
                   "--where", claim["effective_placement"], "--task-id", claim["task_id"],
                   "--claim-token", claim["claim_token"], "--detach", "--wait-for-slot",
                   # Tasks come from a reviewed, promoted plan; the brief shape gate does not apply.
                   "--no-brief-check"]
        # Pin the claimed engine so quota routing only picks a model within it;
        # another engine would fail the claim's engine check.
        engine_family = claim.get("engine_family")
        if engine_family:
            command.extend(("--engine", "claude" if engine_family == "claude_code" else str(engine_family)))
        if self.db:
            command.extend(("--db", self.db))
        if self.registry_db:
            command.extend(("--registry-db", self.registry_db))
        if self.launch_mode:
            command.extend(("--launch-mode", self.launch_mode))
        if task.get("requested_profile"):
            command.extend(("--profile", task["requested_profile"]))
        if task.get("requested_network") is not None:
            command.extend(("--network", "on" if task["requested_network"] else "off"))
        env = {**os.environ, "LAZYTOOLS_CODE_BRIDGE_TASK_PRINCIPAL": self.principal.model_dump_json()}
        result = subprocess.run(command, text=True, capture_output=True, check=False, env=env)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip() or f"bridge exited {result.returncode}")
        candidates = [line.strip() for line in result.stdout.splitlines() if len(line.strip()) == 32]
        if not candidates:
            raise RuntimeError("bridge did not return a job id")
        return candidates[-1]

    def pending(self) -> list[dict[str, Any]]:
        """Pending approval tickets in the shared queue (same view as `pending --json`)."""
        command = [sys.executable, "-m", "lazytools.code_bridge", "pending", "--json"]
        if self.db:
            command.extend(("--db", self.db))
        if self.registry_db:
            command.extend(("--registry-db", self.registry_db))
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        if result.returncode:
            return []
        try:
            data = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return []
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def job(self, job_ref: str) -> dict[str, Any] | None:
        store = _store.build_store(Path(self.db) if self.db else None)
        job = _jobs.find_job(store, _store.build_job_registry(store), job_ref)
        return job

    def find(self, task_id: str, attempt: int) -> dict[str, Any] | None:
        store = _store.build_store(Path(self.db) if self.db else None)
        return next((job for job in _jobs.list_jobs(store, all_jobs=True)
                     if job.get("task_id") == task_id and job.get("task_attempt") == attempt), None)


def _conclude_workspace(cwd: Path, task_id: str) -> tuple[str | None, str | None]:
    """Make a finished job's checkout committed, or say why it is not.

    A sandboxed agent cannot write a linked worktree's merge metadata, so a merge the
    dispatcher left for it to resolve stays open even when every conflict is fixed. If
    no conflict markers remain, conclude that merge here (the dispatcher runs outside
    the sandbox). Any other uncommitted change means the task is not done.
    """
    def git(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(cwd), *argv], text=True, capture_output=True, check=False)

    if not cwd.is_dir():
        return None, None
    dirty = [line[3:] for line in git("status", "--porcelain").stdout.splitlines() if line.strip()]
    if not dirty:
        return None, None
    markers = git("grep", "-l", "-E", "^(<<<<<<<|>>>>>>>) ", "--", ".").stdout.split()
    if markers:
        return f"unresolved conflict markers in {', '.join(markers[:5])}", None
    merging = git("rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0
    # Never stage tool litter (the Codex CLI writes error.log with account details),
    # and unstage any the agent already staged.
    git("add", "-A", "--", ".", *(f":(exclude){pattern}" for pattern in COMMIT_EXCLUDES))
    staged_litter = [path for path in git("diff", "--cached", "--name-only").stdout.splitlines() if _is_litter(path)]
    if staged_litter:
        git("reset", "-q", "--", *staged_litter)
    if not git("diff", "--cached", "--name-only").stdout.strip() and not merging:
        return f"job ended with {len(dirty)} uncommitted path(s), all excluded as tool litter", None
    message = ["commit", "-q", "--no-edit"] if merging else [
        "commit", "-q", "-m", f"{task_id.rsplit('/', 1)[-1]}: changes from the finished task",
        "-m", "Committed by the dispatcher: the engine sandbox keeps .git read-only."]
    commit = git(*message)
    if commit.returncode:
        return f"could not commit the finished task: {(commit.stderr or commit.stdout).strip()[:200]}", None
    left = [line[3:] for line in git("status", "--porcelain").stdout.splitlines()
            if line.strip() and not _is_litter(line[3:])]
    if left:
        return f"checkout still dirty after the commit: {', '.join(left[:5])}", None
    return None, git("rev-parse", "HEAD").stdout.strip() or None


def _is_litter(path: str) -> bool:
    parts = path.strip().strip('"').replace("\\", "/").split("/")
    return (parts[-1].endswith(".log") or "__pycache__" in parts or ".pytest_cache" in parts)


def _has_new_commit(cwd: Path) -> bool:
    """True when the checkout has commits after its `dispatch-base` tag (or has no tag)."""
    count = subprocess.run(["git", "-C", str(cwd), "rev-list", "--count", "dispatch-base..HEAD"],
                           text=True, capture_output=True, check=False)
    return count.returncode != 0 or count.stdout.strip() != "0"


def _publish(cwd: Path) -> tuple[str | None, str | None]:
    """Copy an accepted task's branch from its clone back into the source repository.

    Returns (branch, error). Only the branch the dispatcher created is published, and
    only as a fast-forward: a push never overwrites commits made elsewhere.
    """
    def git(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(cwd), *argv], text=True, capture_output=True, check=False)

    expected = git("config", "--get", "dispatch.branch").stdout.strip()
    if git("remote", "get-url", "dispatch-source").returncode != 0 or not expected:
        return None, None  # not a dispatcher clone (e.g. an assigned external checkout)
    current = git("branch", "--show-current").stdout.strip()
    if current != expected:
        return None, f"checkout is on {current or 'a detached HEAD'}, expected {expected}"
    pushed = git("push", "--quiet", "dispatch-source", f"HEAD:refs/heads/{expected}")
    if pushed.returncode:
        return None, f"publish failed: {(pushed.stderr or pushed.stdout).strip()[:200]}"
    return expected, None


class Dispatcher:
    def __init__(self, service: ProjectTaskService, principal: TaskPrincipal, launcher: Launcher,
                 project_id: str, *, parallel: int = 1, emit: Callable[[str], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 task_reader: Callable[[ProjectTaskService, str], list[Task]] = _tasks,
                 clock: Callable[[], float] = time.time, orphan_grace: float = 30.0,
                 router: Callable[[str], str | None] | None = None, max_retries: int = 1):
        if not 1 <= parallel <= 100:
            raise ValueError("--parallel must be between 1 and 100")
        self.service, self.principal, self.launcher = service, principal, launcher
        self.project_id, self.parallel, self.sleep = project_id, parallel, sleep
        # Flush each event: the dispatcher is usually piped into a log or a watcher.
        self.emit: Callable[[str], None] = emit or (lambda line: print(line, flush=True))
        self.task_reader = task_reader
        self.clock, self.orphan_grace = clock, orphan_grace
        self.router = router
        self.reported_blocked: set[str] = set()
        self.unsupported: set[str] = set()
        self.waiting: set[str] = set()
        self.retries: dict[str, int] = {}
        self.seen_tickets: set[str] = set()
        self.max_retries = max_retries

    def _surface_approvals(self, tasks: list[Task]) -> None:
        """Put every pending ticket of a running task into the event stream, once.

        The dispatcher does not decide tickets; it makes them visible to whoever
        orchestrates (`lazytools-code-bridge approve|reject <id>`), so a job never
        waits on a ticket nobody has seen.
        """
        pending = getattr(self.launcher, "pending", None)
        if pending is None:
            return
        by_job = {str((task.dispatch or {}).get("job_ref")): task.task_id
                  for task in tasks if task.status == "claimed" and (task.dispatch or {}).get("job_ref")}
        if not by_job:
            return
        for ticket in pending():
            approval_id = str(ticket.get("approval_id") or "")
            task_id = by_job.get(str(ticket.get("job_id") or ""))
            if not approval_id or task_id is None or approval_id in self.seen_tickets:
                continue
            self.seen_tickets.add(approval_id)
            command = " ".join(str(ticket.get("command") or ticket.get("gist") or "").split())[:200]
            reason = " ".join(str(ticket.get("reason") or "").split())[:160]
            self.emit(f"approval {task_id} {approval_id} {command} | {reason}")

    def _place(self, tasks: list[Task]) -> None:
        """Record local placement evidence for todo tasks whose dependencies are done.

        `next_ready` only returns tasks with fresh placement evidence; the quota router
        picks the engine family for the task's tier here, and the launch later pins it.
        """
        if self.router is None:
            return
        status = {task.task_id.rsplit("/", 1)[1]: task.status for task in tasks}
        now = self.clock()
        picks: dict[str, str | None] = {}
        for task in tasks:
            data = task.data
            if (task.status != "todo" or data.get("requested_placement", "local") != "local"
                    or data.get("review_of") or data.get("acceptance_contract")
                    or any(status.get(dep) != "done" for dep in data.get("dependency_ids", []))):
                continue
            record = data.get("placement_evidence", {}).get("local", {})
            evidence = record.get("evidence", {})
            if record.get("revision") == task.revision and evidence.get("expires_at", 0) > now + 60:
                continue
            tier = data.get("tier") or "thinking"
            if tier not in picks:
                try:
                    picks[tier] = self.router(tier)
                except Exception as exc:
                    picks[tier] = None
                    self.emit(f"waiting {task.task_id} routing failed: {str(exc).replace(chr(10), ' ')}")
            engine = picks[tier]
            if engine is None:
                if task.task_id not in self.waiting:
                    self.emit(f"waiting {task.task_id} no eligible engine for tier {tier}")
                    self.waiting.add(task.task_id)
                continue
            self.waiting.discard(task.task_id)
            self.service.record_placement(task.task_id, task.revision, PlacementEvidence(
                placement="local", eligible=True, reason=f"quota router picked {engine} for tier {tier}",
                reference=f"dispatch-{task.task_id}-{task.revision}-{int(now)}",
                expires_at=now + self.service.lease_seconds + 60, engine_family=engine,
            ))
            self.emit(f"placed {task.task_id} local {engine} tier={tier}")

    def _blocked(self, tasks: list[Task]) -> None:
        failed = {task.task_id.rsplit("/", 1)[1] for task in tasks if task.status == "failed"}
        for task in tasks:
            if (task.status == "todo" and failed.intersection(task.data.get("dependency_ids", []))
                    and task.task_id not in self.reported_blocked):
                self.emit(f"blocked {task.task_id}")
                self.reported_blocked.add(task.task_id)

    def _settle(self, task: Task) -> bool:
        dispatch = task.dispatch or {}
        receipt = dispatch.get("receipt", {})
        job_ref = dispatch.get("job_ref")
        job = self.launcher.job(job_ref) if job_ref else self.launcher.find(task.task_id, receipt.get("attempt", 0))
        if job and not job_ref:
            try:
                self.service.reconcile_dispatch(self.principal, task.task_id, receipt["attempt"], "submitted",
                                                evidence_ref=f"bridge-job:{job['job_id']}", job_ref=job["job_id"])
            except TaskServiceError as exc:
                if "provider boundary" in str(exc):
                    return False  # the child is still waiting for an engine slot
                raise
            job_ref = str(job["job_id"])
        if not job:
            created = float(dispatch.get("created_at", 0))
            expired = float(receipt.get("lease_expires_at", float("inf"))) <= self.clock()
            if expired and self.clock() - created >= self.orphan_grace:
                dispatch_state = dispatch.get("state", "intent")
                observation: Literal["absent", "terminated"] = "terminated" if dispatch.get("job_ref") else "absent"
                self.service.reconcile_dispatch(
                    self.principal, task.task_id, receipt["attempt"], observation,
                    evidence_ref=None if dispatch_state == "intent" else "dispatcher:no-job-after-grace",
                    job_ref=dispatch.get("job_ref"),
                )
                self.emit(f"failed {task.task_id} no job after dispatch grace period")
                return True
            return False
        if job.get("status") not in TERMINAL:
            return False
        accepted = job.get("status") == "done"
        if accepted and job.get("cwd"):
            workspace_error, merge_commit = _conclude_workspace(Path(str(job["cwd"])), task.task_id)
            if workspace_error:
                accepted = False
                job = {**job, "error": workspace_error}
            elif merge_commit:
                job = {**job, "final_commit": merge_commit}
                self.emit(f"committed {task.task_id} {merge_commit[:12]} (engine sandbox keeps .git read-only)")
            if accepted and not _has_new_commit(Path(str(job["cwd"]))):
                accepted = False
                job = {**job, "error": "no new commit since the task started"}
        verdict = job.get("review_verdict") or {}
        reviewed_sha = job.get("reviewed_sha") or verdict.get("reviewed_sha")
        review_passed = bool(job.get("review_passed", verdict.get("passed", False)))
        review_independence = job.get("review_independence") or verdict.get("independence")
        verification_id = job.get("verification_id")
        needs_evidence = accepted and (
            (task.data.get("review_of") and (not reviewed_sha or not review_passed))
            or (task.data.get("contract_id") and not verification_id)
        )
        expired = float(receipt.get("lease_expires_at", float("inf"))) <= self.clock()
        if needs_evidence:
            accepted = False
            failure_reason = "needs review evidence"
        elif expired:
            accepted = False
            failure_reason = "claim lease expired"
        else:
            failure_reason = str(job.get("error") or job.get("status") or "job failed").replace("\n", " ")
        # Publish only after the lease and evidence checks; a failed publish fails the task.
        if accepted and job.get("cwd"):
            published, publish_error = _publish(Path(str(job["cwd"])))
            if publish_error:
                accepted = False
                failure_reason = publish_error
            elif published:
                self.emit(f"published {task.task_id} {published}")
        state: Literal["accepted", "blocked"] = "accepted" if accepted else "blocked"
        reference = f"bridge-job:{job_ref}:{job.get('status')}"
        evidence = CompletionEvidence(reference=reference, job_ref=str(job_ref), executor=receipt["executor"],
            engine_family=receipt["engine_family"], state=state,
            artifact_ref=job.get("final_commit") or job.get("candidate_commit"),
            verification_id=verification_id, reviewed_sha=reviewed_sha, review_passed=review_passed,
            review_independence=review_independence,
            outcome="success" if accepted else "task_failure")
        self.service.record_completion_evidence(self.principal, task.task_id, receipt["attempt"], evidence)
        complete_options = {"reconcile_failure": True} if expired else {}
        self.service.complete(self.principal, task.task_id, receipt["claim_token"], reference, state, reference,
                              "success" if accepted else "task_failure", **complete_options)
        if accepted:
            self.emit(f"done {task.task_id}")
            return True
        if (self.retries.get(task.task_id, 0) < self.max_retries
                and any(marker in failure_reason for marker in RETRYABLE_FAILURES)):
            current = next((item for item in self.task_reader(self.service, self.project_id)
                            if item.task_id == task.task_id), None)
            if current is not None:
                self.service.prepare_attempt(self.principal, task.task_id, current.revision,
                                             f"retry after engine crash: {failure_reason[:200]}")
                self.retries[task.task_id] = self.retries.get(task.task_id, 0) + 1
                self.emit(f"retry {task.task_id} engine crash: {failure_reason[:120]}")
                return True
        self.emit(f"failed {task.task_id} {failure_reason}")
        return True

    def pass_once(self) -> tuple[int, int]:
        tasks = self.task_reader(self.service, self.project_id)
        for task in tasks:
            if task.status == "claimed":
                try:
                    self._settle(task)
                except Exception as exc:
                    self.emit(f"failed {task.task_id} {str(exc).replace(chr(10), ' ')}")
        tasks = self.task_reader(self.service, self.project_id)
        self._surface_approvals(tasks)
        self._place(tasks)
        tasks = self.task_reader(self.service, self.project_id)
        running = sum(task.status == "claimed" for task in tasks)
        ready = self.service.next_ready(self.principal, self.project_id, limit=100)["tasks"]
        by_id = {task.task_id: task for task in tasks}
        running_branches = {
            str(task.data["assigned_branch"])
            for task in tasks
            if task.status == "claimed" and task.data.get("assigned_branch")
        }
        for summary in ready:
            if running >= self.parallel:
                break
            board_task = by_id.get(summary["task_id"])
            if board_task is None:
                self.emit(f"failed {summary['task_id']} task is missing from board")
                continue
            data = board_task.data
            self.emit(f"ready {summary['task_id']}")
            if summary.get("effective_placement") == "cloud":
                reason = "cloud placement not supported by dispatch v1"
                self.unsupported.add(summary["task_id"])
                self.emit(f"failed {summary['task_id']} {reason}")
                continue
            if data.get("review_of") or data.get("contract_id"):
                reason = "review/contract tasks not supported by dispatch v1"
                self.unsupported.add(summary["task_id"])
                self.emit(f"failed {summary['task_id']} {reason}")
                continue
            branch = data.get("assigned_branch")
            if branch and str(branch) in running_branches:
                self.emit(f"failed {summary['task_id']} assigned branch already running")
                continue
            attempt = len(data.get("dispatches", [])) + 1
            try:
                result = self.service.claim(
                    self.principal, summary["task_id"], summary["revision"],
                    f"dispatch:{summary['task_id']}:{summary['revision']}:{attempt}",
                )
            except Exception as exc:
                self.emit(f"failed {summary['task_id']} {str(exc).replace(chr(10), ' ')}")
                continue
            if not result.get("claim_active", True):
                self.emit(f"failed {summary['task_id']} claim is not active")
                continue
            claim = result["claim"]
            self.emit(f"claimed {summary['task_id']}")
            try:
                job_ref = self.launcher.launch({**summary, **data}, claim)
            except Exception as exc:
                reason = str(exc).replace("\n", " ")
                try:
                    self.service.fail_local_launch(
                        self.principal, summary["task_id"], claim["claim_token"], reason,
                    )
                except Exception as release_exc:
                    reason = f"{reason}; claim release failed: {str(release_exc).replace(chr(10), ' ')}"
                self.emit(f"failed {summary['task_id']} {reason}")
            else:
                self.emit(f"launched {job_ref}")
                running += 1
                if branch:
                    running_branches.add(str(branch))
        tasks = self.task_reader(self.service, self.project_id)
        self._blocked(tasks)
        return sum(task.status == "todo" for task in tasks), sum(task.status == "claimed" for task in tasks)

    def run(self, *, once: bool = False) -> int:
        while True:
            _, running = self.pass_once()
            ready = [task for task in self.service.next_ready(
                self.principal, self.project_id, limit=100
            )["tasks"] if task["task_id"] not in self.unsupported]
            if once or (not ready and not running and not self.waiting):
                return 0
            self.sleep(15)


def _dry_run(service: ProjectTaskService, project_id: str, emit: Callable[[str], None] = print,
             task_reader: Callable[[ProjectTaskService, str], list[Task]] = _tasks) -> int:
    tasks = task_reader(service, project_id)
    remaining = {task.task_id.rsplit("/", 1)[1]: set(task.data.get("dependency_ids", []))
                 for task in tasks if task.status == "todo"}
    done = {task.task_id.rsplit("/", 1)[1] for task in tasks if task.status == "done"}
    number = 0
    while remaining:
        wave = [identifier for identifier, dependencies in remaining.items() if dependencies <= done]
        if not wave:
            break
        number += 1
        emit(f"wave {number}: {' '.join(wave)}")
        for identifier in wave:
            emit(f"ready {project_id}/{identifier}")
            done.add(identifier)
            del remaining[identifier]
    for identifier in remaining:
        emit(f"blocked {project_id}/{identifier}")
    return 0


def dispatch(args: argparse.Namespace) -> int:
    store_db = _resolve_store_db(None)
    principal = TaskPrincipal(actor="code-bridge-dispatch", role="orchestrator", projects=(args.project,))
    service = ProjectTaskService(store_db, lease_seconds=max(1, int(getattr(args, "lease_minutes", 60))) * 60)
    if args.dry_run:
        return _dry_run(service, args.project)
    repo_roots: dict[str, Path] = {}
    for mapping in args.repo_root:
        slug, separator, path = mapping.partition("=")
        if not separator or not slug or not path:
            raise ValueError("--repo-root must be SLUG=PATH")
        repo_roots[slug] = Path(path).resolve()
    registry_db = str(args.registry_db) if getattr(args, "registry_db", None) else None
    launcher = BridgeLauncher(args.db, principal, args.project, repo_roots=repo_roots,
                              worktree_root=Path(args.worktree_root).resolve() if args.worktree_root else None,
                              registry_db=registry_db, launch_mode=getattr(args, "launch_mode", None))
    db_path = Path(args.db) if args.db else None

    def router(tier: str) -> str | None:
        return _routing.choose(tier, cwd=os.getcwd(), db_path=db_path).decision.provider

    return Dispatcher(service, principal, launcher, args.project,
                      parallel=args.parallel, router=router).run(once=args.once)
