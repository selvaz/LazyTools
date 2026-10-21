"""Offline integration tests use real repositories and a local bare origin."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime

import pytest

from lazytools.ecosystem_sync import cli
from lazytools.ecosystem_sync.audit import collect
from lazytools.ecosystem_sync.check import Checker, command, exit_code, markdown, repository_name, run

NOW = datetime(2026, 10, 21, 12, tzinfo=UTC)


def git(path, *args):
    result = run(["git", "-C", str(path), *args], timeout=15)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def result(value, code=0):
    return subprocess.CompletedProcess([], code, json.dumps(value), "")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    repo = root / "Demo"
    git(root, "clone", str(origin), str(repo))
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    git(repo, "push", "-u", "origin", "main")
    git(repo, "remote", "set-head", "origin", "main")
    manifest = tmp_path / "compatibility.toml"
    manifest.write_text(
        '[verified]\nlazybridge = "1.0.0"\n[verified.github.demo]\n'
        'repo = "https://github.com/selvaz/Demo"\nref = "v1.0.0"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(Checker, "disk", lambda self: None)
    return root, repo, origin, manifest


def checker(workspace, **kwargs):
    root, _, _, manifest = workspace
    return Checker(root, manifest, fetch=False, now=NOW, **kwargs)


class GitHub:
    def __init__(self, prs=(), *, checks=None, comments=(), reviews=()):
        self.items = list(prs)
        self.checks = checks if checks is not None else [{"name": "test", "state": "SUCCESS", "bucket": "pass"}]
        self.comments, self.reviews = comments, reviews
        self.calls = []

    def __call__(self, argv, *, timeout):
        assert timeout > 0
        self.calls.append(argv)
        if argv[1:3] == ["repo", "view"]:
            return result({"defaultBranchRef": {"name": "main"}})
        if argv[1:3] == ["pr", "list"]:
            state = argv[argv.index("--state") + 1].upper()
            items = [p for p in self.items if state == "ALL" or p["state"] == state]
            if "--head" in argv:
                items = [p for p in items if p["headRefName"] == argv[argv.index("--head") + 1]]
            return result(items)
        if argv[1:3] == ["pr", "checks"]:
            return result(self.checks, 1 if any(c["bucket"] == "fail" for c in self.checks) else 0)
        if argv[1] == "api":
            if "/issues/" in argv[2]:
                return result([self.comments])
            if argv[2].endswith("/reviews"):
                return result([self.reviews])
            return result([[]])
        raise AssertionError(argv)


def github_git(argv, *, timeout):
    if argv[-3:] == ["remote", "get-url", "origin"]:
        return subprocess.CompletedProcess(argv, 0, "https://github.com/selvaz/Demo\n", "")
    return run(argv, timeout=timeout)


def pr(repo, state="MERGED", branch="feature", number=1):
    return {
        "number": number,
        "title": "Example change",
        "state": state,
        "createdAt": "2026-10-01T00:00:00Z",
        "updatedAt": "2026-10-20T00:00:00Z",
        "mergedAt": None if state == "OPEN" else "2026-10-20T00:00:00Z",
        "headRefName": branch,
        "headRefOid": git(repo, "rev-parse", branch),
        "isCrossRepository": False,
        "url": f"https://github.com/selvaz/Demo/pull/{number}",
    }


def test_ahead_behind_and_unpushed_branch(workspace, tmp_path):
    _, repo, origin, _ = workspace
    other = tmp_path / "other"
    git(tmp_path, "clone", str(origin), str(other))
    git(other, "config", "user.email", "test@example.invalid")
    git(other, "config", "user.name", "Test")
    (other / "remote.txt").write_text("remote", encoding="utf-8")
    git(other, "add", ".")
    git(other, "commit", "-m", "remote")
    git(other, "push", "origin", "main")
    git(repo, "fetch", "origin")
    git(repo, "switch", "-c", "unpublished")
    (repo / "local.txt").write_text("local", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "local")
    plan = checker(workspace).check()
    assert not plan["errors"]
    assert (plan["repositories"][0]["ahead"], plan["repositories"][0]["behind"]) == (1, 1)
    assert any("unpublished: 1 commits absent" in a["fact"] for a in plan["actions"])
    assert exit_code(plan) == 2


def test_dirty_and_untracked_old_work(workspace):
    _, repo, _, _ = workspace
    (repo / "tracked.txt").write_text("changed", encoding="utf-8")
    file = repo / "old untracked.txt"
    file.write_text("old", encoding="utf-8")
    os.utime(file, (NOW.timestamp() - 90 * 86400,) * 2)
    plan = checker(workspace).check()
    row = plan["repositories"][0]
    assert row["dirty"] == row["untracked"] == 1
    assert row["oldest_change_days"] == 90
    assert any("STALE >= 7 days" in a["fact"] for a in plan["actions"])


def test_pinned_at_manifest_tag_is_expected(workspace):
    _, repo, _, _ = workspace
    git(repo, "tag", "v1.0.0")
    git(repo, "switch", "-c", "remote-new")
    (repo / "new.txt").write_text("new", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "new")
    git(repo, "push", "origin", "HEAD:main")
    git(repo, "switch", "--detach", "v1.0.0")
    plan = checker(workspace).check()
    assert not plan["errors"]
    assert plan["repositories"][0]["behind"] == 1
    assert plan["repositories"][0]["pinned_expected"]
    assert not plan["actions"]
    assert exit_code(plan) == 0
    assert any("EXPECTED" in row["fact"] for row in plan["observations"])


def test_manifest_repo_identity_is_case_insensitive(workspace):
    _, repo, _, _ = workspace
    git(repo, "tag", "v1.0.0")

    def lowercase(argv, *, timeout):
        if argv[-3:] == ["remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(argv, 0, "https://github.com/selvaz/demo\n", "")
        return run(argv, timeout=timeout)

    plan = checker(workspace, git_runner=lowercase, gh_runner=GitHub()).check()
    assert not plan["errors"]
    assert plan["repositories"][0]["pinned_expected"]


@pytest.mark.parametrize(
    "dirty, state, expected",
    [(False, "MERGED", "auto_safe"), (True, "MERGED", "session"), (False, "CLOSED", "session")],
)
def test_worktree_pr_state_and_cleanup_class(workspace, dirty, state, expected):
    root, repo, _, _ = workspace
    tree = root / "_wt" / "feature"
    git(repo, "worktree", "add", "-b", "feature", str(tree))
    if dirty:
        (tree / "private.txt").write_text("preserve", encoding="utf-8")
    gh = GitHub([pr(repo, state)])
    plan = checker(workspace, git_runner=github_git, gh_runner=gh).check()
    assert not plan["errors"]
    row = next(w for w in plan["worktrees"] if w.get("branch") == "feature")
    assert row["pr_state"] == state
    action = next(a for a in plan["actions"] if a["fact"].startswith("Worktree"))
    assert action["class"] == expected
    assert tree.exists()
    assert any("--head" in call and "feature" in call for call in gh.calls)


def test_remote_merged_branch_requires_matching_tip(workspace):
    _, repo, _, _ = workspace
    git(repo, "branch", "feature")
    git(repo, "push", "origin", "feature")
    merged = pr(repo)
    plan = checker(workspace, git_runner=github_git, gh_runner=GitHub([merged])).check()
    action = next(a for a in plan["actions"] if "Remote branch feature" in a["fact"])
    assert action["class"] == "auto_safe"
    assert "--force-with-lease=refs/heads/feature:" in action["command"]
    merged["headRefOid"] = "0" * 40
    plan = checker(workspace, git_runner=github_git, gh_runner=GitHub([merged])).check()
    assert not any("Remote branch feature" in a["fact"] for a in plan["actions"])


@pytest.mark.parametrize(
    "unresolved, reviewed, buckets, ready",
    [
        (False, True, ["pass"], True),
        (True, True, ["pass"], False),
        (False, False, ["pass"], False),
        (False, True, ["fail"], False),
        (False, True, [], False),
    ],
)
def test_open_pr_review_head_and_ci(workspace, unresolved, reviewed, buckets, ready):
    _, repo, _, _ = workspace
    opened = pr(repo, "OPEN", "main")
    summary = {
        "user": {"login": "chatgpt-codex-connector[bot]"},
        "updated_at": "2026-10-20T00:00:00Z",
        "body": json.dumps({"has_unresolved_findings": unresolved}),
        "commit_id": opened["headRefOid"] if reviewed else "0" * 40,
    }
    gh = GitHub([opened], checks=[{"name": "test", "bucket": b, "state": b} for b in buckets], reviews=[summary])
    plan = checker(workspace, git_runner=github_git, gh_runner=gh).check()
    assert not plan["errors"]
    row = plan["pull_requests"][0]
    assert row["age_days"] > 20
    assert row["has_unresolved_findings"] == unresolved
    assert row["head_reviewed"] == reviewed
    assert any("ready to merge" in a["fact"] for a in plan["actions"]) == ready


def test_lazybricks_behind_is_owner(workspace, tmp_path):
    root, repo, _, manifest = workspace
    renamed = root / "LazyBricks"
    repo.rename(renamed)
    (renamed / "new.txt").write_text("new", encoding="utf-8")
    git(renamed, "add", ".")
    git(renamed, "commit", "-m", "new")
    git(renamed, "push", "origin", "main")
    git(renamed, "switch", "--detach", "HEAD~1")
    plan = Checker(root, manifest, fetch=False, now=NOW).check()
    assert any(a["repo"] == "LazyBricks" and a["class"] == "owner" and "behind=1" in a["fact"] for a in plan["actions"])


def test_one_repo_failure_does_not_stop_other_repos(workspace):
    root, _, _, _ = workspace
    bad = root / "Broken"
    bad.mkdir()
    (bad / ".git").write_text("gitdir: does-not-exist", encoding="utf-8")
    plan = checker(workspace).check()
    assert plan["errors"]
    assert plan["repositories"][0]["repo"] == "Demo"
    assert exit_code(plan) == 1


def test_read_only_no_fetch_and_protected_worktree(workspace):
    root, repo, _, _ = workspace
    protected = root / "LazyTools-mcp-main"
    git(repo, "worktree", "add", "-b", "production", str(protected))
    calls = []

    def spy(argv, *, timeout):
        calls.append(argv)
        assert str(protected) not in argv
        assert not any(word in argv for word in ("fetch", "checkout", "switch", "pull", "push", "delete", "remove"))
        return run(argv, timeout=timeout)

    before = git(repo, "status", "--porcelain")
    plan = checker(workspace, git_runner=spy).check()
    assert not plan["errors"]
    assert any(w.get("status") == "protected: not inspected" for w in plan["worktrees"])
    assert git(repo, "status", "--porcelain") == before
    assert calls


def test_fetch_is_only_git_mutation(workspace):
    calls = []

    def spy(argv, *, timeout):
        calls.append(argv)
        return run(argv, timeout=timeout)

    root, _, _, manifest = workspace
    plan = Checker(root, manifest, git_runner=spy, now=NOW).check()
    assert not plan["errors"]
    assert sum("fetch" in argv for argv in calls) == 1
    assert any(argv[-3:] == ["fetch", "--prune", "origin"] for argv in calls)


def test_timeout_is_recorded(workspace):
    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    plan = checker(workspace, git_runner=timeout).check()
    assert exit_code(plan) == 1
    assert "TimeoutExpired" in plan["errors"][0]["error"]


def test_audit_adapter_reuses_drift_and_captures_runtime_status(tmp_path):
    scripts = tmp_path / "investmentcommittee" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "ecosystem_audit.py").write_text(
        """
from pathlib import Path
def exact_tag(path): return "v1.0.0"
def installed_versions(python, names): return {"demo": "1.0.0"}
def drift(*, manifest_path, tag_reader, version_reader):
    assert manifest_path.name == "compatibility.toml"
    tag_reader(GITHUB / "Demo-runtime")
    version_reader(Path("python"), ["demo"])
    return ["runtime Demo: tagged but not deployed"]
""",
        encoding="utf-8",
    )
    original_path = sys.path[:]
    data = collect(tmp_path, tmp_path / "compatibility.toml")
    assert sys.path == original_path
    assert data["problems"] == ["runtime Demo: tagged but not deployed"]
    assert data["runtimes"][0]["tag"] == "v1.0.0"
    assert data["environments"]["python"] == {"demo": "1.0.0"}


def test_news_origin_reuses_sibling_helper_and_declarative_config(tmp_path):
    scripts = tmp_path / "investmentcommittee" / "scripts"
    scripts.mkdir(parents=True)
    config = scripts.parent / "config"
    config.mkdir()
    (config / "job_health.toml").write_text('[watchdog]\nnews_origin_cutover = "2026-10-19T18:15:00Z"\n')
    (config / "databases.toml").write_text('[databases.crawler_raw]\npath = "news.db"\n')
    (scripts / "ecosystem_audit.py").write_text(
        """
def drift(**kwargs): return []
def news_origin_drift(path, cutover):
    assert str(path) == "news.db"
    assert cutover == "2026-10-19T18:15:00Z"
    return [{"key": "news-origin", "text": "origin drift", "detail": "existing helper"}]
""",
        encoding="utf-8",
    )
    data = collect(tmp_path, tmp_path / "compatibility.toml")
    assert data["news_origin"]["checked"]
    assert data["problems"][0]["key"] == "news-origin"


def test_prunable_missing_worktree_is_cleanup_candidate(workspace):
    root, repo, _, _ = workspace
    tree = root / "_wt" / "missing"
    git(repo, "worktree", "add", "-b", "feature", str(tree))
    assert tree.resolve().is_relative_to(root.resolve())
    shutil.rmtree(tree)
    plan = checker(workspace, git_runner=github_git, gh_runner=GitHub([pr(repo)])).check()
    assert not plan["errors"]
    assert any(a["class"] == "auto_safe" and "stale prunable registration" in a["fact"] for a in plan["actions"])


def test_origin_missing_still_inspects_local_evidence(workspace):
    _, repo, _, _ = workspace
    git(repo, "remote", "remove", "origin")
    (repo / "unsaved.txt").write_text("save me")
    plan = checker(workspace).check()
    # A missing origin is an owner action, not a check failure.
    assert plan["errors"] == []
    assert any(a["class"] == "owner" and "no origin remote" in a["fact"] for a in plan["actions"])
    assert plan["repositories"][0]["untracked"] == 1
    assert plan["repositories"][0]["ahead"] is None
    assert plan["worktrees"]


def test_unborn_repository_still_reports_dirty_files(workspace):
    root, _, _, _ = workspace
    empty = root / "Unborn"
    empty.mkdir()
    git(empty, "init", "--initial-branch=main")
    (empty / "unsaved.txt").write_text("preserve")
    plan = checker(workspace).check()
    row = next(r for r in plan["repositories"] if r["repo"] == "Unborn")
    assert row["head"] is None
    assert row["untracked"] == 1
    assert row["branch"] == "main"
    assert [e for e in plan["errors"] if e["repo"] == "Unborn"] == []
    assert any(a["repo"] == "Unborn" and "no origin remote" in a["fact"] for a in plan["actions"])


def test_audit_drift_is_owner_and_release_difference_session(workspace):
    root, repo, _, _ = workspace
    git(repo, "tag", "v1.0.0")
    git(repo, "tag", "v1.1.0")
    scripts = root / "investmentcommittee" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "ecosystem_audit.py").touch()

    def audit(argv, *, timeout):
        assert "-B" in argv and timeout == 300
        return result(
            {"problems": ["runtime Demo: release set mismatch"], "runtimes": [{"tag": "v0.9.0"}], "environments": {}}
        )

    plan = checker(workspace, audit_runner=audit).check()
    assert plan["production_audit"]["runtimes"]
    assert any(a["class"] == "owner" and "release set mismatch" in a["fact"] for a in plan["actions"])
    assert any(a["class"] == "session" and "Tagged but not deployed" in a["fact"] for a in plan["actions"])


def test_cli_writes_ten_line_summary_json_and_exit_codes(workspace, tmp_path, monkeypatch, capsys):
    root, _, _, manifest = workspace
    out = tmp_path / "plans"
    monkeypatch.setattr(cli, "notify", lambda text: pytest.fail("Telegram must be opt-in"))
    assert (
        cli.main(["check", "--root", str(root), "--manifest", str(manifest), "--out-dir", str(out), "--no-fetch"]) == 0
    )
    md = next(out.glob("*.md")).read_text(encoding="utf-8")
    data = json.loads(next(out.glob("*.json")).read_text(encoding="utf-8"))
    assert md.splitlines()[4:7] == ["auto_safe: 0", "session: 0", "owner: 0"]
    assert md.splitlines()[9] == "Result: aligned (exit 0)"
    assert not data["fetch"]
    assert "Plans:" in capsys.readouterr().out


def test_cli_initialization_failure_still_writes_plan(tmp_path):
    out = tmp_path / "plans"
    assert cli.main(["check", "--manifest", str(tmp_path / "missing"), "--out-dir", str(out), "--no-fetch"]) == 1
    assert json.loads(next(out.glob("*.json")).read_text())["errors"]


def test_telegram_message_and_document(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    messages, documents = [], []

    class Client:
        def send_message(self, **kwargs):
            messages.append(kwargs)

        def send_document(self, **kwargs):
            documents.append(kwargs)

    cli.notify("short summary", client=Client())
    cli.notify("x" * 4001, client=Client())
    assert messages[0]["chat_id"] == "123"
    assert len(documents[0]["document"]) == 4001


def test_powershell_proposal_quotes_and_github_identity():
    assert command("git", "O'Brien $()") == "& 'git' 'O''Brien $()'"
    assert repository_name("git@github.com:selvaz/LazyTools.git") == "LazyTools"
    assert repository_name("https://github.com/selvaz/LazyTools") == "LazyTools"
    assert repository_name("https://github.com/other/Repo") is None


def test_deterministic_render(workspace):
    plan = checker(workspace).check()
    assert markdown(plan) == markdown(checker(workspace).check())
