"""Run the receipt demo as a subprocess against a temporary Git checkout."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("scenario,engine", [("publish", "codex"), ("drift", "claude")])
def test_receipt_demo_dry_run(tmp_path, scenario, engine):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = dict(os.environ)
    env.update(
        GIT_ALLOW_PROTOCOL="file",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        TEMP=str(tmp_path),
        TMP=str(tmp_path),
        TMPDIR=str(tmp_path),
        LAZYTOOLS_PROJECTS_STORE_DB=str(tmp_path / "ambient-projects.sqlite"),
        LAZYTOOLS_CODE_BRIDGE_DB=str(tmp_path / "ambient-bridge.sqlite"),
        LAZYTOOLS_RECEIPT_ISSUER_KEY=str(tmp_path / "ambient-issuer.pem"),
    )

    def git(cwd, *args):
        return subprocess.check_output(["git", *args], cwd=cwd, env=env, text=True).strip()

    git(repo, "init", "--initial-branch=main")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "Base")
    git(repo, "remote", "add", "origin", "https://github.com/example/real-origin.git")
    start = git(repo, "rev-parse", "HEAD")
    command = [sys.executable, str(ROOT / "tools" / "receipt_live_demo.py"), "--dry-run", "--repo", str(repo)]
    if scenario == "drift":
        command += ["--scenario", scenario, "--engine", engine, "--state-dir", str(tmp_path / "states")]
    result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=90, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(repo, "rev-parse", "HEAD") == start
    assert git(repo, "branch", "--list", "demo/*") == ""
    assert git(repo, "remote", "get-url", "origin") == "https://github.com/example/real-origin.git"
    assert not list(tmp_path.glob("ambient-*"))
    for step in (1, 2, 3, 4, 5 if scenario == "drift" else 6, 7):
        assert f"STEP {step}: " in result.stdout

    state, = list(tmp_path.rglob("receipt-demo-*"))
    paths = list(state.glob("results/*/publication/*/receipt.json"))
    receipts = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    assert all(str(path) in result.stdout for path in paths)
    github = state / "fake-github.json"
    remote = state / "fake-remote.git"
    if scenario == "drift":
        receipt, = receipts
        assert receipt["outcome"]["refusal_reason"] == "head_changed_after_review"
        assert receipt["publication"]["head_sha"] != receipt["reviewer"]["reviewed_sha"]
        assert "signature valid" in result.stdout
        assert not github.exists()
        assert git(remote, "for-each-ref", "refs/heads/demo") == ""
        assert "projects.publication_receipts: 0 -> 1 (+1 rows)" in result.stdout
    else:
        assert len(receipts) == 2
        published, = [receipt for receipt in receipts if receipt["outcome"]["status"] == "published"]
        pending, = [receipt for receipt in receipts if receipt["outcome"]["status"] == "refused"]
        assert pending["outcome"]["refusal_reason"] == "approval_not_decided"
        assert published["approval"]["ticket_id"] == pending["approval"]["ticket_id"]
        assert published["approval"]["decided_by"] == "receipt-demo-operator"
        assert "signature valid" in result.stdout
        assert published["publication"]["head_sha"] == published["reviewer"]["reviewed_sha"]
        assert published["reviewer"]["independence_mode"] == "opposite-engine"
        assert "live valid" in result.stdout
        assert f"returned stored receipt {published['receipt_id']}" in result.stdout
        fake_github = json.loads(github.read_text(encoding="utf-8"))
        assert len(fake_github["prs"]) == len(fake_github["comments"]) == 1
        assert fake_github["prs"][0]["isDraft"] is True
        assert "projects.publication_receipts: 0 -> 2 (+2 rows)" in result.stdout
