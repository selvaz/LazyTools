"""Restart-safe dispatch of a projects task graph through bridge jobs."""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from lazytools.code_bridge import _jobs, _store
from lazytools.connectors.projects.tools import _resolve_store_db
from lazytools.projects.plan_edit import _board_key
from lazytools.projects.task_fields import task_id as board_task_id
from lazytools.projects.task_service import CompletionEvidence, ProjectTaskService, TaskPrincipal
from lazytools.projects.task_store import transaction

TERMINAL = frozenset({"done", "partial", "failed", "interrupted"})


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
                 repo_roots: dict[str, Path] | None = None, worktree_root: Path | None = None):
        self.db = db
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
        base = commits[0] if commits else (branch if branch_exists else None)
        if not base:
            symbolic = subprocess.run(
                ["git", "-C", str(repo), "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
                text=True, capture_output=True, check=False,
            ).stdout.strip()
            base = symbolic.removeprefix("refs/remotes/") if symbolic else "HEAD"
        if assigned and branch_exists:
            self._git(repo, "worktree", "add", str(checkout), str(branch))
            if commits:
                self._git(checkout, "reset", "--hard", commits[0])
        else:
            self._git(repo, "worktree", "add", "-b", str(branch), str(checkout), str(base))
        for commit in commits[1:]:
            result = subprocess.run(
                ["git", "-C", str(checkout), "merge", "--no-edit", commit],
                text=True, capture_output=True, check=False,
            )
            if result.returncode:
                subprocess.run(
                    ["git", "-C", str(checkout), "merge", "--abort"],
                    text=True, capture_output=True, check=False,
                )
                subprocess.run(
                    ["git", "-C", str(repo), "worktree", "remove", "--force", str(checkout)],
                    text=True, capture_output=True, check=False,
                )
                raise RuntimeError("dependency merge conflict")
        return checkout

    def launch(self, task: dict[str, Any], claim: dict[str, Any]) -> str:
        cwd = self._checkout({**task, "task_id": claim["task_id"]})
        command = [sys.executable, "-m", "lazytools.code_bridge", "run", "--cwd", str(cwd),
                   "--root", str(cwd),
                   "--task", task["text"], "--tier", task.get("tier") or "thinking",
                   "--where", claim["effective_placement"], "--task-id", claim["task_id"],
                   "--claim-token", claim["claim_token"], "--detach", "--wait-for-slot"]
        if self.db:
            command.extend(("--db", self.db))
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

    def job(self, job_ref: str) -> dict[str, Any] | None:
        store = _store.build_store(Path(self.db) if self.db else None)
        job = _jobs.find_job(store, _store.build_job_registry(store), job_ref)
        return job

    def find(self, task_id: str, attempt: int) -> dict[str, Any] | None:
        store = _store.build_store(Path(self.db) if self.db else None)
        return next((job for job in _jobs.list_jobs(store, all_jobs=True)
                     if job.get("task_id") == task_id and job.get("task_attempt") == attempt), None)


class Dispatcher:
    def __init__(self, service: ProjectTaskService, principal: TaskPrincipal, launcher: Launcher,
                 project_id: str, *, parallel: int = 1, emit: Callable[[str], None] = print,
                 sleep: Callable[[float], None] = time.sleep,
                 task_reader: Callable[[ProjectTaskService, str], list[Task]] = _tasks,
                 clock: Callable[[], float] = time.time, orphan_grace: float = 30.0):
        if not 1 <= parallel <= 100:
            raise ValueError("--parallel must be between 1 and 100")
        self.service, self.principal, self.launcher = service, principal, launcher
        self.project_id, self.parallel, self.emit, self.sleep = project_id, parallel, emit, sleep
        self.task_reader = task_reader
        self.clock, self.orphan_grace = clock, orphan_grace
        self.reported_blocked: set[str] = set()
        self.unsupported: set[str] = set()

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
            self.service.reconcile_dispatch(self.principal, task.task_id, receipt["attempt"], "submitted",
                                            evidence_ref=f"bridge-job:{job['job_id']}", job_ref=job["job_id"])
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
        else:
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
            if once or (not ready and not running):
                return 0
            self.sleep(15)


def _dry_run(service: ProjectTaskService, project_id: str, emit: Callable[[str], None] = print,
             task_reader: Callable[[ProjectTaskService, str], list[Task]] = _tasks) -> int:
    tasks = task_reader(service, project_id)
    remaining = {task.task_id.rsplit("/", 1)[1]: set(task.data.get("dependency_ids", []))
                 for task in tasks if task.status == "todo"}
    done = {task.task_id.rsplit("/", 1)[1] for task in tasks if task.status == "done"}
    while remaining:
        wave = [identifier for identifier, dependencies in remaining.items() if dependencies <= done]
        if not wave:
            break
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
    service = ProjectTaskService(store_db)
    if args.dry_run:
        return _dry_run(service, args.project)
    repo_roots: dict[str, Path] = {}
    for mapping in args.repo_root:
        slug, separator, path = mapping.partition("=")
        if not separator or not slug or not path:
            raise ValueError("--repo-root must be SLUG=PATH")
        repo_roots[slug] = Path(path).resolve()
    launcher = BridgeLauncher(args.db, principal, args.project, repo_roots=repo_roots,
                              worktree_root=Path(args.worktree_root).resolve() if args.worktree_root else None)
    return Dispatcher(service, principal, launcher, args.project,
                      parallel=args.parallel).run(once=args.once)
