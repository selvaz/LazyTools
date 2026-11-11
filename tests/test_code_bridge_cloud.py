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
    import json, os, sys
    from pathlib import Path

    args = sys.argv[1:]
    with open(os.environ["FAKE_CODEX_LOG"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": args, "cwd": os.getcwd()}) + "\\n")
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
    elif verb == "status":
        print(Path(os.environ["FAKE_CODEX_STATUS"]).read_text(encoding="utf-8"), end="")
    elif verb == "diff":
        print("diff --git a/src/app.py b/src/app.py")
        print("--- a/src/app.py")
        print("+++ b/src/app.py")
        print("@@ -1 +1,2 @@")
        print("-old")
        print("+new")
        print("+more")
    elif verb == "apply":
        Path("cloud_change.txt").write_text("applied by cloud\\n", encoding="utf-8")
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
    _git(repo, "remote", "add", "origin", "git@github.com:acme/widgets.git")

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
    return ctx


def _run_cloud(ctx, *extra: str) -> int:
    return cli.main(
        ["run", "--where", "cloud", "--engine", "codex", "--cwd", str(ctx.repo), "--root", str(ctx.root),
         "--task", "Add a feature", "--no-brief-check", "--db", str(ctx.db), *extra]
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


def test_submit_stores_handle_and_where_cloud(cloud, capsys):
    code = _run_cloud(cloud, "--branch", "feature-x", "--json")
    assert code == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["where"] == "cloud"
    assert payload["cloud_handle"] == TASK_URL
    assert payload["status"] == "running"
    assert payload["repo"] == "acme/widgets" and payload["env_id"] == "env_widgets"

    (call,) = cloud.calls()
    argv = call["argv"]
    assert argv[:6] == ["cloud", "exec", "--env", "env_widgets", "--branch", "feature-x"]
    assert argv[6].startswith(_cloud.CLOUD_PREAMBLE) and argv[6].endswith("Add a feature")

    store = _store.build_store(cloud.db)
    job = _jobs.find_job(store, _store.build_job_registry(store), payload["job_id"])
    assert job["where"] == "cloud" and job["cloud_handle"] == TASK_URL
    assert job["status"] == "running" and job["engine"] == "codex"


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
    assert cli.main(["run", "--where", "cloud", "--tier", "basic", "--cwd", str(cloud.repo), "--task", "x"]) == 2
    assert "requires an explicit --engine" in capsys.readouterr().err
    assert cloud.calls() == []


def test_claude_cloud_is_an_extension_point(cloud, capsys):
    code = cli.main(
        ["run", "--where", "cloud", "--engine", "claude", "--cwd", str(cloud.repo), "--root", str(cloud.root),
         "--task", "x", "--no-brief-check", "--db", str(cloud.db)]
    )
    assert code == 2
    assert "not implemented yet" in capsys.readouterr().err


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
        ("[in progress] working on it\n", "running", "[in progress] working on it"),
        ("cancelled by user\n", "interrupted", "cancelled by user"),
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
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n-x\n+y\n+z\ndiff --git a/b.py b/b.py\n+q\n"
    summary = _cloud.summarize_diff(diff)
    assert summary.files == ["a.py", "b.py"] and summary.added == 3 and summary.removed == 1
    assert "2 file(s) changed, +3 -1" in summary.text()


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
    assert "diff: 1 file(s) changed, +2 -1" in out and "src/app.py" in out
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
