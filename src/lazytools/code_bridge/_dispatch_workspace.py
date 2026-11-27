"""Checkout settlement and publication for the project dispatcher."""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from lazytools.code_bridge import _cloud, _cloud_land, _gh
from lazytools.code_bridge._cloud import CloudError
from lazytools.code_bridge._navigator import _split_command

COMMIT_EXCLUDES = ("*.log", "**/__pycache__", ".pytest_cache", "**/.pytest_cache")


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


def _sanitize_check(command: str) -> str:
    """Hide machine-local paths in the published check list."""
    parts = _split_command(command)
    if not parts:
        return command
    parts[0] = PureWindowsPath(parts[0]).name  # splits on both separators on every platform
    # Only a path that starts a word or an option value is absolute: `tests/x.py` stays.
    return re.sub(r"""(?i)(?<![^\s="'])(?:[A-Z]:[\\/]|/)[^\s`"']+""", "<local>", " ".join(parts))


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
        title = str(data.get("title") or "").strip()[:200]
        if not title:
            lines = str(data.get("text") or "").strip().splitlines()
            sentence = re.split(r"(?<=[.!?])\s+", lines[0], maxsplit=1)[0].rstrip(".") if lines else ""
            title = _cloud_land._short_title(sentence) or task_id
        criteria = data.get("acceptance_criteria") or []
        body = "\n".join([
            f"Task: `{task_id}`", "", "Acceptance criteria:",
            *([f"- {item}" for item in criteria] or ["- none recorded"]), "",
            "Checks that passed:", *([f"- `{_sanitize_check(command)}`" for command in checks] or ["- none configured"]),
        ]) + "\n"
        return _gh.pr_create(repo, base=base, head=branch, title=title, body=body), None
    except (CloudError, subprocess.TimeoutExpired) as exc:
        return None, (" ".join(_cloud.redact(str(exc)).split())[:500] or "landing failed")
