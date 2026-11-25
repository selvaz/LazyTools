"""Exercise the real queue, card lifecycle and writer approval callbacks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from lazybridge import Envelope
from lazybridge.engines.coding import ApprovalRequest
from lazybridge.ext.approval import DEFAULT_PREFIX, ApprovalTicket

import _code_bridge_fakes as fakes
from lazytools.code_bridge import _credentials, _jobs, _registry, _store, _tempdir, cli
from test_code_bridge_continuations import git


def request(cwd, command, *, provider="claude-code", **raw):
    native = provider == "codex"
    return ApprovalRequest(
        provider=provider, kind="command" if native else "tool", name="codex-shell" if native else "Bash",
        arguments={"command": command}, cwd=str(cwd), raw={"command": command, **raw},
    )


@pytest.fixture
def writer(tmp_path, monkeypatch, provision_bridge_approver):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Bridge Trial")
    git(repo, "config", "user.email", "bridge@example.invalid")
    (repo / "old.txt").write_text("baseline", encoding="utf-8")
    git(repo, "add", "--", "old.txt")
    git(repo, "commit", "-m", "Initial fixture")
    db = tmp_path / "jobs.sqlite"
    registry_db = tmp_path / "isolated.sqlite"
    flags = ["--db", str(db), "--registry-db", str(registry_db)]
    provision_bridge_approver(registry_db)
    assert cli.main(["approver-credential", "provision-coder", *flags]) == 0
    assert cli.main(["approver-credential", "provision-coder", *flags]) == 0
    fakes.install(monkeypatch)
    monkeypatch.setattr(_tempdir, "os_temp_bases", lambda: [tmp_path / "scratch-base"])
    monkeypatch.setattr(_jobs, "ticket_ttl", lambda: timedelta(milliseconds=10))
    monkeypatch.setattr(_jobs, "poll_seconds", lambda: 0.005)
    return repo, db, registry_db, flags


def run(writer, monkeypatch, turn, *, engine="claude", network=None):
    repo, db, registry_db, _ = writer
    monkeypatch.setattr(_jobs, "_run_agent", turn)
    return _jobs.run_job(
        engine_name=engine, cwd=str(repo), root=str(repo.parent), task="Exercise the coder hand",
        db_path=db, registry_db=registry_db, timeout=30, network=network,
    )


def test_writer_commits_and_cleans_up_with_audited_cards(writer, monkeypatch, capsys):
    repo, _, registry_db, flags = writer
    # Use an actual linked worktree. Git add and commit need its shared metadata.
    worktree = repo.parent / "task-worktree"
    git(repo, "worktree", "add", "-b", "trial/cards", str(worktree))
    writer = (worktree, *writer[1:])
    async def turn(engine, prompt, name):
        gate = engine.kwargs["config"].approval_gate
        temp = Path(gate.temp_dir)
        (temp / "probe.txt").write_text("temp", encoding="utf-8")
        (worktree / "test_probe.py").write_text("def test_probe():\n    assert True\n", encoding="utf-8")
        (worktree / "scratch").mkdir()
        (worktree / "scratch" / "probe.txt").write_text("scratch", encoding="utf-8")
        python = Path(sys.executable).as_posix()
        venv = (temp / "venv").as_posix()
        cases = [
            ("git rev-parse --git-common-dir", ["git", "rev-parse", "--git-common-dir"]),
            ("git status --short", ["git", "status", "--short"]),
            ('git add -- test_probe.py', ["git", "add", "--", "test_probe.py"]),
            ('git commit -m "Add trial test"', ["git", "commit", "-m", "Add trial test"]),
            (f'"{python}" -m pytest -q -p no:cacheprovider test_probe.py',
             [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_probe.py"]),
            (f'"{python}" -m venv --copies --without-pip "{venv}"',
             [python, "-m", "venv", "--copies", "--without-pip", venv]),
        ]
        for command, argv in cases:
            assert (await gate(request(worktree, command, provider="codex"))).action == "allow", command
            subprocess.run(argv, cwd=worktree, check=True, capture_output=True)
        assert (temp / "venv" / "pyvenv.cfg").is_file()
        for target in (worktree / "scratch", temp / "probe.txt"):
            shell = (
                f'Remove-Item -LiteralPath "{target.as_posix()}" -Recurse -Force'
                if os.name == "nt" else f'rm -rf -- "{target.as_posix()}"'
            )
            assert (await gate(request(worktree, shell, provider="codex"))).action == "allow", shell
            argv = (
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", shell]
                if os.name == "nt" else ["rm", "-rf", "--", str(target)]
            )
            subprocess.run(argv, cwd=worktree, check=True, capture_output=True)
            assert not target.exists()
        return Envelope(payload="done")

    result = run(writer, monkeypatch, turn, engine="codex")
    assert result.status == "done", result.error
    assert git(worktree, "log", "-1", "--format=%s").strip() == "Add trial test"
    assert not (worktree / "scratch").exists()
    with _registry.registry_store(registry_db) as store:
        assert store.items(prefix=DEFAULT_PREFIX) == []
    capsys.readouterr()
    assert cli.main(["status", result.job_id, "--json", *flags]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["registry_ticket_ids"] == []
    assert status["coder_cards_revoked"]
    assert not Path(status["temp_dir"]).exists()
    assert len(status["card_approvals"]) == 8
    assert all(len(receipt["chain"]) == 2 and receipt["task_id"] == result.job_id for receipt in status["card_approvals"])
    assert status["card_approvals"][3]["command"] == 'git commit -m "Add trial test"'
    assert cli.main(["result", result.job_id, *flags]) == 0
    assert 'approved git commit -m "Add trial test"' in capsys.readouterr().out


def test_job_completion_revokes_unused_copies(writer, monkeypatch):
    repo, _, _, _ = writer
    seen = {}

    async def turn(engine, prompt, name):
        seen["channel"] = engine.kwargs["config"].approval_gate.channel
        assert seen["channel"].hand is not None
        return Envelope(payload="done")

    result = run(writer, monkeypatch, turn)
    assert result.status == "done", result.error
    # Bypass the closed channel to exercise the queue's durable revocation.
    decision = seen["channel"]._submit(request(repo, "git status"), "replay")
    assert isinstance(decision[0], ApprovalTicket)


@pytest.mark.parametrize("network", ["on", "off"])
def test_github_reads_spend_cards_only_with_network_access(writer, monkeypatch, network):
    repo, db, registry_db, _ = writer
    reads = [
        "gh pr view 42 --json title",
        "gh pr list --head topic",
        "gh run list --limit 3",
        "gh run view 10 --log",
        "gh api repos/example/repo/pulls/42/comments --paginate",
    ]
    held = [
        "gh pr edit 42 --body changed", "gh run rerun 10",
        "gh api repos/example/repo/pulls/42/comments -X POST",
        "gh api repos/example/repo/pulls/42/comments --method=DELETE",
        "gh api repos/example/repo/pulls/42/comments -f body=changed",
        "gh api repos/example/repo/pulls/42/comments -F body=changed",
        "gh api repos/example/repo/pulls/42/comments --input change.json",
        "gh pr view 42 --web",
    ]
    allowed = reads if network == "on" else []
    if network == "off":
        held.extend(reads)

    async def turn(engine, prompt, name):
        gate = engine.kwargs["config"].approval_gate
        for command in allowed:
            assert (await gate(request(repo, command, provider="codex"))).action == "allow", command
        for command in held:
            assert (await gate(request(repo, command, provider="codex"))).action == "deny", command
        return Envelope(payload="done")

    result = run(writer, monkeypatch, turn, engine="codex", network=network)
    assert result.status == "done", result.error
    registry = _registry.Registry(_store.build_store(db), registry_db)
    meta = _store.read_meta(registry.job_store, result.job_id)
    assert len(meta["registry_ticket_ids"]) == len(held)
    assert [receipt["command"] for receipt in registry.card_audit(meta)] == allowed


def test_external_and_unconfined_requests_file_one_use_tickets(writer, monkeypatch):
    repo, db, registry_db, _ = writer
    outside = repo.parent / "other"
    outside.mkdir()
    git(outside, "init")
    junction = repo / "link"
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)], check=True, capture_output=True)
    else:
        junction.symlink_to(outside, target_is_directory=True)
    for name in ("creds.pem", "id_deploy/note.txt", "release.JKS", "service-credentials.json"):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
    commands = [
        "git push origin feat/writer", "git push origin feat/writer", "git fetch origin",
        "gh pr edit 1 --body changed", "gh issue comment 1 --body changed",
        "python -m pip install requests", "npm install",
        f'git -C "{outside.as_posix()}" status', "git --git-dir=../other/.git status",
        "git add -- ../other.txt", "git add -- link/outside.txt",
        "git add -- creds.pem", "git add -- old.txt id_deploy/note.txt",
        "git add -- release.JKS", "git add -- service-credentials.json",
        "git commit -F ../other/message.txt", "rm -rf old.txt", "rm -rf link",
        "rm -rf ../other", "mkdir ../outside", "git status && git push origin feat/writer",
        "python -m pytest --basetemp ../other", "python -m pytest --basetemp=../other",
        "python -m mypy --cache-dir=../other", "python -m venv ../other-env",
        "python -m pytest C:/ProgramData/live/tests", "git status > ../other.txt",
    ]

    async def turn(engine, prompt, name):
        gate = engine.kwargs["config"].approval_gate
        for command in commands:
            assert (await gate(request(repo, command))).action == "deny", command
        git(repo, "config", "core.hooksPath", "../other")
        assert (await gate(request(repo, "git commit -m 'Should ask'"))).action == "deny"
        extra = request(repo, "git status", provider="codex", additionalPermissions={"network": True})
        assert (await gate(extra)).action == "deny"
        edit = ApprovalRequest(
            provider="claude-code", kind="tool", name="Write",
            arguments={"file_path": str(outside / "escape.txt"), "content": "outside"}, cwd=str(repo),
        )
        assert (await gate(edit)).action == "deny"
        return Envelope(payload="done")

    result = run(writer, monkeypatch, turn)
    assert result.status == "done", result.error
    registry = _registry.Registry(_store.build_store(db), registry_db)
    meta = _store.read_meta(registry.job_store, result.job_id)
    assert len(meta["registry_ticket_ids"]) == len(commands) + 3
    assert all(registry.inspect(ticket).operator_only for ticket in meta["registry_ticket_ids"])
    assert registry.card_audit(meta) == []


def test_original_revocation_and_registry_isolation(writer, monkeypatch):
    repo, db, registry_db, _ = writer

    async def turn(engine, prompt, name):
        gate = engine.kwargs["config"].approval_gate
        assert (await gate(request(repo, "git status"))).action == "allow"
        registry = gate.channel.registry
        (receipt,) = registry.queue.list_card_approvals()
        assert registry.queue.revoke_card(receipt.chain[0], credential=_credentials.load(registry_db))
        assert (await gate(request(repo, "git status"))).action == "deny"
        return Envelope(payload="done")

    assert run(writer, monkeypatch, turn).status == "done"

    async def unprovisioned(engine, prompt, name):
        gate = engine.kwargs["config"].approval_gate
        assert gate.channel.hand is None
        assert (await gate(request(repo, "git status"))).action == "deny"
        return Envelope(payload="done")

    sibling = db.with_suffix(".registry.sqlite")
    isolated = (repo, db, None, [])
    result = run(isolated, monkeypatch, unprovisioned)
    assert result.status == "done", result.error
    with _registry.registry_store(sibling) as store:
        assert len(store.items(prefix=DEFAULT_PREFIX)) == 1
    assert len(_registry.Registry(_store.build_store(db), registry_db).queue.list_card_approvals()) == 1
