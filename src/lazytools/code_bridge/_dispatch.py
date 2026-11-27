"""Restart-safe dispatch of a projects task graph through bridge jobs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from lazybridge import Store
from lazybridge.ext.knowledge import DEFAULT_PREFIX, DurableKnowledgeBase, slugify

from lazytools.code_bridge import (
    _cloud,
    _cloud_land,
    _cloud_place,
    _concurrency,
    _gh,
    _jobs,
    _registry,
    _routing,
    _store,
    _task,
)
from lazytools.code_bridge._cloud import CloudError
from lazytools.code_bridge._lockfile import _pid_alive
from lazytools.code_bridge._navigator import _split_command
from lazytools.connectors.projects.tools import _resolve_store_db
from lazytools.projects.plan_edit import _board_key
from lazytools.projects.task_fields import task_id as board_task_id
from lazytools.projects.task_service import (
    AttemptContinuation,
    CompletionEvidence,
    PlacementEvidence,
    ProjectTaskService,
    TaskPrincipal,
)
from lazytools.projects.task_store import TaskServiceError, transaction

TERMINAL = frozenset({"done", "partial", "failed", "interrupted", "cancelled"})
DEFAULT_STALE_MINUTES = 10
DEFAULT_TOOL_STALE_MINUTES = 60  # one tool call running longer than this is stale
STALE_HEARTBEAT = "engine heartbeat went stale"
# Engine crashes (not task failures): retried once with a fresh attempt.
COMMIT_EXCLUDES = ("*.log", "**/__pycache__", ".pytest_cache", "**/.pytest_cache")
RETRYABLE_FAILURES = ("App Server exited", "outcome is unknown", "3221226505", "0xC0000409",
                      "no new commit since the task started", STALE_HEARTBEAT, "database is locked")
CLOUD_POLL_SECONDS = 30.0  # minimum spacing of provider status polls for one cloud job
# A retry that could not load the failed attempt's conversation: start a fresh one instead.
RESUME_FAILURES = ("use a new session alias", "Local launch binding changed", "Governed Claude resume requires",
                   "No conversation found", "thread not found", "no rollout found")
RESUME_PROMPT = ("Your previous turn on this task was interrupted by the system (an engine or dispatcher "
                 "failure, not a review). Your work so far is kept in this checkout. Continue the task from "
                 "where you stopped: check `git status` and `git log`, finish the work, run the tests and commit.")


def _resumes(continuation: dict[str, Any] | None, engine_family: Any) -> bool:
    """True when an attempt launched with this continuation resumes the failed attempt's conversation."""
    if not continuation or continuation.get("fresh") or not continuation.get("native_id") or not engine_family:
        return False
    return continuation.get("engine") == ("claude" if engine_family == "claude_code" else engine_family)


def _tool_call_in_progress(job: dict[str, Any], now: float, bound: float) -> bool:
    """A tool call is running in a live job process and within its own bound.

    The engine emits no events while a tool (a long shell command) runs, so
    the event heartbeat alone would cancel healthy jobs. The engine is driven by
    the job's worker process (``pid``): when the engine exits, the worker ends
    the turn and clears its tool calls; when the worker dies, nothing is left
    to finish them.
    """
    calls = job.get("tool_calls")
    pid = job.get("pid")
    if not isinstance(calls, dict) or not calls or not isinstance(pid, int) or not _pid_alive(pid):
        return False
    starts = [call.get("started_at") if isinstance(call, dict) else None for call in calls.values()]
    return all(isinstance(start, (int, float)) and now - start < bound for start in starts)


def _has_stale_heartbeat(job: dict[str, Any], cutoff: float, *, now: float | None = None,
                         tool_bound: float = DEFAULT_TOOL_STALE_MINUTES * 60) -> bool:
    heartbeat = job.get("heartbeat_at")
    if job.get("status") != "running" or not isinstance(heartbeat, (int, float)) or heartbeat >= cutoff:
        return False
    return not _tool_call_in_progress(job, time.time() if now is None else now, tool_bound)


@dataclass(frozen=True)
class Task:
    task_id: str
    revision: str
    status: str
    data: dict[str, Any]
    dispatch: dict[str, Any] | None
    hold_reason: str | None = None  # reason recorded with the task's current start_hold


class RepositoryMismatch(ValueError):
    """The repository a cloud task would act on is not the task's repository."""


def cloud_repo(repo_roots: dict[str, Path], task: dict[str, Any]) -> Path:
    """The checkout a cloud task acts on: its --repo-root mapping, verified against the task's `repo`.

    The cloud job takes its repository from this checkout, so there is no fallback to the
    dispatcher's own checkout: a wrong one would publish the work to another repository.
    """
    repo = str(task.get("repo") or "")
    if not repo:
        raise RepositoryMismatch("cloud task names no repository")
    root = next((path for slug, path in repo_roots.items() if slug.casefold() == repo.casefold()), None)
    if root is None:
        raise RepositoryMismatch(f"no --repo-root mapping for the task's repository {repo}")
    try:
        _task.check_repo(_cloud.repo_root(root), repo, label="Cloud")  # the rule local launches apply
    except Exception as exc:
        raise RepositoryMismatch(str(exc)) from exc
    return root


class Launcher(Protocol):
    def launch(self, task: dict[str, Any], claim: dict[str, Any]) -> str: ...
    def job(self, job_ref: str) -> dict[str, Any] | None: ...
    def find(self, task_id: str, attempt: int) -> dict[str, Any] | None: ...


def _tasks(service: ProjectTaskService, project_id: str) -> list[Task]:
    with transaction(service.path) as store:
        board = store.read(_board_key(project_id), {"tasks": []})
        holds: dict[int, str] = {}
        for event in board.get("schedule_events", []):
            if event.get("hold") and event.get("reason") and "task_index" in event:
                holds[event["task_index"]] = str(event["reason"])  # later events win
        return [Task(f"{project_id}/{board_task_id(data, index)}", data.get("revision", "0"),
                     data.get("status", "todo"), data,
                     data.get("dispatches", [None])[-1] if data.get("dispatches") else None,
                     hold_reason=holds.get(index))
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
        self.base_commits: dict[str, str] = {}  # task -> commit its checkout started from
        self.stale_bases: dict[str, str] = {}  # task -> why its source could not be brought up to date
        self._stale_sources: dict[Path, str] = {}  # reported once until the reason changes
        self.principal = principal
        self.project_id = project_id
        self.repo_roots = repo_roots or {}
        self.worktree_root = worktree_root
        self.cloud_checkouts: set[Path] = set()  # acceptance clones this launcher allocated

    @staticmethod
    def _git(repo: Path, *args: str) -> str:
        result = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
        return result.stdout.strip()

    @staticmethod
    def _refresh_source(repo: Path) -> str | None:
        """Fetch the source's origin and fast-forward its base branch (origin's default branch).

        Returns why the source stays behind origin, or None when it is current or has no origin.
        The source is only written when it is clean and on that branch, and only fast-forward.
        """
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}  # never prompt

        def git(*argv: str) -> subprocess.CompletedProcess[str]:
            try:
                return subprocess.run(["git", "-C", str(repo), *argv], text=True, capture_output=True,
                                      check=False, env=env, timeout=120)
            except subprocess.TimeoutExpired:
                return subprocess.CompletedProcess(argv, 1, "", "timed out")

        def failed(action: str, result: subprocess.CompletedProcess[str]) -> str:
            return f"{action} failed: {' '.join(_cloud.redact(result.stderr).split())[:200]}"

        if git("remote", "get-url", "origin").returncode:
            return None
        fetched = git("fetch", "--quiet", "origin")
        if fetched.returncode:
            return failed("fetch from origin", fetched)
        head = git("ls-remote", "--symref", "origin", "HEAD")
        match = re.search(r"^ref: refs/heads/(\S+)\s+HEAD$", head.stdout, re.MULTILINE)
        if match is None:
            return failed("reading origin's default branch", head)
        base = match.group(1)
        target = f"refs/remotes/origin/{base}"
        if git("merge-base", "--is-ancestor", target, "HEAD").returncode == 0:
            return None  # the commit tasks start from already has everything on origin
        current = git("branch", "--show-current").stdout.strip()
        if current != base:
            return f"source is on {current or 'a detached HEAD'}, not {base}"
        status = git("status", "--porcelain")
        if status.returncode or status.stdout.strip():
            return "source checkout is dirty"
        if git("merge-base", "--is-ancestor", "HEAD", target).returncode:
            return f"source {base} has diverged from origin/{base}"
        merged = git("merge", "--ff-only", "--quiet", target)
        return failed("fast-forward", merged) if merged.returncode else None

    def _checkout(self, task: dict[str, Any]) -> Path:
        repo = self.repo_roots.get(str(task.get("repo"))) or Path.cwd()
        task_name = str(task["task_id"]).rsplit("/", 1)[-1]
        safe_project = re.sub(r"[^A-Za-z0-9_.-]+", "-", self.project_id)
        safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "-", task_name)
        root = self.worktree_root or repo.parent / "_dispatch"
        checkout = root / safe_project / safe_task
        preserved = task.get("preserved_commit") or task.get("preserved_branch")
        if not checkout.exists():
            if not preserved:
                # Only a fresh clone starts from the source base; preserved work keeps its own.
                why = self._refresh_source(repo)
                if why is None:
                    self._stale_sources.pop(repo, None)
                elif self._stale_sources.get(repo) != why:
                    self._stale_sources[repo] = self.stale_bases[str(task["task_id"])] = why
                return self._create_checkout(task, repo, checkout, safe_project, safe_task)
            self._create_checkout(task, repo, checkout, safe_project, safe_task)
        elif preserved:
            self._restore_preserved(task, checkout, self._git(checkout, "config", "--get", "dispatch.branch"))
        else:
            # A new attempt in an existing checkout must add its own commits.
            subprocess.run(["git", "-C", str(checkout), "tag", "-f", "dispatch-base", "HEAD"],
                           text=True, capture_output=True, check=False)
        base_tag = subprocess.run(["git", "-C", str(checkout), "rev-parse", "-q", "--verify",
                                   "dispatch-base^{commit}"], text=True, capture_output=True, check=False)
        if base_tag.returncode == 0:
            self.base_commits[str(task["task_id"])] = base_tag.stdout.strip()
        return checkout

    def _restore_preserved(self, task: dict[str, Any], checkout: Path, branch: str) -> None:
        """Put the interrupted attempt's commit on the task branch with its original dispatch-base.

        The preserved history already contains any dependency merges (and their resolution),
        so nothing is replayed, and the base is the recorded one, never the source's current state.
        """
        preserved = str(task.get("preserved_commit") or task.get("preserved_branch"))
        # A recreated clone has the source branch as a remote-tracking ref.
        if not task.get("preserved_commit") and subprocess.run(
            ["git", "-C", str(checkout), "show-ref", "--verify", "--quiet", f"refs/heads/{preserved}"],
            check=False,
        ).returncode != 0:
            preserved = f"origin/{preserved}"
        self._git(checkout, "checkout", "--quiet", "-B", branch, preserved)
        if task.get("preserved_base"):
            self._git(checkout, "tag", "-f", "dispatch-base", str(task["preserved_base"]))

    def _create_checkout(self, task: dict[str, Any], repo: Path, checkout: Path,
                         safe_project: str, safe_task: str) -> Path:
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
        self._git(checkout, "config", "dispatch.branch", str(branch))  # publication checks it
        if task.get("preserved_commit") or task.get("preserved_branch"):
            # Never replay dependency merges or recompute the base for preserved work.
            self._restore_preserved(task, checkout, str(branch))
            return checkout
        self._git(checkout, "checkout", "--quiet", "-B", str(branch), str(base))
        self.base_commits[str(task["task_id"])] = self._git(checkout, "rev-parse", "HEAD")
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
        # Every attempt runs in a named conversation, so a retry can wake the same agent.
        session = str(task.get("resume_session") or f"dispatch-attempt-{claim.get('attempt', 1)}")
        if task.get("resume_native_id") and engine_family:
            from lazybridge.engines.sessions import default_session_registry

            # Point the alias at the failed attempt's conversation (also one that ran without an alias).
            default_session_registry().bind("claude" if engine_family == "claude_code" else str(engine_family),
                                            cwd, session, str(task["resume_native_id"]))
        command.extend(("--session", session))
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

    # Cloud placement: the existing `run --where cloud` path submits, lands and opens the PR.

    def cloud_free(self, engine_family: str) -> bool:
        """Whether the configured cloud limits ([cloud] concurrency, per provider) leave a place."""
        engine = "claude" if engine_family == "claude_code" else engine_family
        config = _cloud.load_config()
        active = _cloud.active_jobs(_store.build_store(Path(self.db) if self.db else None))
        if config.concurrency and len(active) >= config.concurrency:
            return False
        limit = config.provider_concurrency.get(engine)
        return not (limit and sum(job.get("engine") == engine for job in active) >= limit)

    def launch_cloud(self, task: dict[str, Any], claim: dict[str, Any], job_id: str) -> str:
        repo = cloud_repo(self.repo_roots, task)  # checked again: placement may predate the mapping
        family = str(claim["engine_family"])
        command = [sys.executable, "-m", "lazytools.code_bridge", "run", "--where", "cloud", "--allow-cloud",
                   "--engine", "claude" if family == "claude_code" else family,
                   "--branch", _cloud.current_branch(repo), "--open-pr", "--land",
                   "--task-id", claim["task_id"], "--job-id", job_id,
                   "--cwd", str(repo), "--root", str(repo), "--task", str(task["text"]),
                   "--tier", task.get("tier") or "thinking", "--no-brief-check"]
        if self.db:
            command.extend(("--db", self.db))
        if task.get("requested_network") is not None:
            command.extend(("--network", "on" if task["requested_network"] else "off"))
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip() or f"bridge exited {result.returncode}")
        return job_id

    def cloud_job(self, job_ref: str) -> dict[str, Any] | None:
        """Poll a cloud job once and land it when done (as `wait-any` does); return its record."""
        db_path = Path(self.db) if self.db else None
        store = _store.build_store(db_path)
        registry = _store.build_job_registry(store)
        job = _jobs.find_job(store, registry, job_ref)
        if job is None:
            return None
        row = _cloud_land.process_job(store, registry, job, db_path=db_path, poll_interval=CLOUD_POLL_SECONDS,
                                      nonblocking_poll=True)
        job = _jobs.find_job(store, registry, str(job["job_id"])) or job
        if row["action"] in ("land-failed", "skipped"):
            job = {**job, "land_failure": row["detail"]}
        return job

    def cloud_checkout(self, task_id: str, job: dict[str, Any]) -> Path:
        """A fresh local clone of the landed branch at the pushed commit, for the acceptance checks.

        The clone gets a newly allocated directory in its own namespace, never a path derived
        from the task name, so it cannot coincide with a local task's checkout.
        """
        repo = Path(str(job.get("repo_root") or Path.cwd()))
        safe_project = re.sub(r"[^A-Za-z0-9_.-]+", "-", self.project_id)
        safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "-", task_id.rsplit("/", 1)[-1])
        namespace = (self.worktree_root or repo.parent / "_dispatch") / safe_project / ".cloud-accept"
        namespace.mkdir(parents=True, exist_ok=True)
        checkout = Path(tempfile.mkdtemp(prefix=f"{safe_task}-{job.get('task_attempt') or 0}-", dir=namespace))
        self.cloud_checkouts.add(checkout)
        try:
            self._git(repo, "clone", "--quiet", "--local", "--no-checkout", str(repo), str(checkout))
            self._git(checkout, "remote", "set-url", "origin", self._git(repo, "remote", "get-url", "origin"))
            self._git(checkout, "fetch", "--quiet", "origin", f"refs/heads/{job['landed_branch']}")
            self._git(checkout, "checkout", "--quiet", "--detach", str(job["pushed_sha"]))
        except Exception:
            self.remove_cloud_checkout(checkout)
            raise
        return checkout

    def remove_cloud_checkout(self, checkout: Path) -> None:
        """Delete an acceptance clone; only directories `cloud_checkout` allocated are ever touched."""
        if checkout in self.cloud_checkouts:
            self.cloud_checkouts.discard(checkout)
            for directory, _, files in os.walk(checkout):  # git objects are read-only on Windows
                for name in files:
                    os.chmod(os.path.join(directory, name), stat.S_IREAD | stat.S_IWRITE)
            shutil.rmtree(checkout, ignore_errors=True)


def cloud_job_id(claim_token: str) -> str:
    """The cloud job id of a claim: known before the submit, so a restart can find the job."""
    return hashlib.sha256(f"dispatch-cloud:{claim_token}".encode()).hexdigest()[:32]


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
    # Checked even for a clean checkout: a commit (the agent's or a preserved attempt's)
    # does not make conflict markers resolved.
    markers = _conflict_markers(cwd)
    if markers:
        return f"unresolved conflict markers in {', '.join(markers[:5])}", None
    # Tool litter alone (error.log, __pycache__) is not unfinished work.
    dirty = [line[3:] for line in git("status", "--porcelain").stdout.splitlines()
             if line.strip() and not _is_litter(line[3:])]
    if not dirty:
        return None, None
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


_CONFLICT_MARKER = re.compile(rb"^(<<<<<<<|>>>>>>>) ", re.MULTILINE)


def _conflict_markers(cwd: Path) -> list[str]:
    """Files changed since `dispatch-base` (committed or not) that still hold conflict markers.

    The one marker check of settlement. Without a `dispatch-base` tag every file is checked.
    """
    def paths(*argv: str) -> list[str]:
        result = subprocess.run(["git", "-C", str(cwd), *argv, "-z"], text=True, capture_output=True, check=False)
        return [path for path in result.stdout.split("\0") if path]

    has_base = subprocess.run(["git", "-C", str(cwd), "rev-parse", "-q", "--verify", "refs/tags/dispatch-base"],
                              text=True, capture_output=True, check=False).returncode == 0
    candidates = (paths("diff", "--name-only", "dispatch-base") if has_base else paths("ls-files", "--cached"))
    candidates += paths("ls-files", "--others", "--exclude-standard")
    found = []
    for path in sorted(set(candidates)):
        file = cwd / path
        if _is_litter(path) or not file.is_file():
            continue
        try:
            if _CONFLICT_MARKER.search(file.read_bytes()):
                found.append(path)
        except OSError:
            continue
    return found


@dataclass(frozen=True)
class Preserved:
    branch: str
    files: tuple[str, ...]
    message: str
    commit: str
    unresolved: tuple[str, ...] = ()  # files that still hold conflict markers (an unresolved merge)
    base: str | None = None  # the attempt's original dispatch-base


def _preserve_attempt(cwd: Path, task_id: str, attempt: int) -> tuple[Preserved | None, str | None]:
    """Keep a failed attempt's work on `dispatch-attempt/<project>/<task>/<n>` before a retry.

    Commits uncommitted non-litter changes (outside the engine sandbox, like a finished
    task) and points the branch at HEAD. Returns (None, None) when there is nothing to keep.
    A merge left unresolved is recorded as such (commit message and `Preserved.unresolved`)
    so the retry is told; settlement rejects any artifact that still holds the markers.
    """
    def git(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(cwd), *argv], text=True, capture_output=True, check=False)

    if not cwd.is_dir() or not git("config", "--get", "dispatch.branch").stdout.strip():
        return None, None  # only a dispatcher clone is written by the dispatcher
    dirty = [line[3:] for line in git("status", "--porcelain").stdout.splitlines()
             if line.strip() and not _is_litter(line[3:])]
    has_base = git("rev-parse", "-q", "--verify", "refs/tags/dispatch-base").returncode == 0
    if not dirty and not (has_base and _has_new_commit(cwd)):
        return None, None
    project, _, task = task_id.rpartition("/")
    safe_project = re.sub(r"[^A-Za-z0-9_.-]+", "-", project)
    safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "-", task)
    unresolved = tuple(_conflict_markers(cwd))
    if dirty:
        merge_head = git("rev-parse", "-q", "--verify", "MERGE_HEAD").stdout.strip()
        git("add", "-A", "--", ".", *(f":(exclude){pattern}" for pattern in COMMIT_EXCLUDES))
        staged_litter = [path for path in git("diff", "--cached", "--name-only").stdout.splitlines()
                         if _is_litter(path)]
        if staged_litter:
            git("reset", "-q", "--", *staged_litter)
        if git("diff", "--cached", "--name-only").stdout.strip() or merge_head:
            body = ["-m", "Preserved by the dispatcher before a retry: the engine stopped for a system reason."]
            if unresolved:
                what = f"The merge of {merge_head} is UNRESOLVED" if merge_head else "The work is UNRESOLVED"
                body += ["-m", f"{what}: conflict markers remain in {', '.join(unresolved[:20])}."]
            commit = git("commit", "-q", "-m", f"{task}: work of interrupted attempt {attempt}"
                         + (" (unresolved merge)" if unresolved else ""), *body)
            if commit.returncode:
                return None, f"could not commit the attempt's work: {(commit.stderr or commit.stdout).strip()[:200]}"
    branch = f"dispatch-attempt/{safe_project}/{safe_task}/{attempt}"
    created = git("branch", "-f", branch, "HEAD")
    if created.returncode:
        return None, f"could not create {branch}: {(created.stderr or created.stdout).strip()[:200]}"
    # Best effort: a copy in the source repository survives a lost clone.
    git("push", "--quiet", "dispatch-source", f"{branch}:refs/heads/{branch}")
    changed = git("diff", "--name-only", "dispatch-base", "HEAD") if has_base else None
    files = tuple(changed.stdout.splitlines()) if changed and changed.stdout.strip() else tuple(dirty)
    message = git("log", "-1", "--format=%B").stdout.strip()
    base = git("rev-parse", "dispatch-base^{commit}").stdout.strip() if has_base else None
    return Preserved(branch, files, message, git("rev-parse", "HEAD").stdout.strip(),
                     unresolved=unresolved, base=base or None), None


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


def _land(cwd: Path, branch: str, task_id: str, data: dict[str, Any],
          checks: list[str]) -> tuple[str | None, str | None]:
    """Push a published branch to the clone's origin and open its PR (or reuse an open one).

    Returns (pr_url, error). The push is fast-forward only and never targets the default branch.
    """
    try:
        repo = _cloud.repo_slug(cwd)
        base = _gh.default_branch(repo)
        if branch == base:
            return None, f"refusing to land on the default branch {base}"
        _cloud_land._push(cwd, branch, branch)
        existing = _gh.pr_for_branch(repo, branch)
        if existing and existing.get("url"):
            return str(existing["url"]), None  # a revision of the branch updates its PR
        text_lines = str(data.get("text") or "").strip().splitlines()
        title = str(data.get("title") or (text_lines[0] if text_lines else task_id)).strip()[:200] or task_id
        criteria = data.get("acceptance_criteria") or []
        body = "\n".join([
            f"Task: `{task_id}`", "", "Acceptance criteria:",
            *([f"- {item}" for item in criteria] or ["- none recorded"]), "",
            "Checks that passed:", *([f"- `{command}`" for command in checks] or ["- none configured"]),
        ]) + "\n"
        return _gh.pr_create(repo, base=base, head=branch, title=title, body=body), None
    except (CloudError, subprocess.TimeoutExpired) as exc:
        return None, (" ".join(_cloud.redact(str(exc)).split())[:500] or "landing failed")


class Dispatcher:
    def __init__(self, service: ProjectTaskService, principal: TaskPrincipal, launcher: Launcher,
                 project_id: str, *, parallel: int = 1, emit: Callable[[str], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 task_reader: Callable[[ProjectTaskService, str], list[Task]] = _tasks,
                 clock: Callable[[], float] = time.time, orphan_grace: float = 30.0,
                 router: Callable[..., str | None] | None = None, max_retries: int = 1,
                 json_events: bool = False, knowledge_store: Store | None = None,
                 stale_minutes: int = DEFAULT_STALE_MINUTES,
                 capacity: Callable[[str], int | None] | None = None, router_with_engine: bool = False,
                 cloud_placer: Callable[[dict[str, Any], str], tuple[str, str]] | None = None,
                 land: bool = False):
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
        # (task data, tier) -> (engine family, cloud environment id); raises when cloud is not possible
        self.cloud_placer = cloud_placer
        self.land = land  # push each published branch to origin and open its PR
        # Accepted, published tasks awaiting landing; drained after every claimed task is settled.
        self.landings: list[tuple[Task, Path, str, str, list[str]]] = []
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

    def _launch_input(self, task: dict[str, Any], claim: dict[str, Any]) -> dict[str, Any]:
        """The task as launched: a retry continues the failed attempt's work and, if it can, its conversation.

        The continuation is read from the task record, so it survives a dispatcher restart.
        """
        resume = task.get("attempt_continuation")
        if not resume:
            return self._with_lessons(task)
        branch = resume.get("preserved_branch")
        if branch:
            task = {**task, "preserved_branch": branch, "preserved_commit": resume.get("preserved_commit"),
                    "preserved_base": resume.get("preserved_base")}
        unresolved = list(resume.get("preserved_unresolved") or [])
        warning = ("" if not unresolved else
                   " The merge is UNRESOLVED: conflict markers remain in "
                   + ", ".join(unresolved[:20]) + (" ..." if len(unresolved) > 20 else "")
                   + ". Resolve every conflict first so the changes of both sides are kept, then commit;"
                   " the task is rejected while any marker remains.")
        if _resumes(resume, claim.get("engine_family")):
            return {**task, "text": RESUME_PROMPT + warning, "resume_session": resume.get("session"),
                    "resume_native_id": resume["native_id"]}
        task = self._with_lessons(task)
        if branch:
            changed = list(resume.get("preserved_files") or [])
            files = ", ".join(changed[:50]) + (" ..." if len(changed) > 50 else "")
            message = " ".join(str(resume.get("preserved_message") or "").split())[:500]
            task = {**task, "text": (
                "A previous attempt at this task was interrupted by the system. Its work is kept in this "
                f"checkout (also on branch {branch}). Files it changed: {files or 'none'}. Its last "
                f"commit message: {message}.{warning} Review that work, keep what is "
                "right and finish the task below.\n\n" + str(task["text"]))}
        return task

    def _retry(self, task: Task, cause: str, failure_reason: str, continuation: dict[str, Any],
               job_ref: str) -> bool:
        """Prepare the next attempt with its continuation stored durably in the task record."""
        current = next((item for item in self.task_reader(self.service, self.project_id)
                        if item.task_id == task.task_id), None)
        if current is None:
            return False
        reason = f"retry after {cause}: {failure_reason[:200]}"
        if continuation.get("preserved_branch"):
            reason += f" (work preserved on {continuation['preserved_branch']})"
        self.service.prepare_attempt(self.principal, task.task_id, current.revision, reason,
                                     continuation=AttemptContinuation(**continuation))
        self._event("retry", task.task_id, f"{cause}: {failure_reason[:120]}", job_id=job_ref)
        return True

    def _refused_resume(self, task: Task, job: dict[str, Any], continuation: dict[str, Any]) -> bool:
        """Settle a resume the bridge refused before submitting, then start a fresh conversation."""
        receipt = (task.dispatch or {})["receipt"]
        reason = str(job.get("error") or "resume refused").replace("\n", " ")
        self.service.fail_local_launch(self.principal, task.task_id, receipt["claim_token"],
                                       f"resume refused before submit: {reason}")
        # The refused job never ran: the checkout still holds the preserved work.
        return self._retry(task, "resume failed", reason, {**continuation, "native_id": None, "fresh": True},
                           str(job.get("job_id")))

    def _record_base(self, task_id: str, claim: dict[str, Any]) -> None:
        """Report a source that stayed stale and keep the checkout's base commit in the receipt."""
        why = getattr(self.launcher, "stale_bases", {}).pop(task_id, None)
        if why:
            self._event("waiting", task_id, f"source base is stale: {why}")
        base = getattr(self.launcher, "base_commits", {}).pop(task_id, None)
        if base:
            try:
                self.service.record_base_commit(self.principal, task_id, claim["claim_token"], base)
            except Exception as exc:
                self._event("receipt-error", task_id, f"could not record base commit: {exc}")

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
        if self.router is None and self.cloud_placer is None:
            return
        status = {task.task_id.rsplit("/", 1)[1]: task.status for task in tasks}
        now = self.clock()
        picks: dict[str, str | None] = {}
        # Engine families already busy: claimed tasks plus tasks placed and not yet claimed.
        in_use: dict[str, int] = {}
        for task in tasks:
            if task.status == "claimed":
                receipt = (task.dispatch or {}).get("receipt") or {}
                # A cloud job takes no local engine slot.
                local = receipt.get("effective_placement", "local") == "local"
                family = str(receipt.get("engine_family") or "") if local else ""
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
            requested = data.get("requested_placement", "local")
            placer = self.router if requested == "local" else self.cloud_placer if requested == "cloud" else None
            if (task.status != "todo" or placer is None
                    or data.get("review_of") or data.get("acceptance_contract")
                    or any(status.get(dep) != "done" for dep in data.get("dependency_ids", []))):
                continue
            record = data.get("placement_evidence", {}).get(requested, {})
            evidence = record.get("evidence", {})
            if record.get("revision") == task.revision and evidence.get("expires_at", 0) > now + 60:
                continue
            tier = data.get("tier") or "thinking"
            if requested == "cloud":
                self._place_cloud(task, tier, now)
                continue
            assert self.router is not None
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

    def _place_cloud(self, task: Task, tier: str, now: float) -> None:
        """Record cloud placement evidence: the routed engine and its cloud environment."""
        assert self.cloud_placer is not None
        try:
            engine, environment = self.cloud_placer(task.data, tier)
        except RepositoryMismatch as exc:
            # Not a transient condition: the task is not placed, so it can never be claimed.
            self.waiting.discard(task.task_id)
            if task.task_id not in self.unsupported:
                self.unsupported.add(task.task_id)
                self._event("failed", task.task_id, f"cloud placement refused: {str(exc).replace(chr(10), ' ')}")
            return
        except Exception as exc:
            if task.task_id not in self.waiting:
                self._event("waiting", task.task_id, f"cloud placement failed: {str(exc).replace(chr(10), ' ')}")
                self.waiting.add(task.task_id)
            return
        self.waiting.discard(task.task_id)
        self.service.record_placement(task.task_id, task.revision, PlacementEvidence(
            placement="cloud", eligible=True,
            reason=f"quota router picked {engine} for tier {tier}; cloud environment {environment}",
            reference=f"dispatch-{task.task_id}-{task.revision}-{int(now)}-cloud",
            expires_at=now + self.service.lease_seconds + 60, engine_family=engine, environment_id=environment,
        ))
        self._event("placed", task.task_id, f"cloud {engine} env={environment} tier={tier}")

    def _blocked(self, tasks: list[Task]) -> None:
        failed = {task.task_id.rsplit("/", 1)[1] for task in tasks if task.status == "failed"}
        for task in tasks:
            if (task.status == "todo" and failed.intersection(task.data.get("dependency_ids", []))
                    and task.task_id not in self.reported_blocked):
                self._event("blocked", task.task_id)
                self.reported_blocked.add(task.task_id)

    def _settle_missing(self, task: Task, dispatch: dict[str, Any], receipt: dict[str, Any]) -> bool:
        """Release a claim whose job never appeared, once the lease and the grace period are over."""
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

    def _conclude(
        self, task: Task, *, reference: str, job_ref: str, accepted: bool, expired: bool,
        artifact_ref: str | None = None, failure_reason: str | None = None,
        verification_id: str | None = None, reviewed_sha: str | None = None,
        review_passed: bool = False, review_independence: Any = None,
        preserved_branch: str | None = None, pr_url: str | None = None, land_failure: str | None = None,
    ) -> CompletionEvidence:
        """Record completion evidence and complete the claim; return the evidence the task settled on.

        Evidence already recorded under the reference decides (a restart replays it). After lease
        expiry the claim is closed through the service's reconcile path, which only takes a blocked
        result: accepted evidence becomes blocked, under a new reference when the old one is bound.
        """
        dispatch = task.dispatch or {}
        receipt = dispatch.get("receipt", {})
        records = dispatch.get("evidence", {})
        recorded = records.get(reference)
        if recorded:
            evidence = CompletionEvidence.model_validate(recorded)
        else:
            evidence = CompletionEvidence(
                reference=reference, job_ref=job_ref, executor=receipt["executor"],
                engine_family=receipt["engine_family"], state="accepted" if accepted else "blocked",
                artifact_ref=artifact_ref, verification_id=verification_id, reviewed_sha=reviewed_sha,
                review_passed=review_passed, review_independence=review_independence,
                outcome="success" if accepted else "task_failure", failure_reason=failure_reason,
                preserved_branch=preserved_branch, pr_url=pr_url, land_failure=land_failure,
            )
        if expired and evidence.state == "accepted":
            if recorded:
                reference = f"{reference}:lease-expired"
                recorded = records.get(reference)
            evidence = (CompletionEvidence.model_validate(recorded) if recorded else evidence.model_copy(update={
                "reference": reference, "state": "blocked", "outcome": "task_failure",
                "failure_reason": "claim lease expired",
            }))
        if not recorded:
            self.service.record_completion_evidence(self.principal, task.task_id, receipt["attempt"], evidence)
        accepted = evidence.state == "accepted"
        complete_options = {"reconcile_failure": True} if expired else {}
        self.service.complete(self.principal, task.task_id, receipt["claim_token"], evidence.reference,
                              evidence.state, evidence.reference, "success" if accepted else "task_failure",
                              **complete_options)
        return evidence

    def _settle_cloud(self, task: Task) -> bool:
        """Settle a cloud dispatch once its job is terminal and its landing is resolved.

        Accepted only when the job landed (PR url and pushed commit recorded) and the
        task's checks pass in a fresh local clone of the landed branch.
        """
        dispatch = task.dispatch or {}
        receipt = dispatch.get("receipt", {})
        token = receipt["claim_token"]
        job_ref = str(dispatch.get("job_ref") or cloud_job_id(token))
        job = self.launcher.cloud_job(job_ref)  # type: ignore[attr-defined]
        if job is None:
            return self._settle_missing(task, dispatch, receipt)
        if not dispatch.get("job_ref"):
            # Restarted between the submit and its acknowledgement: adopt the job.
            self.service.record_submit(self.principal, task.task_id, token, job_ref)
        status = job.get("status")
        expired = float(receipt.get("lease_expires_at", float("inf"))) <= self.clock()
        landing_owed = status == "done" and _cloud_land.needs_finishing(job) and not job.get("land_failure")
        if status not in TERMINAL or landing_owed:
            cancel = getattr(self.launcher, "cancel", None)
            if (expired and status not in TERMINAL and cancel is not None and job_ref not in self.cancelled
                    and cancel(job_ref)):
                self.cancelled.add(job_ref)
                self._event("cancelled", task.task_id, "claim lease expired; settling once the job has stopped",
                            job_id=job_ref)
            return False
        checks = task.data.get("checks")
        reference = f"cloud-job:{job_ref}:{status}" + (":checks" if checks else "")
        recorded = dispatch.get("evidence", {}).get(reference)
        landed = bool(status == "done" and job.get("landed_at") and job.get("pr_url") and job.get("pushed_sha"))
        land_error = job.get("land_failure") or job.get("land_error")
        land_failure = _cloud.redact(str(land_error)).strip() if land_error else None
        failure: str | None
        if recorded:  # restarted after recording: the evidence decides, the checks are not run again
            failure = None if recorded.get("state") == "accepted" else (
                recorded.get("failure_reason") or "cloud job failed")
        elif status != "done":
            failure = f"cloud job {status}: {_cloud.redact(str(job.get('error') or 'no error recorded'))}"
        elif not landed:
            detail = land_failure or "no PR url or pushed commit recorded"
            failure = f"cloud job did not land: {detail}"
        elif expired:
            failure = "claim lease expired"
        elif checks:
            try:
                cwd = self.launcher.cloud_checkout(task.task_id, job)  # type: ignore[attr-defined]
            except Exception as exc:
                failure = f"could not clone the landed branch: {_cloud.redact(str(exc))[:300]}"
            else:
                try:
                    failure = ("\n".join(filter(None, (_check_failure(command, cwd) for command in checks)))
                               or _checks_changed_checkout(cwd, job.get("pushed_sha")))
                finally:
                    remove = getattr(self.launcher, "remove_cloud_checkout", None)
                    if remove is not None:
                        remove(cwd)
            expired = float(receipt.get("lease_expires_at", float("inf"))) <= self.clock()
            failure = failure or ("claim lease expired" if expired else None)
        else:
            failure = None
        settled = self._conclude(
            task, reference=reference, job_ref=job_ref, accepted=failure is None, expired=expired,
            artifact_ref=str(job["pushed_sha"]) if landed else None,
            failure_reason=failure, pr_url=job.get("pr_url"), land_failure=land_failure or None,
        )
        if settled.state == "accepted":
            self._event("published", task.task_id, settled.pr_url or str(job["pr_url"]), job_id=job_ref)
            self._event("done", task.task_id, job_id=job_ref)
            return True
        failure = settled.failure_reason or failure
        assert failure is not None
        self._record_lesson(task, "task failure", failure)
        self._event("failed", task.task_id, failure.split("\n", 1)[0], job_id=job_ref)
        return True

    def _launch_cloud(self, task: Task, summary: dict[str, Any], claim: dict[str, Any]) -> bool:
        """Submit a claimed cloud task through `run --where cloud`; record its job; True when launched."""
        task_id, token = summary["task_id"], claim["claim_token"]
        job_id = cloud_job_id(token)
        try:
            self.service.begin_submit(self.principal, task_id, token)
        except Exception as exc:
            reason = str(exc).replace("\n", " ")
            self.service.fail_dispatch_launch(self.principal, task_id, token, reason)  # back to todo
            self._event("failed", task_id, reason)
            return False
        try:
            job_ref = self.launcher.launch_cloud(  # type: ignore[attr-defined]
                self._with_lessons({**summary, **task.data}), claim, job_id)
        except Exception as exc:
            reason = _cloud.redact(str(exc)).replace("\n", " ")
            if self.launcher.job(job_id) is None:  # nothing reached the provider
                try:
                    self.service.fail_local_launch(self.principal, task_id, token, reason)
                except Exception as release_exc:
                    reason = f"{reason}; claim release failed: {str(release_exc).replace(chr(10), ' ')}"
                self._record_lesson(task, "launch failure", reason)
                self._event("failed", task_id, reason)
                return False
            job_ref = job_id  # a failed submit is recorded; settlement reports it
        self.service.record_submit(self.principal, task_id, token, job_ref)
        self._event("launched", task_id, job_id=job_ref)
        return True

    def _settle(self, task: Task) -> bool:
        dispatch = task.dispatch or {}
        receipt = dispatch.get("receipt", {})
        if receipt.get("effective_placement") == "cloud":
            return self._settle_cloud(task)
        job_ref = dispatch.get("job_ref")
        job = self.launcher.job(job_ref) if job_ref else self.launcher.find(task.task_id, receipt.get("attempt", 0))
        continuation = task.data.get("attempt_continuation")
        resumed = _resumes(continuation, receipt.get("engine_family"))
        if (job and not job_ref and resumed and continuation and dispatch.get("state") == "intent"
                and job.get("status") in TERMINAL
                and any(marker in str(job.get("error") or "") for marker in RESUME_FAILURES)):
            # Refused before submit (check_resume, governed resume guard): the dispatch never
            # reaches the provider boundary, so reconciliation below could never settle it.
            self._refused_resume(task, job, continuation)
            return True
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
            return self._settle_missing(task, dispatch, receipt)
        if job.get("status") not in TERMINAL:
            now = self.clock()
            lease_end = float(receipt.get("lease_expires_at", float("inf")))
            cutoff = now - self.stale_seconds
            stale = _has_stale_heartbeat(job, cutoff, now=now)
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
        published: str | None = None
        if accepted and cwd is not None:
            published, publish_error = _publish(cwd)
            if publish_error:
                accepted = False
                failure_reason = publish_error
            elif published:
                self._event("published", task.task_id, published, job_id=str(job_ref))
        failed_cleanly = not accepted and not check_failure
        retryable = failed_cleanly and any(marker in failure_reason for marker in RETRYABLE_FAILURES)
        resume_failed = (failed_cleanly and resumed
                         and any(marker in failure_reason for marker in RESUME_FAILURES))
        preserved = None
        if (retryable or resume_failed) and cwd is not None:
            # A retry must not lose the work of an attempt the system stopped.
            preserved, preserve_error = _preserve_attempt(cwd, task.task_id, int(receipt["attempt"]))
            if preserve_error:
                self._event("preserve-failed", task.task_id, preserve_error, job_id=str(job_ref))
            elif preserved:
                self._event("preserved", task.task_id, preserved.branch, job_id=str(job_ref))
        settled = self._conclude(
            task, reference=reference, job_ref=str(job_ref), accepted=accepted, expired=expired,
            artifact_ref=job.get("final_commit") or job.get("candidate_commit"),
            verification_id=verification_id, reviewed_sha=reviewed_sha, review_passed=review_passed,
            review_independence=review_independence, failure_reason=check_failure,
            preserved_branch=preserved.branch if preserved else None,
        )
        self._record_rejection(task, job)
        if settled.state == "accepted":
            self._event("done", task.task_id, job_id=str(job_ref))
            if self.land and published and cwd is not None:
                # Landed by pass_once only after every claimed task is settled, so network
                # latency here never consumes another task's lease.
                self.landings.append((task, cwd, published, str(job_ref), list(checks or [])))
            return True
        if not resume_failed:
            self._record_lesson(task, "engine crash" if retryable else "task failure", failure_reason)
        # A failed resume is retried once more in a fresh conversation, outside the retry budget.
        if resume_failed or (retryable and self.retries.get(task.task_id, 0) < self.max_retries):
            # Wake the same agent, except after a failed resume. A reviewer never resumes a writer.
            native_id = None if resume_failed or task.data.get("review_of") else _jobs._native_id(job)
            kept: dict[str, Any] = {
                "preserved_branch": preserved.branch, "preserved_commit": preserved.commit,
                "preserved_base": preserved.base,
                "preserved_files": list(preserved.files), "preserved_message": preserved.message,
                "preserved_unresolved": list(preserved.unresolved),
            } if preserved else {key: (continuation or {}).get(key) for key in (
                "preserved_branch", "preserved_commit", "preserved_base", "preserved_files", "preserved_message",
                "preserved_unresolved",
            ) if resume_failed and (continuation or {}).get(key)}
            cause = "resume failed" if resume_failed else "engine crash"
            if self._retry(task, cause, failure_reason, {
                **kept, "session": job.get("session_name"), "native_id": native_id,
                "engine": job.get("engine"), "fresh": resume_failed,
            }, str(job_ref)):
                if not resume_failed:
                    self.retries[task.task_id] = self.retries.get(task.task_id, 0) + 1
                return True
        self._event("failed", task.task_id, check_failure.split("\n", 1)[0] if check_failure else failure_reason,
                    job_id=str(job_ref))
        return True

    def _land_settled(self) -> None:
        """Land the tasks accepted in this pass; outcomes are follow-up events only."""
        landings, self.landings = self.landings, []
        for task, cwd, branch, job_ref, checks in landings:
            try:
                pr_url, land_failure = _land(cwd, branch, task.task_id, task.data, checks)
            except Exception as exc:
                pr_url, land_failure = None, " ".join(_cloud.redact(str(exc)).split())[:500] or "landing failed"
            if pr_url:
                self._event("landed", task.task_id, pr_url, job_id=job_ref)
            elif land_failure:
                self._event("land-failed", task.task_id, land_failure, job_id=job_ref)

    def pass_once(self) -> tuple[int, int]:
        tasks = self.task_reader(self.service, self.project_id)
        for task in tasks:
            if task.status == "claimed":
                try:
                    self._settle(task)
                except Exception as exc:
                    self._event("failed", task.task_id, str(exc).replace(chr(10), " "),
                                job_id=(task.dispatch or {}).get("job_ref"))
        self._land_settled()
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
            cloud = summary.get("effective_placement") == "cloud"
            if cloud and not hasattr(self.launcher, "launch_cloud"):
                reason = "cloud placement not supported by this launcher"
                self.unsupported.add(summary["task_id"])
                self._event("failed", summary["task_id"], reason)
                continue
            if data.get("review_of") or data.get("contract_id"):
                reason = "review/contract tasks not supported by dispatch v1"
                self.unsupported.add(summary["task_id"])
                self._event("failed", summary["task_id"], reason)
                continue
            if cloud:
                evidence = data.get("placement_evidence", {}).get("cloud", {}).get("evidence", {})
                if not self.launcher.cloud_free(str(evidence.get("engine_family") or "")):  # type: ignore[attr-defined]
                    if summary["task_id"] not in self.waiting:
                        self._event("waiting", summary["task_id"], "cloud concurrency limit reached")
                        self.waiting.add(summary["task_id"])
                    continue
                self.waiting.discard(summary["task_id"])
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
            if cloud:
                if self._launch_cloud(board_task, summary, claim):
                    running += 1
                continue
            try:
                job_ref = self.launcher.launch(self._launch_input({**summary, **data}, claim), claim)
            except Exception as exc:
                self._record_base(summary["task_id"], claim)
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
                self._record_base(summary["task_id"], claim)
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
    # Same gate as ProjectTaskService.next_ready: a start_hold task is never dispatched,
    # so it stays out of the waves and its dependants can never become ready.
    held = {task.task_id.rsplit("/", 1)[1]: task for task in tasks
            if task.status == "todo" and task.data.get("start_hold")}
    for identifier, task in held.items():
        emit(f"held {project_id}/{identifier}" + (f" ({task.hold_reason})" if task.hold_reason else ""))
    remaining = {task.task_id.rsplit("/", 1)[1]: set(task.data.get("dependency_ids", []))
                 for task in tasks if task.status == "todo" and task.task_id.rsplit("/", 1)[1] not in held}
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
    router, cloud_placer = _routers(Path(args.db) if args.db else None, repo_roots)
    limits = _concurrency.load_limits()

    def capacity(engine_family: str) -> int | None:
        return limits.get("claude" if engine_family == "claude_code" else engine_family) or None

    knowledge_db = getattr(args, "knowledge_db", None)
    knowledge_store = _store.build_store(Path(knowledge_db).expanduser()) if knowledge_db is not None else None
    try:
        return Dispatcher(service, principal, launcher, args.project, parallel=args.parallel, router=router,
                          capacity=capacity, router_with_engine=True, cloud_placer=cloud_placer,
                          json_events=bool(getattr(args, "json", False)),
                          knowledge_store=knowledge_store, land=bool(getattr(args, "land", False)),
                          stale_minutes=getattr(args, "stale_minutes", DEFAULT_STALE_MINUTES)).run(once=args.once)
    finally:
        if knowledge_store is not None:
            knowledge_store.close()


def _routers(
    db_path: Path | None, repo_roots: dict[str, Path],
) -> tuple[Callable[..., str | None], Callable[[dict[str, Any], str], tuple[str, str]]]:
    """The local quota router and the cloud placer used by `dispatch`."""

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

    def cloud_placer(task: dict[str, Any], tier: str) -> tuple[str, str]:
        """The routed engine and its cloud environment, through the same decision `run --where cloud` uses."""
        repo = cloud_repo(repo_roots, task)  # the repository the cloud job would act on
        # Local engine slots do not bound cloud work; cloud admission is checked separately (`cloud_free`).
        family = _routing.choose(tier, cwd=os.getcwd(), db_path=db_path, respect_slots=False).decision.provider
        if family is None:
            raise ValueError(f"no eligible engine for tier {tier}")
        engine = "claude" if family == "claude_code" else family
        config = _cloud.load_config()
        placement = _cloud_place.decide_for_repo("cloud", config=config, engine=engine, cwd=repo)
        if placement.where != "cloud":
            raise ValueError(placement.reason)
        routine = _cloud.select_routine(config, routine=None, model=None) if engine == "claude" else None
        if engine == "claude" and routine is None:
            raise ValueError("Claude cloud jobs need a configured routine ([cloud.claude] default_routine)")
        environment = _cloud.provider(engine).env_for(_cloud.repo_slug(_cloud.repo_root(repo)), config, routine)
        return family, environment

    return router, cloud_placer
