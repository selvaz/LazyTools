"""Opt-in verification of operator-approved commands at immutable Git revisions.

A detached, disposable Git worktree protects configured checkouts from test
side effects. This is *not* a network or OS process sandbox; only run checks
from trusted source revisions. No commands are taken from a PR/manifest/LLM.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .core import MaintenanceError, Workspace, git_status, resolve_sha

_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class CheckSpec:
    """An explicit operator-granted test command, NOT repository configuration.

    argv is always an argument vector. No shell, interpolation, user prompt or
    untrusted PR can add a check. The caller is responsible for trusting the
    code that the executable will run.
    """

    id: str
    repo: str
    argv: tuple[str, ...]
    timeout_seconds: float = 60.0
    max_output_chars: int = 2_000

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _ID.fullmatch(self.id):
            raise ValueError("Check id must be a safe nonempty identifier")
        if not isinstance(self.repo, str) or not _ID.fullmatch(self.repo):
            raise ValueError("Check repository must be a configured safe alias")
        if not isinstance(self.argv, tuple) or not 1 <= len(self.argv) <= 32:
            raise ValueError("Check argv must be a tuple of 1-32 strings")
        if any(not isinstance(arg, str) or not arg or "\0" in arg or len(arg) > 500 for arg in self.argv):
            raise ValueError("Invalid check argv element")
        if not 0 < self.timeout_seconds <= 600:
            raise ValueError("Check timeout_seconds must be in (0, 600]")
        if not 100 <= self.max_output_chars <= 10_000:
            raise ValueError("Check max_output_chars must be between 100 and 10000")


def _test_environment(home: Path) -> dict[str, str]:
    """Only essential process variables survive; no ambient API/SSH secrets."""
    allowed = ("PATH", "SYSTEMROOT", "SystemRoot", "WINDIR", "PATHEXT",
               "LANG", "LC_ALL", "LD_LIBRARY_PATH")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update({
        "HOME": str(home),
        "USERPROFILE": str(home),
        "TMP": str(home),
        "TEMP": str(home),
        "TMPDIR": str(home),
        "CI": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_NO_INDEX": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    return env


def _git_worktree(root: Path, *args: str) -> None:
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "SYSTEMROOT", "SystemRoot", "WINDIR",
                    "TMP", "TEMP", "TMPDIR")
        if key in os.environ
    }
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        p = subprocess.run(
            ["git", "-C", str(root), "worktree", *args],
            env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=40, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MaintenanceError(f"Git worktree operation failed: {type(exc).__name__}") from exc
    if p.returncode:
        raise MaintenanceError(
            f"Git worktree command failed ({p.returncode}): {p.stderr[:200]}"
        )


def _run_one(
    check: CheckSpec, sha: str, working_copy: Path, home: Path
) -> dict[str, object]:
    start = time.perf_counter()
    try:
        # Temporary files bound memory use even if a child prints massive logs.
        with tempfile.TemporaryFile(mode="w+b") as stdout, tempfile.TemporaryFile(mode="w+b") as stderr:
            try:
                proc = subprocess.run(
                    list(check.argv), cwd=working_copy, env=_test_environment(home),
                    stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                    timeout=check.timeout_seconds, check=False,
                )
                exit_code: int | None = proc.returncode
                status = "passed" if proc.returncode == 0 else "failed"
            except subprocess.TimeoutExpired:
                exit_code = None
                status = "timed_out"
            except OSError:
                exit_code = None
                status = "unavailable"
            stdout.seek(0)
            stderr.seek(0)
            raw_out = stdout.read(check.max_output_chars + 1)
            raw_err = stderr.read(check.max_output_chars + 1)
            out = raw_out[:check.max_output_chars].decode("utf-8", "replace")
            err = raw_err[:check.max_output_chars].decode("utf-8", "replace")
            clipped = len(raw_out) > check.max_output_chars or len(raw_err) > check.max_output_chars
    except OSError as exc:
        raise MaintenanceError(f"Could not capture approved test output: {type(exc).__name__}") from exc
    return {
        "id": check.id, "repo": check.repo, "sha": sha,
        "status": status, "exit_code": exit_code,
        "seconds": round(time.perf_counter() - start, 4),
        "stdout": out, "stderr": err, "output_truncated": clipped,
    }


def run_approved_checks(
    workspace: Workspace,
    shas: dict[str, str],
    checks: tuple[CheckSpec, ...],
) -> dict[str, object]:
    """Run an explicitly granted check set in disposable detached worktrees.

    No tests by default. Each checkout is verified against its pinned commit
    before and after, and results never imply the whole product is verified.
    """
    if not checks:
        return {"status": "not_run", "tests": None}
    allowed = workspace.paths()
    if len(checks) > 20:
        raise ValueError("A run may execute no more than 20 approved checks")
    names: set[str] = set()
    for check in checks:
        if check.id in names:
            raise ValueError(f"Duplicate check id: {check.id}")
        names.add(check.id)
        if check.repo not in allowed or check.repo not in shas:
            raise ValueError(f"Check {check.id} uses a repository outside the allowlist")

    for name, root in allowed.items():
        # Approved checks run at committed SHAs only. A dirty working tree
        # could have been indexed differently, so verification must not
        # certify a mismatched code snapshot even in exploratory dirty mode.
        if resolve_sha(root) != shas[name] or git_status(root) != "clean":
            raise MaintenanceError(f"{name} changed before verification; refusing stale check")

    workspace.state_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="maintenance-verify-", dir=workspace.state_dir) as temp:
        work_dir = Path(temp)
        for check in checks:
            repo_id = check.repo
            root = allowed[repo_id]
            # Every check gets a fresh detached worktree; a previous test
            # may modify its own files, so sharing one would violate the
            # promise that every check observed exactly the pinned SHA.
            checkout = work_dir / f"{repo_id}-{check.id}"
            _git_worktree(root, "add", "--detach", str(checkout), shas[repo_id])
            try:
                if resolve_sha(checkout) != shas[repo_id] or git_status(checkout) != "clean":
                    raise MaintenanceError(f"Detached check worktree is not clean at SHA: {repo_id}")
                home = work_dir / f"home-{check.id}"
                home.mkdir(mode=0o700)
                results.append(_run_one(check, shas[repo_id], checkout, home))
            finally:
                _git_worktree(root, "remove", "--force", str(checkout))
            if resolve_sha(root) != shas[repo_id] or git_status(root) != "clean":
                raise MaintenanceError(f"{repo_id} source checkout changed during verification")

    statuses = {str(row["status"]) for row in results}
    overall = (
        "failed" if "failed" in statuses else
        "incomplete" if statuses & {"timed_out", "unavailable"} else
        "passed_selected_checks"
    )
    return {"status": overall, "tests": results, "scope": "operator_approved_checks_only",
            "trusted_code_required": True, "network_isolation": False}
