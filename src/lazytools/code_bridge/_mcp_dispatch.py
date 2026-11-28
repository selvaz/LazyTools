"""Project dispatcher control for the orchestration MCP server."""
from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter, deque
from pathlib import Path
from typing import Any

from lazytools.code_bridge import _dispatch, _dispatch_launch, _jobs, _store, cli
from lazytools.connectors.projects.tools import _resolve_store_db
from lazytools.projects.task_service import ProjectTaskService, TaskPrincipal


class DispatchTools:
    _is_lazy_tool_provider = True

    def __init__(self, *, allow_write: bool = False, projects_db: str | None = None) -> None:
        self.allow_write = allow_write
        self.projects_db = _resolve_store_db(projects_db)

    @staticmethod
    def _project(project: str) -> str:
        if not isinstance(project, str) or not project.strip() or project.startswith("-") or "\n" in project:
            raise ValueError("project must be a nonempty project identifier")
        return project

    def _lock(self, project: str) -> Any:
        return _dispatch.owner_lock(self._project(project), self.projects_db)

    def _log(self, project: str) -> Path:
        return _store.results_dir() / f"dispatch-{self._lock(project).path.stem}.jsonl"

    def dispatch_status(self, project: str, last_n: int = 50) -> dict[str, Any]:
        """Read the project board, running jobs, tickets, owner and recent dispatcher events."""
        project = self._project(project)
        if not isinstance(last_n, int) or isinstance(last_n, bool) or not 0 <= last_n <= 1000:
            raise ValueError("last_n must be between 0 and 1000")
        tasks = _dispatch._tasks(ProjectTaskService(self.projects_db), project)
        with _store.build_store() as store:
            jobs = [job for job in _jobs.list_jobs(store, all_jobs=True)
                    if str(job.get("task_id") or "").startswith(project + "/")
                    and job.get("status") not in _dispatch.TERMINAL]
        principal = TaskPrincipal(actor="code-bridge-dispatch", role="orchestrator", projects=(project,))
        launcher = _dispatch_launch.BridgeLauncher(None, principal, project)
        job_ids = {str(job["job_id"]) for job in jobs}
        tickets = [ticket for ticket in launcher.pending() if ticket.get("job_id") in job_ids]
        events: list[Any] = []
        try:
            with self._log(project).open(encoding="utf-8", errors="replace") as log:
                for line in deque(log, maxlen=last_n):
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        continue
        except FileNotFoundError:
            pass
        owner = _dispatch.live_owner(self._lock(project))
        return {"project": project, "tasks_by_status": dict(Counter(task.status for task in tasks)),
                "running_jobs": jobs, "pending_tickets": tickets, "running": owner is not None,
                "owner": owner, "events": events, "log_path": str(self._log(project))}

    def dispatch_start(self, project: str, repo_roots: dict[str, str] | None = None,
                       worktree_root: str | None = None, land: bool = False) -> dict[str, Any]:
        """Start a detached supervised dispatcher for the project."""
        if not self.allow_write:
            raise PermissionError("dispatch_start requires --allow-unsafe")
        project = self._project(project)
        if owner := _dispatch.live_owner(self._lock(project)):
            return {"ok": False, "error": "dispatcher already running", "owner": owner}
        argv = [sys.executable, "-m", "lazytools.code_bridge", "dispatch", "--project", project, "--supervise", "--json"]
        for slug, path in (repo_roots or {}).items():
            if not slug or not path or "=" in slug:
                raise ValueError("repo_roots must map repository slugs to paths")
            argv.extend(["--repo-root", f"{slug}={path}"])
        if worktree_root:
            argv.extend(["--worktree-root", worktree_root])
        if land:
            argv.append("--land")
        env = dict(os.environ)
        if self.projects_db:
            env["LAZYTOOLS_PROJECTS_STORE_DB"] = self.projects_db
        log = self._log(project)
        pid = cli._spawn_detached(argv, log, env=env)
        return {"ok": True, "pid": pid, "log_path": str(log)}

    def dispatch_stop(self, project: str) -> dict[str, Any]:
        """Request graceful shutdown and wait briefly for the owner to exit."""
        if not self.allow_write:
            raise PermissionError("dispatch_stop requires --allow-unsafe")
        lock = self._lock(project)
        owner = _dispatch.live_owner(lock)
        if owner is None:
            return {"ok": True, "running": False}
        _dispatch.stop_path(lock).write_text(str(owner["instance_id"]), encoding="utf-8")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current = _dispatch.live_owner(lock)
            if current is None or current.get("instance_id") != owner.get("instance_id"):
                return {"ok": True, "running": False}
            time.sleep(0.1)
        return {"ok": True, "running": True, "stop_requested": True}

    def as_tools(self) -> list[Any]:
        from lazybridge import Tool

        methods: list[Any] = [self.dispatch_status]
        if self.allow_write:
            methods.extend([self.dispatch_start, self.dispatch_stop])
        return [Tool.wrap(method, name=method.__name__) for method in methods]
