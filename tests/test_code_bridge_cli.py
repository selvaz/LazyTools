"""CLI-surface tests for ``lazytools-code-bridge`` — argparse wiring, exit codes."""

from __future__ import annotations

import json

import _code_bridge_fakes as fakes
from lazytools.code_bridge import _store
from lazytools.code_bridge.cli import main


def _env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LAZYBRIDGE_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_POLL_SECONDS", "0.05")


def test_run_prints_job_id_first_line_and_exits_zero(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    script = fakes.install(monkeypatch)
    script.text = "cli ok"
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = tmp_path / "store.sqlite"

    code = main(
        [
            "run",
            "--engine",
            "codex",
            "--cwd",
            str(repo),
            "--task",
            "do it",
            "--root",
            str(tmp_path),
            "--db",
            str(db_path),
            "--json",
        ]
    )
    out = capsys.readouterr().out.strip().splitlines()
    assert code == 0
    job_id_line = out[0]
    payload = json.loads(out[-1])
    assert payload["job_id"] == job_id_line
    assert payload["status"] == "done"


def test_run_task_from_file(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    script = fakes.install(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    task_file = tmp_path / "task.txt"
    task_file.write_text("the task text from a file", encoding="utf-8")
    db_path = tmp_path / "store.sqlite"

    code = main(
        ["run", "--engine", "claude", "--cwd", str(repo), "--task", f"@{task_file}", "--root", str(tmp_path), "--db", str(db_path)]
    )
    assert code == 0
    assert script.prompts == ["the task text from a file"]


def test_jobs_status_and_result_roundtrip(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    script = fakes.install(monkeypatch)
    script.text = "the result text"
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = tmp_path / "store.sqlite"

    main(["run", "--engine", "codex", "--cwd", str(repo), "--task", "x", "--root", str(tmp_path), "--db", str(db_path)])
    job_id = capsys.readouterr().out.splitlines()[0]

    code = main(["jobs", "--all", "--json", "--db", str(db_path)])
    rows = json.loads(capsys.readouterr().out)
    assert code == 0
    assert any(r["job_id"] == job_id for r in rows)

    code = main(["status", job_id, "--json", "--db", str(db_path)])
    status = json.loads(capsys.readouterr().out)
    assert code == 0
    assert status["status"] == "done"

    code = main(["result", job_id, "--db", str(db_path)])
    result_out = capsys.readouterr().out
    assert code == 0
    assert "the result text" in result_out


def test_status_unknown_job_id_exits_nonzero(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    db_path = tmp_path / "store.sqlite"
    _store.build_store(db_path)  # create an empty store

    code = main(["status", "no-such-job", "--db", str(db_path)])
    assert code == 1


def test_pending_approve_reject_cycle(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    db_path = tmp_path / "store.sqlite"
    store = _store.build_store(db_path)
    queue = _store.build_approval_queue(store)
    ticket = queue.create_ticket(task_id="job-xyz", prompt="Objective: do the risky thing")

    code = main(["pending", "--json", "--db", str(db_path)])
    rows = json.loads(capsys.readouterr().out)
    assert code == 0
    assert rows[0]["approval_id"] == ticket.approval_id
    assert rows[0]["job_id"] == "job-xyz"

    code = main(["approve", ticket.approval_id, "--db", str(db_path)])
    capsys.readouterr()
    assert code == 0
    assert queue.get_ticket(ticket.approval_id).status == "approved"

    # A second approve on the same (now resolved) ticket is refused.
    code = main(["approve", ticket.approval_id, "--db", str(db_path)])
    capsys.readouterr()
    assert code == 1


def test_reject_requires_a_reason_and_records_it(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    db_path = tmp_path / "store.sqlite"
    store = _store.build_store(db_path)
    queue = _store.build_approval_queue(store)
    ticket = queue.create_ticket(task_id="job-xyz", prompt="Objective: do the risky thing")

    code = main(["reject", ticket.approval_id, "--reason", "too risky", "--db", str(db_path)])
    capsys.readouterr()
    assert code == 0
    resolved = queue.get_ticket(ticket.approval_id)
    assert resolved.status == "rejected"
    assert resolved.reason == "too risky"


def test_request_detail_surfaces_the_real_codex_command_and_reason():
    """A Codex escalation arrives as the opaque tool name "codex-shell"; the
    pending listing must show the actual command and Codex's reason, or the
    person approving is deciding blind."""
    from lazytools.code_bridge.cli import _request_detail

    payload = {
        "kind": "command",
        "reason": "Allow Git to stage hello.txt?",
        "command": "powershell.exe -Command 'git add -- hello.txt'",
        "commandActions": [{"type": "unknown", "command": "git add -- hello.txt"}],
    }
    prompt = (
        "[TieredGate] agent asks to run command 'codex-shell'\n"
        f"  arguments: {json.dumps(payload)}\n"
        "  cwd: C:\repo"
    )
    assert _request_detail(prompt) == {
        "command": "powershell.exe -Command 'git add -- hello.txt'",
        "summary": "git add -- hello.txt",
        "reason": "Allow Git to stage hello.txt?",
    }
    assert _request_detail("[TieredGate] agent asks to run Bash\n  arguments: not json") == {}
    assert _request_detail("no arguments line at all") == {}


# --------------------------------------------------------------------------- #
# run --detach / wait
# --------------------------------------------------------------------------- #


def _run_args(tmp_path, repo, db_path, *extra):
    return ["run", "--engine", "codex", "--cwd", str(repo), "--task", "do it", "--root", str(tmp_path), "--db", str(db_path), *extra]


def test_run_with_job_id_records_the_job_under_that_id(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    fakes.install(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = tmp_path / "store.sqlite"

    assert main(_run_args(tmp_path, repo, db_path, "--job-id", "fixedid123")) == 0
    assert capsys.readouterr().out.splitlines()[0] == "fixedid123"
    assert main(["status", "fixedid123", "--db", str(db_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "done"


def test_detach_spawns_a_child_with_the_same_job_and_reports_it(tmp_path, monkeypatch, capsys):
    import lazytools.code_bridge.cli as cli

    seen: dict = {}

    def fake_spawn(argv, log_path):
        seen["argv"], seen["log"] = argv, log_path
        return 4242

    monkeypatch.setattr(cli, "_spawn_detached", fake_spawn)
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = tmp_path / "store.sqlite"

    code = main(_run_args(tmp_path, repo, db_path, "--detach", "--session", "s1", "--json"))
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    argv = seen["argv"]
    assert argv[1:4] == ["-m", "lazytools.code_bridge", "run"]
    assert argv[argv.index("--job-id") + 1] == payload["job_id"]
    assert argv[argv.index("--session") + 1] == "s1"
    assert "--detach" not in argv  # the child runs in the foreground of its own process
    assert payload["pid"] == 4242
    pid_file = _store.results_dir(db_path) / f"{payload['job_id']}.pid"
    assert pid_file.read_text(encoding="utf-8") == "4242"


def test_detach_refuses_a_missing_task_file_before_spawning(tmp_path, monkeypatch):
    import lazytools.code_bridge.cli as cli

    monkeypatch.setattr(cli, "_spawn_detached", lambda *a: (_ for _ in ()).throw(AssertionError("spawned")))
    repo = tmp_path / "repo"
    repo.mkdir()
    args = _run_args(tmp_path, repo, tmp_path / "s.sqlite", "--detach")
    args[args.index("do it")] = "@" + str(tmp_path / "missing.md")
    assert main(args) == 2  # reported by the launcher, never left to an unwatched child


def test_spawn_detached_really_starts_an_independent_process(tmp_path):
    import sys
    import time

    from lazytools.code_bridge.cli import _spawn_detached

    marker = tmp_path / "ran.txt"
    pid = _spawn_detached([sys.executable, "-c", f"open({str(marker)!r}, 'w').write('ok')"], tmp_path / "child.log")
    assert pid > 0
    for _ in range(200):
        if marker.exists():
            break
        time.sleep(0.05)
    assert marker.read_text() == "ok"


def test_wait_returns_the_outcome_once_the_job_is_terminal(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    fakes.install(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = tmp_path / "store.sqlite"
    assert main(_run_args(tmp_path, repo, db_path, "--job-id", "waitme")) == 0
    capsys.readouterr()

    assert main(["wait", "waitme", "--db", str(db_path), "--interval", "0.01"]) == 0
    assert "[done] job waitme" in capsys.readouterr().out


def test_wait_reports_a_job_whose_process_died(tmp_path, monkeypatch, capsys):
    import lazytools.code_bridge._lockfile as lockfile
    from lazytools.code_bridge import _store as store_mod

    db_path = tmp_path / "store.sqlite"
    store = store_mod.build_store(db_path)
    store_mod.build_job_registry(store).write("ghost", "x", tool_name="codex", status="running")
    (store_mod.results_dir(db_path) / "ghost.pid").write_text("999999", encoding="utf-8")
    monkeypatch.setattr(lockfile, "_pid_alive", lambda pid: False)

    assert main(["wait", "ghost", "--db", str(db_path), "--interval", "0.01", "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "died" and out["last_status"] == "running"


def test_wait_times_out_with_exit_3(tmp_path, monkeypatch, capsys):
    import lazytools.code_bridge._lockfile as lockfile
    from lazytools.code_bridge import _store as store_mod

    db_path = tmp_path / "store.sqlite"
    store = store_mod.build_store(db_path)
    store_mod.build_job_registry(store).write("slow", "x", tool_name="codex", status="running")
    (store_mod.results_dir(db_path) / "slow.pid").write_text("1", encoding="utf-8")
    monkeypatch.setattr(lockfile, "_pid_alive", lambda pid: True)

    assert main(["wait", "slow", "--db", str(db_path), "--interval", "0.01", "--timeout", "0.05"]) == 3


def test_request_detail_survives_an_elided_arguments_payload():
    """TieredGate cuts long arguments in the middle, leaving invalid JSON; the
    command and reason that came before the cut must still be shown."""
    from lazytools.code_bridge.cli import _request_detail

    head = json.dumps({"kind": "command", "reason": "Run the tests outside the sandbox?", "command": "pytest -q tests"})[:-1]
    prompt = (
        "[TieredGate] agent asks to run command 'codex-shell'\n"
        f"  arguments: {head}, \"proposedExecpolicyAmendment\": [\"powershell.exe\", \"-Comm\n"
        "  [...82 characters elided...]\n"
        "  cwd: C:\\repo"
    )
    assert _request_detail(prompt) == {"command": "pytest -q tests", "reason": "Run the tests outside the sandbox?"}


def test_request_detail_when_the_cut_goes_through_the_command_itself():
    """The motivating case: the COMMAND is what is too long, so the real
    elide() splices its marker inside it. The reason (earlier in the payload)
    must come through whole, and the command up to the cut, marked as cut."""
    from lazybridge._display import elide

    from lazytools.code_bridge.cli import CUT_MARK, _request_detail

    long_command = "powershell -Command '$env:PYTHONPATH=" + "C:\\very\\long\\path;" * 400 + "; pytest -q tests'"
    payload = json.dumps({"kind": "command", "reason": "Run the suite outside the sandbox?", "command": long_command})
    prompt = "[TieredGate] agent asks to run command 'codex-shell'\n  arguments: " + elide(payload, 3000) + "\n  cwd: C:\repo"

    detail = _request_detail(prompt)
    assert detail["reason"] == "Run the suite outside the sandbox?"
    assert detail["command"].startswith("powershell -Command '$env:PYTHONPATH=C:")
    assert detail["command"].endswith(CUT_MARK)


def test_run_refuses_a_job_id_that_already_exists(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    fakes.install(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = tmp_path / "store.sqlite"
    assert main(_run_args(tmp_path, repo, db_path, "--job-id", "dupe")) == 0
    capsys.readouterr()

    assert main(_run_args(tmp_path, repo, db_path, "--job-id", "dupe")) == 2
    assert "already exists" in capsys.readouterr().err


def test_detach_refuses_a_cwd_outside_the_root_before_spawning(tmp_path, monkeypatch, capsys):
    import lazytools.code_bridge.cli as cli

    monkeypatch.setattr(cli, "_spawn_detached", lambda *a: (_ for _ in ()).throw(AssertionError("spawned")))
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    args = ["run", "--detach", "--engine", "codex", "--cwd", str(outside), "--task", "x", "--root", str(root), "--db", str(tmp_path / "s.sqlite")]
    assert main(args) == 2
    assert "outside" in capsys.readouterr().err


def test_detach_hands_the_child_absolute_root_cwd_and_task_paths(tmp_path, monkeypatch, capsys):
    import lazytools.code_bridge.cli as cli

    seen: dict = {}
    monkeypatch.setattr(cli, "_spawn_detached", lambda argv, log: seen.setdefault("argv", argv) and 1)
    repo = tmp_path / "repo"
    repo.mkdir()
    brief = tmp_path / "brief.md"
    brief.write_text("do it", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["run", "--detach", "--engine", "codex", "--cwd", "repo", "--task", "@brief.md", "--root", ".", "--db", str(tmp_path / "s.sqlite")]) == 0
    argv = seen["argv"]
    from pathlib import Path

    assert Path(argv[argv.index("--root") + 1]).is_absolute()
    assert Path(argv[argv.index("--cwd") + 1]) == repo.resolve()
    assert argv[argv.index("--task") + 1] == "@" + str(brief.resolve())


def test_result_prints_characters_outside_the_console_codepage(tmp_path, monkeypatch, capsysbinary):
    """A legacy Windows codepage cannot encode "→"; printing a result with one
    used to raise UnicodeEncodeError instead of showing it."""
    _env(monkeypatch, tmp_path)
    script = fakes.install(monkeypatch)
    script.text = "fatto → consegnato"
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = tmp_path / "store.sqlite"
    assert main(_run_args(tmp_path, repo, db_path, "--job-id", "arrow")) == 0
    capsysbinary.readouterr()
    assert main(["result", "arrow", "--db", str(db_path)]) == 0
    assert "→".encode() in capsysbinary.readouterr().out
