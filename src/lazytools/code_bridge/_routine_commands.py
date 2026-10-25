"""Capability validators: a permissive rule regex never bypasses these checks."""

from __future__ import annotations

import re
import subprocess
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lazytools.code_bridge._routine import RoutinePolicy

VALIDATORS = frozenset({"pytest", "gh-read", "gh-api", "gh-pr-body", "git-add", "git-fetch", "git-merge", "git-push", "git-status", "macro-verifier"})
_SENSITIVE = (".env*", ".npmrc", ".netrc", ".pypirc", ".git", "*.pem", "*.key", "*.pfx", "*.p12", "*.token", "*.jks", "id_*", "*secret*", "*credential*")
_SIMPLE_FLAGS = frozenset({"-q", "-x", "-v", "-vv", "--no-header", "-s"})


def git(cwd: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=10)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def repository(value: str, *, remote: bool = False) -> str | None:
    value = value.rstrip("/")
    if remote:
        value = value.removesuffix(".git")
    for prefix in ("https://github.com/", "http://github.com/", "ssh://git@github.com/", "git@github.com:", "github.com/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    if not re.fullmatch(r"[\w-][\w.-]*/[\w-][\w.-]*", value) or ".." in value:
        return None
    return value.casefold()


def path_parts_safe(value: str) -> bool:
    """Reject Windows aliases before PurePath/resolve can hide their spelling."""
    return all(not part.endswith((".", " ")) and "~" not in part for part in re.split(r"[/\\]", value))


def relative_path(value: str) -> bool:
    from lazytools.code_bridge._approvals import _repo_relative

    return path_parts_safe(value) and _repo_relative(value) and value not in (".", "./") and not value.startswith("-") and not any(c in value for c in ":*?[]{}\\")


def _within_repo(policy: RoutinePolicy, value: str) -> bool:
    if policy.cwd is None:
        return False
    try:
        (policy.cwd / value).resolve().relative_to(policy.cwd.resolve())
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def branch(value: str, prefixes: tuple[str, ...]) -> bool:
    parts = value.split("/")
    return len(parts) == 2 and parts[0] in prefixes and bool(re.fullmatch(r"\w[\w.-]*", parts[1])) and ".." not in value and not parts[1].endswith((".", ".lock"))


def basetemp_safe(policy: RoutinePolicy, value: str) -> bool:
    from lazytools.code_bridge._routine import _inside

    if not _inside(value, policy.scratch_root, child=True):
        return False
    # pytest deletes basetemp. Never let it delete bridge state, even when
    # the operator placed the bridge temp tree below scratch_root.
    reserved = (policy.bridge_temp_root, (Path(policy.scratch_root) / "lazytools-bridge").as_posix())
    if any(_inside(value, root) or _inside(root, value) for root in reserved):
        return False
    path = Path(value)
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if path.is_symlink() or not path.is_dir() or not policy.job_id:
        return False
    try:
        first = path.resolve().relative_to(Path(policy.scratch_root).resolve()).parts[0]
    except (ValueError, OSError, IndexError, RuntimeError):
        return False
    # A single-token nonce makes the last '-' the exact job-id boundary,
    # including when another job id starts with this job's id plus '-'.
    return re.fullmatch(re.escape(f"bridge-{policy.job_id}-") + r"[A-Za-z0-9_]+", first) is not None


def pytest_safe(policy: RoutinePolicy, args: list[str]) -> bool:
    start = _python_prefix(policy, args)
    if start is None or args[start:start + 2] != ["-m", "pytest"]:
        return False
    basetemp = False
    index = start + 2
    while index < len(args):
        arg = args[index]
        if arg == "--basetemp":
            if basetemp or index + 1 == len(args) or not basetemp_safe(policy, args[index + 1]):
                return False
            basetemp = True
            index += 2
            continue
        if arg in _SIMPLE_FLAGS or re.fullmatch(r"-r[A-Za-z]+|--tb=[A-Za-z]+|--maxfail=[0-9]+", arg):
            index += 1
            continue
        if arg in ("-k", "-m", "-p"):
            if index + 1 == len(args):
                return False
            value = args[index + 1]
            if not value or value.startswith(("-", "@")) or (arg == "-p" and value != "no:cacheprovider"):
                return False
            index += 2
            continue
        # Unknown, attached and abbreviated flags all fail closed.
        if arg.startswith(("-", "@")):
            return False
        test_path = arg.split("::", 1)[0]
        if not relative_path(test_path) or not _within_repo(policy, test_path):
            return False
        parts = PurePosixPath(test_path).parts
        if not any(
            parts[:len(PurePosixPath(root).parts)] == PurePosixPath(root).parts
            and _under_test_root(policy, test_path, root)
            for root in policy.test_roots
        ):
            return False
        index += 1
    return basetemp


def _under_test_root(policy: RoutinePolicy, value: str, root: str) -> bool:
    if policy.cwd is None:
        return False
    try:
        (policy.cwd / value).resolve().relative_to((policy.cwd / root).resolve())
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def _python_prefix(policy: RoutinePolicy, args: list[str]) -> int | None:
    if not args or args[0] != policy.python_executable:
        return None
    return 2 if args[1:2] == ["-B"] else 1


def _gh_repo_safe(policy: RoutinePolicy, args: list[str]) -> bool:
    index = 0
    while index < len(args):
        arg = args[index]
        value = None
        if arg in ("-R", "--repo"):
            index += 1
            if index == len(args):
                return False
            value = args[index]
        elif arg.startswith("--repo="):
            value = arg.partition("=")[2]
        if value is not None and (not policy.origin_repo or repository(value) != policy.origin_repo):
            return False
        index += 1
    return True


def validate(policy: RoutinePolicy, validator: str, args: list[str]) -> bool:
    from lazytools.code_bridge._routine import _gh_read, _inside

    if validator == "pytest":
        return pytest_safe(policy, args)
    if validator == "gh-read":
        return args[:2] in (["gh", "pr"], ["gh", "run"]) and _gh_repo_safe(policy, args) and _gh_read(" ".join(f"'{a}'" if " " in a or "|" in a else a for a in args))
    if validator == "gh-api":
        if len(args) != 3 or args[:2] != ["gh", "api"]:
            return False
        endpoint = re.fullmatch(r"repos/(selvaz/[\w-][\w.-]*)/(?:actions/jobs/[0-9]+/logs|(?:pulls|issues)/[0-9]+/(?:comments|reviews))", args[2])
        return bool(policy.origin_repo and endpoint and ".." not in endpoint[1] and repository(endpoint[1]) == policy.origin_repo)
    if validator == "gh-pr-body":
        return len(args) == 6 and args[:3] == ["gh", "pr", "edit"] and args[3].isdigit() and args[4] == "--body-file" and _inside(args[5], policy.bridge_temp_root, child=True)
    if validator == "git-add":
        if args[:3] != ["git", "add", "--"] or len(args) < 4 or policy.cwd is None:
            return False
        return all(
            relative_path(path) and _within_repo(policy, path)
            and not any(fnmatch(part.casefold(), pattern) for part in PurePosixPath(path).parts for pattern in _SENSITIVE)
            and not (policy.cwd / path).is_dir()  # directories can contain hidden sensitive files
            for path in args[3:]
        )
    if validator == "git-fetch":
        return args == ["git", "fetch", "origin"]
    if validator == "git-status":
        return args == ["git", "status", "--short"]
    if validator == "git-push":
        return len(args) == 4 and args[:3] == ["git", "push", "origin"] and branch(args[3], ("feat", "fix", "docs"))
    if validator == "git-merge":
        if len(args) != 4 or args[:3] != ["git", "merge", "--no-edit"] or not args[3].startswith("origin/") or policy.cwd is None:
            return False
        current = git(policy.cwd, "symbolic-ref", "--quiet", "--short", "HEAD")
        target = args[3].removeprefix("origin/")
        return bool(current and current.casefold() not in ("main", "master") and (target == "main" or branch(target, ("feat", "fix", "docs", "research"))))
    if validator == "macro-verifier":
        if len(args) not in (7, 9) or _python_prefix(policy, args) != 2:
            return False
        return bool(
            re.fullmatch(r"scripts/verify_macro_\w+\.py", args[2])
            and args[3:6] == ["--source", "C:/ProgramData/InvestmentCommittee/live/db/macro_releases.sqlite", "--destination-root"]
            and _inside(args[6], policy.scratch_root)
            and (len(args) == 7 or (args[7] == "--families" and re.fullmatch(r"[\w,]+", args[8])))
        )
    return False
