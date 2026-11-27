"""Local and cloud job launches for the project dispatcher."""
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
from pathlib import Path
from typing import Any, Protocol

from lazytools.code_bridge import _cloud, _cloud_land, _jobs, _registry, _store, _task
from lazytools.projects.task_service import TaskPrincipal

CLOUD_POLL_SECONDS = 30.0  # minimum spacing of provider status polls for one cloud job
CLOUD_CLAIM_ENV = "LAZYTOOLS_CODE_BRIDGE_CLOUD_CLAIM"
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
        # A slot fallback can choose a later rung with a different quota window.
        if model := claim.get("model"):
            command.extend(("--model", str(model)))
        if effort := claim.get("effort"):
            command.extend(("--effort", str(effort)))
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
        if (task.get("attempt_continuation") or {}).get("check_failures"):
            # Even 60 lines of test output can exceed Windows' command-line limit.
            # The detached launcher snapshots an @brief before starting the child.
            with tempfile.TemporaryDirectory(prefix="dispatch-check-") as directory:
                brief = Path(directory) / "repair.md"
                brief.write_text(text, encoding="utf-8")
                command[command.index("--task") + 1] = f"@{brief}"
                result = subprocess.run(command, text=True, capture_output=True, check=False, env=env)
        else:
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

        from lazytools.code_bridge import cli  # deferred: cli imports the dispatcher

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
            from lazytools.code_bridge._dispatch import STALE_HEARTBEAT

            command.extend(("--stale-before", str(stale_before), "--reason", STALE_HEARTBEAT))
        if self.db:
            command.extend(("--db", self.db))
        if self.registry_db:
            command.extend(("--registry-db", self.registry_db))
        return subprocess.run(command, text=True, capture_output=True, check=False).returncode == 0

    def job(self, job_ref: str) -> dict[str, Any] | None:
        from lazytools.code_bridge import cli

        db_path = Path(self.db) if self.db else None
        with _store.build_store(db_path) as store:
            job = _jobs.find_job(store, _store.build_job_registry(store), job_ref)
        if job is not None:
            return job
        pid = cli._detached_pid(db_path, job_ref)
        if pid is not None:
            return {"job_id": job_ref, "pid": pid, "status": "starting"}
        return None

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
        brief = str(task["text"])
        if family == "codex" and (attempt := claim.get("attempt")) is not None:
            brief += f"\n\n{_cloud.dispatch_label(claim['task_id'], attempt, job_id)}"
        command = [sys.executable, "-m", "lazytools.code_bridge", "run", "--where", "cloud", "--allow-cloud",
                   "--engine", "claude" if family == "claude_code" else family,
                   "--branch", _cloud.current_branch(repo), "--open-pr", "--land",
                   "--task-id", claim["task_id"], "--job-id", job_id,
                   "--cwd", str(repo), "--root", str(repo), "--task", brief,
                   "--tier", task.get("tier") or "thinking", "--no-brief-check"]
        if self.db:
            command.extend(("--db", self.db))
        if task.get("requested_network") is not None:
            command.extend(("--network", "on" if task["requested_network"] else "off"))
        env = os.environ.copy()
        if owner := claim.get("dispatcher_owner"):
            env[_task.PRINCIPAL_ENV] = self.principal.model_dump_json()
            env[CLOUD_CLAIM_ENV] = json.dumps([claim["claim_token"], owner["instance_id"]])
        result = subprocess.run(command, text=True, capture_output=True, check=False, env=env)
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

    def reconcile_cloud_submission(self, job: dict[str, Any], task_id: str, attempt: int) -> dict[str, Any]:
        with _store.build_store(Path(self.db) if self.db else None) as store:
            label = _cloud.dispatch_label(task_id, attempt, str(job["job_id"]))
            return _cloud.reconcile_submission(store, _store.build_job_registry(store), job, label)

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


def local_job_id(claim_token: str) -> str:
    """The detached job id is known even before its worker registers."""
    return hashlib.sha256(f"dispatch-local:{claim_token}".encode()).hexdigest()[:32]
