"""Pre-launch check: is the working branch behind its upstream?

A local job that starts on a branch that is behind ``origin`` builds its fix
on stale code. ``check_freshness`` fetches the current branch's upstream and
counts the commits HEAD is missing. It is advisory: anything that stops the
answer (no git, detached HEAD, no upstream, offline) yields ``behind=None`` and
never blocks a launch.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

FETCH_TIMEOUT = 20.0


@dataclass(frozen=True)
class Freshness:
    behind: int | None  # None: could not be determined
    upstream: str | None = None
    fetched: bool = True  # False: the fetch failed, so the count may be out of date

    def stale(self) -> bool:
        return bool(self.behind)

    def message(self) -> str:
        n = self.behind or 0
        plural = "commit" if n == 1 else "commits"
        suffix = "" if self.fetched else " (fetch failed; count may be out of date)"
        return (
            f"branch is {n} {plural} behind {self.upstream}{suffix}; "
            "pull or rebase first, or the job builds on stale code"
        )


def _git(cwd: Path, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def check_freshness(cwd: Path, *, fetch: bool = True) -> Freshness:
    """Count the commits ``cwd``'s HEAD is behind its upstream after a fetch."""
    proc = _git(cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if proc is None or proc.returncode != 0 or not proc.stdout.strip():
        return Freshness(None)  # not a repo, detached HEAD, or no upstream configured
    upstream = proc.stdout.strip()
    fetched = True
    if fetch:
        remote = _git(cwd, "config", "--get", f"branch.{_branch(cwd)}.remote")
        name = remote.stdout.strip() if remote is not None and remote.returncode == 0 else ""
        proc = _git(cwd, "fetch", "--quiet", name or upstream.split("/", 1)[0], timeout=FETCH_TIMEOUT)
        fetched = proc is not None and proc.returncode == 0
    count = _git(cwd, "rev-list", "--count", "HEAD..@{upstream}")
    if count is None or count.returncode != 0 or not count.stdout.strip().isdigit():
        return Freshness(None, upstream, fetched)
    return Freshness(int(count.stdout.strip()), upstream, fetched)


def _branch(cwd: Path) -> str:
    proc = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    return proc.stdout.strip() if proc is not None and proc.returncode == 0 else ""
