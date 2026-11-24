"""Exercise the publication CLI against a bare repository and a fake GitHub CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest
from lazybridge import Store
from lazybridge.ext.approval import ApprovalQueue
from lazybridge.policy.review import select_reviewer

from code_bridge_test_helpers import write_python_executable
from lazytools.code_bridge import _cloud, _gh, _launch, _store, cli
from lazytools.connectors.projects import ProjectsTools
from lazytools.projects.plan_edit import _board_key
from lazytools.projects.task_service import ProjectTaskService
from lazytools.projects.task_store import transaction
from test_projects_task_service import (
    PROJECT,
    REVIEWER,
    admit,
    claim,
    complete,
    evidence,
    placement,
    submit,
    task,
)

REMOTE = "https://github.com/example/publication.git"
REPOSITORY = "example/publication"
FAKE_GH = """
import json, os, subprocess, sys
from pathlib import Path

state_path = Path(os.environ["FAKE_GH_STATE"])
state = json.loads(state_path.read_text()) if state_path.exists() else {"prs": [], "comments": []}
args = sys.argv[1:]
assert args[0] == "pr"
assert args[args.index("--repo") + 1] == "github.com/example/publication"
if args[1] == "create":
    assert "--draft" in args
    head = args[args.index("--head") + 1]
    base = args[args.index("--base") + 1]
    def sha(branch):
        return subprocess.check_output(
            ["git", "--git-dir", os.environ["FAKE_GH_BARE"], "rev-parse", "refs/heads/" + branch],
            text=True,
        ).strip()
    number = len(state["prs"]) + 1
    url = f"https://github.com/example/publication/pull/{number}"
    state["prs"].append({
        "number": number, "url": url, "headRefName": head, "baseRefName": base,
        "headRefOid": os.environ.get("FAKE_GH_HEAD") or sha(head), "baseRefOid": sha(base),
        "isDraft": True, "isCrossRepository": False, "state": "OPEN",
    })
    print(url)
elif args[1] == "view":
    print(json.dumps(state["prs"][int(args[2]) - 1]))
elif args[1] == "comment":
    assert "--body-file" in args and args[args.index("--body-file") + 1] == "-"
    state["comments"].append(sys.stdin.read())
else:
    raise AssertionError(args)
state_path.write_text(json.dumps(state))
"""


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True, stderr=subprocess.PIPE).strip()


def commit(repo, message):
    git(repo, "commit", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture(params=["shared", "isolated"])
def publication(tmp_path, monkeypatch, request):
    repo, bare = tmp_path / "work", tmp_path / "bare"
    repo.mkdir()
    bare.mkdir()
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    git(repo, "init", "--initial-branch=main")
    git(bare, "init", "--bare", "--initial-branch=main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    base = commit(repo, "base")
    git(repo, "push", str(bare), "main")
    git(repo, "remote", "add", "origin", REMOTE)
    git(repo, "switch", "-c", "feature/reviewed")
    reviewed = commit(repo, "candidate")
    git(repo, "switch", "-c", "feature/other")
    other = commit(repo, "other candidate")
    git(repo, "switch", "feature/reviewed")

    projects_db = tmp_path / "projects.sqlite"
    jobs_db = tmp_path / "jobs.sqlite"
    key = tmp_path / "issuer.pem"
    registry_db = projects_db
    options = []
    if request.param == "isolated":
        registry_db = tmp_path / "jobs.registry.sqlite"
        options = ["--db", str(jobs_db)]
        monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_DB", str(tmp_path / "default-jobs.sqlite"))
    else:
        monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_DB", str(jobs_db))
    monkeypatch.setenv("LAZYTOOLS_PROJECTS_STORE_DB", str(projects_db))
    monkeypatch.setenv("LAZYTOOLS_RECEIPT_ISSUER_KEY", str(key))
    projects = ProjectsTools(store_db=str(projects_db), allow_write=True)
    assert "AS A DRAFT" in projects.projects_create(PROJECT, "Publication", "Publish reviewed commits")
    service = ProjectTaskService(str(projects_db))
    admit(
        projects,
        [
            task("writer", repo=REPOSITORY, assigned_branch="feature/reviewed"),
            task("review", review_of="writer", dependency_ids=["writer"]),
            task("other", repo=REPOSITORY, assigned_branch="feature/other"),
            task("other-review", review_of="other", dependency_ids=["other"]),
        ],
    )
    pushes = []
    real_git = _cloud._git

    def local_git(cwd, *args, **kwargs):
        if "push" in args:
            assert args[:5] == ("-c", "push.followTags=false", "push", "--", REMOTE)
            pushes.append(args[5])
            args = (*args[:4], str(bare), *args[5:])
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(_cloud, "_git", local_git)
    state = tmp_path / "gh.json"
    gh = write_python_executable(tmp_path / "bin", "gh", FAKE_GH)
    monkeypatch.setenv("GH_BIN", str(gh))
    monkeypatch.setenv("FAKE_GH_STATE", str(state))
    monkeypatch.setenv("FAKE_GH_BARE", str(bare))
    if os.name == "nt":
        monkeypatch.setattr(_gh, "gh_argv", lambda: [sys.executable, str(gh.with_suffix(".py"))])
    return SimpleNamespace(
        repo=repo,
        bare=bare,
        reviewed=reviewed,
        other=other,
        base=base,
        service=service,
        jobs_db=jobs_db,
        registry_db=registry_db,
        public_key=key.with_suffix(".pub.pem"),
        pushes=pushes,
        state=state,
        options=options,
    )


def accept(fixture, *, writer="writer", review="review", same_provider=False, include_review=True):
    sha = fixture.other if writer == "other" else fixture.reviewed
    service = fixture.service
    placement(service, writer)
    writer_claim = claim(service, writer, key=writer)
    writer_job = submit(service, writer_claim)
    complete(service, writer_claim, evidence(service, writer_claim, writer_job, artifact_ref=sha))
    selection = select_reviewer("codex", ["codex"] if same_provider else ["claude"], "opposite-or-fresh")
    with Store(str(fixture.jobs_db)) as jobs:
        launch = _launch.resolve_launch("codex", fixture.repo, "default", None, "legacy")
        _store.write_meta(jobs, writer_job, {**launch.metadata(), "cwd": str(fixture.repo), "model": "writer-model"})
        if not include_review:
            return
        placement(service, review, engine="codex" if same_provider else "claude_code")
        review_claim = claim(service, review, key=review, principal=REVIEWER)
        review_job = submit(service, review_claim, principal=REVIEWER)
        review_ref = evidence(
            service,
            review_claim,
            review_job,
            artifact_ref=sha,
            reviewed_sha=sha,
            review_passed=True,
            review_independence=selection.independence,
        )
        complete(service, review_claim, review_ref, principal=REVIEWER)
        launch = _launch.resolve_launch(selection.engine, fixture.repo, "review", None, "legacy", review=True)
        _store.write_meta(jobs, review_job, {**launch.metadata(), "cwd": str(fixture.repo), "model": "review-model"})


def publish(fixture, capsys, *, writer="writer", branch="feature/reviewed", ticket=None, status=1):
    extra = ["--ticket", ticket] if ticket else []
    assert (
        cli.main(
            [
                "publish",
                "--repo",
                str(fixture.repo),
                "--task",
                f"{PROJECT}/{writer}",
                "--branch",
                branch,
                *fixture.options,
                *extra,
            ]
        )
        == status
    )
    output = capsys.readouterr().out.splitlines()
    receipt_id = output[-1].split()[1]
    with transaction(fixture.service.path) as store:
        row = store.connection.execute(
            "SELECT task_id, attempt, head_sha, pr_number, status, receipt_json FROM publication_receipts "
            "WHERE receipt_id = ?",
            (receipt_id,),
        ).fetchone()
    assert row is not None
    receipt = json.loads(row[-1])
    assert row[:5] == (
        f"{PROJECT}/{writer}",
        receipt["writer"]["attempt"],
        receipt["publication"]["head_sha"],
        receipt["publication"]["pr_number"],
        receipt["outcome"]["status"],
    )
    return receipt


def approve(fixture, capsys, **kwargs):
    receipt = publish(fixture, capsys, **kwargs)
    assert receipt["outcome"]["refusal_reason"] == "approval_not_decided"
    ticket = receipt["approval"]["ticket_id"]
    assert cli.main(["approve", ticket, *fixture.options]) == 0
    return ticket


def test_publish_reviewed_head_and_verify_tampering(publication, capsys):
    accept(publication)
    ticket = approve(publication, capsys)
    receipt = publish(publication, capsys, status=0)
    number = receipt["publication"]["pr_number"]
    assert number == 1
    assert receipt["approval"]["ticket_id"] == ticket
    assert datetime.fromisoformat(receipt["approval"]["consumed_at"]) <= datetime.fromisoformat(receipt["issued_at"])
    assert receipt["publication"]["head_sha"] == publication.reviewed
    assert receipt["publication"]["base_sha"] == publication.base
    assert receipt["reviewer"]["independence_mode"] == "opposite-engine"
    assert receipt["writer"]["launch_mode"] == "ungoverned-legacy"
    assert receipt["writer"]["admission"]["eligible"] is False
    assert receipt["reviewer"]["policy"]["tool_classes"] == ["read"]
    assert str(publication.repo) not in json.dumps(receipt)
    assert publication.pushes == [f"{publication.reviewed}:refs/heads/feature/reviewed"]
    assert git(publication.bare, "rev-parse", "refs/heads/feature/reviewed") == publication.reviewed
    state = json.loads(publication.state.read_text())
    assert [pr["isDraft"] for pr in state["prs"]] == [True]
    assert receipt["receipt_id"] in state["comments"][0]
    assert receipt["issuer"]["public_key_fingerprint"] in state["comments"][0]
    assert cli.main(["verify-receipt", receipt["receipt_id"], "--live"]) == 0
    state["prs"][0]["headRefOid"] = publication.other
    publication.state.write_text(json.dumps(state))
    assert cli.main(["verify-receipt", receipt["receipt_id"], "--live"]) == 1
    assert capsys.readouterr().out.splitlines()[-1] == "live_head_mismatch"
    file = publication.jobs_db.parent / "results/job-writer/publication" / receipt["receipt_id"] / "receipt.json"
    assert json.loads(file.read_text()) == receipt
    receipt["reviewer"]["model"] = "changed"
    file.write_text(json.dumps(receipt))
    assert cli.main(["verify-receipt", str(file), "--public-key", str(publication.public_key)]) == 1
    assert capsys.readouterr().out.splitlines()[-1] == "bad_signature"


def test_head_moved_after_review_refuses_before_push(publication, capsys):
    accept(publication)
    ticket = approve(publication, capsys)
    assert commit(publication.repo, "unreviewed change") != publication.reviewed
    receipt = publish(publication, capsys)
    assert receipt["outcome"]["refusal_reason"] == "head_changed_after_review"
    assert receipt["publication"]["head_sha"] == publication.reviewed
    assert cli.main(["verify-receipt", receipt["receipt_id"]]) == 0
    assert not publication.pushes and not publication.state.exists()
    with Store(str(publication.registry_db)) as registry:
        assert ApprovalQueue(registry).get_ticket(ticket).consumed_at is None


def test_ticket_for_other_commit_refuses_without_consuming(publication, capsys):
    accept(publication)
    accept(publication, writer="other", review="other-review")
    ticket = approve(publication, capsys)
    receipt = publish(publication, capsys, writer="other", branch="feature/other", ticket=ticket)
    assert receipt["outcome"]["refusal_reason"] == "approval_target_mismatch"
    assert receipt["approval"]["ticket_id"] == ticket
    assert receipt["approval"]["consumed_at"] is None
    assert cli.main(["verify-receipt", receipt["receipt_id"]]) == 0
    assert not publication.pushes and not publication.state.exists()
    with Store(str(publication.registry_db)) as registry:
        assert ApprovalQueue(registry).get_ticket(ticket).consumed_at is None


def test_replay_returns_stored_outcome(publication, capsys):
    accept(publication)
    approve(publication, capsys)
    first = publish(publication, capsys, status=0)
    evidence_file = publication.jobs_db.parent / "results/job-writer/publication" / first["receipt_id"] / "receipt.json"
    evidence_file.unlink()
    assert publish(publication, capsys, status=0) == first
    assert json.loads(evidence_file.read_text()) == first
    state = json.loads(publication.state.read_text())
    assert len(state["prs"]) == len(state["comments"]) == len(publication.pushes) == 1


def test_interrupted_publication_cannot_reuse_approval(publication, capsys, monkeypatch):
    accept(publication)
    approve(publication, capsys)

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(_gh, "pr_create", interrupted)
    with pytest.raises(KeyboardInterrupt):
        publish(publication, capsys, status=0)
    receipt = publish(publication, capsys)
    assert receipt["outcome"]["refusal_reason"] == "approval_already_consumed"
    assert cli.main(["verify-receipt", receipt["receipt_id"]]) == 0
    assert len(publication.pushes) == 1 and not publication.state.exists()


def test_same_provider_fresh_review_is_visible_and_verifiable(publication, capsys):
    accept(publication, same_provider=True)
    approve(publication, capsys)
    receipt = publish(publication, capsys, status=0)
    assert receipt["reviewer"]["independence_mode"] == "same-provider-fresh"
    assert receipt["writer"]["engine"] == receipt["reviewer"]["engine"] == "codex"
    assert cli.main(["verify-receipt", receipt["receipt_id"], "--live"]) == 0


def test_pr_head_mismatch_has_signed_refusal_and_no_retry(publication, capsys, monkeypatch):
    accept(publication)
    approve(publication, capsys)
    monkeypatch.setenv("FAKE_GH_HEAD", publication.other)
    receipt = publish(publication, capsys)
    assert receipt["outcome"]["refusal_reason"] == "pr_head_mismatch"
    assert receipt["publication"]["pr_number"] is None
    assert cli.main(["verify-receipt", receipt["receipt_id"]]) == 0
    assert publish(publication, capsys) == receipt
    assert len(json.loads(publication.state.read_text())["prs"]) == len(publication.pushes) == 1


def test_missing_review_and_rejected_ticket_are_signed(publication, capsys):
    accept(publication, include_review=False)
    missing = publish(publication, capsys)
    assert missing["outcome"]["refusal_reason"] == "review_missing"
    assert cli.main(["verify-receipt", missing["receipt_id"]]) == 0
    accept(publication, writer="other", review="other-review")
    pending = publish(publication, capsys, writer="other", branch="feature/other")
    assert cli.main(["reject", pending["approval"]["ticket_id"], "--reason", "declined", *publication.options]) == 0
    rejected = publish(publication, capsys, writer="other", branch="feature/other")
    assert rejected["outcome"]["refusal_reason"] == "approval_rejected"
    assert cli.main(["verify-receipt", rejected["receipt_id"]]) == 0
    assert not publication.pushes and not publication.state.exists()


def test_non_independent_saved_review_refuses(publication, capsys):
    accept(publication)
    with transaction(publication.service.path, write=True) as store:
        board = store.read(_board_key(PROJECT))
        reviewer = next(entry for entry in board["tasks"] if entry["task_id"] == "review")
        dispatch = reviewer["dispatches"][-1]
        dispatch["evidence"][dispatch["current_evidence"]]["review_independence"] = "same-provider-fresh"
        store.write(_board_key(PROJECT), board)
    receipt = publish(publication, capsys)
    assert receipt["outcome"]["refusal_reason"] == "review_not_independent"
    assert cli.main(["verify-receipt", receipt["receipt_id"]]) == 0
    assert not publication.pushes and not publication.state.exists()


def test_mcp_receipt_tools_are_read_only(publication, capsys):
    from mcp import types

    from lazytools.mcp_server.providers import default_providers
    from lazytools.mcp_server.server import _mcp_tool_models, expand_tools

    accept(publication, include_review=False)
    receipt = publish(publication, capsys)
    tools = expand_tools(
        default_providers(["projects"], data_source={"projects_store_db": str(publication.service.path)})
    )
    by_name = {tool.name: tool for tool in _mcp_tool_models(types, tools)}
    assert by_name["receipts_get"].annotations.readOnlyHint
    assert by_name["receipts_verify"].annotations.readOnlyHint
    assert not any("publish" in name for name in by_name)
    assert tools["receipts_get"].func(receipt_id=receipt["receipt_id"]) == receipt
    assert tools["receipts_verify"].func(receipt_id=receipt["receipt_id"]) == {"valid": True, "reason": None}
