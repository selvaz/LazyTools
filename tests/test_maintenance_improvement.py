"""Self-improvement uses EXISTING coding/reviewer interfaces on private clones."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from lazytools.maintenance import (
    CheckSpec,
    ImprovementRequest,
    MaintenanceError,
    MaintenanceImprover,
    RepoSpec,
    Workspace,
)
from lazytools.maintenance.improvement import REVIEW_MARKER, _parse_review


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def source(tmp_path: Path):
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "CI")
    git(root, "config", "user.email", "ci@example.test")
    (root / "module.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    git(root, "add", "module.py")
    git(root, "commit", "-qm", "base")
    sha = git(root, "rev-parse", "HEAD")
    space = Workspace((RepoSpec("demo", root),), tmp_path / "state")
    report = {
        "schema_version": 1, "run_id": "scan-fixture",
        "status": "review_required", "verified": False,
        "sha": {"demo": sha}, "dirty": {"demo": "clean"},
        "snapshots": {"demo": {"status": "ready", "sha": sha}},
        "changes": {"demo": [{"status": "M", "path": "module.py"}]},
    }
    return space, root, sha, report


class Writer:
    def __init__(self, results=(2,)):
        self.results = results
        self.calls = []
        self.clone_paths = []

    async def run(self, **kwargs):
        root = Path(kwargs["cwd"])
        assert (root / ".git").is_dir()
        assert git(root, "remote") == ""  # No remote to push to
        assert "only" in kwargs["task"].lower()  # Explicit scope instructions
        self.calls.append(kwargs["task"])
        self.clone_paths.append(root)
        value = self.results[min(len(self.calls) - 1, len(self.results) - 1)]
        (root / "module.py").write_text(f"def value():\n    return {value}\n")
        return {"result": "modified isolated file", "content_is_untrusted": True}


class Reviewer:
    def __init__(self, verdicts=((),)):
        self.verdicts = verdicts
        self.calls = []

    async def run(self, **kwargs):
        assert "diff_ref" in kwargs
        assert kwargs["repo_path"]
        assert REVIEW_MARKER in kwargs["task"]
        self.calls.append(kwargs)
        findings = self.verdicts[min(len(self.calls) - 1, len(self.verdicts) - 1)]
        return "Native reviewer session header\n" + REVIEW_MARKER + "\n" + json.dumps({
            "reviewer_model": "codex-review-model",
            "findings": [{"severity": severity, "message": msg} for severity, msg in findings],
        })


def request(**changes):
    base = {
        "repo": "demo", "objective": "Improve the value function and preserve behavior",
        "allowed_paths": ("module.py",), "approved_local_proposal": True,
        "max_rounds": 2,
    }
    base.update(changes)
    return ImprovementRequest(**base)


def test_denies_unapproved_proposal_and_same_engine(source):
    space, root, sha, report = source
    with pytest.raises(ValueError, match="different engine"):
        request(writer_engine="codex", reviewer_engine="codex")
    with pytest.raises(ValueError, match="approved file"):
        request(allowed_paths=("../secret",))
    with pytest.raises(ValueError, match="repository-relative files"):
        request(allowed_paths=())
    with pytest.raises(PermissionError, match="local proposal approval"):
        MaintenanceImprover(space).run(report, request(approved_local_proposal=False))
    assert git(root, "rev-parse", "HEAD") == sha


def test_two_rounds_independent_review_approved_tests_and_source_unchanged(source):
    space, root, base_sha, report = source
    writer = Writer((2, 3))
    reviewer = Reviewer(((("P2", "Value must be 3"),), ()))
    check = CheckSpec(
        "verify_value", "demo",
        (sys.executable, "-c", "from module import value; assert value() == 3"),
    )
    result = MaintenanceImprover(space).run(
        report, request(checks=(check,)), writer_tool=writer, reviewer_tool=reviewer
    )
    assert result["status"] == "proposal_ready_for_human_review", result
    assert result["writer_engine"] == "claude"
    assert result["reviewer_engine"] == "codex"
    assert result["pushed"] is False and result["merged"] is False
    assert len(result["rounds"]) == 2
    assert len(reviewer.calls) == 2
    assert "Value must be 3" in writer.calls[1]
    assert result["rounds"][1]["verification"]["status"] == "passed_selected_checks"
    assert Path(result["patch_path"]).exists()
    assert b"return 3" in Path(result["patch_path"]).read_bytes()
    assert Path(result["report_json"]).is_file()
    assert json.loads(Path(result["report_json"]).read_text())["status"] == result["status"]
    assert (root / "module.py").read_text() == "def value():\n    return 1\n"
    assert git(root, "rev-parse", "HEAD") == base_sha
    assert git(root, "status", "--porcelain") == ""
    assert all(not path.exists() for path in writer.clone_paths)


def test_p0_escalation_stops_without_second_write(source):
    space, root, _, report = source
    writer, reviewer = Writer(), Reviewer(((("P0", "Critical vulnerability"),),))
    result = MaintenanceImprover(space).run(
        report, request(), writer_tool=writer, reviewer_tool=reviewer
    )
    assert result["status"] == "escalated"
    assert len(writer.calls) == 1
    assert result["rounds"][0]["findings"][0]["severity"] == "P0"
    assert Path(result["patch_path"]).exists()
    assert (root / "module.py").read_text().endswith("return 1\n")


def test_missing_review_contract_is_not_clean(source):
    space, _, _, report = source
    class Malformed:
        async def run(self, **kwargs):
            return "Looks good to me"

    result = MaintenanceImprover(space).run(
        report, request(), writer_tool=Writer(), reviewer_tool=Malformed()
    )
    assert result["status"] == "failed"
    assert "marker" in result["error"]
    assert not result["merged"]


def test_writer_out_of_scope_is_refused(source):
    space, _, _, report = source
    class OutOfScope:
        async def run(self, **kwargs):
            Path(kwargs["cwd"], "surprise.py").write_text("SECRET = 1\n")
            return {"result": "wrote"}

    result = MaintenanceImprover(space).run(
        report, request(), writer_tool=OutOfScope(), reviewer_tool=Reviewer()
    )
    assert result["status"] == "failed"
    assert "outside approved scope" in result["error"]
    assert result["rounds"] == []


def test_no_checks_cannot_mark_proposal_ready(source):
    space, _, _, report = source
    result = MaintenanceImprover(space).run(
        report, request(), writer_tool=Writer(), reviewer_tool=Reviewer()
    )
    assert result["status"] == "needs_approved_checks"
    assert result["patch_path"]
    assert result["rounds"][0]["findings"] == []


def test_stale_sha_and_incomplete_scan_refuse_to_run(source):
    space, root, _, report = source
    with pytest.raises(MaintenanceError, match="completed read-only"):
        MaintenanceImprover(space).run(
            {**report, "status": "verification_failed"}, request()
        )
    root.joinpath("module.py").write_text("def value():\n    return 9\n")
    with pytest.raises(MaintenanceError, match="Source changed"):
        MaintenanceImprover(space).run(report, request())


def test_json_contract_rejects_unknown_severity_and_missing_identity():
    valid = REVIEW_MARKER + "\n" + json.dumps({
        "reviewer_model": "codex", "findings": [{"severity": "P7", "message": "wrong"}],
    })
    with pytest.raises(MaintenanceError, match="severity"):
        _parse_review(valid)
    with pytest.raises(MaintenanceError, match="model identity"):
        _parse_review(REVIEW_MARKER + '\n{"findings":[]}')


def test_failed_approved_check_never_ready(source):
    space, root, _, report = source
    check = CheckSpec("reject", "demo", (sys.executable, "-c", "raise AssertionError()"))
    result = MaintenanceImprover(space).run(
        report, request(checks=(check,), max_rounds=1),
        writer_tool=Writer(), reviewer_tool=Reviewer(),
    )
    assert result["status"] == "checks_failed"
    assert result["rounds"][0]["verification"]["status"] == "failed"
    assert not result["pushed"]
    assert (root / "module.py").read_text().endswith("return 1\n")


def test_lazybridge_plan_composes_scan_and_independent_reviewed_local_proposal(source):
    from lazytools.maintenance import MaintenanceSelfImprovementPlan

    space, root, sha, report = source
    calls = []

    class StubMaintenance:
        workspace = space

        def execute(self, *, base_refs=None):
            calls.append(dict(base_refs or {}))
            return report

    check = CheckSpec(
        "value_test", "demo",
        (sys.executable, "-c", "from module import value; assert value() == 2"),
    )
    flow = MaintenanceSelfImprovementPlan(
        StubMaintenance(), request(checks=(check,), max_rounds=1),
        base_refs={"demo": sha},
    )
    agent = flow.build_agent(writer_tool=Writer(), reviewer_tool=Reviewer())
    assert len(agent.engine.steps) == 2
    assert [step.name for step in agent.engine.steps] == [
        "maintenance_scan", "maintenance_propose",
    ]
    result = flow.run(writer_tool=Writer(), reviewer_tool=Reviewer())
    assert result["status"] == "proposal_ready_for_human_review", result
    assert calls == [{"demo": sha}]
    assert (root / "module.py").read_text().endswith("return 1\n")


def test_self_improvement_plan_approval_before_read_only_scan(source):
    from lazytools.maintenance import MaintenanceSelfImprovementPlan

    space, _, _, report = source
    invoked = []

    class StubMaintenance:
        workspace = space

        def execute(self, *, base_refs=None):
            invoked.append(True)
            return report

    flow = MaintenanceSelfImprovementPlan(
        StubMaintenance(), request(approved_local_proposal=False),
    )
    with pytest.raises(PermissionError, match="Explicit approval"):
        flow.run(writer_tool=Writer(), reviewer_tool=Reviewer())
    assert invoked == []


def test_reverse_engine_pair_reuses_same_contract(source):
    space, _, _, report = source
    check = CheckSpec(
        "target", "demo", (sys.executable, "-c", "from module import value; assert value() == 2"),
    )
    result = MaintenanceImprover(space).run(
        report,
        request(
            writer_engine="codex", reviewer_engine="claude",
            checks=(check,), max_rounds=1,
        ),
        writer_tool=Writer(), reviewer_tool=Reviewer(),
    )
    assert result["writer_engine"] == "codex"
    assert result["reviewer_engine"] == "claude"
    assert result["status"] == "proposal_ready_for_human_review"


def test_failed_checks_trigger_a_bounded_writer_fix(source):
    space, root, _, report = source
    writer = Writer((2, 3))
    reviewer = Reviewer(((), ()))
    check = CheckSpec(
        "accept_value_3", "demo",
        (sys.executable, "-c", "from module import value; assert value() == 3"),
    )
    result = MaintenanceImprover(space).run(
        report, request(checks=(check,), max_rounds=2),
        writer_tool=writer, reviewer_tool=reviewer,
    )
    assert result["status"] == "proposal_ready_for_human_review", result
    assert len(result["rounds"]) == 2
    assert result["rounds"][0]["verification"]["status"] == "failed"
    assert result["rounds"][1]["verification"]["status"] == "passed_selected_checks"
    assert "Address these failed selected checks" in writer.calls[1]
    assert (root / "module.py").read_text().endswith("return 1\n")


def test_source_changes_during_review_invalidate_potentially_clean_proposal(source):
    space, root, _, report = source

    class AdvanceSource:
        async def run(self, **kwargs):
            root.joinpath("module.py").write_text("def value():\n    return 44\n")
            return REVIEW_MARKER + '\n{"reviewer_model":"codex","findings":[]}'

    check = CheckSpec("test", "demo", (sys.executable, "--version"))
    result = MaintenanceImprover(space).run(
        report, request(checks=(check,), max_rounds=1),
        writer_tool=Writer(), reviewer_tool=AdvanceSource(),
    )
    assert result["status"] == "stale_source"
    assert "re-run maintenance scan" in result["error"]
    assert not result["pushed"]


def test_review_severity_p1_halts_before_verification(source):
    space, _, _, report = source
    writer = Writer((2, 3))
    reviewer = Reviewer(((("P1", "Potential data loss"),), ()))
    check = CheckSpec("would_pass", "demo", (sys.executable, "--version"))
    result = MaintenanceImprover(space).run(
        report, request(checks=(check,)),
        writer_tool=writer, reviewer_tool=reviewer,
    )
    assert result["status"] == "escalated"
    assert len(writer.calls) == 1
    assert "verification" not in result["rounds"][0]


def test_declared_plan_has_scan_proposal_publish_and_existing_navigator_steps(
    source, monkeypatch
):
    import lazytools.maintenance.improvement_plan as module
    from lazytools.maintenance import (
        MaintenanceSelfImprovementPlan,
        NavigationApproval,
        PublicationApproval,
    )

    workspace, _, _, report = source
    calls = []

    class Scan:
        def __init__(self, space):
            self.workspace = space

        def execute(self, *, base_refs=None):
            calls.append("scan")
            return report

    class FakeImprover:
        def __init__(self, space):
            pass

        def run(self, result, policy, **kwargs):
            calls.append("propose")
            return {"status": "proposal_ready_for_human_review",
                    "run_id": "local-proposal", "source_sha": result["sha"]["demo"]}

    def publish(space, policy, artifact, approval):
        calls.append("publish")
        return {"status": "draft_pr_created", "github_repo": "example/project",
                "pr_number": 51, "reviewer_engine": "codex"}

    def navigate(published, policy, approval):
        calls.append("navigate")
        return {**published, "status": "merge_ready", "merged": False}

    monkeypatch.setattr(module, "MaintenanceImprover", FakeImprover)
    monkeypatch.setattr(module, "publish_draft_pr", publish)
    monkeypatch.setattr(module, "navigate_published_pr", navigate)
    flow = MaintenanceSelfImprovementPlan(
        Scan(workspace), request(),
        publication=PublicationApproval("example/project", approved_publish_draft=True),
        navigation=NavigationApproval(
            "review-wrapper", ("pytest -q",), approved_navigate=True,
        ),
    )
    assert [step.name for step in flow.build_agent().engine.steps] == [
        "maintenance_scan", "maintenance_propose",
        "maintenance_publish", "maintenance_navigate",
    ]
    result = flow.run()
    assert result["status"] == "merge_ready"
    assert result["merged"] is False
    assert calls == ["scan", "propose", "publish", "navigate"]


def test_plan_requires_separate_publish_and_navigator_approval(source):
    from lazytools.maintenance import (
        MaintenanceSelfImprovementPlan,
        NavigationApproval,
        PublicationApproval,
    )

    space, _, _, _ = source

    class Scan:
        workspace = space

        def execute(self, *, base_refs=None):
            raise AssertionError("No scan before required human authorizations")

    with pytest.raises(PermissionError, match="Publishing"):
        MaintenanceSelfImprovementPlan(
            Scan(), request(),
            publication=PublicationApproval("example/project"),
        ).run()
    with pytest.raises(PermissionError, match="Navigator"):
        MaintenanceSelfImprovementPlan(
            Scan(), request(),
            publication=PublicationApproval("example/project", approved_publish_draft=True),
            navigation=NavigationApproval("review-wrapper", ("pytest -q",)),
        ).run()
    with pytest.raises(ValueError, match="requires a configured publication"):
        MaintenanceSelfImprovementPlan(
            Scan(), request(),
            navigation=NavigationApproval("review-wrapper", ("pytest -q",)),
        )


def test_renaming_unapproved_existing_file_is_refused(source):
    """Destination-only allowlists must not authorize deletion of another file."""
    space, root, _, report = source

    class RenamingWriter:
        async def run(self, **kwargs):
            private = Path(kwargs["cwd"])
            (private / "module.py").rename(private / "new.py")
            return {"result": "renamed"}

    result = MaintenanceImprover(space).run(
        report,
        request(allowed_paths=("new.py",)),
        writer_tool=RenamingWriter(), reviewer_tool=Reviewer(),
    )
    assert result["status"] == "failed"
    assert "outside approved scope" in result["error"]
    assert (root / "module.py").is_file()
