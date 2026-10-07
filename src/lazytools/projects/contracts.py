"""Durable task acceptance contracts, agreed BEFORE a task is delegated.

Ported from ``lazyceo.verification`` (the ``TaskContract`` half; the
``Verification`` state machine is ``verification.py``), mechanism only.

Left behind, as CEO policy: nothing in LazyCEO's own ``TaskContract`` actually
needed to be left behind here -- the model and every function below are
already generic (git-based review-base resolution, falsifiability-adjacent
structural checks, repo bookkeeping). The CEO-specific pieces are all in how
*LazyCEO decides* whether a review may be waived or who may accept one --
that lives in ``verification.accept``'s injectable ``authorization_check``.
"""

from __future__ import annotations

import contextlib
import json
import shlex
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from lazytools.projects.keys import TASK_CONTRACT_PREFIX

if TYPE_CHECKING:
    from lazybridge import Store

_VALID_RISKS = ("low", "medium", "high")


class TaskContract(BaseModel):
    model_config = ConfigDict(extra="allow")

    contract_id: str
    project_id: str
    task_index: int
    repo: str
    observable_result: str
    acceptance_criteria: list[str]
    required_checks: list[str]
    risk: Literal["low", "medium", "high"]
    allowed_effects: str
    #: Whether an independent review must be recorded before this task's work
    #: can be accepted. Defaults to TRUE deliberately.
    requires_review: bool = True
    #: The stated reason review was waived, if it was.
    review_waiver: str | None = None
    contract_review_findings: list[str] = Field(default_factory=list)
    contract_reviewed: bool | None = None
    #: The branch this task's work is meant to land on -- what an independent
    #: review's diff is measured against. See ``resolve_review_base``.
    base_branch: str | None = None
    #: Required-check command strings (matched EXACTLY) pre-approved to use a
    #: partial-suite flag even though this contract otherwise promises a full suite.
    allowed_check_exclusions: list[str] = Field(default_factory=list)
    created_at: datetime


def _contract_key(contract_id: str, *, prefix: str = TASK_CONTRACT_PREFIX) -> str:
    return f"{prefix}{contract_id}"


def open_task_contract(
    store: Store,
    *,
    project_id: str,
    task_index: int,
    repo: str,
    observable_result: str,
    acceptance_criteria: list[str],
    required_checks: list[str],
    risk: str,
    allowed_effects: str,
    requires_review: bool = True,
    review_waiver: str | None = None,
    contract_review_findings: list[str] | None = None,
    contract_reviewed: bool | None = None,
    base_branch: str | None = None,
    allowed_check_exclusions: list[str] | None = None,
    prefix: str = TASK_CONTRACT_PREFIX,
) -> TaskContract:
    """Create a task's durable acceptance contract, BEFORE it is delegated.

    ``contract_id`` -- not ``task_index`` -- is what every later attempt and
    verification references.
    """
    if not repo.strip():
        raise ValueError("repo must not be blank")
    if not observable_result.strip():
        raise ValueError("observable_result must not be blank")
    if not acceptance_criteria or not all(c.strip() for c in acceptance_criteria):
        raise ValueError("acceptance_criteria must be non-empty and contain only non-blank strings")
    if not required_checks or not all(c.strip() for c in required_checks):
        raise ValueError("required_checks must be non-empty and contain only non-blank strings")
    if risk not in _VALID_RISKS:
        raise ValueError(f"risk must be one of {list(_VALID_RISKS)} (got {risk!r})")
    if not allowed_effects.strip():
        raise ValueError("allowed_effects must not be blank")
    if not requires_review:
        if risk == "high":
            raise ValueError("review cannot be waived on a high-risk contract")
        if not (review_waiver or "").strip():
            raise ValueError("waiving review requires review_waiver: say why this work does not need one")

    contract_id = str(uuid.uuid4())
    contract = TaskContract(
        contract_id=contract_id,
        project_id=project_id,
        task_index=task_index,
        repo=repo,
        observable_result=observable_result,
        acceptance_criteria=list(acceptance_criteria),
        required_checks=list(required_checks),
        risk=risk,  # type: ignore[arg-type]
        allowed_effects=allowed_effects,
        requires_review=requires_review,
        review_waiver=review_waiver if not requires_review else None,
        contract_review_findings=list(contract_review_findings or []),
        contract_reviewed=contract_reviewed,
        base_branch=base_branch,
        allowed_check_exclusions=list(allowed_check_exclusions or []),
        created_at=datetime.now(UTC),
    )
    if not store.compare_and_swap(_contract_key(contract_id, prefix=prefix), None, contract.model_dump(mode="json")):
        raise ValueError(f"contract {contract_id!r} already exists")  # a uuid4 collision; never happens in practice
    return contract


def get_task_contract(store: Store, contract_id: str, *, prefix: str = TASK_CONTRACT_PREFIX) -> TaskContract | None:
    raw = store.read(_contract_key(contract_id, prefix=prefix))
    return TaskContract.model_validate(raw) if isinstance(raw, dict) else None


def repos_for_project(store: Store, project_id: str, *, prefix: str = TASK_CONTRACT_PREFIX) -> set[str]:
    """Every repo any task contract of this project has ever used."""
    repos: set[str] = set()
    for _key, raw in store.items(prefix=prefix):
        if not isinstance(raw, dict) or raw.get("project_id") != project_id:
            continue
        repo = raw.get("repo")
        if isinstance(repo, str) and repo:
            repos.add(repo)
    return repos


def find_contract_for_task(
    store: Store, project_id: str, task_index: int, *, prefix: str = TASK_CONTRACT_PREFIX
) -> TaskContract | None:
    """The contract governing one project task, or None if it never had one."""
    candidates = [
        TaskContract.model_validate(raw)
        for _key, raw in store.items(prefix=prefix)
        if isinstance(raw, dict) and raw.get("project_id") == project_id and raw.get("task_index") == task_index
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda c: c.created_at)


def _run_git(repo_path: Path, *args: str, timeout: float = 15.0) -> str | None:
    """One best-effort git subprocess. None on any failure."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except Exception:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _pr_base(repo_path: Path, *, timeout: float = 15.0) -> tuple[str, str] | None:
    """``(baseRefName, baseRefOid)`` for the PR open on the current branch, via ``gh``."""
    try:
        result = subprocess.run(
            ["gh", "pr", "view", "--json", "baseRefName,baseRefOid"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    try:
        parsed = json.loads(result.stdout)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    name, oid = parsed.get("baseRefName"), parsed.get("baseRefOid")
    if not name or not oid:
        return None
    return str(name), str(oid)


def _fetch(repo_path: Path, ref: str, *, timeout: float = 30.0) -> None:
    with contextlib.suppress(Exception):
        subprocess.run(
            ["git", "-C", str(repo_path), "fetch", "origin", ref], capture_output=True, timeout=timeout, check=False
        )


_DEFAULT_BASE_BRANCH = "main"


def resolve_review_base(repo_path: Path, contract: TaskContract) -> str | None:
    """The commit an independent review's diff must be measured against.

    NEVER the commit a delegate job happened to start from, and NEVER a bare
    local branch name either -- both are mutable from this function's point
    of view. The base is always either a real commit sha (a PR's
    ``baseRefOid``) or an explicit, freshly-fetched remote-tracking ref.
    Returns the merge-base with HEAD, or that base's own commit if
    merge-base cannot resolve (e.g. a detached HEAD sharing no history).
    None only when git cannot resolve anything at all.
    """
    pr = _pr_base(repo_path)
    if pr is not None:
        _, base_oid = pr
        base_ref = base_oid
    else:
        base_branch = contract.base_branch or _DEFAULT_BASE_BRANCH
        _fetch(repo_path, base_branch)
        base_ref = f"origin/{base_branch}"

    merge_base = _run_git(repo_path, "merge-base", base_ref, "HEAD")
    if merge_base:
        return merge_base
    return _run_git(repo_path, "rev-parse", base_ref)


def _diff_quiet(repo_path: Path, base_ref: str, head_ref: str) -> bool | None:
    try:
        diff = subprocess.run(
            ["git", "-C", str(repo_path), "diff", "--quiet", f"{base_ref}..{head_ref}"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except Exception:
        return None
    if diff.returncode not in (0, 1):
        return None
    return diff.returncode == 0


def diff_is_empty(repo_path: Path, base_ref: str, head_ref: str = "HEAD") -> bool:
    """Whether ``base_ref..head_ref`` shows NO change at all, computed purely from git.

    When ``head_ref`` is literally ``"HEAD"``, the working tree is checked
    too (a delegate's uncommitted edits are real, reviewable work). For any
    other, explicit ``head_ref`` (a historical sha), the working tree is
    current state and unrelated, so it is not consulted. Conservative on any
    failure: returns False (not empty).
    """
    quiet = _diff_quiet(repo_path, base_ref, head_ref)
    if quiet is not True:
        return False
    if head_ref != "HEAD":
        return True
    status = subprocess.run(
        ["git", "-C", str(repo_path), "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if status.returncode != 0:
        return False
    return not status.stdout.strip()


def snapshot_repo_state(repo_path: Path) -> tuple[str | None, bool]:
    """``(head_sha, working_tree_dirty)`` at THIS moment.

    For a caller that must judge emptiness against what an independent
    review actually SAW, not against a live re-query made after a
    long-running review call has let the repo move on.
    """
    head = _run_git(repo_path, "rev-parse", "HEAD")
    status = subprocess.run(
        ["git", "-C", str(repo_path), "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    dirty = status.returncode == 0 and bool(status.stdout.strip())
    return head, dirty


def diff_is_empty_snapshot(repo_path: Path, base_ref: str, head_sha: str, *, working_tree_dirty: bool) -> bool:
    """Like ``diff_is_empty``, judged against a SNAPSHOT taken before a long-running review started."""
    if working_tree_dirty:
        return False
    return _diff_quiet(repo_path, base_ref, head_sha) is True


_FULL_SUITE_MARKERS = ("full suite", "full test suite")
_EXCLUSION_FLAG_PREFIXES = ("--deselect", "-k", "--ignore")


def contract_requires_full_suite(contract: TaskContract) -> bool:
    haystack = " ".join([contract.observable_result, *contract.acceptance_criteria, *contract.required_checks])
    return any(marker in haystack.lower() for marker in _FULL_SUITE_MARKERS)


def check_uses_exclusion_flag(command: str) -> bool:
    try:
        tokens = shlex.split(command, posix=False)
    except ValueError:
        return True
    return any(token.startswith(prefix) for token in tokens for prefix in _EXCLUSION_FLAG_PREFIXES)


__all__ = [
    "TaskContract",
    "check_uses_exclusion_flag",
    "contract_requires_full_suite",
    "diff_is_empty",
    "diff_is_empty_snapshot",
    "find_contract_for_task",
    "get_task_contract",
    "open_task_contract",
    "repos_for_project",
    "resolve_review_base",
    "snapshot_repo_state",
]
