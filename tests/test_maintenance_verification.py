"""Operator-granted checks run at pinned SHAs without changing source checkout."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from lazytools.maintenance import CheckSpec, MaintenanceError, RepoSpec, Workspace
from lazytools.maintenance.verification import run_approved_checks


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def checked_repo(tmp_path: Path) -> tuple[Workspace, Path, str]:
    root = tmp_path / "library"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.test")
    (root / "source.py").write_text("VALUE = 3\n", encoding="utf-8")
    _git(root, "add", "source.py")
    _git(root, "commit", "-qm", "initial")
    sha = _git(root, "rev-parse", "HEAD")
    workspace = Workspace((RepoSpec("library", root),), tmp_path / "state")
    return workspace, root, sha


def test_explicit_check_passes_in_disposable_worktree_and_does_not_modify_source(checked_repo):
    workspace, root, sha = checked_repo
    check = CheckSpec(
        "smoke", "library",
        (sys.executable, "-c",
         "from pathlib import Path; assert Path('source.py').read_text() == 'VALUE = 3\\n';"
         "Path('source.py').write_text('CHANGED IN TEMP WORKTREE\\n')"),
    )
    outcome = run_approved_checks(workspace, {"library": sha}, (check,))
    assert outcome["status"] == "passed_selected_checks"
    assert outcome["scope"] == "operator_approved_checks_only"
    assert outcome["network_isolation"] is False
    row = outcome["tests"][0]
    assert row["status"] == "passed"
    assert row["repo"] == "library"
    assert row["sha"] == sha
    assert row["exit_code"] == 0
    assert (root / "source.py").read_text() == "VALUE = 3\n"
    assert _git(root, "status", "--porcelain") == ""
    assert len(_git(root, "worktree", "list", "--porcelain").split("worktree ")) == 2


def test_no_checks_means_not_run(checked_repo):
    workspace, _, sha = checked_repo
    assert run_approved_checks(workspace, {"library": sha}, ()) == {
        "status": "not_run", "tests": None,
    }


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ((sys.executable, "-c", "import sys; sys.exit(9)"), "failed"),
        ((sys.executable, "-c", "import time; time.sleep(1)"), "timed_out"),
        (("command-that-does-not-exist-exactly-zzxyz",), "unavailable"),
    ],
)
def test_check_failure_modes_are_not_success(checked_repo, command, expected):
    workspace, root, sha = checked_repo
    check = CheckSpec("demo", "library", command, timeout_seconds=0.05)
    outcome = run_approved_checks(workspace, {"library": sha}, (check,))
    assert outcome["tests"][0]["status"] == expected
    assert outcome["status"] == ("failed" if expected == "failed" else "incomplete")
    assert _git(root, "status", "--porcelain") == ""


def test_approved_command_does_not_inherit_secrets(checked_repo, monkeypatch):
    workspace, _, sha = checked_repo
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-leak")
    monkeypatch.setenv("MY_PRIVATE_SECRET", "must-not-leak")
    check = CheckSpec(
        "no_secrets", "library", (sys.executable, "-c",
        "import os;print(os.getenv('GITHUB_TOKEN'), os.getenv('MY_PRIVATE_SECRET'))"),
    )
    result = run_approved_checks(workspace, {"library": sha}, (check,))
    assert result["tests"][0]["stdout"].strip() == "None None"


def test_output_bounded_and_marked_as_truncated(checked_repo):
    workspace, _, sha = checked_repo
    check = CheckSpec(
        "large_log", "library",
        (sys.executable, "-c", "print('A'*5000)"),
        max_output_chars=100,
    )
    outcome = run_approved_checks(workspace, {"library": sha}, (check,))
    row = outcome["tests"][0]
    assert len(row["stdout"]) == 100
    assert row["output_truncated"] is True
    assert outcome["status"] == "passed_selected_checks"


def test_stale_revision_prevents_test_execution(checked_repo):
    workspace, root, old_sha = checked_repo
    (root / "source.py").write_text("VALUE = 4\n")
    _git(root, "add", "source.py")
    _git(root, "commit", "-qm", "advance HEAD")
    check = CheckSpec("do_nothing", "library", (sys.executable, "--version"))
    with pytest.raises(MaintenanceError, match="changed before verification"):
        run_approved_checks(workspace, {"library": old_sha}, (check,))


def test_validation_refuses_unknown_repo_duplicate_check_and_bad_argv(checked_repo):
    workspace, _, sha = checked_repo
    with pytest.raises(ValueError, match="argv"):
        CheckSpec("bad", "library", ("python", "\0evil"))
    with pytest.raises(ValueError, match="alias"):
        CheckSpec("x", "../other", (sys.executable,))
    check = CheckSpec("one", "library", (sys.executable, "--version"))
    with pytest.raises(ValueError, match="Duplicate check id"):
        run_approved_checks(workspace, {"library": sha}, (check, check))
    with pytest.raises(ValueError, match="outside the allowlist"):
        run_approved_checks(
            workspace, {"library": sha},
            (CheckSpec("invalid", "another", (sys.executable, "--version")),),
        )
