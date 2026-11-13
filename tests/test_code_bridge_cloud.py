"""Cloud dispatch (``run --where cloud``): offline, against a fake ``codex`` executable.

The fake CLI is a small Python script reached through ``CODEX_BIN``. It logs
every call (argv + cwd) to a JSONL file, prints a task URL for ``cloud exec``,
the contents of a status file for ``cloud status``, a canned unified diff for
``cloud diff``, and for ``cloud apply`` writes the change into its working
directory -- which is how the tests prove ``apply`` ran in a separate worktree.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from lazytools.code_bridge import _cloud, _concurrency, _jobs, _store, cli
from lazytools.code_bridge._concurrency import EngineSlot

TASK_URL = "https://chatgpt.com/codex/tasks/task_e_abc123"

FAKE_CODEX = textwrap.dedent(
    """
    import json, os, re, sys
    from pathlib import Path

    args = sys.argv[1:]
    stdin = sys.stdin.read() if args[:2] == ["cloud", "exec"] else ""
    with open(os.environ["FAKE_CODEX_LOG"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": args, "cwd": os.getcwd(), "stdin": stdin}) + "\\n")
    if args[:1] != ["cloud"]:
        sys.exit(64)
    verb = args[1]
    if verb == "apply" and os.environ.get("FAKE_CODEX_PARTIAL"):
        Path("half_applied.txt").write_text("partial\\n", encoding="utf-8")
        print("apply failed midway", file=sys.stderr)
        sys.exit(1)
    if os.environ.get("FAKE_CODEX_FAIL") == verb:
        print("boom: " + verb, file=sys.stderr)
        sys.exit(1)
    if verb == "exec":
        counter = Path(os.environ["FAKE_CODEX_LOG"] + ".n")
        n = int(counter.read_text()) + 1 if counter.exists() else 1
        counter.write_text(str(n))
        url = os.environ["FAKE_CODEX_URL"] + ("" if n == 1 else str(n))
        print("Submitted task. View it at " + url)
    elif verb == "list":
        print(Path(os.environ["FAKE_CODEX_LIST"]).read_text(encoding="utf-8"), end="")
    elif verb == "status":
        # A per-task status file (<status file>.<last url segment>) wins over the shared one.
        per_task = Path(os.environ["FAKE_CODEX_STATUS"] + "." + args[2].rsplit("/", 1)[-1])
        text = (per_task if per_task.exists() else Path(os.environ["FAKE_CODEX_STATUS"])).read_text(encoding="utf-8")
        print(text, end="")
        # Like the real CLI: a task that ended in ERROR exits 1 (and still prints its status).
        if re.search(r"(?im)^\\s*(?:\\[error\\]|.*\\bstatus\\s*[:=]\\s*\\[?error\\b)", text):
            sys.exit(1)
    elif verb == "diff" and os.environ.get("FAKE_CODEX_DIFF"):
        print(Path(os.environ["FAKE_CODEX_DIFF"]).read_text(encoding="utf-8"), end="")
    elif verb == "diff" and os.environ.get("FAKE_CODEX_SHAPE") == "space":
        print("diff --git a/with space.txt b/with space.txt")
    elif verb == "diff" and os.environ.get("FAKE_CODEX_SHAPE") == "rename":
        print("diff --git a/README.md b/RENAMED.md")
    elif verb == "diff":
        print("diff --git a/cloud_change.txt b/cloud_change.txt")
        print("new file mode 100644")
        print("--- /dev/null")
        print("+++ b/cloud_change.txt")
        print("@@ -0,0 +1 @@")
        print("+applied by cloud")
    elif verb == "apply":
        shape = os.environ.get("FAKE_CODEX_SHAPE")
        if shape == "space":
            Path("with space.txt").write_text("spaced\\n", encoding="utf-8")
        elif shape == "rename":
            Path("README.md").rename("RENAMED.md")
        elif not os.environ.get("FAKE_CODEX_NOCHANGE"):
            Path("cloud_change.txt").write_text("applied by cloud\\n", encoding="utf-8")
        if os.environ.get("FAKE_CODEX_ERROR_LOG"):
            Path("error.log").write_text("account_id=acct-secret\\n", encoding="utf-8")
        if os.environ.get("FAKE_CODEX_UNEXPECTED"):
            Path("unexpected.tmp").write_text("tool scratch\\n", encoding="utf-8")
        print("Applied task " + args[2] + " to the working tree")
    else:
        sys.exit(65)
    """
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def cloud(tmp_path, monkeypatch):
    """A git repo with an origin remote, a fake codex CLI, and an env mapping for the repo."""
    tools = tmp_path / "tools"
    tools.mkdir()
    script = tools / "fake_codex.py"
    script.write_text(FAKE_CODEX, encoding="utf-8")
    if sys.platform == "win32":
        exe = tools / "codex.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        exe = tools / "codex"
        exe.write_text(f"#!{sys.executable}\n" + FAKE_CODEX, encoding="utf-8")
        exe.chmod(0o755)
    log = tmp_path / "codex-calls.jsonl"
    status_file = tmp_path / "status.txt"
    status_file.write_text("Task status: pending\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_BIN", str(exe))
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    monkeypatch.setenv("FAKE_CODEX_URL", TASK_URL)
    monkeypatch.setenv("FAKE_CODEX_STATUS", str(status_file))
    monkeypatch.delenv("FAKE_CODEX_FAIL", raising=False)
    monkeypatch.delenv("FAKE_CODEX_PARTIAL", raising=False)
    monkeypatch.delenv("FAKE_CODEX_DIFF", raising=False)
    monkeypatch.delenv("FAKE_CODEX_NOCHANGE", raising=False)
    monkeypatch.delenv("FAKE_CODEX_ERROR_LOG", raising=False)
    monkeypatch.delenv("FAKE_CODEX_UNEXPECTED", raising=False)
    list_file = tmp_path / "list.json"
    list_file.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("FAKE_CODEX_LIST", str(list_file))
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
    # A real, fetchable origin (a bare repo at .../acme/widgets.git, so the
    # slug still reads "acme/widgets"): submit records its tip as base_commit,
    # which apply requires.
    origin = tmp_path / "remotes" / "acme" / "widgets.git"
    origin.parent.mkdir(parents=True)
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    _git(repo, "remote", "add", "origin", origin.as_posix())
    _git(repo, "push", "-q", "origin", "main")

    with open(os.environ[_concurrency.CONFIG_ENV], "a", encoding="utf-8") as fh:
        fh.write('\n[cloud.codex.envs]\n"acme/widgets" = "env_widgets"\n')

    class Ctx:
        pass

    ctx = Ctx()
    ctx.repo, ctx.log, ctx.status_file = repo, log, status_file
    ctx.db = tmp_path / "work" / "store.sqlite"
    ctx.root = tmp_path / "work"
    ctx.calls = lambda: [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    ctx.set_status = lambda text: status_file.write_text(text, encoding="utf-8")
    ctx.list_file = list_file
    return ctx


def _run_cloud(ctx, *extra: str) -> int:
    return cli.main(
        ["run", "--where", "cloud", "--engine", "codex", "--cwd", str(ctx.repo), "--root", str(ctx.root),
         "--task", "Add a feature", "--no-brief-check", "--db", str(ctx.db), "--allow-cloud", *extra]
    )


def _set_cloud_config(text: str) -> None:
    path = Path(os.environ[_concurrency.CONFIG_ENV])
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# config / mapping
# --------------------------------------------------------------------------- #


def test_missing_mapping_fails_with_create_environment_hint(cloud, capsys):
    _git(cloud.repo, "remote", "set-url", "origin", "https://github.com/acme/other.git")
    code = _run_cloud(cloud)
    err = capsys.readouterr().err
    assert code == 2
    assert "no Codex Cloud environment mapped for acme/other" in err
    assert "create a single-repo Codex Cloud environment for acme/other" in err
    assert "[cloud.codex.envs]" in err
    assert cloud.calls() == []  # nothing submitted
    store = _store.build_store(cloud.db)
    assert _store.all_job_records(store) == []  # nothing recorded


def test_config_rejects_bad_cloud_keys(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("[cloud]\nconcurrency = -1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="concurrency must be an integer"):
        _cloud.load_config(path)
    path.write_text("[cloud]\nbogus = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown key 'bogus'"):
        _cloud.load_config(path)
    path.write_text('[cloud.codex.envs]\n"not-a-slug" = "env"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="is not 'owner/name'"):
        _cloud.load_config(path)
    path.write_text('[cloud]\nconcurrency = 2\n[cloud.codex.envs]\n"Acme/Widgets" = "env_x"\n', encoding="utf-8")
    config = _cloud.load_config(path)
    assert config.concurrency == 2
    assert config.envs == {"codex": {"acme/widgets": "env_x"}}
    # The [cloud] table must not break the existing [concurrency] loader.
    assert _concurrency.load_config(path).limits["codex"] == 1


def test_default_cloud_concurrency_is_four(tmp_path):
    assert _cloud.load_config(tmp_path / "missing.toml").concurrency == 4


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:acme/widgets.git",
        "https://github.com/acme/widgets.git",
        "https://github.com/acme/widgets",
        "ssh://git@github.com/acme/widgets.git",
        "http://127.0.0.1:8080/git/acme/widgets",
    ],
)
def test_parse_remote(url):
    assert _cloud.parse_remote(url) == "acme/widgets"


# --------------------------------------------------------------------------- #
# submit
# --------------------------------------------------------------------------- #


def test_submit_refuses_a_branch_missing_on_origin(cloud, capsys):
    code = _run_cloud(cloud, "--branch", "task/n11", "--json")
    assert code == 2
    err = capsys.readouterr().err
    assert "task/n11" in err and "does not exist on the 'origin' remote" in err and "base branch" in err
    assert cloud.calls() == []  # nothing reached the cloud CLI


def test_submit_stores_handle_and_where_cloud(cloud, capsys):
    _git(cloud.repo, "push", "-q", "origin", "main:refs/heads/feature-x")
    code = _run_cloud(cloud, "--branch", "feature-x", "--json")
    assert code == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["where"] == "cloud"
    assert payload["cloud_handle"] == TASK_URL
    assert payload["status"] == "running"
    assert payload["repo"] == "acme/widgets" and payload["env_id"] == "env_widgets"

    (call,) = cloud.calls()
    argv = call["argv"]
    # The brief travels on stdin, never as a command-line argument.
    assert argv == ["cloud", "exec", "--env", "env_widgets", "--branch", "feature-x"]
    assert call["stdin"].startswith(_cloud.CLOUD_PREAMBLE) and call["stdin"].endswith("Add a feature")

    store = _store.build_store(cloud.db)
    job = _jobs.find_job(store, _store.build_job_registry(store), payload["job_id"])
    assert job["where"] == "cloud" and job["cloud_handle"] == TASK_URL
    assert job["status"] == "running" and job["engine"] == "codex"


def test_submit_passes_a_long_brief_on_stdin(cloud, capsys):
    # Windows caps a command line at ~32K characters; a 40K brief must still submit.
    brief = cloud.root / "brief.md"
    brief.write_text("# Goal\n\n" + "x" * 40_000 + "\nEND-OF-BRIEF\n", encoding="utf-8")
    assert _run_cloud(cloud, "--task", "@" + str(brief)) == 0
    (call,) = cloud.calls()
    assert len(call["argv"]) == 6
    assert "END-OF-BRIEF" in call["stdin"] and len(call["stdin"]) > 40_000


def test_submit_defaults_branch_to_current(cloud, capsys):
    assert _run_cloud(cloud) == 0
    out = capsys.readouterr().out
    assert "cloud: codex task " + TASK_URL in out
    assert cloud.calls()[0]["argv"][5] == "main"


def test_submit_failure_is_recorded_failed(cloud, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CODEX_FAIL", "exec")
    assert _run_cloud(cloud) == 2
    assert "codex cloud exec exited 1" in capsys.readouterr().err
    store = _store.build_store(cloud.db)
    (record,) = _store.all_job_records(store)
    assert record["status"] == "failed" and record["where"] == "cloud"
    assert _cloud.active_jobs(store) == []  # a failed submit frees its place


def test_cloud_rejects_local_only_flags(cloud, capsys):
    assert _run_cloud(cloud, "--session", "s1") == 2
    assert "--where cloud does not take --session" in capsys.readouterr().err
    assert cli.main(["run", "--where", "cloud", "--allow-cloud", "--tier", "basic", "--cwd", str(cloud.repo),
                     "--task", "x"]) == 2
    assert "requires an explicit --engine" in capsys.readouterr().err
    assert cloud.calls() == []


def test_claude_cloud_requires_a_routine_before_starting_provider_cli(cloud, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_BIN", str(cloud.root / "no-such-claude"))
    code = cli.main(
        ["run", "--where", "cloud", "--engine", "claude", "--cwd", str(cloud.repo), "--root", str(cloud.root),
         "--task", "x", "--no-brief-check", "--db", str(cloud.db), "--allow-cloud"]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "require a routine API trigger" in err and "default_routine" in err
    assert cloud.calls() == []  # never routed to codex


def test_branch_flag_is_cloud_only(cloud, capsys):
    code = cli.main(["run", "--engine", "codex", "--cwd", str(cloud.repo), "--task", "x", "--branch", "b"])
    assert code == 2
    assert "--branch is only for --where cloud" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# status parsing / status / wait / result
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "status", "raw"),
    [
        ("Task status: pending\n", "running", "pending"),
        ("status: [READY]\nAdded the feature and tests.\n", "done", "READY"),
        ("Title: x\nStatus: Error\nenvironment setup failed\n", "failed", "Error"),
        ('{"status": "completed", "message": "all good"}', "done", "completed"),
        ("[in progress] working on it\n", "running", "in progress"),
        ("cancelled by user\n", "interrupted", "cancelled"),
        # The title after the leading token is free text, never a status.
        ("[READY] Fix error handling\n", "done", "READY"),
        ("[READY] Add cancellation support\n", "done", "READY"),
        ("[PENDING] Fix failing tests\n", "running", "PENDING"),
        ("something we do not understand\n", "running", ""),
        ("Title: x\nfixed the error path\n", "running", ""),
    ],
)
def test_parse_status(text, status, raw):
    parsed = _cloud.parse_status(text)
    assert parsed.status == status
    assert parsed.raw_status == raw


def test_parse_status_keeps_message():
    parsed = _cloud.parse_status("status: ready\nAdded the feature and tests.\n")
    assert parsed.message == "Added the feature and tests."


def test_summarize_diff():
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n-x\n+y\n+z\n"
        "diff --git a/old.py b/new.py\n+q\n"
    )
    summary = _cloud.summarize_diff(diff)
    assert summary.files == ["a.py", "old.py", "new.py"] and summary.added == 3 and summary.removed == 1
    assert "3 file(s) changed, +3 -1" in summary.text()


def _submit(cloud, capsys) -> str:
    assert _run_cloud(cloud, "--json") == 0
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])["job_id"]


def test_status_polls_codex_cloud_status(cloud, capsys):
    job_id = _submit(cloud, capsys)
    assert cli.main(["status", job_id, "--db", str(cloud.db)]) == 0
    out = capsys.readouterr().out
    assert "where=cloud" in out and "running" in out and "cloud status: pending" in out
    assert cloud.calls()[-1]["argv"] == ["cloud", "status", TASK_URL]

    cloud.set_status("status: ready\nDone: feature added.\n")
    assert cli.main(["status", job_id, "--db", str(cloud.db), "--json"]) == 0
    job = json.loads(capsys.readouterr().out)
    assert job["status"] == "done" and job["cloud_status"] == "ready"


def test_status_poll_error_keeps_job_running(cloud, monkeypatch, capsys):
    job_id = _submit(cloud, capsys)
    monkeypatch.setenv("FAKE_CODEX_FAIL", "status")
    assert cli.main(["status", job_id, "--db", str(cloud.db)]) == 0
    captured = capsys.readouterr()
    assert "running" in captured.out
    assert "last poll failed" in captured.err


def test_wait_and_result_show_message_and_diff(cloud, capsys):
    job_id = _submit(cloud, capsys)
    assert cli.main(["wait", job_id, "--db", str(cloud.db), "--timeout", "0.2", "--interval", "0.05"]) == 3
    assert "still running" in capsys.readouterr().err

    cloud.set_status("status: ready\nAdded the feature and tests.\n")
    assert cli.main(["wait", job_id, "--db", str(cloud.db), "--interval", "0.05"]) == 0
    out = capsys.readouterr().out
    assert "[done] cloud job" in out
    assert "Added the feature and tests." in out
    assert "diff: 1 file(s) changed, +1 -0" in out and "cloud_change.txt" in out
    assert ["cloud", "diff", TASK_URL] in [c["argv"] for c in cloud.calls()]


def test_result_exit_code_for_failed_cloud_task(cloud, capsys):
    job_id = _submit(cloud, capsys)
    cloud.set_status("status: error\nsetup script failed\n")
    assert cli.main(["result", job_id, "--db", str(cloud.db)]) == 1
    out = capsys.readouterr().out
    assert "[failed]" in out and "setup script failed" in out


def test_cancel_cloud_job_frees_place_and_says_task_still_runs(cloud, capsys):
    job_id = _submit(cloud, capsys)
    assert cli.main(["cancel", job_id, "--db", str(cloud.db)]) == 0
    out = capsys.readouterr().out
    assert "[interrupted]" in out and "NOT stopped" in out and TASK_URL in out
    assert _cloud.active_jobs(_store.build_store(cloud.db)) == []


# --------------------------------------------------------------------------- #
# concurrency
# --------------------------------------------------------------------------- #


def test_cloud_jobs_ignore_local_engine_slots(cloud, capsys):
    # Local codex limit is 1 and a (live) local job holds it...
    slot = EngineSlot("codex", 1, _concurrency.load_config().slot_dir)
    slot.try_acquire("local-job")
    try:
        assert _concurrency.engine_at_limit("codex")  # ...so a local codex job would be refused,
        assert _run_cloud(cloud) == 0  # but a cloud job is not.
        capsys.readouterr()
        # And a cloud job takes no local slot: nothing new in the slot directory.
        slots = sorted(p.name for p in _concurrency.load_config().slot_dir.glob("*.slot"))
        assert slots == ["codex-local-job.slot"]
    finally:
        slot.release()


def test_cloud_limit_is_separate_and_configurable(cloud, capsys):
    _set_cloud_config("\n[cloud]\nconcurrency = 1\n")
    assert _run_cloud(cloud) == 0
    capsys.readouterr()
    assert _run_cloud(cloud) == 2
    err = capsys.readouterr().err
    assert "cloud jobs are at their concurrency limit (1)" in err
    # The refused submit never reached codex cloud exec (status polls only).
    assert [c["argv"][1] for c in cloud.calls()].count("exec") == 1
    # Local codex slots are untouched by cloud jobs.
    assert _concurrency.engine_at_limit("codex") == []


def test_finished_cloud_task_frees_its_place_on_next_submit(cloud, capsys):
    _set_cloud_config("\n[cloud]\nconcurrency = 1\n")
    assert _run_cloud(cloud) == 0
    capsys.readouterr()
    cloud.set_status("status: ready\nok\n")
    assert _run_cloud(cloud) == 0  # the refresh before refusing saw the first task finish
    store = _store.build_store(cloud.db)
    assert len(_cloud.active_jobs(store)) == 1


def test_default_cloud_limit_admits_four(cloud, capsys):
    for _ in range(4):
        assert _run_cloud(cloud) == 0
    assert _run_cloud(cloud) == 2
    assert "concurrency limit (4)" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #


def test_apply_runs_in_a_separate_worktree_and_does_not_push(cloud, capsys):
    job_id = _submit(cloud, capsys)
    assert cli.main(["apply", job_id, "--db", str(cloud.db)]) == 1
    assert "not done" in capsys.readouterr().err

    cloud.set_status("status: ready\nok\n")
    assert cli.main(["apply", job_id, "--db", str(cloud.db), "--json"]) == 0
    outcome = json.loads(capsys.readouterr().out)
    worktree = Path(outcome["worktree"])
    assert outcome["pushed"] is False
    assert outcome["branch"] == f"cloud/{job_id[:8]}"
    assert worktree != cloud.repo and cloud.repo not in worktree.parents

    # codex cloud apply ran inside the new worktree...
    apply_call = [c for c in cloud.calls() if c["argv"][1] == "apply"][-1]
    assert apply_call["argv"] == ["cloud", "apply", TASK_URL]
    assert Path(apply_call["cwd"]).resolve() == worktree.resolve()
    assert (worktree / "cloud_change.txt").is_file()
    # ...and the user's checkout is untouched, still on its own branch.
    assert not (cloud.repo / "cloud_change.txt").exists()
    assert _git(cloud.repo, "status", "--porcelain") == ""
    assert _git(cloud.repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert _git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == outcome["branch"]
    assert any("cloud_change.txt" in line for line in outcome["changed"])

    # A second apply is refused rather than clobbering the first worktree.
    assert cli.main(["apply", job_id, "--db", str(cloud.db)]) == 1
    assert "already applied" in capsys.readouterr().err


def test_apply_refuses_local_jobs(cloud, capsys):
    store = _store.build_store(cloud.db)
    registry = _store.build_job_registry(store)
    registry.write("localjob1", "x", tool_name="codex", status="done")
    _store.write_meta(store, "localjob1", {"job_id": "localjob1", "engine": "codex", "cwd": str(cloud.repo)})
    assert cli.main(["apply", "localjob1", "--db", str(cloud.db)]) == 2
    assert "local job" in capsys.readouterr().err


def test_stale_poll_does_not_overwrite_cancellation(cloud, monkeypatch, capsys):
    job_id = _submit(cloud, capsys)
    real_status = _cloud.CodexCloud.status

    def status_then_cancelled_meanwhile(self, handle):
        answer = real_status(self, handle)  # the provider says "pending"...
        assert cli.main(["cancel", job_id, "--db", str(cloud.db)]) == 0  # ...while another process cancels
        return answer

    monkeypatch.setattr(_cloud.CodexCloud, "status", status_then_cancelled_meanwhile)
    assert cli.main(["status", job_id, "--db", str(cloud.db), "--json"]) == 0
    job = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert job["status"] == "interrupted"
    assert job["cloud_status"] == "pending"  # the poll's metadata is still kept
    assert _cloud.active_jobs(_store.build_store(cloud.db)) == []  # no place resurrected


def test_cancel_during_submit_keeps_interrupted_and_records_handle(cloud, monkeypatch, capsys):
    real_submit = _cloud.CodexCloud.submit

    def submit_then_cancelled_meanwhile(self, **kwargs):
        sub = real_submit(self, **kwargs)
        store = _store.build_store(cloud.db)
        (record,) = _store.all_job_records(store)
        _cloud.mark_interrupted(store, _store.build_job_registry(store), record["job_id"], "cancelled")
        return sub

    monkeypatch.setattr(_cloud.CodexCloud, "submit", submit_then_cancelled_meanwhile)
    assert _run_cloud(cloud, "--json") == 0
    job = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert job["status"] == "interrupted" and job["cloud_handle"] == TASK_URL


def test_failed_apply_cleans_untouched_worktree_and_is_retryable(cloud, monkeypatch, capsys):
    job_id = _submit(cloud, capsys)
    cloud.set_status("status: ready\nok\n")
    monkeypatch.setenv("FAKE_CODEX_FAIL", "apply")
    assert cli.main(["apply", job_id, "--db", str(cloud.db)]) == 1
    assert "codex cloud apply exited 1" in capsys.readouterr().err
    branch = f"cloud/{job_id[:8]}"
    assert _git(cloud.repo, "branch", "--list", branch) == ""
    assert not (_cloud.worktrees_dir(cloud.db) / job_id).exists()

    monkeypatch.delenv("FAKE_CODEX_FAIL")
    assert cli.main(["apply", job_id, "--db", str(cloud.db), "--json"]) == 0
    outcome = json.loads(capsys.readouterr().out)
    assert (Path(outcome["worktree"]) / "cloud_change.txt").is_file()


def test_failed_apply_keeps_partial_changes(cloud, monkeypatch, capsys):
    job_id = _submit(cloud, capsys)
    cloud.set_status("status: ready\nok\n")
    monkeypatch.setenv("FAKE_CODEX_PARTIAL", "1")
    assert cli.main(["apply", job_id, "--db", str(cloud.db)]) == 1
    err = capsys.readouterr().err
    worktree = _cloud.worktrees_dir(cloud.db) / job_id
    assert "partial changes kept in" in err
    assert (worktree / "half_applied.txt").is_file()  # never deleted blindly
    store = _store.build_store(cloud.db)
    job = _jobs.find_job(store, _store.build_job_registry(store), job_id)
    assert job["apply_failed_worktree"] == str(worktree.resolve()) and "applied_worktree" not in job


def test_status_nonzero_exit_with_error_status_marks_failed(cloud, capsys):
    job_id = _submit(cloud, capsys)
    cloud.set_status("status: error\nsetup script failed\n")  # the fake exits 1 for this, like the CLI
    assert cli.main(["wait", job_id, "--db", str(cloud.db), "--interval", "0.05"]) == 1
    out = capsys.readouterr().out
    assert "[failed]" in out and "setup script failed" in out


def test_wait_keeps_job_running_after_persistent_poll_failures(cloud, monkeypatch, capsys):
    job_id = _submit(cloud, capsys)
    monkeypatch.setenv("FAKE_CODEX_FAIL", "status")  # exit 1 with no status in the output
    code = cli.main(["wait", job_id, "--db", str(cloud.db), "--timeout", "0.05", "--interval", "0.005"])
    assert code == 4
    assert "still unknown" in capsys.readouterr().err
    store = _store.build_store(cloud.db)
    job = _jobs.find_job(store, _store.build_job_registry(store), job_id)
    assert job["status"] == "running" and _cloud.active_jobs(store)


def test_wait_caps_backoff_exponent_after_many_poll_failures(cloud, monkeypatch, capsys):
    job_id = _submit(cloud, capsys)
    store = _store.build_store(cloud.db)
    meta = _store.read_meta(store, job_id) or {}
    meta["poll_errors_in_a_row"] = 1025
    _store.write_meta(store, job_id, meta)
    cloud.set_status("status: ready\nRecovered result.\n")
    monkeypatch.setattr(_cloud.random, "uniform", lambda _a, _b: 0.0)
    monkeypatch.setattr(_cloud.time, "sleep", lambda _delay: None)

    assert cli.main(["wait", job_id, "--db", str(cloud.db), "--interval", "0.01"]) == 0
    assert "Recovered result." in capsys.readouterr().out


def test_poll_clock_spaces_first_poll_from_separate_waiters(cloud, monkeypatch):
    stores = [_store.build_store(cloud.db), _store.build_store(cloud.db)]
    now = [100.0]
    sleeps = []
    monkeypatch.setattr(_cloud.time, "time", lambda: now[0])

    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    monkeypatch.setattr(_cloud.time, "sleep", sleep)
    for store in stores:
        assert _cloud._wait_for_poll_turn(store, "codex", 5.0, 0.0, None)

    assert sleeps == [5.0]


def test_short_waiters_do_not_advance_poll_clock(cloud, monkeypatch):
    store = _store.build_store(cloud.db)
    key = f"{_cloud.POLL_CLOCK_PREFIX}codex"
    now = [100.0]
    store.write(key, now[0])
    monkeypatch.setattr(_cloud.time, "time", lambda: now[0])
    monkeypatch.setattr(_cloud.time, "monotonic", lambda: now[0])

    def sleep(delay):
        now[0] += delay

    monkeypatch.setattr(_cloud.time, "sleep", sleep)
    for _ in range(3):
        deadline = _cloud.time.monotonic() + 1.0
        assert not _cloud._wait_for_poll_turn(store, "codex", 5.0, 0.0, deadline)

    assert now[0] == 103.0
    assert store.read(key) == 100.0


def test_wait_deadline_does_not_poll_before_reserved_turn(cloud, monkeypatch, capsys):
    job_id = _submit(cloud, capsys)
    store = _store.build_store(cloud.db)
    store.write(f"{_cloud.POLL_CLOCK_PREFIX}codex", _cloud.time.time() + 120.0)
    sleeps = []
    monkeypatch.setattr(_cloud.random, "uniform", lambda _a, _b: 0.0)
    monkeypatch.setattr(_cloud.time, "sleep", sleeps.append)

    def unexpected_refresh(*_args, **_kwargs):
        pytest.fail("refresh issued a provider poll before the reserved turn")

    monkeypatch.setattr(_cloud, "refresh", unexpected_refresh)
    result = _cloud.wait_job(lambda: _store.build_store(cloud.db), job_id, timeout=1.0, interval=5.0)

    assert result is not None and result["status"] == "running"
    assert len(sleeps) == 1 and 0.0 < sleeps[0] <= 1.0


def test_resumed_waiter_subtracts_elapsed_time_from_retry_after(cloud, monkeypatch, capsys):
    # A Retry-After stored 200 s ago has already expired: a short resumed wait
    # must poll again instead of rescheduling the full 120 s from now.
    job_id = _submit(cloud, capsys)
    store = _store.build_store(cloud.db)
    meta = _store.read_meta(store, job_id) or {}
    meta.update(poll_errors_in_a_row=1, last_poll_error="HTTP 429 Too Many Requests; Retry-After: 120",
                last_polled_at=_cloud.time.time() - 200.0)
    _store.write_meta(store, job_id, meta)
    monkeypatch.setattr(_cloud.random, "uniform", lambda _a, _b: 0.0)
    monkeypatch.setattr(_cloud.time, "sleep", lambda _s: None)
    polled = []
    real_refresh = _cloud.refresh

    def counting_refresh(*args, **kwargs):
        polled.append(True)
        return real_refresh(*args, **kwargs)

    monkeypatch.setattr(_cloud, "refresh", counting_refresh)
    _cloud.wait_job(lambda: _store.build_store(cloud.db), job_id, timeout=1.0, interval=0.01)
    assert polled, "the resumed waiter never polled although Retry-After had expired"


def test_unparseable_status_output_never_fails_job(cloud, capsys):
    job_id = _submit(cloud, capsys)
    cloud.set_status("something we do not understand\n")  # exit 0, but names no status
    for _ in range(_cloud.MAX_CONSECUTIVE_POLL_ERRORS - 1):
        assert cli.main(["status", job_id, "--db", str(cloud.db), "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "running"
    assert cli.main(["result", job_id, "--db", str(cloud.db)]) == 0
    out = capsys.readouterr().out
    assert "[running]" in out and "no recognisable status" not in out


def test_429_then_completed_retries_and_honours_retry_after(cloud, monkeypatch, capsys):
    job_id = _submit(cloud, capsys)
    answers = iter([_cloud.CloudError("HTTP 429 Too Many Requests; Retry-After: 2"),
                    _cloud.CloudStatus("done", "completed", "finished", "status: completed")])

    def status(_self, _handle):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    sleeps = []
    monkeypatch.setattr(_cloud.CodexCloud, "status", status)
    monkeypatch.setattr(_cloud.random, "uniform", lambda _a, _b: 0.0)
    monkeypatch.setattr(_cloud.time, "sleep", sleeps.append)
    assert cli.main(["wait", job_id, "--db", str(cloud.db), "--timeout", "5", "--interval", "0.01"]) == 0
    assert any(delay >= 1.9 for delay in sleeps)
    job = _jobs.find_job(_store.build_store(cloud.db), _store.build_job_registry(_store.build_store(cloud.db)), job_id)
    assert job["status"] == "done"


def test_resync_recovers_legacy_poll_failed_job(cloud, capsys):
    job_id = _submit(cloud, capsys)
    store = _store.build_store(cloud.db)
    registry = _store.build_job_registry(store)
    meta = _store.read_meta(store, job_id) or {}
    meta["last_poll_error"] = "HTTP 429 Too Many Requests"
    _cloud._write(store, registry, job_id, meta, status="failed", error="cloud status: Error")
    cloud.set_status("status: ready\nRecovered result.\n")

    assert cli.main(["resync", job_id, "--db", str(cloud.db), "--json"]) == 0
    recovered = json.loads(capsys.readouterr().out)
    assert recovered["status"] == "done"
    assert recovered["job_log"][-1]["event"] == "resync"


def test_bracketed_error_status_exits_nonzero_and_fails_job(cloud, capsys):
    job_id = _submit(cloud, capsys)
    cloud.set_status("[ERROR] Fix flaky test\nsetup script failed\n")  # the fake exits 1, like upstream
    assert cli.main(["wait", job_id, "--db", str(cloud.db), "--interval", "0.01"]) == 1
    assert "[failed]" in capsys.readouterr().out


def test_cloud_wait_has_a_default_timeout(cloud, monkeypatch, capsys):
    job_id = _submit(cloud, capsys)
    monkeypatch.setattr(_cloud, "DEFAULT_CLOUD_WAIT_TIMEOUT", 0.1)
    assert cli.main(["wait", job_id, "--db", str(cloud.db), "--interval", "0.02"]) == 3
    assert "still running after 0s" in capsys.readouterr().err


@pytest.mark.parametrize(
    "bad", ["12345678/../../../../escape", "../x", "a b", "x" * 65, "fixedid1", "ABCDEF12", "1234567", "f" * 33]
)
def test_job_id_must_be_hex_or_uuid(cloud, bad, capsys):
    assert _run_cloud(cloud, "--job-id", bad) == 2
    assert "job id must be" in capsys.readouterr().err
    assert cloud.calls() == []
    assert not any(_cloud.worktrees_dir(cloud.db).parent.rglob("escape"))


@pytest.mark.parametrize("good", ["deadbeef", "0123456789abcdef0123456789abcdef", "12345678-9abc-4def-8123-456789abcdef"])
def test_job_id_accepts_hex_and_uuid(cloud, good, capsys):
    assert _run_cloud(cloud, "--job-id", good, "--json") == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["job_id"] == good


def test_apply_refuses_a_traversal_job_id_from_a_tampered_record(cloud, capsys):
    bad = "12345678/../../../../escape"
    store = _store.build_store(cloud.db)
    job = {"job_id": bad, "where": "cloud", "status": "done", "engine": "codex", "cloud_handle": TASK_URL,
           "cwd": str(cloud.repo), "repo_root": str(cloud.repo), "branch": "main"}
    with pytest.raises(_cloud.CloudError, match="not hex or a UUID"):
        _cloud.apply_job(store, _store.build_job_registry(store), job, db_path=cloud.db)
    assert not any(cloud.root.parent.rglob("escape"))
    assert [c for c in cloud.calls() if c["argv"][1] == "apply"] == []


def test_worktree_containment_uses_resolved_paths(tmp_path):
    container = tmp_path / "cloud-worktrees"
    container.mkdir()
    assert _cloud._inside(container / "deadbeef", container)
    assert not _cloud._inside(container / ".." / "escape", container)
    (container / "link").symlink_to(tmp_path)
    assert not _cloud._inside(container / "link" / "escape", container)


def test_existing_job_id_is_not_overwritten(cloud, capsys):
    assert _run_cloud(cloud, "--job-id", "deadbeef") == 0
    capsys.readouterr()
    assert _run_cloud(cloud, "--job-id", "deadbeef") == 2
    assert "already exists" in capsys.readouterr().err
    assert [c["argv"][1] for c in cloud.calls()].count("exec") == 1


def test_bad_branch_name_is_refused(cloud, capsys):
    assert _run_cloud(cloud, "--branch", "bad..name") == 2
    assert "not a valid branch name" in capsys.readouterr().err
    assert cloud.calls() == []


def test_apply_refuses_moved_branch_unless_rebase(cloud, tmp_path, capsys):
    # A bare "origin" so the branch has a real remote tip to record and later move.
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    _git(cloud.repo, "remote", "add", "real", str(origin))
    _git(cloud.repo, "push", "-q", "real", "main")
    _git(cloud.repo, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    _git(cloud.repo, "config", "remote.origin.url", str(origin))  # fetchable; slug comes from the map below
    _set_cloud_config(f'"{_cloud.parse_remote(str(origin))}" = "env_widgets"\n')
    base = _git(cloud.repo, "rev-parse", "HEAD")

    job_id = _submit(cloud, capsys)
    store = _store.build_store(cloud.db)
    assert _jobs.find_job(store, _store.build_job_registry(store), job_id)["base_commit"] == base

    # The branch moves on the remote after the task started.
    (cloud.repo / "later.txt").write_text("later\n", encoding="utf-8")
    _git(cloud.repo, "add", "later.txt")
    _git(cloud.repo, "commit", "-q", "-m", "later")
    _git(cloud.repo, "push", "-q", "real", "main")
    _git(cloud.repo, "fetch", "-q", "origin")

    cloud.set_status("status: ready\nok\n")
    head = _git(cloud.repo, "rev-parse", "HEAD")
    assert cli.main(["apply", job_id, "--db", str(cloud.db)]) == 1
    err = capsys.readouterr().err
    assert f"has moved from {base[:12]}" in err and head[:12] in err and "--rebase" in err
    assert [c for c in cloud.calls() if c["argv"][1] == "apply"] == []  # nothing applied
    assert not (_cloud.worktrees_dir(cloud.db) / job_id).exists()

    assert cli.main(["apply", job_id, "--db", str(cloud.db), "--rebase", "--json"]) == 0
    outcome = json.loads(capsys.readouterr().out)
    assert outcome["base"] == head and outcome["rebased_from"] == base
    assert _git(Path(outcome["worktree"]), "rev-parse", "HEAD") == head


def test_apply_uses_recorded_base_when_branch_has_not_moved(cloud, tmp_path, capsys):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    _git(cloud.repo, "push", "-q", str(origin), "main")
    _git(cloud.repo, "config", "remote.origin.url", str(origin))
    _git(cloud.repo, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    _set_cloud_config(f'"{_cloud.parse_remote(str(origin))}" = "env_widgets"\n')
    base = _git(cloud.repo, "rev-parse", "HEAD")
    job_id = _submit(cloud, capsys)
    cloud.set_status("status: ready\nok\n")
    assert cli.main(["apply", job_id, "--db", str(cloud.db), "--json"]) == 0
    outcome = json.loads(capsys.readouterr().out)
    assert outcome["base"] == base and outcome["rebased_from"] is None


def test_provider_diagnostics_are_redacted(cloud, monkeypatch, capsys):
    job_id = _submit(cloud, capsys)
    cloud.set_status("oops\n")
    monkeypatch.setenv("FAKE_CODEX_FAIL", "status")
    real_run = _cloud.CodexCloud._run

    def leaky(self, *args, **kwargs):
        if args and args[0] == "status":
            return 1, "", "401: Authorization: Bearer abcdefghijklmnop0123 rejected (token=sk-live-0123456789abcdef)"
        return real_run(self, *args, **kwargs)

    monkeypatch.setattr(_cloud.CodexCloud, "_run", leaky)
    assert cli.main(["status", job_id, "--db", str(cloud.db)]) == 0
    err = capsys.readouterr().err
    assert "abcdefghijklmnop0123" not in err and "sk-live-0123456789abcdef" not in err
    assert "[redacted]" in err
    store = _store.build_store(cloud.db)
    stored = json.dumps(_jobs.find_job(store, _store.build_job_registry(store), job_id))
    assert "abcdefghijklmnop0123" not in stored and "sk-live" not in stored


def test_windows_batch_shim_is_never_executed(tmp_path, monkeypatch):
    shim_dir = tmp_path / "npm"
    shim_dir.mkdir()
    shim = shim_dir / "codex.cmd"
    shim.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_BIN", str(shim))
    with pytest.raises(_cloud.CloudError, match="batch shim"):
        _cloud._codex_argv()
    script = shim_dir / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    (shim_dir / "node.exe").write_text("", encoding="utf-8")
    assert _cloud._codex_argv() == [str(shim_dir / "node.exe"), str(script)]


def test_brief_secrets_are_redacted_in_the_stored_record(cloud, capsys):
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2"
    bearer = "Bearer " + "zyxwvutsrqponmlk9876"
    brief = f"Call the API with {bearer} and clone with {token}"
    code = cli.main(
        ["run", "--where", "cloud", "--engine", "codex", "--cwd", str(cloud.repo), "--root", str(cloud.root),
         "--task", brief, "--no-brief-check", "--db", str(cloud.db), "--allow-cloud", "--json"]
    )
    assert code == 0
    captured = capsys.readouterr()
    assert "credential-looking text" in captured.err
    job_id = json.loads(captured.out.strip().splitlines()[-1])["job_id"]
    store = _store.build_store(cloud.db)
    stored = json.dumps(_jobs.find_job(store, _store.build_job_registry(store), job_id))
    assert token not in stored and "zyxwvutsrqponmlk9876" not in stored and "[redacted]" in stored
    assert cli.main(["jobs", "--db", str(cloud.db)]) == 0
    listed = capsys.readouterr().out
    assert token not in listed and "zyxwvutsrqponmlk9876" not in listed


@pytest.mark.parametrize(
    "secret",
    ["Bearer abcdefghij0123456789", "sk-ant-api03-abcdefghijklmnop", "ghp_abcdefghijklmnopqrstuvwx",
     "github_pat_11ABCDEFG0123456789_abcdef", "AKIAABCDEFGHIJKLMNOP", "api_key=supersecretvalue"],
)
def test_redact_masks_common_credentials(secret):
    assert _cloud.redact(f"before {secret} after").startswith("before ")
    assert secret not in _cloud.redact(f"before {secret} after")


# --------------------------------------------------------------------------- #
# approval gate for cloud dispatch
# --------------------------------------------------------------------------- #


def test_cloud_requires_allow_cloud(cloud, capsys):
    code = cli.main(
        ["run", "--where", "cloud", "--engine", "codex", "--cwd", str(cloud.repo), "--root", str(cloud.root),
         "--task", "Add a feature", "--no-brief-check", "--db", str(cloud.db)]
    )
    assert code == 2
    assert "requires --allow-cloud" in capsys.readouterr().err
    assert cloud.calls() == []
    assert _store.all_job_records(_store.build_store(cloud.db)) == []


def test_allow_and_approve_cloud_are_cloud_only(cloud, capsys):
    assert cli.main(["run", "--engine", "codex", "--cwd", str(cloud.repo), "--task", "x", "--allow-cloud"]) == 2
    assert "--allow-cloud is only for --where cloud" in capsys.readouterr().err
    assert cli.main(["run", "--engine", "codex", "--cwd", str(cloud.repo), "--task", "x",
                     "--approve-cloud", "push"]) == 2
    assert "--approve-cloud is only for --where cloud" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("brief", "actions"),
    [
        ("Fix the bug, then git push origin main", ["push"]),
        ("Fix the bug and push the branch when done", ["push"]),
        ("Open a PR and merge it into main", ["pr-create", "merge"]),
        ("Run gh pr merge 12 --squash", ["merge"]),
        ("Clean up: delete the stale release branches", ["delete"]),
        ("Remove old data with rm -rf build/", ["delete"]),
        ("Rebase, force-push, then delete the old branch", ["push", "delete"]),
    ],
)
def test_brief_asking_for_gated_action_is_refused(cloud, brief, actions, capsys):
    code = cli.main(
        ["run", "--where", "cloud", "--engine", "codex", "--cwd", str(cloud.repo), "--root", str(cloud.root),
         "--task", brief, "--no-brief-check", "--db", str(cloud.db), "--allow-cloud"]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert f"gated action(s) {', '.join(actions)}" in err
    for action in actions:
        assert f"--approve-cloud {action}" in err
    assert cloud.calls() == []
    assert _store.all_job_records(_store.build_store(cloud.db)) == []


def test_gated_action_runs_with_matching_approval(cloud, capsys):
    base = ["run", "--where", "cloud", "--engine", "codex", "--cwd", str(cloud.repo), "--root", str(cloud.root),
            "--task", "Fix the bug, push the branch, then delete the old branch", "--no-brief-check",
            "--db", str(cloud.db), "--allow-cloud"]
    assert cli.main([*base, "--approve-cloud", "push"]) == 2  # delete still unapproved
    assert "gated action(s) delete" in capsys.readouterr().err
    assert cli.main([*base, "--approve-cloud", "push", "--approve-cloud", "delete", "--json"]) == 0
    job_id = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["job_id"]
    store = _store.build_store(cloud.db)
    assert _jobs.find_job(store, _store.build_job_registry(store), job_id)["cloud_approvals"] == ["delete", "push"]


@pytest.mark.parametrize("brief", ["Add a feature", "Fix error handling in the parser", "Write tests for the merge sort"])
def test_plain_briefs_need_no_approval(brief):
    assert _cloud.gated_actions(brief) == []


# --------------------------------------------------------------------------- #
# the cloud gate reuses the local rule table (_policy.CODING_RULES)
# --------------------------------------------------------------------------- #


def test_every_gating_rule_of_the_local_table_has_a_cloud_action():
    from lazytools.code_bridge._approvals import ClassifiedRule
    from lazytools.code_bridge._policy import CLOUD_RULE_ACTIONS, CODING_RULES

    gating = [
        rule.arg_pattern for rule in CODING_RULES
        if rule.name_pattern == "Bash" and not isinstance(rule, ClassifiedRule)
        and rule.tier in ("ask", "session") and rule.arg_pattern is not None
    ]
    assert gating and set(gating) == set(CLOUD_RULE_ACTIONS)
    assert set(CLOUD_RULE_ACTIONS.values()) <= set(_cloud.GATED_ACTIONS)


@pytest.mark.parametrize(
    ("brief", "action"),
    [
        ("Fix the flaky test, then run `gh pr create --fill --base main`", "pr-create"),
        ("When CI is green run gh pr create --title fix --body done", "pr-create"),
        ("Finish with: gh pr merge 42 --squash --delete-branch", "merge"),
        ("Fix the bug, then git push origin main", "push"),
        ("git push origin feat/x", "push"),
        ("Tidy up with git reset --hard origin/main", "reset"),
        ("git commit -am 'wip'", "commit"),
        ("git -C ../other status", "git-option"),
    ],
)
def test_brief_command_gated_by_local_rule_table_needs_matching_approval(cloud, brief, action, capsys):
    base = ["run", "--where", "cloud", "--engine", "codex", "--cwd", str(cloud.repo), "--root", str(cloud.root),
            "--task", brief, "--no-brief-check", "--db", str(cloud.db), "--allow-cloud"]
    assert action in _cloud.gated_actions(brief)
    assert cli.main(base) == 2
    err = capsys.readouterr().err
    assert f"--approve-cloud {action}" in err
    assert cloud.calls() == []
    assert _store.all_job_records(_store.build_store(cloud.db)) == []
    approvals = [flag for name in _cloud.gated_actions(brief) for flag in ("--approve-cloud", name)]
    assert cli.main([*base, *approvals, "--json"]) == 0
    capsys.readouterr()
    assert [c["argv"][1] for c in cloud.calls()] == ["exec"]


def test_gh_pr_create_is_not_covered_by_other_approvals(cloud, capsys):
    assert cli.main(
        ["run", "--where", "cloud", "--engine", "codex", "--cwd", str(cloud.repo), "--root", str(cloud.root),
         "--task", "gh pr create --fill", "--no-brief-check", "--db", str(cloud.db), "--allow-cloud",
         "--approve-cloud", "push", "--approve-cloud", "merge"]
    ) == 2
    assert "gated action(s) pr-create" in capsys.readouterr().err
    assert cloud.calls() == []


def test_rule_table_word_anchoring_avoids_prose_false_positives():
    from lazytools.code_bridge._policy import cloud_rule_actions

    assert cloud_rule_actions("Confirm the parser handles it and inform the user") == set()
    assert cloud_rule_actions("clean with rm -rf build/") == {"delete"}
    assert cloud_rule_actions("GIT PUSH origin MAIN") == {"push"}


# --------------------------------------------------------------------------- #
# job id uniqueness is atomic with admission
# --------------------------------------------------------------------------- #


def test_concurrent_submits_with_the_same_job_id_admit_exactly_one(cloud, monkeypatch):
    import threading

    workers = 4
    barrier = threading.Barrier(workers, timeout=30)
    real_remote_base = _cloud._remote_base

    def remote_base_after_everyone_passed_the_fast_check(root, branch):
        barrier.wait()  # every thread is past the early existence check before any admits
        return real_remote_base(root, branch)

    monkeypatch.setattr(_cloud, "_remote_base", remote_base_after_everyone_passed_the_fast_check)
    outcomes: list[object] = []
    lock = threading.Lock()

    def submit() -> None:
        try:
            job = _cloud.submit_job(engine="codex", cwd=cloud.repo, task="Add a feature", db_path=cloud.db,
                                    job_id="cafebabe")
        except _cloud.CloudError as exc:
            result: object = exc
        else:
            result = job
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=submit) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert len(outcomes) == workers
    wins = [o for o in outcomes if isinstance(o, dict)]
    losses = [o for o in outcomes if isinstance(o, _cloud.CloudError)]
    assert len(wins) == 1 and wins[0]["job_id"] == "cafebabe" and wins[0]["status"] == "running"
    assert len(losses) == workers - 1 and all("already exists" in str(e) for e in losses)
    assert [c["argv"][1] for c in cloud.calls()].count("exec") == 1


# --------------------------------------------------------------------------- #
# apply refuses a job with no recorded base commit
# --------------------------------------------------------------------------- #


def test_apply_refuses_when_the_initial_fetch_failed(cloud, tmp_path, capsys):
    origin = tmp_path / "remotes" / "acme" / "widgets.git"
    hidden = tmp_path / "remotes" / "hidden.git"
    origin.rename(hidden)  # the fetch at submit fails
    _git(cloud.repo, "update-ref", "-d", "refs/remotes/origin/main")
    job_id = _submit(cloud, capsys)
    store = _store.build_store(cloud.db)
    assert _jobs.find_job(store, _store.build_job_registry(store), job_id)["base_commit"] is None
    hidden.rename(origin)  # reachable again by apply time: still no unchecked fallback

    cloud.set_status("status: ready\nok\n")
    assert cli.main(["apply", job_id, "--db", str(cloud.db)]) == 1
    assert "no recorded base commit" in capsys.readouterr().err
    assert cli.main(["apply", job_id, "--db", str(cloud.db), "--rebase"]) == 1
    assert "no recorded base commit" in capsys.readouterr().err
    assert [c for c in cloud.calls() if c["argv"][1] == "apply"] == []
    assert not (_cloud.worktrees_dir(cloud.db) / job_id).exists()
    assert "cloud/" not in _git(cloud.repo, "branch", "--list")


# --------------------------------------------------------------------------- #
# every CloudError and every cloud CLI print is redacted
# --------------------------------------------------------------------------- #

_BEARER = "Bearer abcdefghij0123456789KLMN"


def test_exit_zero_submit_without_handle_is_redacted(cloud, monkeypatch, capsys):
    def leaky(self, *args, **kwargs):
        assert args[0] == "exec"
        return 0, f"Authorization: {_BEARER}\nqueued, but no task link printed\n", ""

    monkeypatch.setattr(_cloud.CodexCloud, "_run", leaky)
    assert _run_cloud(cloud, "--json") == 2
    captured = capsys.readouterr()
    assert "abcdefghij0123456789KLMN" not in captured.err + captured.out
    assert "[redacted]" in captured.err and "did not print a task URL" in captured.err
    records = _store.all_job_records(_store.build_store(cloud.db))
    assert len(records) == 1 and records[0]["status"] == "failed"
    assert "abcdefghij0123456789KLMN" not in json.dumps(records)


def test_cloud_error_messages_are_always_redacted():
    exc = _cloud.CloudError(f"provider said {_BEARER} token=sk-live-0123456789abcdef")
    assert "abcdefghij0123456789KLMN" not in str(exc) and "sk-live-0123456789abcdef" not in str(exc)
    assert str(exc).count("[redacted]") == 2


def test_status_result_and_apply_output_is_redacted(cloud, capsys):
    job_id = _submit(cloud, capsys)
    cloud.set_status("status: ready\nall done\n")
    store = _store.build_store(cloud.db)
    registry = _store.build_job_registry(store)
    _cloud.refresh(store, registry, _jobs.find_job(store, registry, job_id))
    meta = _store.read_meta(store, job_id)
    meta["cloud_status"] = f"ready ({_BEARER})"  # e.g. a record written by an older version
    _store.write_meta(store, job_id, meta)
    record = registry.find(job_id)
    registry.write(job_id, record.get("objective", ""), tool_name="codex", status="done",
                   result=f"done; used {_BEARER}", engine="codex",
                   extra={"where": "cloud", "cloud_handle": record.get("cloud_handle")})
    for argv in (["status", job_id], ["status", job_id, "--json"], ["result", job_id], ["result", job_id, "--json"],
                 ["apply", job_id, "--branch", "token=sk-live-0123456789abcdef..bad"]):
        cli.main([*argv, "--db", str(cloud.db)])
        captured = capsys.readouterr()
        assert "abcdefghij0123456789KLMN" not in captured.out + captured.err, argv
        assert "sk-live-0123456789abcdef" not in captured.out + captured.err, argv
    assert cli.main(["status", job_id, "--json", "--db", str(cloud.db)]) == 0
    json.loads(capsys.readouterr().out)  # still valid JSON after redaction
