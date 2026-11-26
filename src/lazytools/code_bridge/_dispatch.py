"""Restart-safe dispatch of a projects task graph through bridge jobs."""
from __future__ import annotations

import argparse
import os
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
    def __init__(self, db: str | None, principal: TaskPrincipal):
        self.db = db
        self.principal = principal

    def launch(self, task: dict[str, Any], claim: dict[str, Any]) -> str:
        command = [sys.executable, "-m", "lazytools.code_bridge", "run", "--cwd", os.getcwd(),
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
        if job and job.get("status") == "done" and not job.get("artifact_ref"):
            commit = subprocess.run(
                ["git", "-C", str(job.get("cwd") or os.getcwd()), "rev-parse", "HEAD"],
                text=True, capture_output=True, check=False,
            )
            if commit.returncode == 0:
                job["artifact_ref"] = commit.stdout.strip()
        return job

    def find(self, task_id: str, attempt: int) -> dict[str, Any] | None:
        store = _store.build_store(Path(self.db) if self.db else None)
        return next((job for job in _jobs.list_jobs(store, all_jobs=True)
                     if job.get("task_id") == task_id and job.get("task_attempt") == attempt), None)


class Dispatcher:
    def __init__(self, service: ProjectTaskService, principal: TaskPrincipal, launcher: Launcher,
                 project_id: str, *, parallel: int = 1, emit: Callable[[str], None] = print,
                 sleep: Callable[[float], None] = time.sleep,
                 task_reader: Callable[[ProjectTaskService, str], list[Task]] = _tasks):
        if not 1 <= parallel <= 100:
            raise ValueError("--parallel must be between 1 and 100")
        self.service, self.principal, self.launcher = service, principal, launcher
        self.project_id, self.parallel, self.emit, self.sleep = project_id, parallel, emit, sleep
        self.task_reader = task_reader
        self.reported_blocked: set[str] = set()

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
        if not job or job.get("status") not in TERMINAL:
            return False
        accepted = job.get("status") == "done"
        state: Literal["accepted", "blocked"] = "accepted" if accepted else "blocked"
        reference = f"bridge-job:{job_ref}:{job.get('status')}"
        evidence = CompletionEvidence(reference=reference, job_ref=str(job_ref), executor=receipt["executor"],
            engine_family=receipt["engine_family"], state=state,
            artifact_ref=job.get("artifact_ref") or job.get("candidate_commit") or
                         (receipt.get("revision") if accepted else None),
            outcome="success" if accepted else "task_failure")
        self.service.record_completion_evidence(self.principal, task.task_id, receipt["attempt"], evidence)
        self.service.complete(self.principal, task.task_id, receipt["claim_token"], reference, state, reference,
                              "success" if accepted else "task_failure")
        if accepted:
            self.emit(f"done {task.task_id}")
        else:
            reason = str(job.get("error") or job.get("status") or "job failed").replace("\n", " ")
            self.emit(f"failed {task.task_id} {reason}")
        return True

    def pass_once(self) -> tuple[int, int]:
        tasks = self.task_reader(self.service, self.project_id)
        for task in tasks:
            if task.status == "claimed":
                self._settle(task)
        tasks = self.task_reader(self.service, self.project_id)
        running = sum(task.status == "claimed" for task in tasks)
        ready = self.service.next_ready(self.principal, self.project_id, limit=100)["tasks"]
        for summary in ready[:max(0, self.parallel - running)]:
            self.emit(f"ready {summary['task_id']}")
            result = self.service.claim(self.principal, summary["task_id"], summary["revision"],
                                        f"dispatch:{summary['task_id']}:{summary['revision']}")
            claim = result["claim"]
            self.emit(f"claimed {summary['task_id']}")
            try:
                job_ref = self.launcher.launch(summary, claim)
            except Exception as exc:
                reason = str(exc).replace("\n", " ")
                self.service.fail_local_launch(self.principal, summary["task_id"], claim["claim_token"], reason)
                self.emit(f"failed {summary['task_id']} {reason}")
            else:
                self.emit(f"launched {job_ref}")
        tasks = self.task_reader(self.service, self.project_id)
        self._blocked(tasks)
        return sum(task.status == "todo" for task in tasks), sum(task.status == "claimed" for task in tasks)

    def run(self, *, once: bool = False) -> int:
        while True:
            _, running = self.pass_once()
            ready = self.service.next_ready(self.principal, self.project_id, limit=1)["tasks"]
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
    return Dispatcher(service, principal, BridgeLauncher(args.db, principal), args.project,
                      parallel=args.parallel).run(once=args.once)
