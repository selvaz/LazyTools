"""MCP tools for pull-request work over the code bridge (the ``pr`` provider).

``pr_watch_poll`` is one pass of ``lazytools-code-bridge watch``: it calls the
watcher's own ``poll_pull`` and shares its persisted state, so an event reported
here is not reported again, here or by a CLI watcher on the same Store. The
write tools (``pr_navigate``, ``bridge_publish``, ``bridge_apply``) run the CLI
command in a child process, so every gate behaves as on the command line, and
are emitted only with ``allow_write``.

None of these tools carries a ``CODE_ACCESS`` grant, so a task-scoped server
never exposes them, and each call refuses inside an authenticated task context.
"""

from __future__ import annotations

import re
import subprocess
from inspect import getdoc
from pathlib import Path
from typing import Any

from lazybridge.policy import PolicyDenied

from lazytools.code_bridge._mcp import SUBMIT_TIMEOUT, CodeBridgeTools
from lazytools.code_bridge._mcp_fleet import _unscoped

MAX_NAVIGATE_ROUNDS = 10
#: A round is a reviewer run (the CLI allows it an hour) plus a fix job.
NAVIGATE_ROUND_TIMEOUT = 7200.0
_REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*/(?!\.{1,2}$)[A-Za-z0-9_.-]+")


class PrTools(CodeBridgeTools):
    """Watch, navigate, publish and apply pull-request work."""

    _is_lazy_tool_provider = True

    def __init__(self, root: str | None = None, *, projects_db: str | None = None, allow_write: bool = False) -> None:
        super().__init__(root, projects_db=projects_db)
        self._allow_write = allow_write

    # -- validation -------------------------------------------------------- #

    @staticmethod
    def _text(name: str, value: str) -> str:
        if not isinstance(value, str) or not value.strip() or value.startswith("-"):
            raise ValueError(f"{name} must be non-empty text and may not start with '-'")
        return value

    @classmethod
    def _texts(cls, name: str, values: list[str]) -> list[str]:
        if not isinstance(values, list) or not values:
            raise ValueError(f"{name} must be a non-empty list")
        return [cls._text(name, value) for value in values]

    @classmethod
    def _repo(cls, value: str) -> str:
        value = cls._text("repo", value)
        if not _REPO_RE.fullmatch(value):
            raise ValueError("repo must use owner/repo format")
        return value

    @staticmethod
    def _flag(name: str, value: bool) -> bool:
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be a boolean")
        return value

    # -- tools ------------------------------------------------------------- #

    def pr_watch_poll(self, pulls: list[str], no_ci: bool = False) -> dict[str, Any]:
        """Poll each pull request once and return the events not reported before.

        Args: pulls (list of 'owner/repo#number'); no_ci (the repository has no CI
        by policy: allow `ready` without checks). Event kinds: head, comment,
        inline, verdict, check, review, conflict, ready, merged, closed. State is
        shared with `lazytools-code-bridge watch`. Changes nothing on GitHub.
        A pull request that cannot be polled is listed under `errors`.
        """
        from lazytools.code_bridge import _cloud, _store, _watch

        self._flag("no_ci", no_ci)
        targets = []
        for value in dict.fromkeys(self._texts("pulls", pulls)):
            target = _watch.parse_pull(value)
            self._repo(target.repo)
            targets.append(target)
        store = _store.build_store()
        events: list[dict[str, str]] = []
        terminal: list[str] = []
        errors: list[dict[str, str]] = []
        for target in targets:
            try:
                found, done = _watch.poll_pull(target, store, no_ci=no_ci)
            except Exception as exc:  # earlier pulls are already marked seen: their events must still be returned
                errors.append({"pull": target.label, "error": _cloud.redact(str(exc))[:2000]})
                continue
            events += [event.as_dict() for event in found]
            if done:
                terminal.append(target.label)
        return {"ok": not errors, "events": events, "terminal": terminal, "errors": errors}

    def pr_navigate(
        self, repo: str, pr: int, reviewer_cmd: str, checks: list[str], max_rounds: int = 3
    ) -> dict[str, Any]:
        """Review and fix a pull request until it is merge-ready or escalated.

        Runs `lazytools-code-bridge navigate` and waits for it. Args: repo
        ('owner/repo'); pr (number); reviewer_cmd (command run on this machine for
        each review); checks (commands run on this machine against the head);
        max_rounds (1-10). Fix rounds are cloud jobs that push to the pull request.
        """
        if type(pr) is not int or pr < 1:
            raise ValueError("pr must be a positive integer")
        if type(max_rounds) is not int or not 1 <= max_rounds <= MAX_NAVIGATE_ROUNDS:
            raise ValueError(f"max_rounds must be an integer from 1 to {MAX_NAVIGATE_ROUNDS}")
        argv = ["navigate", "--repo", self._repo(repo), "--pr", str(pr),
                "--reviewer-cmd", self._text("reviewer_cmd", reviewer_cmd)]
        for check in self._texts("checks", checks):
            argv += ["--checks", check]
        argv += ["--max-rounds", str(max_rounds)]
        return self._call(argv, max_rounds * NAVIGATE_ROUND_TIMEOUT)

    def bridge_publish(self, task: str, repo: str, branch: str, base: str = "main") -> dict[str, Any]:
        """Publish a reviewed commit as a draft pull request.

        Runs `lazytools-code-bridge publish`, including its approval ticket: while
        approval is pending, `output` names the ticket and `ok` is false. Args:
        task (project task id); repo (local repository under the code root);
        branch (head branch); base (default 'main').
        """
        from lazytools.code_bridge import _engines

        task, branch, base = self._text("task", task), self._text("branch", branch), self._text("base", base)
        resolved = _engines.resolve_cwd(self._text("repo", repo), self._root)
        argv = ["publish", "--task", task, "--repo", str(resolved), "--branch", branch, "--base", base]
        try:
            code, out, err = self._invoke(argv, SUBMIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"the bridge command did not finish within {SUBMIT_TIMEOUT:g}s"}
        # `publish` prints text, not JSON.
        return {"ok": code == 0, "exit_code": code, "output": out.strip()[:4000],
                **({"stderr": err.strip()[:2000]} if err.strip() else {})}

    def bridge_apply(self, job: str, land: bool = False) -> dict[str, Any]:
        """Apply a finished cloud job's diff in a new worktree and branch.

        Runs `lazytools-code-bridge apply`. Args: job (id or unique prefix); land
        (also commit, push, and update or open the pull request).
        """
        from lazytools.code_bridge import _jobs, _store

        job = self._job_ref(self._text("job", job))
        self._flag("land", land)
        db_path = _store.default_db_path()
        record = None
        if db_path.is_file():
            store = _store.build_store(db_path)
            record = _jobs.find_job(store, _store.build_job_registry(store), job)
        if record is None:
            return {"ok": False, "exit_code": 1, "error": f"no job found matching {job!r}"}
        cwd = record.get("cwd")
        if not cwd or not Path(cwd).resolve().is_relative_to(Path(self._root)):
            raise PolicyDenied("bridge_apply", "The job is outside the code root.")
        argv = ["apply", str(record["job_id"]), *(["--land"] if land else []), "--json"]
        return self._call(argv, SUBMIT_TIMEOUT)

    def as_tools(self) -> list[Any]:
        from lazybridge import Tool

        methods: list[Any] = [self.pr_watch_poll]
        if self._allow_write:
            methods += [self.pr_navigate, self.bridge_publish, self.bridge_apply]
        # The whole docstring: by default only its first line reaches the client, without the argument formats.
        return [Tool.wrap(_unscoped(method), name=method.__name__, description=getdoc(method)) for method in methods]


__all__ = ["PrTools"]
