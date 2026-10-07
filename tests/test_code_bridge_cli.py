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
        "command": "git add -- hello.txt",
        "reason": "Allow Git to stage hello.txt?",
    }
    assert _request_detail("[TieredGate] agent asks to run Bash\n  arguments: not json") == {}
    assert _request_detail("no arguments line at all") == {}
