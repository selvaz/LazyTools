"""Cloud parity: PR targets, landing, auto-landing, cloud reviews, placement, models, envs, MCP.

Everything is offline. ``codex`` and ``claude`` are the fake executables of the
two sibling test modules; ``gh`` is a fake below that keeps its PRs and comments
in a JSON file and logs every call; git is real, against bare repositories in
the test's tmp dir.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path

import pytest

from lazytools.code_bridge import _cloud, _cloud_envs, _cloud_land, _cloud_place, _concurrency, _gh, _jobs, _store, cli
from lazytools.code_bridge._concurrency import EngineSlot
from test_code_bridge_cloud import TASK_URL, _git, _run_cloud, _set_cloud_config  # noqa: F401
from test_code_bridge_cloud import cloud as cloud
from test_code_bridge_cloud_claude import FAKE_CLAUDE, SESSION_URL

FAKE_GH = textwrap.dedent(
    """
    import json, os, sys

    args = sys.argv[1:]
    path = os.environ["FAKE_GH_STATE"]
    state = json.load(open(path)) if os.path.exists(path) else {"prs": {}, "comments": {}, "calls": [], "next": 100}
    stdin = sys.stdin.read() if "--body-file" in args else ""
    state["calls"].append({"argv": args, "stdin": stdin})

    def opt(name):
        return args[args.index(name) + 1] if name in args else None

    def done(out=""):
        json.dump(state, open(path, "w"))
        print(out)
        sys.exit(0)

    if os.environ.get("FAKE_GH_FAIL") == " ".join(args[:2]):
        json.dump(state, open(path, "w"))
        print("gh: simulated failure", file=sys.stderr)
        sys.exit(1)
    if args[:2] == ["pr", "view"]:
        number = args[2]
        pr = state["prs"].get(number)
        if pr is None:
            json.dump(state, open(path, "w"))
            print("no pull requests found", file=sys.stderr)
            sys.exit(1)
        fields = opt("--json").split(",")
        if fields == ["comments"]:
            done(json.dumps({"comments": [{"body": b} for b in state["comments"].get(number, [])]}))
        done(json.dumps({k: pr.get(k) for k in fields}))
    elif args[:2] == ["pr", "list"]:
        head = opt("--head")
        done(json.dumps([pr for pr in state["prs"].values() if pr["headRefName"] == head and pr["state"] == "OPEN"]))
    elif args[:2] == ["pr", "create"]:
        number = str(state["next"])
        state["next"] += 1
        url = "https://github.com/acme/widgets/pull/" + number
        state["prs"][number] = {"number": int(number), "url": url, "state": "OPEN", "title": opt("--title"),
                                "headRefName": opt("--head"), "baseRefName": opt("--base"), "headRefOid": None,
                                "isCrossRepository": False, "body": stdin}
        done(url)
    elif args[:2] == ["pr", "comment"]:
        number = args[2]
        state["comments"].setdefault(number, []).append(stdin)
        done("https://github.com/acme/widgets/pull/" + number + "#issuecomment-1")
    sys.exit(64)
    """
)

MARKER = "<!-- independent-review v1 -->"


class Gh:
    def __init__(self, path: Path) -> None:
        self.path = path

    @property
    def state(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {"prs": {}, "comments": {}, "calls": []}

    def calls(self, *verb: str) -> list[dict]:
        return [c for c in self.state["calls"] if tuple(c["argv"][: len(verb)]) == verb]

    def seed_pr(self, number: int, *, head: str, base: str = "main", state: str = "OPEN", cross: bool = False) -> None:
        data = self.state
        data.setdefault("next", 100)
        data["prs"][str(number)] = {
            "number": number, "url": f"https://github.com/acme/widgets/pull/{number}", "state": state,
            "title": "t", "headRefName": head, "baseRefName": base, "headRefOid": None, "isCrossRepository": cross,
        }
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def comments(self, number: int) -> list[str]:
        return self.state["comments"].get(str(number), [])


@pytest.fixture
def gh(tmp_path, monkeypatch) -> Gh:
    exe = tmp_path / "tools" / "gh"
    exe.parent.mkdir(exist_ok=True)
    exe.write_text(f"#!{sys.executable}\n" + FAKE_GH, encoding="utf-8")
    exe.chmod(0o755)
    state = tmp_path / "gh-state.json"
    monkeypatch.setenv("GH_BIN", str(exe))
    monkeypatch.setenv("FAKE_GH_STATE", str(state))
    monkeypatch.delenv("FAKE_GH_FAIL", raising=False)
    return Gh(state)


@pytest.fixture
def claude(tmp_path, monkeypatch, cloud):
    """A fake claude CLI plus a 'cloud VM' clone of the same origin, for the codex fixture's repo."""
    exe = tmp_path / "tools" / "claude"
    exe.write_text(f"#!{sys.executable}\n" + FAKE_CLAUDE, encoding="utf-8")
    exe.chmod(0o755)
    log = tmp_path / "claude-calls.jsonl"
    monkeypatch.setenv("CLAUDE_BIN", str(exe))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("FAKE_CLAUDE_URL", SESSION_URL)
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    vm = tmp_path / "cloud-vm"
    _git(tmp_path, "clone", "-q", (tmp_path / "remotes" / "acme" / "widgets.git").as_posix(), str(vm))

    class C:
        pass

    ctx = C()
    ctx.vm, ctx.log = vm, log
    ctx.calls = lambda: [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

    def push(branch: str, files: dict[str, str], base: str = "origin/main") -> str:
        _git(vm, "fetch", "-q", "origin")
        _git(vm, "checkout", "-q", "-B", branch, base)
        for name, text in files.items():
            (vm / name).write_text(text, encoding="utf-8")
            _git(vm, "add", name)
        _git(vm, "commit", "-q", "-m", f"cloud work on {branch}")
        _git(vm, "push", "-q", "origin", branch)
        return _git(vm, "rev-parse", "HEAD")

    ctx.push = push
    return ctx


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _origin(cloud) -> Path:
    return cloud.repo.parent.parent / "remotes" / "acme" / "widgets.git"


def _tip(cloud, branch: str) -> str | None:
    out = _git(cloud.repo, "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    return out.split()[0] if out else None


def _push_feature(cloud, branch: str = "feature") -> str:
    """Create ``branch`` on origin (one commit ahead of main) and return to main."""
    _git(cloud.repo, "checkout", "-q", "-b", branch)
    name = branch.replace("/", "_") + ".txt"
    (cloud.repo / name).write_text("feature\n", encoding="utf-8")
    _git(cloud.repo, "add", name)
    _git(cloud.repo, "commit", "-q", "-m", f"{branch} work")
    _git(cloud.repo, "push", "-q", "origin", branch)
    sha = _git(cloud.repo, "rev-parse", "HEAD")
    _git(cloud.repo, "checkout", "-q", "main")
    return sha


def _advance_main(cloud) -> str:
    (cloud.repo / "later.txt").write_text("later\n", encoding="utf-8")
    _git(cloud.repo, "add", "later.txt")
    _git(cloud.repo, "commit", "-q", "-m", "later")
    _git(cloud.repo, "push", "-q", "origin", "main")
    return _git(cloud.repo, "rev-parse", "HEAD")


def _submit_json(cloud, capsys, *extra: str, task: str = "Add a feature") -> dict:
    code = cli.main(["run", "--where", "cloud", "--engine", "codex", "--cwd", str(cloud.repo), "--root", str(cloud.root),
                     "--task", task, "--no-brief-check", "--db", str(cloud.db), "--allow-cloud", "--json", *extra])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    return json.loads(captured.out.strip().splitlines()[-1])


def _job(cloud, job_id: str) -> dict:
    store = _store.build_store(cloud.db)
    return _jobs.find_job(store, _store.build_job_registry(store), job_id)


def _main(cloud, *argv: str) -> int:
    return cli.main([*argv, "--db", str(cloud.db)])


READY = "status: ready\nAdded the feature.\n"


# --------------------------------------------------------------------------- #
# 1. PR targets
# --------------------------------------------------------------------------- #


def test_pr_target_works_on_the_pr_head_branch_and_records_the_pr(cloud, gh, capsys):
    feature_sha = _push_feature(cloud)
    gh.seed_pr(7, head="feature", base="main")
    payload = _submit_json(cloud, capsys, "--pr", "7")
    assert payload["branch"] == "feature" and payload["pr_number"] == 7
    assert payload["pr_url"].endswith("/pull/7")
    exec_argv = next(c["argv"] for c in cloud.calls() if c["argv"][1] == "exec")
    assert exec_argv[exec_argv.index("--branch") + 1] == "feature"
    job = _job(cloud, payload["job_id"])
    assert job["pr_head"] == "feature" and job["pr_base"] == "main" and job["base_commit"] == feature_sha
    assert job["open_pr"] is False and job["land"] is False


def test_pr_target_refusals(cloud, gh, capsys):
    _push_feature(cloud)
    gh.seed_pr(8, head="feature", state="MERGED")
    assert _run_cloud(cloud, "--pr", "8") == 2
    assert "is merged, not open" in capsys.readouterr().err
    gh.seed_pr(9, head="feature", cross=True)
    assert _run_cloud(cloud, "--pr", "9") == 2
    assert "comes from a fork" in capsys.readouterr().err
    gh.seed_pr(10, head="feature")
    assert _run_cloud(cloud, "--pr", "10", "--branch", "other") == 2
    assert "conflicts with PR #10" in capsys.readouterr().err
    assert _run_cloud(cloud, "--pr", "10", "--open-pr") == 2
    assert "alternatives" in capsys.readouterr().err
    assert _run_cloud(cloud, "--pr", "404") == 2  # unknown PR: gh's own message
    assert "gh pr view" in capsys.readouterr().err
    assert cloud.calls() == []  # nothing was ever submitted
    assert _store.all_job_records(_store.build_store(cloud.db)) == []


def test_open_pr_records_the_intent_and_without_flags_nothing_changes(cloud, gh, capsys):
    payload = _submit_json(cloud, capsys, "--open-pr")
    assert payload["open_pr"] is True and payload["pr_number"] is None and payload["land"] is False
    plain = _submit_json(cloud, capsys)
    job = _job(cloud, plain["job_id"])
    assert job["open_pr"] is False and not job.get("pr_number") and job["land"] is False
    assert gh.calls() == []  # no PR flag, no gh call: behaviour as before


def test_pr_flags_are_cloud_only(cloud, capsys):
    for flag in (["--pr", "3"], ["--open-pr"], ["--land"], ["--rebase-ok"]):
        code = cli.main(["run", "--where", "local", "--engine", "codex", "--cwd", str(cloud.repo), "--root",
                         str(cloud.root), "--task", "x", "--db", str(cloud.db), *flag])
        assert code == 2
        assert f"{flag[0]} is only for --where cloud" in capsys.readouterr().err


def test_a_repo_without_an_environment_fails_early_with_the_setup_pointer(cloud, gh, capsys):
    _git(cloud.repo, "remote", "set-url", "origin", "https://github.com/acme/other.git")
    assert _run_cloud(cloud, "--pr", "5") == 2
    err = capsys.readouterr().err
    assert "no Codex Cloud environment mapped for acme/other" in err
    assert "docs/code-bridge-cloud-setup.md" in err
    assert gh.calls() == [] and cloud.calls() == []  # failed before any gh or codex call


# --------------------------------------------------------------------------- #
# 2. landing (Codex Cloud: commit, push, PR)
# --------------------------------------------------------------------------- #


def test_apply_land_commits_pushes_and_opens_a_pr(cloud, gh, capsys):
    payload = _submit_json(cloud, capsys, "--open-pr", task="Add retry logic to the fetcher\n\nDetails follow.")
    job_id = payload["job_id"]
    cloud.set_status("status: ready\nAdded retries and tests.\n")
    assert _main(cloud, "apply", job_id, "--land", "--json") == 0
    outcome = json.loads(capsys.readouterr().out)
    branch = f"cloud/{job_id[:8]}"
    sha = _tip(cloud, branch)
    assert outcome["landed"] and outcome["branch"] == branch and outcome["pushed_sha"] == sha
    assert outcome["pr_url"].endswith("/pull/100") and outcome["worktree_removed"] is True

    message = _git(cloud.repo, "log", "-1", "--format=%B", sha)
    assert message.splitlines()[0] == "Add retry logic to the fetcher"
    assert f"Cloud job {job_id[:8]} (codex)." in message and "Added retries and tests." in message
    for banned in ("Co-Authored-By", "Generated", "claude.ai", "session_"):
        assert banned not in message
    assert _git(cloud.repo, "rev-parse", f"{sha}^") == _job(cloud, job_id)["base_commit"]

    (create,) = gh.calls("pr", "create")
    argv = create["argv"]
    assert argv[argv.index("--base") + 1] == "main" and argv[argv.index("--head") + 1] == branch
    assert argv[argv.index("--title") + 1] == "Add retry logic to the fetcher"
    assert str(cloud.repo.parent) not in create["stdin"] and "Cloud job" in create["stdin"]  # no machine paths
    job = _job(cloud, job_id)
    assert job["pushed_sha"] == sha and job["pr_url"].endswith("/pull/100") and job["landed_branch"] == branch

    assert _main(cloud, "result", job_id) == 0
    out = capsys.readouterr().out
    assert f"pushed: {branch} @ {sha[:12]}" in out and "pr: https://github.com/acme/widgets/pull/100" in out
    # Landing twice is a no-op, not a second PR.
    assert _main(cloud, "apply", job_id, "--land") == 0
    assert "already landed" in capsys.readouterr().out
    assert len(gh.calls("pr", "create")) == 1


def test_land_discards_codex_error_log_and_stages_only_provider_diff(cloud, gh, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_ERROR_LOG", "1")
    job_id = _submit_json(cloud, capsys, "--open-pr")["job_id"]
    cloud.set_status(READY)
    assert _main(cloud, "apply", job_id, "--land", "--json") == 0
    outcome = json.loads(capsys.readouterr().out)
    names = _git(cloud.repo, "show", "--format=", "--name-only", outcome["pushed_sha"]).splitlines()
    assert names == ["cloud_change.txt"]
    assert "error.log" not in _git(cloud.repo, "ls-tree", "-r", "--name-only", outcome["pushed_sha"]).splitlines()


def test_land_handles_file_names_with_spaces(cloud, gh, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_SHAPE", "space")
    job_id = _submit_json(cloud, capsys, "--open-pr")["job_id"]
    cloud.set_status(READY)
    assert _main(cloud, "apply", job_id, "--land", "--json") == 0
    outcome = json.loads(capsys.readouterr().out)
    assert _git(cloud.repo, "show", "--format=", "--name-only", outcome["pushed_sha"]).splitlines() == ["with space.txt"]


def test_land_stages_both_sides_of_a_rename(cloud, gh, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_SHAPE", "rename")
    job_id = _submit_json(cloud, capsys, "--open-pr")["job_id"]
    cloud.set_status(READY)
    assert _main(cloud, "apply", job_id, "--land", "--json") == 0
    outcome = json.loads(capsys.readouterr().out)
    tree = _git(cloud.repo, "ls-tree", "-r", "--name-only", outcome["pushed_sha"]).splitlines()
    assert "RENAMED.md" in tree and "README.md" not in tree


def test_claude_land_refuses_a_source_that_is_not_the_jobs_branch(cloud, claude, gh, capsys):
    job_id = _claude_submit(cloud, capsys, "--open-pr")
    claude.push(_job(cloud, job_id)["claude_result_branch"], {"mine.txt": "x\n"})
    claude.push("claude/unrelated", {"other.txt": "x\n"})
    assert _main(cloud, "apply", job_id, "--land", "--source", "claude/unrelated") == 1
    assert "does not belong to this job" in capsys.readouterr().err
    assert gh.calls("pr", "create") == []


def test_land_refuses_an_unexpected_untracked_file(cloud, gh, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_UNEXPECTED", "1")
    job_id = _submit_json(cloud, capsys, "--open-pr")["job_id"]
    cloud.set_status(READY)
    assert _main(cloud, "apply", job_id, "--land") == 1
    err = capsys.readouterr().err
    assert "unexpected untracked" in err and "unexpected.tmp" in err
    assert _tip(cloud, f"cloud/{job_id[:8]}") is None and gh.calls("pr", "create") == []


def test_land_updates_the_recorded_pr_by_pushing_its_head_branch(cloud, gh, capsys):
    feature_sha = _push_feature(cloud)
    gh.seed_pr(7, head="feature")
    job_id = _submit_json(cloud, capsys, "--pr", "7")["job_id"]
    cloud.set_status(READY)
    assert _main(cloud, "apply", job_id, "--land", "--json") == 0
    outcome = json.loads(capsys.readouterr().out)
    new_tip = _tip(cloud, "feature")
    assert new_tip != feature_sha and outcome["pushed_sha"] == new_tip and outcome["branch"] == "feature"
    assert _git(cloud.repo, "rev-parse", f"{new_tip}^") == feature_sha  # a fast-forward on top of the PR head
    assert gh.calls("pr", "create") == []  # the recorded PR is updated, no new one opened
    assert _tip(cloud, f"cloud/{job_id[:8]}") is None
    assert outcome["pr_url"].endswith("/pull/7") and outcome["pr_number"] == 7


def test_land_refuses_when_a_recorded_pr_changes_its_head_branch(cloud, gh, capsys):
    _push_feature(cloud)
    gh.seed_pr(7, head="feature")
    job_id = _submit_json(cloud, capsys, "--pr", "7")["job_id"]
    cloud.set_status(READY)
    gh.seed_pr(7, head="replacement")
    assert _main(cloud, "apply", job_id, "--land") == 1
    err = capsys.readouterr().err
    assert "head branch moved from 'feature' to 'replacement'" in err and "--rebase-ok" in err
    assert _tip(cloud, "feature") == _job(cloud, job_id)["base_commit"]
    assert _main(cloud, "apply", job_id, "--land", "--rebase-ok") == 0


def test_land_refuses_a_moved_base_unless_rebase_ok(cloud, gh, capsys):
    job_id = _submit_json(cloud, capsys, "--open-pr")["job_id"]
    base = _job(cloud, job_id)["base_commit"]
    head = _advance_main(cloud)
    cloud.set_status(READY)
    assert _main(cloud, "apply", job_id, "--land") == 1
    err = capsys.readouterr().err
    assert "not landed" in err and f"moved from {base[:12]}" in err and head[:12] in err and "--rebase-ok" in err
    assert _tip(cloud, f"cloud/{job_id[:8]}") is None and gh.calls("pr", "create") == []
    assert [c for c in cloud.calls() if c["argv"][1] == "apply"] == []
    assert "moved" in _job(cloud, job_id)["land_error"]

    assert _main(cloud, "apply", job_id, "--land", "--rebase-ok", "--json") == 0
    outcome = json.loads(capsys.readouterr().out)
    assert _git(cloud.repo, "rev-parse", f"{outcome['pushed_sha']}^") == head  # applied onto the new head
    assert len(gh.calls("pr", "create")) == 1


def test_a_push_race_is_rejected_and_never_forced(cloud, gh, capsys, monkeypatch):
    feature_sha = _push_feature(cloud)
    gh.seed_pr(7, head="feature")
    job_id = _submit_json(cloud, capsys, "--pr", "7")["job_id"]
    cloud.set_status(READY)
    assert _main(cloud, "apply", job_id) == 0  # plain apply first: a worktree on the PR head's old tip
    capsys.readouterr()
    # Someone pushes to the PR branch meanwhile.
    _git(cloud.repo, "checkout", "-q", "feature")
    (cloud.repo / "other.txt").write_text("someone else\n", encoding="utf-8")
    _git(cloud.repo, "add", "other.txt")
    _git(cloud.repo, "commit", "-q", "-m", "someone else")
    _git(cloud.repo, "push", "-q", "origin", "feature")
    theirs = _git(cloud.repo, "rev-parse", "HEAD")
    _git(cloud.repo, "checkout", "-q", "main")

    pushes: list[tuple] = []
    real_git = _cloud._git

    def spy(cwd, *args, **kwargs):
        if args and args[0] == "push":
            pushes.append(args)
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(_cloud, "_git", spy)
    assert _main(cloud, "apply", job_id, "--land") == 1  # moved base, no --rebase-ok
    assert "--rebase-ok" in capsys.readouterr().err and pushes == []
    assert _main(cloud, "apply", job_id, "--land", "--rebase-ok") == 1  # allowed, but git rejects the non-fast-forward
    err = capsys.readouterr().err
    assert "never force-pushes" in err
    assert _tip(cloud, "feature") == theirs and theirs != feature_sha  # the other person's push is intact
    assert pushes and all(not (set(p) & _cloud_land.FORBIDDEN_PUSH_ARGS) for p in pushes)
    assert all(not a.startswith("+") for p in pushes for a in p)
    assert all(p[0:2] == ("push", "origin") and p[2].startswith("HEAD:refs/heads/") for p in pushes)


def test_land_resumes_after_a_failed_pr_step_without_pushing_again(cloud, gh, capsys, monkeypatch):
    job_id = _submit_json(cloud, capsys, "--open-pr")["job_id"]
    cloud.set_status(READY)
    monkeypatch.setenv("FAKE_GH_FAIL", "pr create")
    assert _main(cloud, "apply", job_id, "--land") == 1
    assert "gh pr create exited 1" in capsys.readouterr().err
    sha = _tip(cloud, f"cloud/{job_id[:8]}")
    job = _job(cloud, job_id)
    assert sha and job["pushed_sha"] == sha and not job.get("landed_at")

    monkeypatch.delenv("FAKE_GH_FAIL")
    pushes: list = []
    real_git = _cloud._git
    monkeypatch.setattr(_cloud, "_git", lambda cwd, *a, **k: (pushes.append(a) if a[:1] == ("push",) else None,
                                                              real_git(cwd, *a, **k))[1])
    assert _main(cloud, "apply", job_id, "--land") == 0
    assert pushes == []  # the branch was already on origin at the right commit
    assert _job(cloud, job_id)["pr_url"].endswith("/pull/100")
    assert len(gh.state["prs"]) == 1  # the failed attempt created nothing, the retry created one


def test_land_with_no_changes_pushes_nothing(cloud, gh, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_NOCHANGE", "1")
    job_id = _submit_json(cloud, capsys, "--open-pr")["job_id"]
    cloud.set_status(READY)
    assert _main(cloud, "apply", job_id, "--land") == 0
    assert "nothing to commit" in capsys.readouterr().out
    assert _tip(cloud, f"cloud/{job_id[:8]}") is None and gh.calls() == []
    assert _job(cloud, job_id)["landed_at"]


def test_land_refuses_a_job_that_is_not_done(cloud, gh, capsys):
    job_id = _submit_json(cloud, capsys, "--open-pr")["job_id"]
    assert _main(cloud, "apply", job_id, "--land") == 1
    assert "not done" in capsys.readouterr().err


def test_an_existing_branch_on_origin_is_never_taken_over(cloud, gh, capsys):
    job_id = _submit_json(cloud, capsys, "--open-pr")["job_id"]
    _push_feature(cloud, f"cloud/{job_id[:8]}")  # someone else already uses the name on origin
    _git(cloud.repo, "branch", "-q", "-D", f"cloud/{job_id[:8]}")  # ...though not in this clone
    cloud.set_status(READY)
    assert _main(cloud, "apply", job_id, "--land") == 1
    assert "refusing to overwrite" in capsys.readouterr().err
    assert gh.calls("pr", "create") == []


# --------------------------------------------------------------------------- #
# 2b. landing (Claude Cloud: verify and record what the session pushed)
# --------------------------------------------------------------------------- #


def _claude_submit(cloud, capsys, *extra: str, task: str = "Add a feature") -> str:
    code = cli.main(["run", "--where", "cloud", "--engine", "claude", "--cwd", str(cloud.repo), "--root",
                     str(cloud.root), "--task", task, "--no-brief-check", "--db", str(cloud.db), "--allow-cloud",
                     "--json", *extra])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    return json.loads(captured.out.strip().splitlines()[-1])["job_id"]


def test_claude_land_verifies_the_pushed_branch_and_opens_a_pr_without_pushing(cloud, claude, gh, capsys):
    job_id = _claude_submit(cloud, capsys, "--open-pr")
    branch = _job(cloud, job_id)["claude_result_branch"]  # the branch bound to this job
    sha = claude.push(branch, {"feature.txt": "x\n"})
    pushes: list = []
    real = _cloud._git
    _orig = _cloud._git
    try:
        _cloud._git = lambda cwd, *a, **k: (pushes.append(a) if a[:1] == ("push",) else None, real(cwd, *a, **k))[1]
        assert _main(cloud, "apply", job_id, "--land", "--json") == 0
    finally:
        _cloud._git = _orig
    outcome = json.loads(capsys.readouterr().out)
    assert pushes == []  # the session pushed; the bridge only verified
    assert outcome["branch"] == branch and outcome["pushed_sha"] == sha
    (create,) = gh.calls("pr", "create")
    argv = create["argv"]
    assert argv[argv.index("--head") + 1] == branch and argv[argv.index("--base") + 1] == "main"
    job = _job(cloud, job_id)
    assert job["pushed_sha"] == sha and job["pr_url"].endswith("/pull/100")


def test_claude_land_fast_forwards_the_recorded_pr_and_refuses_a_diverged_one(cloud, claude, gh, capsys):
    feature_sha = _push_feature(cloud)
    gh.seed_pr(7, head="feature")
    job_id = _claude_submit(cloud, capsys, "--pr", "7")
    assert _job(cloud, job_id)["branch"] == "feature"
    sha = claude.push(_job(cloud, job_id)["claude_result_branch"], {"claude.txt": "x\n"}, base="origin/feature")
    assert _main(cloud, "apply", job_id, "--land", "--json") == 0
    outcome = json.loads(capsys.readouterr().out)
    assert _tip(cloud, "feature") == sha and outcome["branch"] == "feature" and outcome["pr_number"] == 7
    assert _git(cloud.repo, "rev-parse", f"{sha}^") == feature_sha

    # A session that did not build on the PR head cannot be fast-forwarded in: refused, nothing forced.
    gh.seed_pr(11, head="other")
    _push_feature(cloud, "other")
    job2 = _claude_submit(cloud, capsys, "--pr", "11", task="Second feature")
    claude.push(_job(cloud, job2)["claude_result_branch"], {"second.txt": "x\n"}, base="origin/main")
    other_tip = _tip(cloud, "other")
    assert _main(cloud, "apply", job2, "--land") == 1
    assert "not a fast-forward" in capsys.readouterr().err
    assert _tip(cloud, "other") == other_tip


# --------------------------------------------------------------------------- #
# 3. automatic landing: wait and cloud-sync
# --------------------------------------------------------------------------- #


def test_run_land_then_wait_lands_the_job(cloud, gh, capsys):
    payload = _submit_json(cloud, capsys, "--land", task="Fix the parser")
    assert payload["land"] is True and payload["open_pr"] is True  # --land implies a new PR without --pr
    job_id = payload["job_id"]
    cloud.set_status("status: ready\nFixed.\n")
    assert _main(cloud, "wait", job_id, "--interval", "0.05") == 0
    captured = capsys.readouterr()
    assert "landed: https://github.com/acme/widgets/pull/100" in captured.err
    assert "pr: https://github.com/acme/widgets/pull/100" in captured.out
    assert _tip(cloud, f"cloud/{job_id[:8]}") == _job(cloud, job_id)["pushed_sha"]


def test_wait_without_land_does_not_touch_git(cloud, gh, capsys):
    job_id = _submit_json(cloud, capsys)["job_id"]
    cloud.set_status(READY)
    assert _main(cloud, "wait", job_id, "--interval", "0.05") == 0
    assert gh.calls() == [] and _tip(cloud, f"cloud/{job_id[:8]}") is None


def test_wait_reports_a_landing_failure_with_exit_1(cloud, gh, capsys):
    job_id = _submit_json(cloud, capsys, "--land")["job_id"]
    _advance_main(cloud)
    cloud.set_status(READY)
    assert _main(cloud, "wait", job_id, "--interval", "0.05") == 1
    captured = capsys.readouterr()
    assert "land-failed" in captured.err and "--rebase-ok" in captured.err
    assert "[done]" in captured.out  # the job itself finished fine


def test_cloud_sync_reconciles_all_live_jobs(cloud, gh, capsys):
    a = _submit_json(cloud, capsys, "--land", task="Job A")["job_id"]  # url ...abc123
    b = _submit_json(cloud, capsys, "--land", task="Job B")["job_id"]  # url ...abc1232
    c = _submit_json(cloud, capsys, "--land", task="Job C")["job_id"]  # url ...abc1233
    status = cloud.status_file
    Path(str(status) + ".task_e_abc123").write_text("status: ready\nA is done.\n", encoding="utf-8")
    Path(str(status) + ".task_e_abc1232").write_text("status: pending\n", encoding="utf-8")
    Path(str(status) + ".task_e_abc1233").write_text("status: error\nsetup script exploded\n", encoding="utf-8")
    assert _main(cloud, "cloud-sync", "--json") == 0
    rows = {r["job_id"]: r for r in json.loads(capsys.readouterr().out)["jobs"]}
    assert rows[a]["action"] == "landed" and rows[a]["detail"].endswith("/pull/100")
    assert rows[b]["action"] == "waiting"
    assert rows[c]["action"] == "failed" and "setup script exploded" in rows[c]["detail"]
    assert _job(cloud, c)["status"] == "failed" and "setup script exploded" in _job(cloud, c)["error"]
    assert _tip(cloud, f"cloud/{a[:8]}") and _tip(cloud, f"cloud/{b[:8]}") is None

    # The next run only has job B left to look at: A is landed, C's failure was reported once.
    Path(str(status) + ".task_e_abc1232").write_text("status: ready\nB is done.\n", encoding="utf-8")
    assert _main(cloud, "cloud-sync", "--json") == 0
    rows = {r["job_id"]: r for r in json.loads(capsys.readouterr().out)["jobs"]}
    assert set(rows) == {b} and rows[b]["action"] == "landed"
    assert _main(cloud, "cloud-sync") == 0
    assert "no live cloud jobs" in capsys.readouterr().out
    assert len(gh.calls("pr", "create")) == 2


def test_cloud_sync_refuses_a_moved_base_unless_submitted_with_rebase_ok(cloud, gh, capsys):
    strict = _submit_json(cloud, capsys, "--land", task="Strict job")["job_id"]
    lenient = _submit_json(cloud, capsys, "--land", "--rebase-ok", task="Lenient job")["job_id"]
    head = _advance_main(cloud)
    cloud.set_status(READY)
    assert _main(cloud, "cloud-sync", "--json") == 1  # exit 1: one landing was refused
    rows = {r["job_id"]: r for r in json.loads(capsys.readouterr().out)["jobs"]}
    assert rows[strict]["action"] == "land-failed" and "--rebase-ok" in rows[strict]["detail"]
    assert rows[lenient]["action"] == "landed"
    assert _git(cloud.repo, "rev-parse", f"{_job(cloud, lenient)['pushed_sha']}^") == head
    assert _tip(cloud, f"cloud/{strict[:8]}") is None


def test_cloud_sync_lands_a_claude_job_once_its_branch_appears(cloud, claude, gh, capsys):
    job_id = _claude_submit(cloud, capsys, "--land")
    assert _main(cloud, "cloud-sync", "--json") == 0
    assert json.loads(capsys.readouterr().out)["jobs"][0]["action"] == "waiting"
    sha = claude.push(_job(cloud, job_id)["claude_result_branch"], {"auto.txt": "x\n"})
    assert _main(cloud, "cloud-sync", "--json") == 0
    (row,) = json.loads(capsys.readouterr().out)["jobs"]
    assert row["action"] == "landed" and _job(cloud, job_id)["pushed_sha"] == sha


def test_two_syncs_cannot_land_the_same_job_at_once(cloud, gh, capsys):
    job_id = _submit_json(cloud, capsys, "--land")["job_id"]
    cloud.set_status(READY)
    store = _store.build_store(cloud.db)
    assert _cloud_land._claim(store, job_id, "land")  # another process holds the claim
    assert _main(cloud, "cloud-sync", "--json") == 1
    (row,) = json.loads(capsys.readouterr().out)["jobs"]
    assert row["action"] == "land-failed" and "another process" in row["detail"]
    assert gh.calls("pr", "create") == []
    _cloud_land._release(store, job_id, "land")
    assert _main(cloud, "cloud-sync") == 0
    assert len(gh.calls("pr", "create")) == 1


# --------------------------------------------------------------------------- #
# 4. cloud reviews
# --------------------------------------------------------------------------- #


def _land_codex_job(cloud, capsys, task="Add a parser") -> tuple[str, str]:
    job_id = _submit_json(cloud, capsys, "--land", task=task)["job_id"]
    cloud.set_status(READY)
    assert _main(cloud, "wait", job_id, "--interval", "0.05") == 0
    capsys.readouterr()
    return job_id, _job(cloud, job_id)["pushed_sha"]


def _review(cloud, *extra: str, engine: str | None = None, reviewed: str) -> int:
    argv = ["run", "--where", "cloud", "--review-of", reviewed, "--cwd", str(cloud.repo), "--root", str(cloud.root),
            "--allow-cloud", "--no-brief-check", "--db", str(cloud.db), *extra]
    if engine:
        argv += ["--engine", engine]
    return cli.main(argv)


def test_review_uses_the_opposite_engine_and_posts_one_comment(cloud, claude, gh, capsys):
    job_id, sha = _land_codex_job(cloud, capsys)
    assert _review(cloud, "--json", reviewed=job_id[:8]) == 0
    review_id = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["job_id"]
    (call,) = claude.calls()  # codex wrote it, so claude reviews it
    brief = call["argv"][1]
    assert call["argv"][0] == "--cloud" and call["branch"] == f"cloud/{job_id[:8]}"
    assert f"claude/review-{review_id[:8]}" in brief and "REVIEW.md" in brief and sha[:12] in brief
    assert "verdict: no-blocking" in brief
    review = _job(cloud, review_id)["review"]
    assert review["author"] == "codex" and review["reviewer"] == "claude" and review["pr_number"] == 100

    claude.push(f"claude/review-{review_id[:8]}", {"REVIEW.md": "Looks right.\n\n- non-blocking: nit in a.py:3\n\nverdict: no-blocking\n"},
                base=f"origin/cloud/{job_id[:8]}")
    assert _main(cloud, "wait", review_id, "--interval", "0.05") == 0
    captured = capsys.readouterr()
    assert "review-posted" in captured.err
    (comment,) = gh.comments(100)
    lines = comment.splitlines()
    assert lines[:5] == [MARKER, "author: codex", "reviewer: claude", "verdict: no-blocking", f"head: {sha}"]
    assert "non-blocking: nit in a.py:3" in comment and "verdict: no-blocking" not in "\n".join(lines[5:])
    for footer in ("Generated", "Claude Code", "---", "Co-Authored"):
        assert footer not in comment
    assert "review comment:" in captured.out and "verdict no-blocking" in captured.out
    # Once only: syncing again posts nothing.
    assert _main(cloud, "cloud-sync") == 0
    assert len(gh.comments(100)) == 1 and len(gh.calls("pr", "comment")) == 1


def test_a_codex_review_of_claude_work_reads_review_md_from_the_diff(cloud, claude, gh, capsys, tmp_path, monkeypatch):
    job_id = _claude_submit(cloud, capsys, "--land")
    branch = _job(cloud, job_id)["claude_result_branch"]
    sha = claude.push(branch, {"f.txt": "x\n"})
    assert _main(cloud, "cloud-sync") == 0
    capsys.readouterr()
    assert _review(cloud, "--json", reviewed=job_id) == 0
    review_id = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["job_id"]
    exec_call = [c for c in cloud.calls() if c["argv"][1] == "exec"][-1]["argv"]
    assert exec_call[exec_call.index("--branch") + 1] == branch  # codex reviews claude's branch
    diff = tmp_path / "review.diff"
    diff.write_text(
        "diff --git a/REVIEW.md b/REVIEW.md\n--- /dev/null\n+++ b/REVIEW.md\n@@ -0,0 +1,3 @@\n"
        "+One blocking bug in a.py:9.\n+\n+VERDICT: blocking\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_CODEX_DIFF", str(diff))
    cloud.set_status("status: ready\nReview written.\n")
    assert _main(cloud, "cloud-sync") == 0
    (comment,) = gh.comments(_job(cloud, job_id)["pr_number"])
    assert comment.splitlines()[:5] == [MARKER, "author: claude", "reviewer: codex", "verdict: blocking", f"head: {sha}"]
    assert "One blocking bug in a.py:9." in comment
    assert _job(cloud, review_id)["review_verdict"] == "blocking"


def test_a_review_of_a_job_without_a_pr_stays_in_the_result(cloud, claude, gh, capsys, tmp_path, monkeypatch):
    job_id = _claude_submit(cloud, capsys)  # no --land / PR flags
    claude.push(_job(cloud, job_id)["claude_result_branch"], {"f.txt": "x\n"})
    assert _main(cloud, "status", job_id) == 0
    capsys.readouterr()
    assert _review(cloud, "--json", reviewed=job_id) == 0
    review_id = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["job_id"]
    diff = tmp_path / "r.diff"
    diff.write_text("diff --git a/REVIEW.md b/REVIEW.md\n+++ b/REVIEW.md\n+Fine.\n+verdict: no-blocking\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_CODEX_DIFF", str(diff))
    cloud.set_status("status: ready\nok\n")
    assert _main(cloud, "wait", review_id, "--interval", "0.05") == 0
    out = capsys.readouterr().out
    assert "not posted (the reviewed job has no PR" in out and gh.calls("pr", "comment") == []


def test_review_refusals(cloud, claude, gh, capsys):
    unlanded = _submit_json(cloud, capsys)["job_id"]
    assert _review(cloud, reviewed=unlanded) == 2
    assert "land it first" in capsys.readouterr().err
    landed, _ = _land_codex_job(cloud, capsys, task="Another change")
    assert _review(cloud, engine="codex", reviewed=landed) == 2
    assert "runs on claude, not codex" in capsys.readouterr().err
    assert _review(cloud, "--land", reviewed=landed) == 2
    assert "does not take --land" in capsys.readouterr().err
    assert _review(cloud, reviewed="deadbeef") == 2
    assert "no job found" in capsys.readouterr().err
    assert claude.calls() == []


def test_an_unreadable_review_is_retried_a_bounded_number_of_times(cloud, claude, gh, capsys):
    job_id, _ = _land_codex_job(cloud, capsys)
    assert _review(cloud, "--json", reviewed=job_id) == 0
    review_id = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["job_id"]
    claude.push(f"claude/review-{review_id[:8]}", {"notes.txt": "no REVIEW.md here\n"}, base=f"origin/cloud/{job_id[:8]}")
    for attempt in (1, 2, 3):
        assert _main(cloud, "cloud-sync", "--json") == 1
        (row,) = json.loads(capsys.readouterr().out)["jobs"]
        assert row["action"] == "review-failed" and f"attempt {attempt} of 3" in row["detail"]
    assert _main(cloud, "cloud-sync", "--json") == 0  # given up: no longer owed
    assert json.loads(capsys.readouterr().out)["jobs"] == []
    assert gh.calls("pr", "comment") == []


def test_review_refuses_a_branch_that_advanced_past_the_landed_commit(cloud, claude, gh, capsys):
    job_id, sha = _land_codex_job(cloud, capsys)
    claude.push(f"cloud/{job_id[:8]}", {"later.txt": "x\n"}, base=f"origin/cloud/{job_id[:8]}")  # branch moves on
    assert _review(cloud, reviewed=job_id) == 2
    err = capsys.readouterr().err
    assert "has advanced" in err and sha[:12] in err
    assert claude.calls() == []


def test_review_brief_pins_the_exact_commit(cloud, claude, gh, capsys):
    job_id, sha = _land_codex_job(cloud, capsys)
    assert _review(cloud, reviewed=job_id) == 0
    assert f"...{sha}`" in claude.calls()[-1]["argv"][1]


def test_landing_resumes_after_the_pr_branch_push_when_the_pr_step_failed(cloud, gh, capsys, monkeypatch):
    _push_feature(cloud)
    gh.seed_pr(7, head="feature")
    job_id = _submit_json(cloud, capsys, "--pr", "7")["job_id"]
    cloud.set_status(READY)
    assert _main(cloud, "apply", job_id) == 0  # keep the worktree so the retry reuses it
    capsys.readouterr()
    monkeypatch.setenv("FAKE_GH_FAIL", "pr view")
    assert _main(cloud, "apply", job_id, "--land") == 1  # validation failed before push
    capsys.readouterr()
    assert _tip(cloud, "feature") == _job(cloud, job_id)["base_commit"]
    monkeypatch.delenv("FAKE_GH_FAIL")
    assert _main(cloud, "apply", job_id, "--land") == 0
    pushed = _tip(cloud, "feature")
    assert _job(cloud, job_id)["landed_at"] and _tip(cloud, "feature") == pushed


def test_review_comment_format_and_verdict_parsing():
    text = _cloud_land.build_review_comment(author="codex", reviewer="claude", verdict="blocking", head="a" * 40,
                                            body="- blocking: x.py:1 leaks a handle")
    assert text == f"{MARKER}\nauthor: codex\nreviewer: claude\nverdict: blocking\nhead: {'a' * 40}\n\n" \
                   "- blocking: x.py:1 leaks a handle"
    bare = _cloud_land.build_review_comment(author="a", reviewer="b", verdict="no-blocking", head="h", body="")
    assert bare.splitlines() == [MARKER, "author: a", "reviewer: b", "verdict: no-blocking", "head: h"]
    assert _cloud_land.parse_verdict("ok\nverdict: no-blocking") == ("no-blocking", "ok")
    assert _cloud_land.parse_verdict("**Verdict:** Blocking\n")[0] == "blocking"
    assert _cloud_land.parse_verdict("verdict: blocking\nlater: verdict: no blocking")[0] == "blocking"
    assert _cloud_land.parse_verdict("verdict: no blocking")[0] == "no-blocking"
    assert _cloud_land.parse_verdict("no verdict anywhere") == (None, "no verdict anywhere")
    assert _cloud_land.opposite_engine("codex") == "claude" and _cloud_land.opposite_engine("claude") == "codex"


def test_the_review_brief_asks_for_no_gated_action():
    from lazytools.code_bridge import _cloud as c

    for reviewer in ("claude", "codex"):
        brief = _cloud_land.review_brief(branch="cloud/abc", head="f" * 40, base="main", author="x",
                                         job_id="0123456789abcdef", reviewer=reviewer)
        assert c.gated_actions(brief) == []


# --------------------------------------------------------------------------- #
# 5. placement
# --------------------------------------------------------------------------- #


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 12, hour, minute)


@pytest.mark.parametrize(
    ("window", "hour", "minute", "inside"),
    [
        ("21:00-01:30", 22, 0, True), ("21:00-01:30", 0, 59, True), ("21:00-01:30", 21, 0, True),
        ("21:00-01:30", 1, 30, False), ("21:00-01:30", 12, 0, False), ("21:00-01:30", 20, 59, False),
        ("09:00-17:00", 9, 0, True), ("09:00-17:00", 17, 0, False), ("09:00-17:00", 3, 0, False),
    ],
)
def test_busy_window_matching_including_midnight_wrap(window, hour, minute, inside):
    hit = _cloud_place.inside_window(_at(hour, minute).time(), [window])
    assert (hit is not None) is inside


@pytest.mark.parametrize("bad", ["", "21:00", "25:00-01:00", "21:00-21:00", "ab-cd", "9-17"])
def test_bad_busy_windows_are_rejected(bad):
    with pytest.raises(ValueError, match="busy window"):
        _cloud_place.parse_window(bad)


def test_auto_placement_decisions_and_reasons():
    inside = _cloud_place.decide("auto", windows=("21:00-01:30",), engine="codex", now=_at(22, 10))
    assert inside.where == "cloud" and "22:10" in inside.reason and "21:00-01:30" in inside.reason
    outside = _cloud_place.decide("auto", windows=("21:00-01:30",), engine="codex", now=_at(14))
    assert outside.where == "local" and "outside every busy window" in outside.reason
    none = _cloud_place.decide("auto", windows=(), engine="claude", now=_at(23))
    assert none.where == "local" and "no busy windows configured" in none.reason  # empty by default: never redirects
    assert _cloud_place.decide("cloud", windows=(), now=_at(3)).where == "cloud"
    assert _cloud_place.decide("local", windows=("00:00-23:59",), now=_at(3)).where == "local"


def test_auto_goes_cloud_when_no_local_slot_is_free():
    slot = EngineSlot("codex", 1, _concurrency.load_config().slot_dir)
    slot.try_acquire("local-job-1")
    try:
        busy = _cloud_place.decide("auto", windows=(), engine="codex", now=_at(14))
        assert busy.where == "cloud" and "no free local engine slot" in busy.reason and "local-jo" in busy.reason
        # Engine unspecified: claude (unlimited) is still free, so the job can stay local.
        either = _cloud_place.decide("auto", windows=(), engine=None, now=_at(14))
        assert either.where == "local" and either.busy == {"codex": ["local-jo"]}
        assert _cloud_place.decide("auto", windows=(), engine="claude", now=_at(14)).where == "local"
    finally:
        slot.release()


def test_cloud_config_validates_placement_keys(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[cloud]\ndefault_where = "sometimes"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="default_where"):
        _cloud.load_config(path)
    path.write_text('[cloud]\nbusy_windows = ["21:00-25:61"]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="busy window"):
        _cloud.load_config(path)
    path.write_text('[cloud]\nbusy_windows = "21:00-01:30"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="list"):
        _cloud.load_config(path)
    path.write_text('[cloud]\ndefault_where = "auto"\nbusy_windows = ["21:00-01:30", "05:00-06:00"]\n', encoding="utf-8")
    config = _cloud.load_config(path)
    assert config.default_where == "auto" and config.busy_windows == ("21:00-01:30", "05:00-06:00")
    assert _cloud.load_config(tmp_path / "none.toml").default_where == "local"
    assert _cloud.load_config(tmp_path / "none.toml").busy_windows == ()


def test_run_where_auto_goes_cloud_inside_a_busy_window(cloud, gh, capsys, monkeypatch):
    _set_cloud_config('\n[cloud]\nbusy_windows = ["21:00-01:30"]\n')
    monkeypatch.setattr(_cloud_place, "local_now", lambda: _at(22, 5))
    code = cli.main(["run", "--where", "auto", "--engine", "codex", "--cwd", str(cloud.repo), "--root", str(cloud.root),
                     "--task", "x", "--no-brief-check", "--db", str(cloud.db), "--allow-cloud", "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    payload = json.loads(captured.out.strip().splitlines()[-1])
    assert payload["where"] == "cloud" and payload["placement"]["window"] == "21:00-01:30"
    assert "placement: cloud (requested auto): local time 22:05 is inside the busy window 21:00-01:30" in captured.err


def test_run_where_auto_stays_local_outside_the_window_and_default_where_applies(cloud, capsys, monkeypatch):
    import _code_bridge_fakes as fakes

    fakes.install(monkeypatch)
    monkeypatch.setenv("LAZYBRIDGE_SESSIONS_FILE", str(cloud.root / "sessions.json"))
    _set_cloud_config('\n[cloud]\ndefault_where = "auto"\nbusy_windows = ["21:00-01:30"]\n')
    monkeypatch.setattr(_cloud_place, "local_now", lambda: _at(14))
    code = cli.main(["run", "--engine", "codex", "--cwd", str(cloud.repo), "--root", str(cloud.root), "--task", "x",
                     "--no-brief-check", "--db", str(cloud.db)])
    captured = capsys.readouterr()
    assert code == 0 and "[done] job" in captured.out
    assert "placement: local (requested auto)" in captured.err
    assert cloud.calls() == []  # no cloud call: it ran on the (fake) local engine


def test_default_where_cloud_and_explicit_local_override(cloud, capsys, monkeypatch):
    import _code_bridge_fakes as fakes

    fakes.install(monkeypatch)
    monkeypatch.setenv("LAZYBRIDGE_SESSIONS_FILE", str(cloud.root / "sessions.json"))
    _set_cloud_config('\n[cloud]\ndefault_where = "cloud"\n')
    base = ["--engine", "codex", "--cwd", str(cloud.repo), "--root", str(cloud.root), "--task", "x", "--no-brief-check",
            "--db", str(cloud.db)]
    assert cli.main(["run", *base, "--allow-cloud"]) == 0
    assert "cloud: codex task" in capsys.readouterr().out
    assert cli.main(["run", "--where", "local", *base]) == 0
    assert "[done] job" in capsys.readouterr().out


def test_auto_cloud_without_allow_cloud_explains_itself(cloud, capsys, monkeypatch):
    _set_cloud_config('\n[cloud]\nbusy_windows = ["00:00-23:59"]\n')
    monkeypatch.setattr(_cloud_place, "local_now", lambda: _at(12))
    code = cli.main(["run", "--where", "auto", "--engine", "codex", "--cwd", str(cloud.repo), "--root", str(cloud.root),
                     "--task", "x", "--no-brief-check", "--db", str(cloud.db)])
    err = capsys.readouterr().err
    assert code == 2 and "placement: cloud" in err and "requires --allow-cloud" in err


@pytest.fixture
def routed(tmp_path, monkeypatch):
    from datetime import UTC, timedelta

    from lazytools.code_bridge import _routing
    from lazytools.projects.admission import TelemetryReading, WindowReading
    from lazytools.routing.catalogue import DEFAULT_PATH, load_tiers

    monkeypatch.setattr(_routing, "load_default_tiers", lambda: load_tiers(DEFAULT_PATH))
    now = datetime.now(UTC)

    def reading(engine):
        return TelemetryReading(engine, "fake", now, (
            WindowReading("codex/10080m" if engine == "codex" else "weekly/all models", 10, 10080, now + timedelta(days=7)),
            WindowReading("codex/300m" if engine == "codex" else "session", 20, 300, now + timedelta(hours=5)),
        ))

    monkeypatch.setattr(_routing, "read_readings", lambda: {"codex": reading("codex"), "claude_code": reading("claude_code")})
    repo = tmp_path / "rrepo"
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo, tmp_path / "route.sqlite"


def test_route_where_auto_shows_the_decision_and_why(routed, capsys, monkeypatch):
    repo, db = routed
    _set_cloud_config('\n[cloud]\nbusy_windows = ["21:00-01:30"]\n')
    monkeypatch.setattr(_cloud_place, "local_now", lambda: _at(23))
    assert cli.main(["route", "--tier", "writing", "--where", "auto", "--cwd", str(repo), "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "placement: cloud (requested auto): local time 23:00 is inside the busy window 21:00-01:30" in out
    assert cli.main(["route", "--tier", "writing", "--where", "auto", "--cwd", str(repo), "--db", str(db), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["placement"]["where"] == "cloud" and payload["placement"]["requested"] == "auto"
    monkeypatch.setattr(_cloud_place, "local_now", lambda: _at(9))
    assert cli.main(["route", "--tier", "writing", "--where", "auto", "--cwd", str(repo), "--db", str(db)]) == 0
    assert "placement: local (requested auto)" in capsys.readouterr().out
    assert cli.main(["route", "--tier", "writing", "--cwd", str(repo), "--db", str(db)]) == 0  # no --where: unchanged
    assert "placement" not in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# 6. models
# --------------------------------------------------------------------------- #


def test_claude_cloud_models_follow_the_tier_map(cloud, claude, capsys):
    _set_cloud_config('\n[cloud.claude.models]\ncritical = "claude-opus-5-5"\ndefault = "claude-haiku-5-5"\n')

    def model_of_last_call() -> str:
        argv = claude.calls()[-1]["argv"]
        return argv[argv.index("--model") + 1]

    _claude_submit(cloud, capsys, "--tier", "critical")
    assert model_of_last_call() == "claude-opus-5-5"
    _claude_submit(cloud, capsys, "--tier", "basic", task="second")
    assert model_of_last_call() == "claude-haiku-5-5"  # unmapped tier: the configured default
    job = _claude_submit(cloud, capsys, "--tier", "critical", "--model", "claude-sonnet-5-5", task="third")
    assert model_of_last_call() == "claude-sonnet-5-5"  # explicit --model wins
    assert _job(cloud, job)["model"] == "claude-sonnet-5-5" and _job(cloud, job)["tier"] == "critical"


def test_claude_cloud_default_model_is_sonnet_5_5(cloud, claude, capsys):
    _claude_submit(cloud, capsys)
    argv = claude.calls()[-1]["argv"]
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5" == _cloud.DEFAULT_CLAUDE_CLOUD_MODEL


def test_codex_cloud_uses_the_environment_model_and_refuses_overrides(cloud, capsys):
    assert _run_cloud(cloud, "--tier", "thinking") == 0  # a tier is fine: nothing to pass, the env default applies
    out = capsys.readouterr().out
    assert "environment's default model" in out
    exec_argv = next(c["argv"] for c in cloud.calls() if c["argv"][1] == "exec")
    assert "--model" not in exec_argv and "-c" not in exec_argv and not any("model" in a for a in exec_argv[:-1])
    assert _run_cloud(cloud, "--model", "gpt-6.1-sol") == 2
    assert "ignores model overrides" in capsys.readouterr().err
    assert len([c for c in cloud.calls() if c["argv"][1] == "exec"]) == 1


def test_model_config_validation(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[cloud.codex.models]\nbasic = "gpt-x"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="CLI ignores `-c model`"):
        _cloud.load_config(path)
    path.write_text('[cloud.claude.models]\nfancy = "claude-x"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="not a tier"):
        _cloud.load_config(path)
    path.write_text('[cloud.claude.models]\nbasic = ""\n', encoding="utf-8")
    with pytest.raises(ValueError, match="model id"):
        _cloud.load_config(path)
    path.write_text('[cloud.claude.models]\nbasic = "claude-haiku-5-5"\ncritical = "claude-opus-5-5[1m]"\n', encoding="utf-8")
    assert _cloud.load_config(path).models["claude"]["critical"] == "claude-opus-5-5[1m]"
    with pytest.raises(_cloud.CloudError, match="routine"):
        _cloud.model_for("claude", _cloud.CloudConfig(), explicit="x", routine="nightly")


# --------------------------------------------------------------------------- #
# 7. environments
# --------------------------------------------------------------------------- #


def test_parse_codex_list_shapes():
    row = {"id": "task_1", "environment_id": "env_a", "environment_label": "acme/widgets"}
    assert _cloud_envs.parse_codex_list(json.dumps([row, {"id": "task_2", "environment_id": "env_a"}])) == {"env_a": "acme/widgets"}
    assert _cloud_envs.parse_codex_list(json.dumps({"tasks": [row, {"environment_id": "env_b", "environment_label": "x"}]})) == {
        "env_a": "acme/widgets", "env_b": "x"}
    assert _cloud_envs.parse_codex_list("[]") == {} and _cloud_envs.parse_codex_list('{"tasks": [{"id": 1}]}') == {}
    with pytest.raises(_cloud.CloudError, match="no JSON"):
        _cloud_envs.parse_codex_list("not json")


def test_cloud_envs_lists_codex_environments_with_their_repo_and_claude_ones(cloud, capsys, monkeypatch):
    cloud.list_file.write_text(json.dumps([
        {"id": "t1", "environment_id": "env_widgets", "environment_label": "acme/widgets"},
        {"id": "t2", "environment_id": "env_stray", "environment_label": "acme/stray"},
        {"id": "t3", "environment_id": "env_named", "environment_label": "Prod sandbox"},
    ]), encoding="utf-8")
    monkeypatch.setenv("LZ_ROUTINE_TOKEN", "t")
    _set_cloud_config('\n[cloud.claude.routines.nightly]\nid = "trig_1"\ntoken_env = "LZ_ROUTINE_TOKEN"\nrepo = "acme/widgets"\nbranch = "main"\n')
    assert cli.main(["cloud-envs", "--cwd", str(cloud.repo), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    rows = {r["env_id"]: r for r in report["environments"]}
    assert rows["env_widgets"]["repo"] == "acme/widgets" and rows["env_widgets"]["configured"] is True
    assert rows["env_stray"]["repo"] == "acme/stray" and rows["env_stray"]["configured"] is False  # label hint, unmapped
    assert "[cloud.codex.envs]" in rows["env_stray"]["note"] and rows["env_named"]["repo"] is None
    assert rows["claude.ai-default"]["provider"] == "claude" and "claude.ai/code" in rows["claude.ai-default"]["note"]
    assert rows["routine:nightly"]["token_env"] == "LZ_ROUTINE_TOKEN" and rows["routine:nightly"]["token_set"] is True
    assert json.dumps(report).count("LZ_ROUTINE_TOKEN=") != "t"  # only the variable NAME is ever shown
    assert report["current_repo"] == "acme/widgets" and report["current_codex_env"] == "env_widgets"
    assert cli.main(["cloud-envs", "--cwd", str(cloud.repo)]) == 0
    text = capsys.readouterr().out
    assert "env_widgets" in text and "unmapped" in text and "this repo: acme/widgets -> codex env env_widgets" in text


def test_cloud_envs_survives_a_failing_codex_list_and_flags_an_unmapped_repo(cloud, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_FAIL", "list")
    _git(cloud.repo, "remote", "set-url", "origin", "https://github.com/acme/other.git")
    assert cli.main(["cloud-envs", "--cwd", str(cloud.repo), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert any("could not read Codex Cloud environments" in n for n in report["notes"])
    assert any("acme/other has no Codex Cloud environment" in n for n in report["notes"])
    assert [r["env_id"] for r in report["environments"] if r["provider"] == "codex"] == ["env_widgets"]
    assert cli.main(["cloud-envs", "--no-probe", "--json", "--cwd", str(cloud.repo)]) == 0
    assert json.loads(capsys.readouterr().out)["notes"][0].startswith("acme/other")


# --------------------------------------------------------------------------- #
# 8. concurrency per provider
# --------------------------------------------------------------------------- #


def test_per_provider_limits_are_in_addition_to_the_global_one(cloud, claude, capsys):
    _set_cloud_config("\n[cloud]\nconcurrency = 4\n[cloud.codex]\nconcurrency = 1\n[cloud.claude]\nconcurrency = 2\n")
    assert _run_cloud(cloud) == 0
    capsys.readouterr()
    assert _run_cloud(cloud) == 2  # codex: one live job already
    err = capsys.readouterr().err
    assert "codex cloud jobs are at their concurrency limit (1)" in err and "[cloud.codex] concurrency" in err
    _claude_submit(cloud, capsys)  # claude has its own room
    _claude_submit(cloud, capsys, task="second claude")
    assert cli.main(["run", "--where", "cloud", "--engine", "claude", "--cwd", str(cloud.repo), "--root", str(cloud.root),
                     "--task", "third", "--no-brief-check", "--db", str(cloud.db), "--allow-cloud"]) == 2
    assert "claude cloud jobs are at their concurrency limit (2)" in capsys.readouterr().err
    assert len(_cloud.active_jobs(_store.build_store(cloud.db), "codex")) == 1
    assert len(_cloud.active_jobs(_store.build_store(cloud.db), "claude")) == 2


def test_a_finished_job_frees_the_provider_place(cloud, capsys):
    _set_cloud_config("\n[cloud.codex]\nconcurrency = 1\n")
    assert _run_cloud(cloud) == 0
    cloud.set_status(READY)
    assert _run_cloud(cloud) == 0  # the first job is refreshed to done before the count


def test_global_limit_still_applies_with_provider_limits(cloud, capsys):
    _set_cloud_config("\n[cloud]\nconcurrency = 1\n[cloud.codex]\nconcurrency = 5\n")
    assert _run_cloud(cloud) == 0
    capsys.readouterr()
    assert _run_cloud(cloud) == 2
    assert "cloud jobs are at their concurrency limit (1)" in capsys.readouterr().err


def test_provider_concurrency_config_validation(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("[cloud.codex]\nconcurrency = -2\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"\[cloud.codex\] concurrency"):
        _cloud.load_config(path)
    path.write_text('[cloud.claude]\nconcurrency = "two"\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"\[cloud.claude\] concurrency"):
        _cloud.load_config(path)
    path.write_text("[cloud.codex]\nconcurrency = 0\n[cloud.claude]\nconcurrency = 3\n", encoding="utf-8")
    config = _cloud.load_config(path)
    assert config.provider_concurrency == {"codex": None, "claude": 3}
    assert _concurrency.load_config(path).limits["codex"] == 1  # [cloud.*] never leaks into [concurrency]


# --------------------------------------------------------------------------- #
# 9. MCP
# --------------------------------------------------------------------------- #


def test_code_bridge_provider_is_gated_like_the_other_write_tools(tmp_path):
    from lazytools.mcp_server import default_providers, expand_tools, providers

    assert "code_bridge" in providers.PROVIDER_FACTORIES
    assert default_providers(["code_bridge"], allow_write=False) == []  # refused at construction, so skipped
    built = default_providers(["code_bridge"], allow_write=True, data_source={"code_root": str(tmp_path)})
    tools = expand_tools(built, read_only=False)
    assert {"code_bridge_submit", "code_bridge_status", "code_bridge_result"} <= tools.keys()
    # Even handed to a read-only server by mistake, the coarse net drops all three.
    assert not {n for n in expand_tools(built, read_only=True) if n.startswith("code_bridge_")}


class _InProcess:
    """Run the bridge CLI in this process instead of a child (the tool code is unchanged)."""

    def __init__(self, tools) -> None:
        self.argvs: list[list[str]] = []
        tools._invoke = self  # type: ignore[method-assign]

    def __call__(self, argv, timeout):
        import contextlib
        import io

        self.argvs.append(list(argv))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()


def test_mcp_submit_status_result_for_a_cloud_job_with_landing(cloud, gh, capsys):
    from lazytools.code_bridge._mcp import CodeBridgeTools

    tools = CodeBridgeTools(root=str(cloud.root))
    runner = _InProcess(tools)
    os.environ["LAZYTOOLS_CODE_BRIDGE_DB"] = str(cloud.db)
    try:
        refused = tools.code_bridge_submit("Add a thing", cwd=str(cloud.repo), engine="codex", where="cloud")
        assert refused["ok"] is False and "--allow-cloud" in refused["error"]  # the caller must acknowledge cloud runs
        submitted = tools.code_bridge_submit("Add a thing", cwd=str(cloud.repo), engine="codex", where="cloud",
                                             allow_cloud=True, land=True)
        assert submitted["ok"] and submitted["where"] == "cloud" and submitted["land"] is True
        assert submitted["placement"]["where"] == "cloud"
        job_id = submitted["job_id"]
        argv = runner.argvs[-1]
        assert argv[:3] == ["run", "--where", "cloud"] and "--land" in argv and "--detach" not in argv
        assert "--root" in argv and argv[argv.index("--root") + 1] == str(cloud.root.resolve())

        assert tools.code_bridge_status(job_id)["status"] == "running"
        cloud.set_status(READY)
        assert cli.main(["cloud-sync", "--db", str(cloud.db)]) == 0
        result = tools.code_bridge_result(job_id)
        assert result["ok"] and result["status"] == "done"
        assert result["pr_url"].endswith("/pull/100") and result["pushed_sha"] and result["landed_branch"]
    finally:
        os.environ.pop("LAZYTOOLS_CODE_BRIDGE_DB", None)


def test_mcp_submit_validation_and_local_placement(cloud, capsys, monkeypatch):
    from lazytools.code_bridge._mcp import CodeBridgeTools

    tools = CodeBridgeTools(root=str(cloud.root))
    runner = _InProcess(tools)
    with pytest.raises(ValueError, match="'@'"):
        tools.code_bridge_submit("@/etc/passwd", engine="codex")
    with pytest.raises(ValueError, match="job_id"):
        tools.code_bridge_status("../../etc")
    with pytest.raises(ValueError, match="cloud options"):
        tools.code_bridge_submit("x", engine="codex", where="local", land=True)
    assert runner.argvs == []
    seen: dict = {}
    monkeypatch.setattr(cli, "_spawn_detached", lambda argv, log: seen.update(argv=argv) or 4242)
    result = tools.code_bridge_submit("x", cwd=str(cloud.repo), engine="codex", where="local")
    assert result["ok"] and result["pid"] == 4242 and result["placement"]["where"] == "local"
    assert "--detach" in runner.argvs[-1] and seen["argv"][seen["argv"].index("--where") + 1] == "local"


def test_mcp_tools_really_run_the_cli_in_a_child_process(cloud):
    from lazytools.code_bridge._mcp import CodeBridgeTools

    tools = CodeBridgeTools(root=str(cloud.root))
    os.environ["LAZYTOOLS_CODE_BRIDGE_DB"] = str(cloud.db)
    try:
        out = tools.code_bridge_status("deadbeef")
    finally:
        os.environ.pop("LAZYTOOLS_CODE_BRIDGE_DB", None)
    assert out["ok"] is False and "no job found matching 'deadbeef'" in out["error"]


# --------------------------------------------------------------------------- #
# gh wrapper
# --------------------------------------------------------------------------- #


def test_gh_wrapper_never_puts_a_body_on_the_command_line(gh):
    url = _gh.pr_create("acme/widgets", base="main", head="topic", title="T", body="BODY-TEXT")
    assert url.endswith("/pull/100")
    (call,) = gh.calls("pr", "create")
    assert "BODY-TEXT" not in " ".join(call["argv"]) and call["stdin"] == "BODY-TEXT"
    assert _gh.pr_number_from_url(url) == 100 and _gh.pr_number_from_url("nope") is None


def test_missing_gh_is_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.setenv("GH_BIN", "")
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(_cloud.CloudError, match="GitHub CLI"):
        _gh.gh_argv()


def test_subprocess_default_python_is_importable_for_mcp_child():
    proc = subprocess.run([sys.executable, "-c", "import lazytools.code_bridge._mcp"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
