"""Claude Code cloud sessions (``run --where cloud --engine claude``): offline.

A fake ``claude`` executable (reached through ``CLAUDE_BIN``) logs argv, cwd
and the checkout's current branch, and prints a session line. The "cloud
session pushing its branch" is simulated by pushing a ``claude/...`` branch
to the bare ``origin`` from a second clone. The routine fire endpoint is
replaced by a fake ``_http_post``; nothing touches the network.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from lazytools.code_bridge import _cloud, _cloud_claude, _concurrency, _jobs, _store, cli

SESSION_URL = "https://claude.ai/code/session_abc123def"

FAKE_CLAUDE = textwrap.dedent(
    """
    import json, os, subprocess, sys

    args = sys.argv[1:]
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True).stdout.strip()
    with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": args, "cwd": os.getcwd(), "branch": branch}) + "\\n")
    mode = os.environ.get("FAKE_CLAUDE_MODE", "")
    if mode == "auth":
        print("Error: not logged in. Run claude auth login.", file=sys.stderr)
        sys.exit(1)
    if "--output-format" in args:
        if mode == "reject_format":
            print("error: unknown option '--output-format'", file=sys.stderr)
            sys.exit(2)
        print(json.dumps({"ok": True, "session_id": "session_abc123def", "url": os.environ["FAKE_CLAUDE_URL"]}))
    else:
        print("Started cloud session: " + os.environ["FAKE_CLAUDE_URL"])
    """
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def cc(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    exe = tools / "claude"
    exe.write_text(f"#!{sys.executable}\n" + FAKE_CLAUDE, encoding="utf-8")
    exe.chmod(0o755)
    log = tmp_path / "claude-calls.jsonl"
    monkeypatch.setenv("CLAUDE_BIN", str(exe))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("FAKE_CLAUDE_URL", SESSION_URL)
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    for key, value in {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }.items():
        monkeypatch.setenv(key, value)

    repo = tmp_path / "work" / "widgets"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")
    origin = tmp_path / "remotes" / "acme" / "widgets.git"
    origin.parent.mkdir(parents=True)
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    _git(repo, "remote", "add", "origin", origin.as_posix())
    _git(repo, "push", "-q", "origin", "main")
    other = tmp_path / "cloud-vm"
    _git(tmp_path, "clone", "-q", origin.as_posix(), str(other))

    class Ctx:
        pass

    ctx = Ctx()
    ctx.repo, ctx.origin, ctx.vm, ctx.log = repo, origin, other, log
    ctx.db = tmp_path / "work" / "store.sqlite"
    ctx.root = tmp_path / "work"
    ctx.calls = lambda: [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    return ctx


def _vm_push(ctx, branch: str, name: str = "cloud_change.txt", base: str = "origin/main") -> str:
    """What the hosted session does: commit on a claude/ branch and push it to origin."""
    _git(ctx.vm, "fetch", "-q", "origin")
    _git(ctx.vm, "checkout", "-q", "-B", branch, base)
    (ctx.vm / name).write_text("made in the cloud\n", encoding="utf-8")
    _git(ctx.vm, "add", name)
    _git(ctx.vm, "commit", "-q", "-m", f"cloud work on {branch}")
    _git(ctx.vm, "push", "-q", "origin", branch)
    return _git(ctx.vm, "rev-parse", "HEAD")


def _run(ctx, *extra: str, task: str = "Add a feature") -> int:
    return cli.main(
        ["run", "--where", "cloud", "--engine", "claude", "--cwd", str(ctx.repo), "--root", str(ctx.root),
         "--task", task, "--no-brief-check", "--db", str(ctx.db), "--allow-cloud", *extra]
    )


def _submit_json(ctx, capsys, *extra: str, task: str = "Add a feature") -> dict:
    assert _run(ctx, *extra, "--json", task=task) == 0, capsys.readouterr().err
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _job(ctx, job_id: str) -> dict:
    store = _store.build_store(ctx.db)
    return _jobs.find_job(store, _store.build_job_registry(store), job_id)


def _config(text: str) -> None:
    path = Path(os.environ[_concurrency.CONFIG_ENV])
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# submit (CLI mode)
# --------------------------------------------------------------------------- #


def test_submit_runs_claude_cloud_from_the_pushed_checkout(cc, capsys):
    payload = _submit_json(cc, capsys)
    assert payload["engine"] == "claude" and payload["where"] == "cloud" and payload["status"] == "running"
    assert payload["cloud_handle"] == SESSION_URL and payload["cloud_url"] == SESSION_URL
    assert payload["env_id"] == _cloud_claude.ENV_LABEL
    (call,) = cc.calls()
    assert call["argv"][0] == "--cloud"
    assert call["argv"][1].startswith(_cloud.CLOUD_PREAMBLE) and call["argv"][1].endswith("Add a feature")
    assert call["argv"][2:] == ["--output-format", "json"]
    assert Path(call["cwd"]).resolve() == cc.repo.resolve() and call["branch"] == "main"
    job = _job(cc, payload["job_id"])
    assert job["cloud_mode"] == "cli" and job["claude_session_id"] == "session_abc123def"
    assert "claude.ai/code" in job["environment_note"] and "no flag" in job["environment_note"]
    assert job["claude_refs_before"] == {}
    assert '"ok": true' in job["claude_submit_raw"]


def test_submit_text_output_mentions_no_status_api(cc, capsys):
    assert _run(cc) == 0
    out = capsys.readouterr().out
    assert "no status API" in out and "claude.ai/code" in out


def test_submit_checks_out_requested_branch_in_a_temporary_worktree(cc, capsys):
    _vm_push(cc, "feature-x")  # exists on origin, not locally
    payload = _submit_json(cc, capsys, "--branch", "feature-x")
    (call,) = cc.calls()
    assert call["branch"] == "feature-x"
    assert Path(call["cwd"]).resolve() != cc.repo.resolve()
    assert not Path(call["cwd"]).exists()  # removed after submit
    assert _git(cc.repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"  # operator checkout untouched
    assert _job(cc, payload["job_id"])["branch"] == "feature-x"


def test_unpushed_commits_are_refused_and_never_pushed(cc, capsys):
    (cc.repo / "local.txt").write_text("x\n", encoding="utf-8")
    _git(cc.repo, "add", "local.txt")
    _git(cc.repo, "commit", "-q", "-m", "local only")
    assert _run(cc) == 2
    err = capsys.readouterr().err
    assert "differs from origin/main" in err and "never pushes" in err
    assert cc.calls() == []
    assert _store.all_job_records(_store.build_store(cc.db)) == []
    assert _git(cc.origin, "rev-parse", "main") != _git(cc.repo, "rev-parse", "HEAD")


def test_branch_missing_on_origin_is_refused(cc, capsys):
    _git(cc.repo, "checkout", "-q", "-b", "only-local")
    assert _run(cc) == 2
    assert "is not on origin" in capsys.readouterr().err
    assert cc.calls() == []


def test_retries_without_output_format_and_parses_text(cc, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "reject_format")
    payload = _submit_json(cc, capsys)
    first, second = cc.calls()
    assert "--output-format" in first["argv"] and "--output-format" not in second["argv"]
    assert payload["cloud_url"] == SESSION_URL


def test_auth_failure_is_recorded_with_login_hint(cc, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "auth")
    assert _run(cc) == 2
    assert "claude auth login" in capsys.readouterr().err
    (record,) = _store.all_job_records(_store.build_store(cc.db))
    assert record["status"] == "failed"


def test_claude_cloud_needs_allow_cloud_and_gated_approval(cc, capsys):
    assert cli.main(["run", "--where", "cloud", "--engine", "claude", "--cwd", str(cc.repo), "--task", "x",
                     "--no-brief-check", "--db", str(cc.db)]) == 2
    assert "requires --allow-cloud" in capsys.readouterr().err
    assert _run(cc, task="Fix it and merge it into main") == 2
    assert "--approve-cloud merge" in capsys.readouterr().err
    assert cc.calls() == []


def test_routine_flag_is_claude_cloud_only(cc, capsys):
    assert cli.main(["run", "--where", "cloud", "--engine", "codex", "--allow-cloud", "--routine", "r",
                     "--cwd", str(cc.repo), "--task", "x", "--no-brief-check", "--db", str(cc.db)]) == 2
    assert "--routine is only for" in capsys.readouterr().err
    assert cli.main(["run", "--engine", "claude", "--routine", "r", "--cwd", str(cc.repo), "--task", "x"]) == 2
    assert "--routine is only for --where cloud" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# status / result / apply from git
# --------------------------------------------------------------------------- #


def test_status_says_no_status_api_until_a_branch_appears(cc, capsys):
    job_id = _submit_json(cc, capsys)["job_id"]
    assert cli.main(["status", job_id, "--db", str(cc.db)]) == 0
    out = capsys.readouterr().out
    assert f"submitted (no status API): open {SESSION_URL}" in out
    assert _job(cc, job_id)["status"] == "running"


def test_preexisting_claude_branches_are_not_the_result(cc, capsys):
    _vm_push(cc, "claude/old-work", name="old.txt")
    job_id = _submit_json(cc, capsys)["job_id"]
    assert _job(cc, job_id)["claude_refs_before"].keys() == {"claude/old-work"}
    cli.main(["status", job_id, "--db", str(cc.db)])
    capsys.readouterr()
    assert _job(cc, job_id)["status"] == "running"


def test_result_and_apply_use_the_pushed_claude_branch(cc, capsys):
    job_id = _submit_json(cc, capsys)["job_id"]
    sha = _vm_push(cc, "claude/add-feature-x1")
    assert cli.main(["result", job_id, "--db", str(cc.db)]) == 0
    out = capsys.readouterr().out
    assert "claude/add-feature-x1" in out and "1 file(s) changed" in out and "cloud_change.txt" in out
    job = _job(cc, job_id)
    assert job["status"] == "done" and job["result_branch"] == "claude/add-feature-x1"
    assert job["result_commit"] == sha

    assert cli.main(["apply", job_id, "--db", str(cc.db), "--json"]) == 0
    outcome = json.loads(capsys.readouterr().out)
    worktree = Path(outcome["worktree"])
    assert (worktree / "cloud_change.txt").is_file()
    assert outcome["branch"] == f"cloud/{job_id[:8]}" and outcome["pushed"] is False
    assert outcome["source_branch"] == "claude/add-feature-x1" and outcome["base"] == sha
    assert _git(cc.repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert not (cc.repo / "cloud_change.txt").exists()
    assert "cloud/" not in _git(cc.origin, "branch", "--list")  # nothing pushed
    assert cli.main(["apply", job_id, "--db", str(cc.db)]) == 1  # applied once
    assert "already applied" in capsys.readouterr().err


def test_apply_before_any_branch_is_refused(cc, capsys):
    job_id = _submit_json(cc, capsys)["job_id"]
    assert cli.main(["apply", job_id, "--db", str(cc.db)]) == 1
    assert "not done" in capsys.readouterr().err


def test_branch_named_in_the_brief_wins(cc, capsys):
    job_id = _submit_json(cc, capsys, task="Leave the result on branch claude/named-x please.")["job_id"]
    assert _job(cc, job_id)["named_result_branch"] == "claude/named-x"
    _vm_push(cc, "claude/someone-else", name="a.txt")
    _vm_push(cc, "claude/named-x", name="b.txt")
    cli.main(["status", job_id, "--db", str(cc.db)])
    capsys.readouterr()
    assert _job(cc, job_id)["result_branch"] == "claude/named-x"


def test_two_jobs_never_claim_the_same_branch(cc, capsys):
    first = _submit_json(cc, capsys)["job_id"]
    second = _submit_json(cc, capsys)["job_id"]
    _vm_push(cc, "claude/one", name="one.txt")
    cli.main(["status", first, "--db", str(cc.db)])
    _vm_push(cc, "claude/two", name="two.txt")
    cli.main(["status", second, "--db", str(cc.db)])
    capsys.readouterr()
    assert _job(cc, first)["result_branch"] == "claude/one"
    assert _job(cc, second)["result_branch"] == "claude/two"


def test_apply_source_picks_another_candidate(cc, capsys):
    job_id = _submit_json(cc, capsys)["job_id"]
    _vm_push(cc, "claude/a", name="a.txt")
    _vm_push(cc, "claude/b", name="b.txt")
    cli.main(["status", job_id, "--db", str(cc.db)])
    capsys.readouterr()
    job = _job(cc, job_id)
    other = (job["result_branch_candidates"] or [None])[0]
    assert other in ("claude/a", "claude/b") and other != job["result_branch"]
    assert cli.main(["apply", job_id, "--db", str(cc.db), "--source", other, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["source_branch"] == other


def test_no_status_jobs_share_the_cloud_limit_and_cancel_frees_it(cc, capsys):
    _config("\n[cloud]\nconcurrency = 1\n")
    job_id = _submit_json(cc, capsys)["job_id"]
    assert _run(cc) == 2
    assert "concurrency limit (1)" in capsys.readouterr().err
    assert cli.main(["cancel", job_id, "--reason", "done by hand", "--db", str(cc.db)]) == 0
    assert "NOT stopped" in capsys.readouterr().out
    assert _run(cc) == 0


def test_cloud_jobs_take_no_local_engine_slot(cc, capsys, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a cloud submit must not take a local engine slot")

    monkeypatch.setattr(_concurrency.EngineSlot, "__init__", boom)
    assert _run(cc) == 0


# --------------------------------------------------------------------------- #
# routine mode
# --------------------------------------------------------------------------- #


ROUTINE_CONFIG = '\n[cloud.claude.routines.nightly]\nid = "trig_01ABC"\ntoken_env = "TEST_ROUTINE_TOKEN"\n'


def test_routine_fire_posts_text_with_bearer_and_version(cc, capsys, monkeypatch):
    _config(ROUTINE_CONFIG)
    monkeypatch.setenv("TEST_ROUTINE_TOKEN", "sk-ant-oat01-abcdefghijklmnop")
    seen = {}

    def fake_post(url, headers, body, timeout):
        seen.update(url=url, headers=headers, body=json.loads(body))
        return 200, json.dumps({"claude_code_session_id": "session_r1xyz9",
                                "claude_code_session_url": "https://claude.ai/code/session_r1xyz9"})

    monkeypatch.setattr(_cloud_claude, "_http_post", fake_post)
    payload = _submit_json(cc, capsys, "--routine", "nightly")
    assert seen["url"] == "https://api.anthropic.com/v1/claude_code/routines/trig_01ABC/fire"
    assert seen["headers"]["Authorization"] == "Bearer sk-ant-oat01-abcdefghijklmnop"
    assert seen["headers"]["anthropic-version"] == "2026-10-21"
    assert seen["body"]["text"].startswith(_cloud.CLOUD_PREAMBLE)
    assert payload["cloud_url"] == "https://claude.ai/code/session_r1xyz9" and payload["env_id"] == "routine:nightly"
    job = _job(cc, payload["job_id"])
    assert job["cloud_mode"] == "routine" and job["routine"] == "nightly"
    assert cc.calls() == []  # no CLI
    assert "sk-ant-oat01-abcdefghijklmnop" not in json.dumps(job)


def test_routine_without_token_env_is_refused(cc, capsys, monkeypatch):
    _config(ROUTINE_CONFIG)
    monkeypatch.delenv("TEST_ROUTINE_TOKEN", raising=False)
    assert _run(cc, "--routine", "nightly") == 2
    assert "TEST_ROUTINE_TOKEN is not set" in capsys.readouterr().err


def test_routine_http_error_is_redacted(cc, capsys, monkeypatch):
    _config(ROUTINE_CONFIG)
    token = "sk-ant-oat01-secretsecretsecret"
    monkeypatch.setenv("TEST_ROUTINE_TOKEN", token)
    monkeypatch.setattr(_cloud_claude, "_http_post", lambda *a: (401, f"bad token {token}"))
    assert _run(cc, "--routine", "nightly") == 2
    err = capsys.readouterr().err
    assert "HTTP 401" in err and token not in err


def test_unknown_routine_shows_config_snippet(cc, capsys):
    assert _run(cc, "--routine", "missing") == 2
    err = capsys.readouterr().err
    assert "[cloud.claude.routines.missing]" in err and "token_env" in err


# --------------------------------------------------------------------------- #
# config and parsing
# --------------------------------------------------------------------------- #


def test_config_routines_and_refusals(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(ROUTINE_CONFIG, encoding="utf-8")
    assert _cloud.load_config(path).routines == {"nightly": {"id": "trig_01ABC", "token_env": "TEST_ROUTINE_TOKEN"}}
    path.write_text('[cloud.claude.routines.n]\nid = "trig_1"\ntoken = "sk-ant-xyz"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="never store the token"):
        _cloud.load_config(path)
    path.write_text('[cloud.claude.routines.n]\nid = "trig_1"\ntoken_env = "not a var"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="environment variable NAME"):
        _cloud.load_config(path)
    path.write_text('[cloud.claude.envs]\n"a/b" = "env"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="no environment flag"):
        _cloud.load_config(path)
    path.write_text('[cloud.codex.routines.x]\nid = "a"\ntoken_env = "B"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="unknown keys"):
        _cloud.load_config(path)


@pytest.mark.parametrize(
    ("text", "sid", "url"),
    [
        ('{"ok": true, "session_id": "session_1abcdef", "url": "https://claude.ai/code/session_1abcdef"}',
         "session_1abcdef", "https://claude.ai/code/session_1abcdef"),
        ("noise\n" + '{"sessionId": "session_2abcdef"}', "session_2abcdef", None),
        ("Started: https://claude.ai/code/session_3abcdef.", "session_3abcdef", "https://claude.ai/code/session_3abcdef"),
        ("id session_4abcdef queued", "session_4abcdef", None),
    ],
)
def test_parse_cli_submit(text, sid, url):
    got_sid, got_url, raw = _cloud_claude.parse_cli_submit(text)
    assert (got_sid, got_url) == (sid, url) and raw


def test_parse_cli_submit_failures():
    with pytest.raises(_cloud.CloudError, match="no session id"):
        _cloud_claude.parse_cli_submit("hello")
    with pytest.raises(_cloud.CloudError, match="reported failure"):
        _cloud_claude.parse_cli_submit('{"ok": false, "error": "no repo"}')


def test_named_branch():
    assert _cloud_claude.named_branch("use claude/fix-1.") == "claude/fix-1"
    assert _cloud_claude.named_branch("claude/a or claude/b") is None
    assert _cloud_claude.named_branch("no branch here") is None


# --------------------------------------------------------------------------- #
# setup skill
# --------------------------------------------------------------------------- #


def test_setup_skill_is_bundled_and_installable(tmp_path, capsys):
    from lazytools.skills.agent_skills import agent_skill_path, list_agent_skills, skill_metadata

    assert "code-bridge-cloud-setup" in list_agent_skills()
    meta = skill_metadata("code-bridge-cloud-setup")
    assert meta["name"] == "code-bridge-cloud-setup" and "code-bridge.toml" in meta["description"]
    text = agent_skill_path("code-bridge-cloud-setup").read_text(encoding="utf-8")
    for needle in ("claude --version", "claude auth status", "codex --version", "gh auth status",
                   "git -C <repo> remote get-url origin", "[cloud.codex.envs]", "token_env"):
        assert needle in text
    assert cli.main(["setup-skill"]) == 0
    assert capsys.readouterr().out.strip().endswith("SKILL.md")
    assert cli.main(["setup-skill", "--install", str(tmp_path)]) == 0
    installed = tmp_path / "code-bridge-cloud-setup" / "SKILL.md"
    assert installed.read_text(encoding="utf-8") == text
    assert cli.main(["setup-skill", "--install", str(tmp_path)]) == 1
    assert cli.main(["setup-skill", "--install", str(tmp_path), "--force"]) == 0
    with pytest.raises(KeyError):
        agent_skill_path("../etc")


def test_named_branch_already_claimed_is_not_reused(cc, capsys):
    brief = "Leave the result on branch claude/shared-x please."
    first = _submit_json(cc, capsys, task=brief)["job_id"]
    second = _submit_json(cc, capsys, task=brief)["job_id"]
    _vm_push(cc, "claude/shared-x")
    cli.main(["status", first, "--db", str(cc.db)])
    cli.main(["status", second, "--db", str(cc.db)])
    capsys.readouterr()
    assert _job(cc, first)["result_branch"] == "claude/shared-x"
    assert _job(cc, second)["status"] == "running" and not _job(cc, second).get("result_branch")


def test_claims_are_scoped_to_the_repository(cc, capsys):
    store = _store.build_store(cc.db)
    registry = _store.build_job_registry(store)
    other = "ab" * 16
    meta = {"job_id": other, "engine": "claude", "where": "cloud", "repo": "acme/elsewhere",
            "result_branch": "claude/fix-login", "objective": "x"}
    _store.write_meta(store, other, meta)
    registry.write(other, "x", tool_name="claude", status="done", engine="claude",
                   extra={"where": "cloud", "cloud_handle": "h"})
    assert _cloud._claimed_result_branches(store, "cd" * 16, "acme/widgets") == set()
    assert _cloud._claimed_result_branches(store, "cd" * 16, "ACME/elsewhere") == {"claude/fix-login"}
