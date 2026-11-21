"""Full public handoff contracts using a LOCAL bare Git remote, no GitHub credentials."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from lazytools.maintenance import (
    CheckSpec, ImprovementRequest, MaintenanceError, NavigationApproval,
    PublicationApproval, RepoSpec, Workspace, publish_draft_pr,
)
from lazytools.maintenance.navigation import navigate_published_pr


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def proposal_fixture(tmp_path: Path):
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "CI")
    git(root, "config", "user.email", "ci@example.test")
    (root / "module.py").write_text("def value():\n    return 1\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    sha = git(root, "rev-parse", "HEAD")
    state = tmp_path / "state"
    workspace = Workspace((RepoSpec("demo", root),), state)
    staging = tmp_path / "staging"
    git(tmp_path, "clone", "--no-local", str(root), str(staging))
    (staging / "module.py").write_text("def value():\n    return 2\n")
    git(staging, "add", ".")
    git(staging, "-c", "user.name=CI", "-c", "user.email=ci@example.test",
        "commit", "-qm", "isolated proposal")
    patch = subprocess.check_output(
        ["git", "-C", str(staging), "diff", "--binary", sha, "HEAD"]
    )
    artifact = state / "improvements" / "local-run-123"
    artifact.mkdir(parents=True)
    file = artifact / "proposal.patch"
    file.write_bytes(patch)
    request = ImprovementRequest(
        repo="demo", objective="Improve module behavior without breaking tests",
        allowed_paths=("module.py",), writer_engine="claude",
        reviewer_engine="codex", approved_local_proposal=True,
        checks=(CheckSpec("unit", "demo",
                          (sys.executable, "-c",
                           "from module import value; assert value() == 2")),),
    )
    proposal = {
        "run_id": "local-run-123", "status": "proposal_ready_for_human_review",
        "repo": "demo", "writer_engine": "claude", "reviewer_engine": "codex",
        "source_sha": sha, "patch_path": str(file),
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
        "rounds": [{
            "review_status": "completed", "findings": [],
            "verification": {"status": "passed_selected_checks"},
        }],
    }
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    git(root, "remote", "add", "test", str(remote))
    git(root, "push", "-q", "test", "HEAD:refs/heads/main")
    git(root, "remote", "remove", "test")
    return workspace, root, remote, request, proposal


def _mock_github(monkeypatch, remote: Path) -> None:
    import lazytools.maintenance.handoff as handoff
    from lazytools.code_bridge import _gh

    original = handoff._git

    def reroute_git(root, *args, **kwargs):
        if args[:3] == ("remote", "add", "origin"):
            return original(root, "remote", "add", "origin", str(remote), **kwargs)
        return original(root, *args, **kwargs)

    monkeypatch.setattr(handoff, "_git", reroute_git)
    monkeypatch.setattr(_gh, "run_gh", lambda *args, **kwargs:
                        "https://github.com/example/project/pull/42\n")


def test_publishes_only_draft_branch_rechecks_tests_and_leaves_main_untouched(
    proposal_fixture, monkeypatch
):
    workspace, source, remote, request, proposal = proposal_fixture
    _mock_github(monkeypatch, remote)
    result = publish_draft_pr(
        workspace, request, proposal,
        PublicationApproval("example/project", approved_publish_draft=True),
    )
    assert result["status"] == "draft_pr_created"
    assert result["pr_number"] == 42
    assert result["merged"] is False and result["merge_ready"] is False
    assert result["branch"] == "maintenance/local-run-123"
    pushed = subprocess.check_output(
        ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/maintenance/local-run-123"],
        text=True,
    ).strip()
    assert pushed == result["head_sha"]
    assert subprocess.check_output(
        ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"],
        text=True,
    ).strip() == proposal["source_sha"]
    assert git(source, "status", "--porcelain") == ""
    assert git(source, "rev-parse", "HEAD") == proposal["source_sha"]


def test_permission_and_review_integrity_gate_before_any_remote_action(proposal_fixture):
    workspace, _, _, request, proposal = proposal_fixture
    with pytest.raises(PermissionError, match="publication grant"):
        publish_draft_pr(workspace, request, proposal, PublicationApproval("example/project"))
    with pytest.raises(MaintenanceError, match="independently reviewed"):
        publish_draft_pr(
            workspace, request, {**proposal, "status": "needs_fixes"},
            PublicationApproval("example/project", approved_publish_draft=True),
        )
    with pytest.raises(MaintenanceError, match="SHA-256"):
        publish_draft_pr(
            workspace, request, {**proposal, "patch_sha256": "0" * 64},
            PublicationApproval("example/project", approved_publish_draft=True),
        )
    with pytest.raises(ValueError, match="github_repo"):
        PublicationApproval("../other")


def test_existing_remote_maintenance_branch_refuses_overwrite(proposal_fixture, monkeypatch):
    workspace, _, remote, request, proposal = proposal_fixture
    _mock_github(monkeypatch, remote)
    approval = PublicationApproval("example/project", approved_publish_draft=True)
    publish_draft_pr(workspace, request, proposal, approval)
    with pytest.raises(MaintenanceError, match="already exists"):
        publish_draft_pr(workspace, request, proposal, approval)


def test_navigator_delegates_to_existing_codebridge_state_machine(monkeypatch, proposal_fixture):
    workspace, _, _, request, _ = proposal_fixture
    import lazytools.code_bridge._navigator as existing

    called = {}

    class Backend:
        def __init__(self, **kwargs):
            called.update(kwargs)

    class Navigator:
        def __init__(self, backend, *, checks, max_rounds):
            called["checks"] = checks
            called["max_rounds"] = max_rounds

        def run(self, job_id):
            assert job_id == ""
            return existing.NavigatorRecord(job_id="navigation", status="merge-ready")

    monkeypatch.setattr(existing, "ProductionBackend", Backend)
    monkeypatch.setattr(existing, "Navigator", Navigator)
    published = {
        "status": "draft_pr_created", "github_repo": "example/project",
        "pr_number": 42, "reviewer_engine": "codex", "merged": False,
    }
    approval = NavigationApproval(
        reviewer_command="trusted-review-wrapper", check_commands=("pytest -q",),
        approved_navigate=True, max_rounds=2,
    )
    result = navigate_published_pr(published, request, approval)
    assert called["repo"] == "example/project"
    assert called["fix_engine"] == "claude"
    assert called["checks"] == ("pytest -q",)
    assert result["status"] == "merge_ready"
    assert result["requires_human_merge"] is True
    assert result["merged"] is False
    with pytest.raises(PermissionError, match="Navigator permission"):
        navigate_published_pr(
            published, request,
            NavigationApproval("trusted-review-wrapper", ("pytest -q",)),
        )
