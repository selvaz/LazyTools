from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import pytest
from lazybridge.engines.coding import ApprovalRequest
from lazybridge.ext.approval import CardApproval

from lazytools.code_bridge import _coder, _coder_actions, _registry, _store, cli
from test_code_bridge_coder_cards import request
from test_code_bridge_continuations import git


@pytest.mark.parametrize(("command", "kind"), [
    ("git add -- src/file.py", "git_local"),
    ('git commit -m "Record changes"', "git_local"),
    ("git push origin feature/work", "git_push_job_branch"),
    ("git push origin another-branch", "unknown"),
    ("git push origin trunk", "push_default_branch"),
    ("git push origin main", "push_default_branch"),
    ("gh pr create --fill", "gh_pr_create"),
    ("gh pr merge 17 --squash", "gh_pr_merge"),
    ("rm -rf -- scratch", "delete_files"),
    ("python -m pip install example", "package_install"),
    ("git ls-remote origin", "network_other"),
    ("schtasks /Run /TN Daily", "production_task"),
    (r'Remove-Item -LiteralPath "C:\ProgramData\service\cache"', "production_task"),
    ("npm install --prefix=/srv/production", "production_task"),
    ("gh issue comment 17 --body approved", "payment_or_external_post"),
    ("gh api repos/example/name/issues/17/comments -f body=hello", "payment_or_external_post"),
    ("stripe payment_intents create", "payment_or_external_post"),
    ("git status && gh pr merge 17", "unknown"),
    ("arbitrary-command", "unknown"),
])
def test_ticket_classification(command, kind):
    assert _coder_actions.ticket_class(
        command, job_branch="feature/work", default_branch="trunk", powershell=True,
    ) == kind


def test_owner_can_select_autonomy_preset(tmp_path, provision_bridge_approver, capsys):
    registry = tmp_path / "registry.sqlite"
    provision_bridge_approver(registry)
    command = ["approver-credential", "autonomy", "--registry-db", str(registry)]

    assert cli.main([*command, "--mode", "total", "--preset", "with-production"]) == 0
    capsys.readouterr()
    assert cli.main([*command, "--show"]) == 0
    output = capsys.readouterr().out
    assert "total" in output and "with-production" in output and "gh_pr_merge" in output
    assert "production_task" not in output


@pytest.mark.parametrize(("preset", "floor"), [
    ("solo-dev", {"unknown", "payment_or_external_post"}),
    ("with-production", {"production_task", "payment_or_external_post", "delete_files", "push_default_branch", "unknown"}),
])
def test_packaged_autonomy_presets(preset, floor):
    assert _coder.delegated_classes("supervised", preset) == {"git_local", "git_push_job_branch", "gh_pr_create"}
    assert _coder.delegated_classes("total", preset) == _coder_actions.TICKET_COMMANDS.keys() - floor


def test_autonomy_mode_changes_require_owner_credential(tmp_path, provision_bridge_approver, capsys):
    registry = tmp_path / "registry.sqlite"
    other_registry = tmp_path / "other.sqlite"
    command = ["approver-credential", "autonomy", "--registry-db", str(registry), "--mode"]
    owner_file = provision_bridge_approver(registry)
    assert cli.main([*command, "supervised"]) == 0
    original = _coder.show(registry)

    owner_file.unlink()
    assert cli.main([*command, "total"]) == 1
    assert "credential is missing" in capsys.readouterr().err
    assert _coder.show(registry) == original

    wrong_file = provision_bridge_approver(other_registry)
    owner_file.write_bytes(wrong_file.read_bytes())
    if os.name != "nt":
        owner_file.chmod(0o600)
    for mode in ("supervised", "total"):
        assert cli.main([*command, mode]) == 1
        assert "requires an owner credential" in capsys.readouterr().err
    assert _coder.show(registry) == original


def test_autonomy_refuses_registry_in_a_checkout(tmp_path, provision_bridge_approver, capsys):
    git(tmp_path, "init")
    registry = tmp_path / "registry.sqlite"
    provision_bridge_approver(registry)
    assert cli.main([
        "approver-credential", "autonomy", "--registry-db", str(registry), "--mode", "total",
    ]) == 1
    assert "Owner approval state must be outside Git checkouts" in capsys.readouterr().err
    assert not _coder.show(registry).cards


@pytest.fixture
def autonomy_registry(tmp_path, provision_bridge_approver, monkeypatch):
    monkeypatch.delenv("GH_REPO", raising=False)
    monkeypatch.delenv("GH_HOST", raising=False)
    cwd = tmp_path / "checkout"
    cwd.mkdir()
    git(cwd, "init")
    git(cwd, "symbolic-ref", "HEAD", "refs/heads/feature/work")
    git(cwd, "remote", "add", "origin", "https://github.com/example/autonomy.git")
    git(cwd, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    path = tmp_path / "registry.sqlite"
    provision_bridge_approver(path)
    store = _store.build_store(tmp_path / "jobs.sqlite")
    return cwd, _registry.Registry(store, path)


def configure(registry, mode, *flags):
    return cli.main([
        "approver-credential", "autonomy", "--registry-db", str(registry.path), "--mode", mode, *flags,
    ])


def channel_for(registry, cwd, job_id, *, network="on"):
    expires = datetime.now(UTC) + timedelta(minutes=5)
    hand = registry.copy_coder_hand(job_id, job_id, cwd, None, expires, network=network)
    assert hand is not None
    return _registry.ApprovalChannel(
        registry, job_id, "revision", "digest", timedelta(minutes=5), 0.01,
        lambda: None, lambda: None, lambda: None, actor="ceo", hand=hand,
    )


def test_mode_switch_revokes_cards_in_existing_hand(autonomy_registry):
    cwd, registry = autonomy_registry
    assert configure(registry, "total", "--preset", "with-production") == 0
    total = channel_for(registry, cwd, "total-job")
    push = request(cwd, "git push origin feature/work", provider="codex")
    merge = request(cwd, "gh pr merge 17 --squash", provider="codex")
    production = request(cwd, "schtasks /Run /TN Daily", provider="codex")
    for req, kind in [(push, "git_push_job_branch"), (merge, "gh_pr_merge")]:
        decision = total._submit(req, "Approve?")
        assert isinstance(decision, CardApproval)
        assert decision.actor == "ceo"
        assert decision.action_class == kind
    receipt = registry.card_audit(_store.read_meta(registry.job_store, total.job_id))[-1]
    assert receipt["mode"] == "total" and receipt["ticket_class"] == "gh_pr_merge"
    assert receipt["reason"] == "Matched an owner card for gh_pr_merge in the with-production preset"

    held, _ = total._submit(production, "Approve?")
    assert held.status == "pending"
    assert held.request.payload["ticket_class"] == "production_task"
    edit = ApprovalRequest(
        provider="claude-code", kind="tool", name="Write", cwd=str(cwd), raw={},
        arguments={"file_path": str(cwd / "production" / "settings.yml"), "content": "example"},
    )
    held, _ = total._submit(edit, "Approve?")
    assert held.status == "pending"
    assert held.request.payload["ticket_class"] == "production_task"
    push_default = request(cwd, "git push origin trunk")
    held, _ = total._submit(push_default, "Approve?")
    assert held.status == "pending"
    assert held.request.payload["ticket_class"] == "push_default_branch"

    assert configure(registry, "supervised") == 0
    held, _ = total._submit(merge, "Approve?")
    assert held.status == "pending"
    supervised = channel_for(registry, cwd, "supervised-job")
    assert isinstance(supervised._submit(push, "Approve?"), CardApproval)
    held, _ = supervised._submit(merge, "Approve?")
    assert held.status == "pending"

    assert configure(registry, "total") == 0
    held, _ = total._submit(merge, "Approve?")
    assert held.status == "pending"
    assert isinstance(channel_for(registry, cwd, "next-total-job")._submit(merge, "Approve?"), CardApproval)


def test_total_cards_respect_native_and_command_scope(autonomy_registry):
    cwd, registry = autonomy_registry
    assert configure(registry, "total", "--preset", "solo-dev") == 0
    local = channel_for(registry, cwd, "local", network="off")
    networked = channel_for(registry, cwd, "networked")
    for channel, command in [
        (local, "git push origin feature/work"),
        (networked, "git push origin feature/work --force"),
        (networked, "git push origin other/branch"),
        (networked, "gh pr merge 17 --repo other/repo"),
        (networked, "gh pr merge 17 --delete-branch"),
        (networked, "gh issue comment 17 --body hello"),
        (networked, "python -m pytest"),
        (networked, "rm -rf ../outside"),
        (networked, "lazytools-code-bridge approver-credential autonomy --mode total"),
    ]:
        ticket, _ = channel._submit(request(cwd, command), command)
        assert ticket.status == "pending"


def test_pr_create_card_requires_an_explicit_job_head(autonomy_registry):
    cwd, registry = autonomy_registry
    git(cwd, "-c", "user.name=Bridge Trial", "-c", "user.email=bridge@example.invalid",
        "commit", "--allow-empty", "-m", "Unpushed job commit")
    hook = cwd / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    assert configure(registry, "supervised") == 0
    channel = channel_for(registry, cwd, "create-pr")

    for command in ("git push origin feature/work", "gh pr create --fill", "gh pr create --fill --head other/work"):
        decision = channel._submit(request(cwd, command), command)
        assert isinstance(decision, tuple) and decision[0].status == "pending", command
    for head in ("--head feature/work", "--head=feature/work", "-H feature/work"):
        command = f"gh pr create --fill {head}"
        assert isinstance(channel._submit(request(cwd, command), command), CardApproval)


@pytest.mark.parametrize(("variable", "target"), [
    ("GH_REPO", "other/repo"),
    ("GH_REPO", "github.example.com/example/autonomy"),
    ("GH_HOST", "github.example.com"),
])
def test_github_cards_refuse_conflicting_environment(autonomy_registry, monkeypatch, variable, target):
    cwd, registry = autonomy_registry
    assert configure(registry, "total") == 0
    channel = channel_for(registry, cwd, "github-env")
    monkeypatch.setenv(variable, target)

    for command in ("gh pr merge 17 --squash", "gh pr create --fill --head feature/work", "gh pr view 17"):
        decision = channel._submit(request(cwd, command, provider="codex"), command)
        assert isinstance(decision, tuple) and decision[0].status == "pending", command


def test_github_cards_check_repo_flags_and_allow_matching_overrides(autonomy_registry, monkeypatch):
    cwd, registry = autonomy_registry
    assert configure(registry, "total") == 0
    channel = channel_for(registry, cwd, "github-repo")
    monkeypatch.setenv("GH_HOST", "github.com")
    monkeypatch.setenv("GH_REPO", "example/autonomy")

    for override in ("--repo other/repo", "-R other/repo", "-R=other/repo", "--repo=github.example.com/example/autonomy"):
        command = f"gh run list {override}"
        decision = channel._submit(request(cwd, command), command)
        assert isinstance(decision, tuple) and decision[0].status == "pending", command
    for command in ("gh pr merge 17 --squash", "gh pr view 17 --repo example/autonomy"):
        assert isinstance(channel._submit(request(cwd, command), command), CardApproval), command

    monkeypatch.delenv("GH_HOST")
    monkeypatch.setenv("GH_REPO", "github.com/example/autonomy")
    command = "gh.exe run list -R GitHub.com/Example/Autonomy"
    assert isinstance(channel._submit(request(cwd, command), command), CardApproval)


def test_solo_total_accepts_scoped_commands_outside_supervised_classes(autonomy_registry):
    cwd, registry = autonomy_registry
    assert configure(registry, "total", "--preset", "solo-dev") == 0
    (cwd / "old.txt").touch()
    total = channel_for(registry, cwd, "solo-job")
    commands = {
        "rm -- old.txt": "delete_files",
        "python -I -m pip install --isolated --no-cache-dir --only-binary=:all: --target deps example==1.0":
            "package_install",
        "git ls-remote origin": "network_other",
        "gh pr create --fill --head feature/work": "gh_pr_create",
    }
    if os.name == "nt":
        commands["schtasks /Run /TN Daily"] = "production_task"
    for command, kind in commands.items():
        decision = total._submit(request(cwd, command, provider="codex"), command)
        assert isinstance(decision, CardApproval), command
        assert decision.action_class == kind


@pytest.fixture
def autonomy_db(tmp_path):
    """A real SQLite Store shaped like one day of bridge job history."""
    path = tmp_path / "code-bridge.sqlite"
    store = _store.build_store(path)
    registry = _store.build_job_registry(store)
    stamp = datetime(2026, 11, 10, 12, tzinfo=UTC).timestamp()
    for job_id, status in (("auto-1", "done"), ("auto-2", "failed"), ("helped", "done")):
        registry.write(job_id, "fixture task", tool_name="codex", status=status)
        _store.write_meta(store, job_id, {"job_id": job_id, "created_at": stamp - 60, "updated_at": stamp})
    _store.record_intervention(store, "helped", "resync", "provider recovered")
    _store.record_intervention(store, "helped", "apply")
    registry.write("running", "not finished", tool_name="codex", status="running")
    _store.write_meta(store, "running", {"job_id": "running", "updated_at": stamp})
    return path


def test_autonomy_sample_day_matches_hand_count(autonomy_db, capsys):
    assert cli.main(
        ["autonomy", "--since", "2026-11-10", "--until", "2026-11-11", "--json", "--db", str(autonomy_db)]
    ) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "since": "2026-11-10",
        "until": "2026-11-11",
        "finished_jobs": 3,
        "autonomous_jobs": 2,
        "autonomy_percent": pytest.approx(66.6666666667),
        "target_percent": 90.0,
        "target_met": False,
        "intervention_causes": {"apply": 1, "resync": 1},
        "card_approvals_by_class": {},
    }


def test_autonomy_week_and_human_output(autonomy_db, capsys):
    assert cli.main(["autonomy", "--week", "2026-W46", "--db", str(autonomy_db)]) == 0
    output = capsys.readouterr().out
    assert "Autonomy: 2/3 finished jobs (66.7%); target 90.0%" in output
    assert "apply: 1" in output
    assert "resync: 1" in output
    assert "Card approvals by class:" in output


def test_autonomy_report_counts_only_spent_cards_in_utc_range(autonomy_registry, capsys):
    cwd, registry = autonomy_registry
    assert configure(registry, "total", "--preset", "with-production") == 0
    channel = channel_for(registry, cwd, "active")
    _store.build_job_registry(registry.job_store).write("active", "Test autonomy", tool_name="codex", status="running")
    for command in ("git push origin feature/work", "gh pr merge 17 --squash", "gh pr merge 19 --merge"):
        assert isinstance(channel._submit(request(cwd, command), command), CardApproval)
    ticket, _ = channel._submit(request(cwd, "schtasks /Run /TN Daily"), "Approve?")
    assert ticket.status == "pending"
    capsys.readouterr()

    today = datetime.now(UTC).date()
    db = registry.path.with_name("jobs.sqlite")
    for since, until, expected in [
        (today - timedelta(days=1), today, {}),
        (today, today + timedelta(days=1), {"gh_pr_merge": 2, "git_push_job_branch": 1}),
    ]:
        assert cli.main([
            "autonomy", "--since", since.isoformat(), "--until", until.isoformat(), "--json", "--db", str(db),
        ]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["card_approvals_by_class"] == expected
        assert report["finished_jobs"] == 0


def test_manual_approval_records_reason(tmp_path):
    db = tmp_path / "bridge.sqlite"
    store = _store.build_store(db)
    _store.write_meta(store, "job-1", {"job_id": "job-1"})
    ticket = _store.build_approval_queue(store).create_ticket(task_id="job-1", prompt="approve?")
    assert cli.main(["approve", ticket.approval_id, "--reason", "reviewed", "--db", str(db)]) == 0
    assert _store.read_meta(store, "job-1")["manual_interventions"][0]["reason"] == "reviewed"


def test_approval_for_non_bridge_task_does_not_create_meta(tmp_path):
    db = tmp_path / "bridge.sqlite"
    store = _store.build_store(db)
    ticket = _store.build_approval_queue(store).create_ticket(task_id="foreign-task", prompt="approve?")
    assert cli.main(["approve", ticket.approval_id, "--db", str(db)]) == 0
    assert _store.read_meta(store, "foreign-task") is None


def test_intervention_record_failure_only_warns(monkeypatch, capsys):
    def fail(*_args, **_kwargs):
        raise RuntimeError("busy")

    monkeypatch.setattr(_store, "record_intervention", fail)
    cli._record_intervention(object(), "job-1", "apply")
    assert "warning: action succeeded" in capsys.readouterr().err


def test_stale_meta_writer_preserves_concurrent_intervention(tmp_path):
    store = _store.build_store(tmp_path / "bridge.sqlite")
    _store.write_meta(store, "job-1", {"job_id": "job-1", "status_detail": "old"})
    stale = _store.read_meta(store, "job-1")
    _store.record_intervention(store, "job-1", "apply")
    stale["status_detail"] = "new"
    _store.write_meta(store, "job-1", stale)

    meta = _store.read_meta(store, "job-1")
    assert meta["status_detail"] == "new"
    assert [item["kind"] for item in meta["manual_interventions"]] == ["apply"]


def test_report_uses_stable_completion_time_after_later_metadata_update(tmp_path, capsys):
    db = tmp_path / "bridge.sqlite"
    store = _store.build_store(db)
    registry = _store.build_job_registry(store)
    completed = datetime(2026, 11, 10, 23, tzinfo=UTC).timestamp()
    registry.write("job-1", "task", tool_name="codex", status="done")
    _store.write_meta(store, "job-1", {
        "job_id": "job-1", "completed_at": completed,
        "updated_at": datetime(2026, 11, 12, tzinfo=UTC).timestamp(),
    })

    assert cli.main([
        "autonomy", "--since", "2026-11-10", "--until", "2026-11-11",
        "--json", "--db", str(db),
    ]) == 0
    assert json.loads(capsys.readouterr().out)["finished_jobs"] == 1


def test_relaunch_marks_recent_matching_failed_job(tmp_path):
    store = _store.build_store(tmp_path / "bridge.sqlite")
    registry = _store.build_job_registry(store)
    now = datetime(2026, 11, 10, 12, tzinfo=UTC).timestamp()
    registry.write("old", "same brief", tool_name="codex", status="failed")
    _store.write_meta(store, "old", {
        "job_id": "old", "cwd": "/repo", "completed_at": now - 60,
        "brief_hash": _store.brief_hash("same brief"),
    })

    assert _store.record_relaunch(store, "new", "same brief", "/repo", now=now) == "old"
    assert _store.read_meta(store, "old")["manual_interventions"][0]["kind"] == "relaunch"


def test_relaunch_ignores_old_or_different_jobs(tmp_path):
    store = _store.build_store(tmp_path / "bridge.sqlite")
    registry = _store.build_job_registry(store)
    now = datetime(2026, 11, 10, 12, tzinfo=UTC).timestamp()
    registry.write("old", "same brief", tool_name="codex", status="failed")
    _store.write_meta(store, "old", {"job_id": "old", "cwd": "/repo", "completed_at": now - 86401})
    assert _store.record_relaunch(store, "new", "same brief", "/repo", now=now) is None
    assert not _store.read_meta(store, "old").get("manual_interventions")
