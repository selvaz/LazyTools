"""Read-only maintenance regression tests on temporary real Git repositories."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from lazytools.maintenance import CheckSpec, Dependency, MaintenanceError, MaintenancePipeline, RepoSpec, Workspace
from lazytools.maintenance.core import RunState, changed_paths, resolve_sha


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def make_repo(root: Path, filename: str) -> tuple[Path, str]:
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Maintainer")
    git(root, "config", "user.email", "maintainer@example.test")
    (root / filename).write_text("def old_api():\n    return 1\n", encoding="utf-8")
    git(root, "add", filename)
    git(root, "commit", "-qm", "initial")
    return root, resolve_sha(root)


class FakeMapper:
    _use_embeddings = False

    def __init__(self) -> None:
        self.index_calls: list[str] = []
        self.impact_calls: list[tuple[str, str]] = []

    def _invoke(self, action, args, repo):
        assert action == "version" and not args
        return "repomap 1.9.0"

    def index(self, repo):
        self.index_calls.append(repo)
        return json.dumps({"root": "/fixture/" + repo, "files": 3, "symbols": 4})

    def impact(self, target, repo, depth):
        self.impact_calls.append((repo, target))
        return f"Potential callers of {target}; verify with tests"


def make_workspace(tmp_path: Path) -> tuple[Workspace, str, str]:
    repo_a, a = make_repo(tmp_path / "core", "api.py")
    repo_b, b = make_repo(tmp_path / "consumer", "app.py")
    workspace = Workspace(
        repos=(RepoSpec("core", repo_a), RepoSpec("consumer", repo_b)),
        state_dir=tmp_path / "state",
        dependencies=(Dependency(consumer="consumer", provider="core"),),
    )
    return workspace, a, b


def test_commit_pinned_cache_refresh_and_declared_cross_repo_impact(tmp_path):
    workspace, old_a, old_b = make_workspace(tmp_path)
    mapper = FakeMapper()
    initial = RunState(workspace, {}, mapper)
    initial.resolve()
    initial.refresh()
    initial.diff()
    initial.impact()
    initial.finalize()
    assert initial.sha == {"core": old_a, "consumer": old_b}
    assert mapper.index_calls == ["core", "consumer"]
    assert not initial.snapshots["core"]["cache_hit"]
    assert initial.report["verified"] is False
    assert initial.report["verification"]["status"] == "not_run"
    assert initial.report_path.is_file()
    assert initial.report_path.with_suffix(".md").is_file()

    cached = RunState(workspace, {}, mapper)
    cached.resolve()
    cached.refresh()
    assert mapper.index_calls == ["core", "consumer", "core", "consumer"]
    assert cached.snapshots["core"]["cache_hit"] is True
    assert cached.snapshots["core"]["index_revalidated"] is True

    root = workspace.repos[0].root
    (root / "api.py").write_text("def new_api():\n    return 2\n")
    git(root, "add", "api.py")
    git(root, "commit", "-qm", "change API")
    next_run = RunState(workspace, {"core": old_a}, mapper)
    next_run.resolve()
    next_run.refresh()
    next_run.diff()
    next_run.impact()
    next_run.finalize()
    assert next_run.sha["core"] != old_a
    assert next_run.snapshots["core"]["cache_hit"] is False
    assert next_run.snapshots["consumer"]["cache_hit"] is True
    assert mapper.index_calls == ["core", "consumer", "core", "consumer", "core", "consumer"]
    assert next_run.changes["core"] == [{"status": "M", "path": "api.py"}]
    assert ("core", "api.py") in mapper.impact_calls
    assert next_run.report["cross_repo_candidates"] == [{
        "consumer": "consumer", "provider": "core", "reason": "manifest_dependency",
        "confidence": "declared", "action": "Check consumer compatibility and tests",
    }]
    assert git(root, "status", "--porcelain") == ""
    assert git(workspace.repos[1].root, "status", "--porcelain") == ""


def test_git_rename_is_recorded(tmp_path):
    root, base = make_repo(tmp_path / "source", "old.py")
    git(root, "mv", "old.py", "new.py")
    git(root, "commit", "-qm", "rename")
    rows, clipped = changed_paths(root, base, resolve_sha(root))
    assert rows == [{"status": "R100", "path": "new.py", "from": "old.py"}]
    assert clipped is False


def test_scope_ref_validation_and_dirty_checkout_fail_closed(tmp_path):
    workspace, old, _ = make_workspace(tmp_path)
    with pytest.raises(ValueError, match="unique"):
        Workspace((workspace.repos[0], workspace.repos[0]), tmp_path / "state")
    with pytest.raises(ValueError, match="outside"):
        Workspace((workspace.repos[0],), workspace.repos[0].root / ".state")
    with pytest.raises(ValueError, match="Unknown dependency"):
        Workspace((workspace.repos[0],), tmp_path / "other", (Dependency("missing", "core"),))
    with pytest.raises(ValueError, match="Git ref"):
        resolve_sha(workspace.repos[0].root, "--upload-pack=/tmp/a")
    with pytest.raises(ValueError, match="Git ref"):
        resolve_sha(workspace.repos[0].root, "../bad")
    (workspace.repos[0].root / "untracked.txt").write_text("no")
    with pytest.raises(MaintenanceError, match="uncommitted/untracked"):
        RunState(workspace, {}, FakeMapper()).resolve()
    assert old == resolve_sha(workspace.repos[0].root)


def test_corrupted_snapshot_is_not_a_cache_hit(tmp_path):
    workspace, _, _ = make_workspace(tmp_path)
    mapper = FakeMapper()
    first = RunState(workspace, {}, mapper)
    first.resolve()
    first.refresh()
    files = list((workspace.state_dir / "snapshots").rglob("*.json"))
    assert len(files) == 2
    files[0].write_text("{broken cache", encoding="utf-8")
    later = RunState(workspace, {}, mapper)
    later.resolve()
    with pytest.raises(MaintenanceError, match="unreadable"):
        later.refresh()


def test_real_lazybridge_plan_executes_all_deterministic_stages(tmp_path, monkeypatch):
    workspace, _, _ = make_workspace(tmp_path)
    mapper = FakeMapper()
    import lazytools.maintenance.pipeline as module

    class StubCLI:
        def __new__(cls, *args, **kwargs):
            return mapper

    monkeypatch.setattr(module, "RepoMapCLI", StubCLI)
    result = MaintenancePipeline(workspace).execute()
    assert result["verified"] is False
    assert result["status"] == "review_required"
    assert result["reviews"]["status"] == "not_run"
    assert Path(result["report_json"]).is_file()
    assert result["usage"]["source"] == "lazybridge_envelope"
    assert result["usage"]["input_tokens"] == 0
    persisted = json.loads(Path(result["report_json"]).read_text())
    assert persisted["usage"] == result["usage"]
    assert mapper.index_calls == ["core", "consumer"]


def test_pipeline_rejects_unconfigured_base_refs_and_bad_reviewer_names(tmp_path):
    workspace, _, _ = make_workspace(tmp_path)
    with pytest.raises(ValueError, match="Unknown repository"):
        MaintenancePipeline(workspace).execute(base_refs={"notallowed": "HEAD"})

    class DummyReviewer:
        name = "unsafe"

    with pytest.raises(ValueError, match="beginning with"):
        MaintenancePipeline(workspace, reviewers=[DummyReviewer()])


@pytest.mark.parametrize("reviewer_count", [1, 2])
def test_nested_subagents_are_plan_tools_and_cannot_claim_verification(
    tmp_path, monkeypatch, reviewer_count
):
    """Nested Plan Agents run with bounded impact evidence, no LLM/network."""
    from lazybridge import Agent, Plan, Step

    import lazytools.maintenance.pipeline as module

    workspace, _, _ = make_workspace(tmp_path)
    mapper = FakeMapper()
    seen: list[str] = []

    class StubCLI:
        def __new__(cls, *args, **kwargs):
            return mapper

    monkeypatch.setattr(module, "RepoMapCLI", StubCLI)

    def make_reviewer(name):
        def review(task: str) -> str:
            payload = json.loads(task)
            assert set(payload["sha"]) == {"core", "consumer"}
            assert "Static analysis" in payload["note"]
            seen.append(name)
            return "Suggestion from " + name + ": inspect API consumers"

        return Agent(name=name, engine=Plan(Step(review, name="assess")))

    reviewers = [make_reviewer("review_architecture")]
    if reviewer_count == 2:
        reviewers.append(make_reviewer("review_tests"))
    result = MaintenancePipeline(workspace, reviewers=reviewers).execute()
    assert set(seen) == {reviewer.name for reviewer in reviewers}
    assert result["reviews"]["status"] == "unverified_suggestions"
    for reviewer in reviewers:
        assert reviewer.name in result["reviews"]["text"]
    assert result["verification"]["status"] == "not_run"
    assert result["verified"] is False


def test_head_advances_mid_scan_is_rejected(tmp_path):
    workspace, _, _ = make_workspace(tmp_path)
    state = RunState(workspace, {}, FakeMapper())
    state.resolve()
    state.refresh()
    state.diff()
    state.impact()
    root = workspace.repos[0].root
    (root / "api.py").write_text("def newer_api():\n    return 42\n")
    git(root, "add", "api.py")
    git(root, "commit", "-qm", "concurrent head advance")
    with pytest.raises(MaintenanceError, match="HEAD moved during analysis"):
        state.finalize()
    assert state.report is None


def test_real_plan_approved_checks_are_reported_without_claiming_full_review(tmp_path, monkeypatch):
    """Verification runs after impact and never launders tests into full approval."""
    import lazytools.maintenance.pipeline as module

    workspace, first_sha, _ = make_workspace(tmp_path)
    mapper = FakeMapper()

    class StubCLI:
        def __new__(cls, *args, **kwargs):
            return mapper

    monkeypatch.setattr(module, "RepoMapCLI", StubCLI)
    good = CheckSpec(
        "python_smoke", "core", (sys.executable, "-c",
        "from pathlib import Path; assert Path('api.py').is_file()"),
    )
    success = MaintenancePipeline(workspace, checks=(good,)).execute(base_refs={"core": first_sha})
    assert success["verification"]["status"] == "passed_selected_checks"
    assert success["verification"]["tests"][0]["exit_code"] == 0
    assert success["verified"] is False
    assert "python_smoke" in Path(success["report_markdown"]).read_text()

    bad = CheckSpec("failing_test", "core", (sys.executable, "-c", "import sys;sys.exit(3)"))
    failure = MaintenancePipeline(workspace, checks=(bad,)).execute()
    assert failure["verification"]["status"] == "failed"
    assert failure["status"] == "verification_failed"
    assert failure["verification"]["tests"][0]["exit_code"] == 3
    assert failure["verified"] is False
    assert any("Approved tests failed" in w for w in failure["warnings"])
    assert git(workspace.repos[0].root, "status", "--porcelain") == ""


def test_cross_repo_python_imports_are_grounded_and_linked_to_changed_provider(tmp_path):
    workspace, old_provider_sha, _ = make_workspace(tmp_path)
    provider, consumer = workspace.repos[0].root, workspace.repos[1].root
    (consumer / "app.py").write_text(
        "from corelib.api import old_api\nimport corelib.helpers\n", encoding="utf-8"
    )
    git(consumer, "add", "app.py")
    git(consumer, "commit", "-qm", "declare provider imports")
    with_imports = Workspace(
        workspace.repos,
        workspace.state_dir,
        dependencies=(Dependency(consumer="consumer", provider="core", import_prefix="corelib"),),
    )
    initial = RunState(with_imports, {}, FakeMapper())
    initial.resolve()
    initial.refresh()
    initial.diff()
    initial.impact()
    initial.finalize()
    # An initial tracked-file inventory is NOT itself evidence of a change.
    assert initial.report["cross_repo_candidates"] == []

    (provider / "api.py").write_text("def renamed_api():\n    return 5\n", encoding="utf-8")
    git(provider, "add", "api.py")
    git(provider, "commit", "-qm", "rename provider API")
    state = RunState(with_imports, {"core": old_provider_sha}, FakeMapper())
    state.resolve()
    state.refresh()
    state.diff()
    state.impact()
    state.finalize()
    candidates = state.report["cross_repo_candidates"]
    assert len(candidates) == 1
    assert candidates[0]["confidence"] == "direct_import"
    evidence = candidates[0]["import_evidence"]
    assert evidence["language"] == "python"
    assert evidence["files_scanned"] >= 1
    assert [row["module"] for row in evidence["matches"]] == [
        "corelib.api", "corelib.helpers"
    ]
    assert all(row["path"] == "app.py" for row in evidence["matches"])
    assert all(row["line"] in (1, 2) for row in evidence["matches"])
    assert not evidence["incomplete"]
    assert "consumer/app.py:1 imports corelib.api" in state.report_path.with_suffix(".md").read_text()


def test_cross_repo_import_scans_fail_open_as_unknown_not_as_clean(tmp_path):
    workspace, old_sha, _ = make_workspace(tmp_path)
    provider, consumer = workspace.repos[0].root, workspace.repos[1].root
    (consumer / "bad.py").write_text("def unfinished(\n", encoding="utf-8")
    git(consumer, "add", "bad.py")
    git(consumer, "commit", "-qm", "unparsable consumer file")
    (provider / "api.py").write_text("def new_api():\n    return 5\n", encoding="utf-8")
    git(provider, "add", "api.py")
    git(provider, "commit", "-qm", "new provider API")
    with_imports = Workspace(
        workspace.repos, workspace.state_dir,
        dependencies=(Dependency("consumer", "core", import_prefix="corelib"),),
    )
    state = RunState(with_imports, {"core": old_sha}, FakeMapper())
    state.resolve()
    state.refresh()
    state.diff()
    state.impact()
    state.finalize()
    candidate = state.report["cross_repo_candidates"][0]
    assert candidate["confidence"] == "declared"
    assert candidate["import_evidence"]["incomplete"] is True
    assert candidate["import_evidence"]["files_skipped"] == 1
    assert any("incomplete" in warning for warning in state.report["warnings"])


def test_run_report_paths_are_immutable_for_repeat_same_sha(tmp_path):
    workspace, _, _ = make_workspace(tmp_path)
    old = RunState(workspace, {}, FakeMapper())
    old.resolve()
    old.refresh()
    old.diff()
    old.impact()
    old.finalize()
    fresh = RunState(workspace, {}, FakeMapper())
    fresh.resolve()
    fresh.refresh()
    fresh.diff()
    fresh.impact()
    fresh.finalize()
    assert old.report_path != fresh.report_path
    assert old.report["run_id"] != fresh.report["run_id"]
    assert old.report_path.is_file()
    assert fresh.report_path.is_file()


def test_dirty_checkout_allowed_explicitly_but_never_uses_sha_only_cache(tmp_path):
    workspace, old_sha, _ = make_workspace(tmp_path)
    dirty_workspace = Workspace(
        workspace.repos, workspace.state_dir,
        dependencies=workspace.dependencies, require_clean=False,
    )
    root = dirty_workspace.repos[0].root
    (root / "api.py").write_text("def uncommitted():\n    return 7\n")
    mapper = FakeMapper()
    first = RunState(dirty_workspace, {}, mapper)
    first.resolve()
    first.refresh()
    assert first.sha["core"] == old_sha
    assert first.dirty["core"] == "dirty"
    assert first.snapshots["core"]["status"] == "partial"
    assert first.snapshots["core"]["cache_hit"] is False

    second = RunState(dirty_workspace, {}, mapper)
    second.resolve()
    second.refresh()
    assert second.snapshots["core"]["status"] == "partial"
    assert second.snapshots["core"]["cache_hit"] is False
    assert mapper.index_calls.count("core") == 2
