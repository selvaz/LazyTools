from __future__ import annotations

import json
import os
import time
from pathlib import Path

from lazytools.code_bridge import _prune
from lazytools.code_bridge.cli import main

OLD = time.time() - 30 * 86400


def _claude(root: Path, project: str, sid: str, entrypoint: str | None, *, mtime: float = OLD, subagents: bool = False):
    folder = root / project
    folder.mkdir(parents=True, exist_ok=True)
    line = {"type": "user", "sessionId": sid}
    if entrypoint is not None:
        line["entrypoint"] = entrypoint
    path = folder / f"{sid}.jsonl"
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    if subagents:
        (folder / sid / "subagents").mkdir(parents=True)
        (folder / sid / "subagents" / "agent-1.jsonl").write_text("{}\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _codex(root: Path, tid: str, originator: str, *, mtime: float = OLD):
    folder = root / "2026" / "09" / "01"
    folder.mkdir(parents=True, exist_ok=True)
    meta = {"type": "session_meta", "payload": {"id": tid, "originator": originator}}
    path = folder / f"rollout-2026-09-01T10-00-00-{tid}.jsonl"
    path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


SDK_ID = "11111111-1111-1111-1111-111111111111"
MINE_ID = "22222222-2222-2222-2222-222222222222"
FRESH_ID = "33333333-3333-3333-3333-333333333333"
UNKNOWN_ID = "44444444-4444-4444-4444-444444444444"
ALIASED_ID = "55555555-5555-5555-5555-555555555555"
BRIDGE_ID = "66666666-6666-6666-6666-666666666666"
DESKTOP_ID = "77777777-7777-7777-7777-777777777777"
REMOTE_ID = "88888888-8888-8888-8888-888888888888"


def _layout(tmp_path: Path):
    claude, codex = tmp_path / "claude", tmp_path / "codex"
    paths = {
        "sdk": _claude(claude, "p1", SDK_ID, "sdk-py", subagents=True),
        "mine": _claude(claude, "p1", MINE_ID, "claude-desktop"),
        "fresh": _claude(claude, "p2", FRESH_ID, "sdk-py", mtime=time.time()),
        "unknown": _claude(claude, "p2", UNKNOWN_ID, None),
        "aliased": _claude(claude, "p2", ALIASED_ID, "sdk-py"),
        "remote": _claude(claude, "p2", REMOTE_ID, "sdk-cli"),
        "bridge": _codex(codex, BRIDGE_ID, "lazybridge"),
        "desktop": _codex(codex, DESKTOP_ID, "Codex Desktop"),
    }
    return claude, codex, paths


def test_only_old_unaliased_automated_sessions_are_candidates(tmp_path):
    claude, codex, _ = _layout(tmp_path)
    found = [*_prune.claude_transcripts(claude), *_prune.codex_transcripts(codex)]

    chosen, kept = _prune.candidates(found, older_than_days=7, protected_ids={ALIASED_ID})

    assert {item.session_id for item in chosen} == {SDK_ID, BRIDGE_ID}
    assert kept == {"not automated": 3, "unknown origin": 1, "too recent": 1, "aliased": 1}


def test_codex_session_id_comes_from_the_rollout_name(tmp_path):
    _, codex, _ = _layout(tmp_path)
    ids = {item.session_id: item.origin for item in _prune.codex_transcripts(codex)}
    assert ids == {BRIDGE_ID: "lazybridge", DESKTOP_ID: "Codex Desktop"}


def test_cli_dry_run_deletes_nothing(tmp_path, monkeypatch, capsys):
    claude, codex, paths = _layout(tmp_path)
    monkeypatch.setenv("LAZYBRIDGE_SESSIONS_FILE", str(tmp_path / "sessions.json"))

    code = main(["prune-sessions", "--claude-root", str(claude), "--codex-root", str(codex), "--json"])

    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["mode"] == "dry-run" and summary["candidates"] == 3  # sdk + aliased (no alias bound) + bridge
    assert all(path.exists() for path in paths.values())


def test_cli_apply_deletes_candidates_and_subagent_folders_only(tmp_path, monkeypatch, capsys):
    claude, codex, paths = _layout(tmp_path)
    monkeypatch.setenv("LAZYBRIDGE_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    from lazybridge.engines.sessions import SessionRegistry

    SessionRegistry(tmp_path / "sessions.json").bind("claude", tmp_path, "kept", ALIASED_ID)

    code = main(["prune-sessions", "--claude-root", str(claude), "--codex-root", str(codex), "--apply", "--json"])

    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["deleted"] == 2 and summary["kept"]["aliased"] == 1
    assert not paths["sdk"].exists() and not (paths["sdk"].parent / SDK_ID).exists()
    assert not paths["bridge"].exists()
    for name in ("mine", "fresh", "unknown", "aliased", "desktop", "remote"):
        assert paths[name].exists(), name


def test_destructive_retention_values_are_refused(tmp_path, capsys):
    claude, codex, paths = _layout(tmp_path)
    for bad in ("-1", "nan", "inf"):
        args = ["prune-sessions", "--claude-root", str(claude), "--codex-root", str(codex), "--older-than-days", bad]
        assert main([*args, "--apply"]) == 2
    assert all(path.exists() for path in paths.values())
