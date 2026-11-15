"""The pre-launch freshness check: a branch behind its upstream is reported, never silently built on."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import _code_bridge_fakes as fakes
from lazytools.code_bridge import _freshness, cli
from test_code_bridge_brief import job_count, repo, run_args  # noqa: F401  (`repo` is a fixture)

ENV = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *ENV, "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(cwd: Path, name: str) -> None:
    (cwd / name).write_text(name, encoding="utf-8")
    git(cwd, "add", name)
    git(cwd, "commit", "-q", "-m", name)


@pytest.fixture
def clone(tmp_path):
    """A clone (the work tree) of a bare remote, plus a second clone to push from."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(remote), str(seed)], check=True, capture_output=True)
    git(seed, "checkout", "-q", "-B", "main")
    commit(seed, "a.txt")
    git(seed, "push", "-q", "-u", "origin", "main")
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True, capture_output=True)
    return work, seed


def push_ahead(seed: Path, n: int) -> None:
    for i in range(n):
        commit(seed, f"new{i}.txt")
    git(seed, "push", "-q", "origin", "main")


def test_up_to_date_branch_is_fresh(clone):
    work, _ = clone
    result = _freshness.check_freshness(work)
    assert result.behind == 0 and not result.stale()


def test_counts_commits_behind_after_fetching(clone):
    work, seed = clone
    push_ahead(seed, 3)
    result = _freshness.check_freshness(work)  # the work tree has not fetched yet
    assert result.behind == 3 and result.upstream == "origin/main"
    assert "3 commits behind origin/main" in result.message()


def test_singular_commit(clone):
    work, seed = clone
    push_ahead(seed, 1)
    assert "1 commit behind" in _freshness.check_freshness(work).message()


def test_local_commits_ahead_are_not_stale(clone):
    work, _ = clone
    commit(work, "mine.txt")
    assert _freshness.check_freshness(work).behind == 0


def test_not_a_repo_or_no_upstream_is_unknown(tmp_path, clone):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _freshness.check_freshness(plain).behind is None
    work, _ = clone
    git(work, "checkout", "-q", "-b", "topic")
    assert _freshness.check_freshness(work).behind is None  # no upstream configured


def test_unreachable_remote_uses_the_last_known_count(clone):
    work, seed = clone
    push_ahead(seed, 2)
    _freshness.check_freshness(work)  # fetch once while the remote is reachable
    git(work, "remote", "set-url", "origin", str(work.parent / "gone.git"))
    result = _freshness.check_freshness(work)
    assert result.behind == 2 and not result.fetched
    assert "fetch failed" in result.message()


def test_run_warns_on_stderr_and_still_launches(clone, tmp_path, monkeypatch, capsys):
    work, seed = clone
    push_ahead(seed, 2)
    script = fakes.install(monkeypatch)
    argv = run_args(tmp_path, work, "do it", "--no-brief-check")
    assert cli.main(argv) == 0
    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.startswith("freshness:")]
    assert lines == [
        "freshness: warn branch is 2 commits behind origin/main; pull or rebase first, "
        "or the job builds on stale code"
    ]
    assert "freshness" not in captured.out
    assert script.prompts == ["do it"]


def test_require_fresh_refuses_before_any_job_exists(clone, tmp_path, monkeypatch, capsys):
    work, seed = clone
    push_ahead(seed, 1)
    script = fakes.install(monkeypatch)
    assert cli.main(run_args(tmp_path, work, "do it", "--no-brief-check", "--require-fresh")) == 2
    assert "refused by --require-fresh" in capsys.readouterr().err
    assert script.engines == [] and job_count(tmp_path) == 0


def test_require_fresh_launches_when_up_to_date(clone, tmp_path, monkeypatch, capsys):
    work, _ = clone
    script = fakes.install(monkeypatch)
    assert cli.main(run_args(tmp_path, work, "do it", "--no-brief-check", "--require-fresh")) == 0
    assert "freshness" not in capsys.readouterr().err
    assert script.prompts == ["do it"]


def test_no_freshness_check_skips_the_fetch(clone, tmp_path, monkeypatch, capsys):
    work, seed = clone
    push_ahead(seed, 1)
    script = fakes.install(monkeypatch)
    monkeypatch.setattr(
        _freshness, "check_freshness", lambda *a, **k: pytest.fail("must not run with --no-freshness-check")
    )
    argv = run_args(tmp_path, work, "do it", "--no-brief-check", "--require-fresh", "--no-freshness-check")
    assert cli.main(argv) == 0
    assert script.prompts == ["do it"]


def test_detached_child_does_not_repeat_the_check(clone, tmp_path, monkeypatch, capsys):
    work, seed = clone
    push_ahead(seed, 1)
    fakes.install(monkeypatch)
    spawned = []
    monkeypatch.setattr(cli, "_spawn_detached", lambda argv, log: spawned.append(argv) or 4242)
    assert cli.main(run_args(tmp_path, work, "do it", "--no-brief-check", "--detach")) == 0
    assert capsys.readouterr().err.count("freshness: warn") == 1
    assert "--brief-checked" in spawned[0]  # the child skips both pre-launch checks
