"""Explicit GitHub draft-PR handoff for independently reviewed local patches.

The local writer/reviewer logic belongs to MaintenanceImprover; published PR
review/fix cycles belong to the EXISTING code_bridge.Navigator. This module
only validates the patch and performs the operator-authorized handoff.
"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core import MaintenanceError, RepoSpec, Workspace, git_status, resolve_sha
from .improvement import ImprovementRequest, _git
from .verification import run_approved_checks

_REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,99}/[A-Za-z0-9_.-]{1,100}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,120}$")


@dataclass(frozen=True)
class PublicationApproval:
    """Publication is a separate privilege from proposing a *local* patch."""

    github_repo: str
    base_branch: str = "main"
    approved_publish_draft: bool = False

    def __post_init__(self) -> None:
        if not _REPOSITORY.fullmatch(self.github_repo) or ".." in self.github_repo:
            raise ValueError("github_repo must be an explicit OWNER/REPO")
        if (not _BRANCH.fullmatch(self.base_branch)
                or ".." in self.base_branch or "//" in self.base_branch
                or self.base_branch.endswith(("/", "."))):
            raise ValueError("Unsafe base branch")


def _validated_patch(
    workspace: Workspace, request: ImprovementRequest, proposal: dict[str, Any]
) -> tuple[Path, str]:
    if not request.approved_local_proposal:
        raise PermissionError("Local proposal approval is required")
    if proposal.get("status") != "proposal_ready_for_human_review":
        raise MaintenanceError("Only independently reviewed, checks-green local proposals may publish")
    if proposal.get("repo") != request.repo:
        raise MaintenanceError("Proposal repository mismatch")
    if proposal.get("writer_engine") != request.writer_engine or proposal.get("reviewer_engine") != request.reviewer_engine:
        raise MaintenanceError("Proposed engine identities do not match the approved request")
    rounds = proposal.get("rounds")
    if not isinstance(rounds, list) or not rounds:
        raise MaintenanceError("Missing completed review history")
    final = rounds[-1]
    if (not isinstance(final, dict) or final.get("findings") != []
            or final.get("review_status") != "completed"
            or final.get("verification", {}).get("status") != "passed_selected_checks"):
        raise MaintenanceError("Missing clean independent review and passing approved checks")
    # The caller's dict is not authority for a clean review: require it to
    # match the durable report written by MaintenanceImprover for this run.
    run_id = proposal.get("run_id")
    if not isinstance(run_id, str) or re.fullmatch(r"[0-9a-f]{32}", run_id) is None:
        raise MaintenanceError("Proposal must carry its generated 32-hex run id")
    artifact_dir = workspace.state_dir / "improvements" / run_id
    report_path = artifact_dir / "result.json"
    expected_patch = artifact_dir / "proposal.patch"
    if (artifact_dir.is_symlink() or report_path.is_symlink()
            or not report_path.is_file()):
        raise MaintenanceError("Missing or unsafe saved improvement evidence")
    try:
        saved = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise MaintenanceError("Saved improvement evidence could not be read") from exc
    if saved != proposal or saved.get("report_json") != str(report_path):
        raise MaintenanceError("Proposal differs from the saved independent-review evidence")
    source = workspace.paths().get(request.repo)
    if source is None:
        raise ValueError("Unknown repository alias")
    sha = proposal.get("source_sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise MaintenanceError("Invalid source commit SHA")
    if resolve_sha(source) != sha or git_status(source) != "clean":
        raise MaintenanceError("Original checkout is no longer at the scanned revision")
    patch_name = proposal.get("patch_path")
    if not isinstance(patch_name, str):
        raise MaintenanceError("Missing patch artifact")
    patch = Path(patch_name)
    if patch.is_symlink():
        raise MaintenanceError("Patch artifact cannot be a symlink")
    patch = patch.resolve(strict=True)
    if not patch.is_file() or patch != expected_patch.resolve():
        raise MaintenanceError("Patch must exactly match this improvement run's artifact")
    data = patch.read_bytes()
    if (not data or len(data) > request.max_patch_bytes
            or hashlib.sha256(data).hexdigest() != proposal.get("patch_sha256")):
        raise MaintenanceError("Patch size or SHA-256 does not match immutable review evidence")
    return patch, sha


def publish_draft_pr(
    workspace: Workspace,
    request: ImprovementRequest,
    proposal: dict[str, Any],
    approval: PublicationApproval,
) -> dict[str, Any]:
    """Create one *draft* PR only after an explicit separate publish grant.

    Strictly require the remote base head to equal the scanned source commit,
    and never force-push. For any later PR review/fix use CodeBridge Navigator.
    """
    if not approval.approved_publish_draft:
        raise PermissionError("An explicit draft-PR publication grant is required")
    patch, source_sha = _validated_patch(workspace, request, proposal)
    from lazytools.code_bridge import _gh

    branch = "maintenance/" + str(proposal["run_id"])
    if not _BRANCH.fullmatch(branch):
        raise MaintenanceError("Invalid generated maintenance branch")
    source = workspace.paths()[request.repo]
    with tempfile.TemporaryDirectory(prefix="maintenance-pr-", dir=workspace.state_dir) as workdir:
        root = Path(workdir) / "repo"
        _git(workspace.state_dir, "clone", "--no-local", "--no-checkout",
             "--", str(source), str(root))
        _git(root, "remote", "remove", "origin")
        _git(root, "checkout", "-b", branch, source_sha)
        _git(root, "apply", "--index", "--", str(patch))
        names = _git(root, "diff", "--cached", "--name-only", "--no-renames", "-z")
        changed = [p.decode("utf-8", "replace") for p in names.split(b"\0") if p]
        if not changed or any(p not in request.allowed_paths for p in changed):
            raise MaintenanceError("Patch changed a file outside approved scope")
        if any((root / p).is_symlink() for p in changed):
            raise MaintenanceError("Patch cannot introduce symlinks")
        _git(root, "diff", "--cached", "--check")
        _git(root, "-c", "user.name=LazyTools Maintenance",
             "-c", "user.email=maintenance@invalid.example",
             "-c", "commit.gpgsign=false", "commit", "-qm", "Proposed maintenance improvement")
        candidate_sha = resolve_sha(root)
        remote = f"https://github.com/{approval.github_repo}.git"
        _git(root, "remote", "add", "origin", remote)
        observed = _git(root, "ls-remote", "--heads", "origin", approval.base_branch)
        heads = observed.decode("utf-8", "replace").split()
        if not heads or heads[0] != source_sha:
            raise MaintenanceError("Remote base branch moved since scan; rescan before publishing")
        existing = _git(root, "ls-remote", "--heads", "origin", branch)
        if existing.strip():
            raise MaintenanceError("A maintenance branch already exists; manual reconciliation required")
        checks = run_approved_checks(
            Workspace(repos=(RepoSpec(request.repo, root),),
                      state_dir=Path(workdir) / "verify"),
            {request.repo: candidate_sha},
            request.checks,
        )
        if checks["status"] != "passed_selected_checks":
            raise MaintenanceError("Reverification on publication commit did not pass")
        _git(root, "push", "--porcelain", "origin", f"HEAD:refs/heads/{branch}")
        body = (
            "Automated maintenance **proposal**; human review required. Never auto-merge.\n\n"
            f"Source SHA: {source_sha}\n"
            f"Proposal run: {proposal['run_id']}\n"
            f"Writer/reviewer: {request.writer_engine} / {request.reviewer_engine}\n"
            "Independent review: completed; approved checks: passed on published commit.\n"
        )
        output = _gh.run_gh(
            "pr", "create", "--draft", "--repo", approval.github_repo,
            "--base", approval.base_branch, "--head", branch,
            "--title", "maintenance: reviewed code improvement",
            "--body-file", "-", stdin=body,
        )
        number = _gh.pr_number_from_url(output)
        if number is None:
            raise MaintenanceError("Draft PR command returned no parseable URL; inspect remote branch")
        return {
            "status": "draft_pr_created", "github_repo": approval.github_repo,
            "pr_number": number, "pr_url": output.strip(), "head_sha": candidate_sha,
            "base_sha": source_sha, "branch": branch,
            "reviewer_engine": request.reviewer_engine,
            "merge_ready": False, "merged": False,
        }
