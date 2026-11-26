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

from lazybridge import Store
from lazybridge.ext.knowledge import DEFAULT_PREFIX, DurableKnowledgeBase, slugify

from lazytools.code_bridge import _cloud, _concurrency, _jobs, _registry, _routing, _store
from lazytools.code_bridge._navigator import _split_command
from lazytools.connectors.projects.tools import _resolve_store_db
from lazytools.projects.plan_edit import _board_key
from lazytools.projects.task_fields import task_id as board_task_id
from lazytools.projects.task_service import CompletionEvidence, PlacementEvidence, ProjectTaskService, TaskPrincipal
from lazytools.projects.task_store import TaskServiceError, transaction

TERMINAL = frozenset({"done", "partial", "failed", "interrupted", "cancelled"})
DEFAULT_STALE_MINUTES = 10
STALE_HEARTBEAT = "engine heartbeat went stale"
# Engine crashes (not task failures): retried once with a fresh attempt.
COMMIT_EXCLUDES = ("*.log", "**/__pycache__", ".pytest_cache", "**/.pytest_cache")
RETRYABLE_FAILURES = ("App Server exited", "outcome is unknown", "3221226505", "0xC0000409",
                      "no new commit since the task started", STALE_HEARTBEAT, "database is locked")


def _has_stale_heartbeat(job: dict[str, Any], cutoff: float) -> bool:
    heartbeat = job.get("heartbeat_at")
    return job.get("status") == "running" and isinstance(heartbeat, (int, float)) and heartbeat < cutoff


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
            # A new attempt in an existing checkout must add its own commits.
            subprocess.run(["git", "-C", str(checkout), "tag", "-f", "dispatch-base", "HEAD"],
                           text=True, capture_output=True, check=False)
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
            # No dependency and no assigned branch: start from the commit the source
            # checkout is on (the branch the operator pointed the dispatcher at). A
            # remote-tracking ref of the source does not exist in the clone.
            base = self._git(repo, "rev-parse", "HEAD")
        # A local clone, not a linked worktree: its .git lives inside the checkout, so a
        # sandboxed agent can commit there (a linked worktree keeps its index and merge
        # state under the main repository, outside the sandbox).
        self._git(repo, "clone", "--quiet", "--local", "--no-checkout", str(repo), str(checkout))
        origin = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"],
                                text=True, capture_output=True, check=False).stdout.strip()
        if origin:
            self._git(checkout, "remote", "set-url", "origin", origin)  # the claim checks the repo slug
        self._git(checkout, "remote", "add", "dispatch-source", str(repo))
        # A clone does not inherit the source's local identity; dependency merges and
        # dispatcher commits need one (CI runners have no global identity).
        for key, fallback in (("user.name", "lazytools-dispatch"), ("user.email", "dispatch@lazytools.invalid")):
            value = subprocess.run(["git", "-C", str(repo), "config", "--get", key],
                                   text=True, capture_output=True, check=False).stdout.strip()
            if value or not subprocess.run(["git", "-C", str(checkout), "config", "--get", key],
                                           text=True, capture_output=True, check=False).stdout.strip():
                self._git(checkout, "config", key, value or fallback)
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
        """Pending approval tickets, read through the same function `wait-any` uses."""
        from lazybridge.ext.approval import ticket_gist

        from lazytools.code_bridge import cli  # deferred: cli imports this module

        args = argparse.Namespace(db=self.db, registry_db=Path(self.registry_db) if self.registry_db else None)
        return [{"approval_id": ticket.approval_id, "job_id": _registry.job_ref(ticket),
                 "gist": ticket_gist(ticket.prompt), **cli._request_detail(ticket.prompt)}
                for ticket in cli._pending_tickets(args, cli._ticket_job_store(args))]

    def rejected_ticket_reason(self, job: dict[str, Any]) -> str:
        """Read terminal ticket decisions even if the dispatcher never saw them pending."""
        job_id = str(job["job_id"])
        with _store.build_store(Path(self.db) if self.db else None) as store:
            if ticket_ids := job.get("registry_ticket_ids"):
                path = Path(self.registry_db).expanduser() if self.registry_db else None
                registry = _registry.Registry.for_job(store, job_id, path)
                tickets = [registry.inspect(ticket_id) for ticket_id in ticket_ids]
            else:
                queue = _store.build_approval_queue(store)
                tickets = [
                    queue.get_ticket(key.removeprefix(_store.APPROVAL_PREFIX))
                    for key, raw in store.items(prefix=_store.APPROVAL_PREFIX)
                    if isinstance(raw, dict) and raw.get("task_id") == job_id and raw.get("status") == "rejected"
                ]
            reasons = (
                (ticket.decision.reason if ticket.decision else ticket.reason) or "ticket rejected without a reason"
                for ticket in tickets
                if ticket is not None and ticket.status == "rejected" and ticket.actor != "bridge-cleanup"
                and _registry.job_ref(ticket) == job_id
            )
            return "\n".join(dict.fromkeys(reasons))

    def cancel(self, job_ref: str, *, stale_before: float | None = None) -> bool:
        command = [sys.executable, "-m", "lazytools.code_bridge", "cancel", job_ref]
        if stale_before is not None:
            command.extend(("--stale-before", str(stale_before), "--reason", STALE_HEARTBEAT))
        if self.db:
            command.extend(("--db", self.db))
        if self.registry_db:
            command.extend(("--registry-db", self.registry_db))
        return subprocess.run(command, text=True, capture_output=True, check=False).returncode == 0

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
    # Tool litter alone (error.log, __pycache__) is not unfinished work.
    dirty = [line[3:] for line in git("status", "--porcelain").stdout.splitlines()
             if line.strip() and not _is_litter(line[3:])]
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


def _clone_head(cwd: Path) -> str | None:
    """HEAD of a dispatcher-created clone (marked by `dispatch.branch`), else None."""
    def git(*argv: str) -> str:
        return subprocess.run(["git", "-C", str(cwd), *argv], text=True, capture_output=True,
                              check=False).stdout.strip()
    return git("rev-parse", "HEAD") or None if git("config", "--get", "dispatch.branch") else None


def _has_new_commit(cwd: Path) -> bool:
    """True when the checkout has commits after its `dispatch-base` tag (or has no tag)."""
    count = subprocess.run(["git", "-C", str(cwd), "rev-list", "--count", "dispatch-base..HEAD"],
                           text=True, capture_output=True, check=False)
    return count.returncode != 0 or count.stdout.strip() != "0"


def _checks_changed_checkout(cwd: Path, expected_head: Any) -> str | None:
    """Why the acceptance checks invalidated the artifact (moved HEAD or edited tracked files), else None."""
    def git(*argv: str) -> str:  # not stripped: porcelain lines start with a status column
        return subprocess.run(["git", "-C", str(cwd), *argv], text=True, capture_output=True,
                              check=False).stdout
    head = git("rev-parse", "HEAD").strip()
    if expected_head and head != expected_head:
        return f"acceptance checks moved HEAD from {str(expected_head)[:12]} to {head[:12]}"
    edited = [line[3:] for line in git("status", "--porcelain", "--untracked-files=no").splitlines()
              if line.strip() and not _is_litter(line[3:])]
    if edited:
        return f"acceptance checks modified tracked files: {', '.join(edited[:5])}"
    return None


def _check_failure(command: str, cwd: Path | None) -> str | None:
    message = f"check failed: {command}"
    if cwd is None:
        return f"{message} (missing task checkout)"
    try:
        argv = _split_command(command)  # Windows paths keep their backslashes
        if not argv:
            raise ValueError("empty command")
        result = subprocess.run(
            argv, cwd=cwd, env={**os.environ, "PYTHONPATH": "src"}, shell=False,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15 * 60, check=False,
        )
        if result.returncode == 0:
            return None
        message += f" (exit {result.returncode})"
        output = result.stdout
    except subprocess.TimeoutExpired as exc:
        message += " (timed out after 15 minutes)"
        output = exc.output or b""
    except (OSError, ValueError) as exc:
        return f"{message} (could not run)\n{exc}"
    tail = _cloud.redact(output[-2000:].decode("utf-8", errors="replace")).strip()
    return f"{message}\n{tail}" if tail else message


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
                 router: Callable[..., str | None] | None = None, max_retries: int = 1,
                 json_events: bool = False, knowledge_store: Store | None = None,
                 stale_minutes: int = DEFAULT_STALE_MINUTES,
                 capacity: Callable[[str], int | None] | None = None, router_with_engine: bool = False):
        if not 1 <= parallel <= 100:
            raise ValueError("--parallel must be between 1 and 100")
        if stale_minutes < 1:
            raise ValueError("--stale-minutes must be at least 1")
        self.service, self.principal, self.launcher = service, principal, launcher
        self.project_id, self.parallel, self.sleep = project_id, parallel, sleep
        # Flush each event: the dispatcher is usually piped into a log or a watcher.
        self.emit: Callable[[str], None] = emit or (lambda line: print(line, flush=True))
        self.task_reader = task_reader
        self.clock, self.orphan_grace = clock, orphan_grace
        self.stale_seconds = stale_minutes * 60
        self.router = router
        self.capacity = capacity  # engine family -> local slot limit (None = unlimited)
        self.router_with_engine = router_with_engine  # router accepts (tier, engine_family)
        self.reported_blocked: set[str] = set()
        self.unsupported: set[str] = set()
        self.waiting: set[str] = set()
        self.retries: dict[str, int] = {}
        self.seen_tickets: set[str] = set()
        self.cancelled: set[str] = set()
        self.max_retries = max_retries
        self.json_events = json_events
        self.knowledge_store = knowledge_store
        self.knowledge = DurableKnowledgeBase(knowledge_store) if knowledge_store is not None else None

    def _event(self, kind: str, task_id: str, detail: str = "", *, job_id: str | None = None) -> None:
        """Emit one event: a text line, or with --json the same keys wait-any events carry."""
        if self.json_events:
            self.emit(json.dumps({"kind": kind, "job_id": job_id, "task_id": task_id, "detail": detail}))
        elif kind == "launched":
            self.emit(f"launched {job_id}")
        else:
            self.emit(f"{kind} {task_id} {detail}" if detail else f"{kind} {task_id}")

    def _with_lessons(self, task: dict[str, Any]) -> dict[str, Any]:
        if self.knowledge is None:
            return task
        try:
            text = str(task["text"])
            matches = self.knowledge.find_lessons(text, limit=3)
            if matches:
                lines = []
                for lesson in matches:
                    topic = " ".join(str(lesson.get("topic", "")).split())[:500]
                    line = f"- {topic}"
                    for field, label in (("what_worked", "What worked"), ("gotchas", "Gotchas")):
                        value = " ".join(str(lesson.get(field, "")).split())[:500]
                        if value:
                            line += f" | {label} — {value}"
                    lines.append(line)
                return {**task, "text": "Known lessons\n" + "\n".join(lines) + "\n\n" + text}
        except Exception as exc:
            self._event("knowledge-error", task["task_id"], f"could not read lessons: {exc}")
        return task

    def _record_lesson(self, task: Task, kind: str, reason: str) -> None:
        store = self.knowledge_store
        if store is None:
            return
        try:
            topic = f"{task.data.get('repo') or 'unknown repository'}: {kind}"
            slug = slugify(topic)
            key = DEFAULT_PREFIX + slug
            now = self.clock()
            gotchas = _cloud.redact(reason).strip()
            # LazyBridge requires what_worked in its public write API. Record unresolved
            # lessons with Store CAS so they stay searchable and can be revised once fixed.
            for _ in range(20):
                previous = store.read(key)
                lesson = previous or {
                    "topic": topic, "slug": slug, "what_worked": "", "status": "active",
                    "correction_reason": "", "created_at": now, "verified_at": now,
                }
                updated = {**lesson, "gotchas": gotchas, "updated_at": now,
                           "revision": lesson.get("revision", 0) + 1}
                if store.compare_and_swap(key, previous, updated):
                    return
            raise RuntimeError("concurrent lesson updates did not settle")
        except Exception as exc:
            self._event("knowledge-error", task.task_id, f"could not save lesson: {exc}")

    def _record_rejection(self, task: Task, job: dict[str, Any]) -> None:
        rejected_ticket_reason = getattr(self.launcher, "rejected_ticket_reason", None)
        if self.knowledge is None or rejected_ticket_reason is None:
            return
        try:
            reason = rejected_ticket_reason(job)
        except Exception as exc:
            self._event("knowledge-error", task.task_id, f"could not read rejected tickets: {exc}")
            return
        if reason:
            self._record_lesson(task, "rejected ticket", reason)

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
            job_id = str(ticket.get("job_id") or "")
            task_id = by_job.get(job_id)
            if not approval_id or task_id is None or approval_id in self.seen_tickets:
                continue
            self.seen_tickets.add(approval_id)
            command = " ".join(str(ticket.get("command") or ticket.get("gist") or "").split())[:200]
            reason = " ".join(str(ticket.get("reason") or "").split())[:160]
            self._event("approval", task_id, f"{approval_id} {command} | {reason}", job_id=job_id)

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
        # Engine families already busy: claimed tasks plus tasks placed and not yet claimed.
        in_use: dict[str, int] = {}
        for task in tasks:
            if task.status == "claimed":
                family = str(((task.dispatch or {}).get("receipt") or {}).get("engine_family") or "")
            elif task.status == "todo":
                evidence = task.data.get("placement_evidence", {}).get("local", {})
                fresh = evidence.get("revision") == task.revision and                     evidence.get("evidence", {}).get("expires_at", 0) > now + 60
                family = str(evidence.get("evidence", {}).get("engine_family") or "") if fresh else ""
            else:
                family = ""
            if family:
                in_use[family] = in_use.get(family, 0) + 1
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
                    self._event("waiting", task.task_id, f"routing failed: {str(exc).replace(chr(10), ' ')}")
            engine = picks[tier]
            limit = self.capacity(engine) if engine and self.capacity else None
            if engine and limit and in_use.get(engine, 0) >= limit:
                # The routed engine is full: ask for the other one instead of queueing for a slot.
                other = "claude_code" if engine == "codex" else "codex"
                try:
                    alternative = self.router(tier, other) if self.router_with_engine else None
                except Exception:
                    alternative = None
                other_limit = self.capacity(other) if alternative and self.capacity else None
                if alternative and not (other_limit and in_use.get(alternative, 0) >= other_limit):
                    engine = alternative
            if engine is None:
                if task.task_id not in self.waiting:
                    self._event("waiting", task.task_id, f"no eligible engine for tier {tier}")
                    self.waiting.add(task.task_id)
                continue
            self.waiting.discard(task.task_id)
            self.service.record_placement(task.task_id, task.revision, PlacementEvidence(
                placement="local", eligible=True, reason=f"quota router picked {engine} for tier {tier}",
                reference=f"dispatch-{task.task_id}-{task.revision}-{int(now)}",
                expires_at=now + self.service.lease_seconds + 60, engine_family=engine,
            ))
            in_use[engine] = in_use.get(engine, 0) + 1
            self._event("placed", task.task_id, f"local {engine} tier={tier}")

    def _blocked(self, tasks: list[Task]) -> None:
        failed = {task.task_id.rsplit("/", 1)[1] for task in tasks if task.status == "failed"}
        for task in tasks:
            if (task.status == "todo" and failed.intersection(task.data.get("dependency_ids", []))
                    and task.task_id not in self.reported_blocked):
                self._event("blocked", task.task_id)
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
                reason = "no job after dispatch grace period"
                self._record_lesson(task, "missing job", reason)
                self._event("failed", task.task_id, reason, job_id=dispatch.get("job_ref"))
                return True
            return False
        if job.get("status") not in TERMINAL:
            now = self.clock()
            lease_end = float(receipt.get("lease_expires_at", float("inf")))
            cutoff = now - self.stale_seconds
            stale = _has_stale_heartbeat(job, cutoff)
            cancel = getattr(self.launcher, "cancel", None)
            if (not stale and lease_end > now) or cancel is None or str(job_ref) in self.cancelled:
                return False
            try:
                stopped = cancel(str(job_ref), stale_before=cutoff) if stale else cancel(str(job_ref))
            except Exception as exc:
                self._event("waiting", task.task_id, f"cancel failed: {str(exc).replace(chr(10), ' ')[:160]}",
                            job_id=str(job_ref))
                return False
            if not stopped:
                return False
            self.cancelled.add(str(job_ref))
            reason = STALE_HEARTBEAT if stale else "claim lease expired"
            self._event("cancelled", task.task_id, f"{reason}; settling once the job has stopped", job_id=str(job_ref))
            job = self.launcher.job(str(job_ref))
            if not job or job.get("status") not in TERMINAL:
                return False
        checks = task.data.get("checks")
        reference = f"bridge-job:{job_ref}:{job.get('status')}"
        if checks:
            reference += ":checks"  # evidence for this gate survives a restart between recording and completion
        recorded = dispatch.get("evidence", {}).get(reference, {})
        recorded_failure = recorded.get("failure_reason")
        cwd = Path(str(job["cwd"])) if job.get("cwd") else None
        accepted = job.get("status") == "done" and not recorded_failure
        if accepted and cwd is not None:
            workspace_error, merge_commit = _conclude_workspace(cwd, task.task_id)
            if workspace_error:
                accepted = False
                job = {**job, "error": workspace_error}
            elif merge_commit:
                self._event("committed", task.task_id, f"{merge_commit[:12]} (engine sandbox keeps .git read-only)",
                            job_id=str(job_ref))
            if accepted and not workspace_error:
                # The dispatcher's clone is written by nobody else once the job ended, so its
                # HEAD is the task's artifact, also after a restart that lost an earlier commit.
                head = _clone_head(cwd)
                if head:
                    job = {**job, "final_commit": head}
            if accepted and not _has_new_commit(cwd):
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
        job_error = str(job.get("error") or job.get("status") or "job failed").replace("\n", " ")
        check_failure = recorded_failure
        if accepted and checks and not (needs_evidence or expired or recorded):
            check_failure = "\n".join(filter(None, (_check_failure(command, cwd) for command in checks))) or None
            if not check_failure and cwd is not None:
                # Evidence must describe the artifact that is published: a check that edits
                # tracked files (e.g. `ruff --fix`) or commits would pass on unpublished content.
                check_failure = _checks_changed_checkout(cwd, job.get("final_commit"))
            expired = float(receipt.get("lease_expires_at", float("inf"))) <= self.clock()
        if check_failure:
            accepted = False
            failure_reason = check_failure
        elif needs_evidence:
            accepted = False
            failure_reason = "needs review evidence"
        elif expired:
            accepted = False
            failure_reason = job_error if STALE_HEARTBEAT in job_error else "claim lease expired"
        else:
            failure_reason = job_error
        # Publish only after the lease and evidence checks; a failed publish fails the task.
        if accepted and cwd is not None:
            published, publish_error = _publish(cwd)
            if publish_error:
                accepted = False
                failure_reason = publish_error
            elif published:
                self._event("published", task.task_id, published, job_id=str(job_ref))
        state: Literal["accepted", "blocked"] = "accepted" if accepted else "blocked"
        evidence = CompletionEvidence(reference=reference, job_ref=str(job_ref), executor=receipt["executor"],
            engine_family=receipt["engine_family"], state=state,
            artifact_ref=job.get("final_commit") or job.get("candidate_commit"),
            verification_id=verification_id, reviewed_sha=reviewed_sha, review_passed=review_passed,
            review_independence=review_independence,
            outcome="success" if accepted else "task_failure", failure_reason=check_failure)
        if not recorded_failure:
            self.service.record_completion_evidence(self.principal, task.task_id, receipt["attempt"], evidence)
        complete_options = {"reconcile_failure": True} if expired else {}
        self.service.complete(self.principal, task.task_id, receipt["claim_token"], reference, state, reference,
                              "success" if accepted else "task_failure", **complete_options)
        self._record_rejection(task, job)
        if accepted:
            self._event("done", task.task_id, job_id=str(job_ref))
            return True
        retryable = not check_failure and any(marker in failure_reason for marker in RETRYABLE_FAILURES)
        self._record_lesson(task, "engine crash" if retryable else "task failure", failure_reason)
        if retryable and self.retries.get(task.task_id, 0) < self.max_retries:
            current = next((item for item in self.task_reader(self.service, self.project_id)
                            if item.task_id == task.task_id), None)
            if current is not None:
                self.service.prepare_attempt(self.principal, task.task_id, current.revision,
                                             f"retry after engine crash: {failure_reason[:200]}")
                self.retries[task.task_id] = self.retries.get(task.task_id, 0) + 1
                self._event("retry", task.task_id, f"engine crash: {failure_reason[:120]}", job_id=str(job_ref))
                return True
        self._event("failed", task.task_id, check_failure.split("\n", 1)[0] if check_failure else failure_reason,
                    job_id=str(job_ref))
        return True

    def pass_once(self) -> tuple[int, int]:
        tasks = self.task_reader(self.service, self.project_id)
        for task in tasks:
            if task.status == "claimed":
                try:
                    self._settle(task)
                except Exception as exc:
                    self._event("failed", task.task_id, str(exc).replace(chr(10), " "),
                                job_id=(task.dispatch or {}).get("job_ref"))
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
                self._event("failed", summary["task_id"], "task is missing from board")
                continue
            data = board_task.data
            self._event("ready", summary["task_id"])
            if summary.get("effective_placement") == "cloud":
                reason = "cloud placement not supported by dispatch v1"
                self.unsupported.add(summary["task_id"])
                self._event("failed", summary["task_id"], reason)
                continue
            if data.get("review_of") or data.get("contract_id"):
                reason = "review/contract tasks not supported by dispatch v1"
                self.unsupported.add(summary["task_id"])
                self._event("failed", summary["task_id"], reason)
                continue
            branch = data.get("assigned_branch")
            if branch and str(branch) in running_branches:
                self._event("failed", summary["task_id"], "assigned branch already running")
                continue
            attempt = len(data.get("dispatches", [])) + 1
            try:
                result = self.service.claim(
                    self.principal, summary["task_id"], summary["revision"],
                    f"dispatch:{summary['task_id']}:{summary['revision']}:{attempt}",
                )
            except Exception as exc:
                self._event("failed", summary["task_id"], str(exc).replace(chr(10), " "))
                continue
            if not result.get("claim_active", True):
                self._event("failed", summary["task_id"], "claim is not active")
                continue
            claim = result["claim"]
            self._event("claimed", summary["task_id"])
            try:
                job_ref = self.launcher.launch(self._with_lessons({**summary, **data}), claim)
            except Exception as exc:
                reason = str(exc).replace("\n", " ")
                try:
                    self.service.fail_local_launch(
                        self.principal, summary["task_id"], claim["claim_token"], reason,
                    )
                except Exception as release_exc:
                    reason = f"{reason}; claim release failed: {str(release_exc).replace(chr(10), ' ')}"
                self._record_lesson(board_task, "launch failure", reason)
                self._event("failed", summary["task_id"], reason)
            else:
                self._event("launched", summary["task_id"], job_id=job_ref)
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


def usable_reading(reading: Any, *, max_age_seconds: float = 600.0) -> bool:
    """A quota reading the dispatcher may act on unattended: present, error-free, recent."""
    if reading is None or getattr(reading, "error", None) or not getattr(reading, "windows", ()):
        return False
    observed = getattr(reading, "observed_at", None)
    if observed is None:
        return False
    from datetime import datetime
    return reading.age_seconds(now=datetime.now(observed.tzinfo)) <= max_age_seconds


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

    def router(tier: str, engine_family: str | None = None) -> str | None:
        if engine_family is None:
            return _routing.choose(tier, cwd=os.getcwd(), db_path=db_path).decision.provider
        # Fallback to the other engine: an explicit engine enables the router's operator
        # override for unreadable telemetry, so require a usable, recent reading here.
        engine = "claude" if engine_family == "claude_code" else engine_family
        selection = _routing.choose(tier, cwd=os.getcwd(), db_path=db_path, engine=engine)
        provider = selection.decision.provider
        reading = selection.readings.get(provider) if provider else None
        return provider if usable_reading(reading) else None

    limits = _concurrency.load_limits()

    def capacity(engine_family: str) -> int | None:
        return limits.get("claude" if engine_family == "claude_code" else engine_family) or None

    knowledge_db = getattr(args, "knowledge_db", None)
    knowledge_store = _store.build_store(Path(knowledge_db).expanduser()) if knowledge_db is not None else None
    try:
        return Dispatcher(service, principal, launcher, args.project, parallel=args.parallel, router=router,
                          capacity=capacity, router_with_engine=True,
                          json_events=bool(getattr(args, "json", False)),
                          knowledge_store=knowledge_store,
                          stale_minutes=getattr(args, "stale_minutes", DEFAULT_STALE_MINUTES)).run(once=args.once)
    finally:
        if knowledge_store is not None:
            knowledge_store.close()
