"""Prototype coverage and hostile inputs for the routine-auto tier."""

import asyncio
import os
from dataclasses import replace
from pathlib import Path

import pytest
from lazybridge.engines.coding import ApprovalRequest

import _code_bridge_fakes as fakes
from lazytools.code_bridge import _concurrency, _jobs, _routine, _store, cli
from lazytools.code_bridge._approvals import BridgeGate
from lazytools.code_bridge._policy import CODING_RULES

PYTHON = "C:/ProgramData/spyder-6/python.exe"
SOURCE = "C:/ProgramData/InvestmentCommittee/live/db/macro_releases.sqlite"
BRIDGE_TEMP = "C:/Users/Administrator/AppData/Local/Temp/lazytools-bridge"
WRAPPER = '"C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command '


@pytest.fixture
def policy():
    path = _concurrency.config_path()
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f'\n[routine_auto]\npython_executable = "{PYTHON}"\nscratch_root = "D:/Temp"\n')
    return replace(_routine.load_policy(), bridge_temp_root=BRIDGE_TEMP)


POSITIVE = [
    ("gh pr view 123 --json state,mergeable", "gh-pr-read"),
    ("gh pr checks 123 --watch --interval 60", "gh-pr-read"),
    ("gh pr view 123 --json reviews --jq '.reviews[] | .state'", "gh-pr-read"),
    ("gh pr list --head feat/ticket --json number,state", "gh-pr-list"),
    ("gh run view 432 --log-failed", "gh-run-read"),
    ("gh run watch 432 --exit-status", "gh-run-read"),
    ("gh run list --branch feat/ticket --limit 5", "gh-run-list"),
    ("gh api repos/selvaz/LazyTools/actions/jobs/123/logs", "gh-job-logs"),
    *[(f"gh api repos/selvaz/LazyTools/{kind}/123/{sub}", "gh-comments-reviews")
      for kind in ("pulls", "issues") for sub in ("comments", "reviews")],
    (f"gh pr edit 123 --body-file '{BRIDGE_TEMP}/job/body.md'", "gh-pr-body"),
    (f"gh pr edit 123 --body-file '{BRIDGE_TEMP}/job/body with spaces.md'", "gh-pr-body"),
    ("git add -- src/lazytools/a.py tests/test_a.py", "git-add-paths"),
    ("git add -- 'docs/a file.md'", "git-add-paths"),
    *[(f"git push origin {prefix}/ticket", "git-push-feature") for prefix in ("feat", "fix", "docs", "release")],
    ("git fetch origin", "git-fetch"),
    *[(f"git merge --no-edit origin/{branch}", "git-merge") for branch in ("main", "feat/ticket", "fix/ticket", "docs/ticket", "research/ticket")],
    (f"'{PYTHON}' -m pytest tests -q --basetemp D:/Temp/bridge-tests", "pytest-scratch"),
    (f"& '{PYTHON}' -B -m pytest --basetemp D:/Temp/bridge-tests tests", "pytest-scratch"),
    (f"$env:PYTHONPATH=(Get-Location).Path; & '{PYTHON}' -m pytest -q --basetemp D:/Temp/bridge-tests", "pytest-scratch"),
    (f"& {PYTHON} -B scripts/verify_macro_release.py --source {SOURCE} --destination-root D:/Temp", "macro-verifier-scratch"),
    (f"{PYTHON} -B scripts/verify_macro_release.py --source {SOURCE} --destination-root D:/Temp --families cpi,pmi", "macro-verifier-scratch"),
    ("git p'u'\"s\"h origin feat/ticket", "git-push-feature"),
]

NEGATIVE = [
    "gh pr view 123; gh pr merge 123",
    "gh pr view 123 && gh pr merge 123",
    "gh pr view 123 | Stop-Process",
    "gh pr view 123 --jq '.id; .state'",
    "gh pr merge 123",
    "git push origin main",
    "git push origin master",
    "git push --force origin feat/ticket",
    "git push origin feat/ticket --force",
    "git push origin feat/ticket:main",
    "git push origin feat/ticket main",
    f"{PYTHON} -m pytest --basetemp C:/outside/test",
    f"{PYTHON} -m pytest --basetemp D:/Temp-evil/test",
    f"{PYTHON} -m pytest --basetemp D:/Temp/../outside/test",
    f"{PYTHON} -m pytest --basetemp D:/Temp/safe --basetemp C:/outside",
    f"{PYTHON} -m pytest --basetemp D:/Temp/safe --basetemp=C:/outside",
    f"{PYTHON} -m pytest --basetemp D:/Temp/safe --basetem C:/outside",
    f"{PYTHON} -m pytest -- --basetemp D:/Temp/safe",
    f"{PYTHON} -m pytest --basetemp D:/Temp",
    f"{PYTHON} -B scripts/verify_macro_release.py --source {SOURCE} --destination-root C:/outside",
    f"{PYTHON} -B scripts/verify_macro_release.py --source {SOURCE} --destination-root D:/Temp/../outside",
    f"{PYTHON} -B scripts/verify_macro_release.py --source {SOURCE} --destination-root 'D:/Temp --families cpi'",
    "gh api graphql -f query='mutation { deleteIssue }'",
    "gh api repos/selvaz/LazyTools/pulls/123/comments -f body=hello",
    "Stop-Process -Id 123",
    f"{PYTHON} -c 'import curl_cffi; impersonate=chrome'",
    "curl_cffi --impersonate chrome https://example.com",
    "git add -- 'file;Stop-Process'",
    "gh pr edit 123 --body-file C:/elsewhere/body.md",
    f"gh pr edit 123 --body-file {BRIDGE_TEMP}/body.md --base main",
    "& 'git push origin feat/ticket'",
    f"gh pr edit 123 --body-file {BRIDGE_TEMP}/../body.md",
    "gh pr view 123\ngh pr merge 123",
    "gh pr view 123 --jq '$(Stop-Process)'",
    "gh pr view 123 `; Stop-Process",
    f"$env:PYTHONPATH = (Get-Location).Path; {PYTHON} -m pytest --basetemp D:/Temp/test",
    f"{PYTHON} -m pytest --basetemp D:/Temp/test > C:/outside/out",
]


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("command,rule_id", POSITIVE)
def test_prototype_positive_examples(policy, command, rule_id, wrapped):
    command = WRAPPER + '"' + command + '"' if wrapped else command
    # JSON transports can double backslashes; every run is normalised.
    assert policy.match(command.replace("\\", "\\\\")) == rule_id


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("command", NEGATIVE)
def test_negative_examples(policy, command, wrapped):
    command = WRAPPER + '"' + command + '"' if wrapped else command
    assert policy.match(command) is None


@pytest.mark.parametrize("command", NEGATIVE)
def test_negative_examples_reach_human_gate(policy, command, tmp_path):
    prompts = []

    class Channel:
        async def ask(self, prompt):
            prompts.append(prompt)
            return False

    store = _store.build_store(tmp_path / "jobs.sqlite")
    gate = BridgeGate(channel=Channel(), rules=CODING_RULES,
                      routine=_routine.RoutineAuto(policy, store, _store.build_approval_queue(store), "job"))
    request = ApprovalRequest(provider="codex", kind="command", name="codex-shell",
                              arguments={"command": command}, raw={"command": command})
    assert asyncio.run(gate(request)).action == "deny"
    assert prompts
    assert list(store.items(prefix=_store.APPROVAL_PREFIX)) == []


def test_auto_approval_is_consumed_audited_ticket(policy, tmp_path):
    store = _store.build_store(tmp_path / "jobs.sqlite")
    queue = _store.build_approval_queue(store)

    class Channel:
        async def ask(self, prompt):
            pytest.fail("routine command reached human channel")

    gate = BridgeGate(channel=Channel(), rules=CODING_RULES,
                      routine=_routine.RoutineAuto(policy, store, queue, "job"))
    command = WRAPPER + '"git push origin feat/ticket"'
    request = ApprovalRequest(provider="codex", kind="command", name="codex-shell",
                              arguments={"command": command}, raw={"command": command})
    for _ in range(2):
        assert asyncio.run(gate(request)).action == "allow"
    tickets = [raw for _, raw in store.items(prefix=_store.APPROVAL_PREFIX)]
    assert len(tickets) == 2
    for ticket in tickets:
        assert ticket["status"] == "approved"
        assert ticket["actor"] == ticket["approved_by"] == "routine-auto"
        assert ticket["matched_rule_id"] == "git-push-feature"
        assert ticket["consumed_at"]
        assert ticket["command"] == command
    assert gate.log[-1].tier == "routine-auto"
    assert queue.list_pending_tickets() == []


def test_unmatched_command_still_files_pending_ticket(policy, tmp_path, monkeypatch):
    monkeypatch.setenv("LAZYBRIDGE_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    script = fakes.install(monkeypatch)
    script.request_approval = True
    script.approval_overrides = {"arguments": {"command": "Stop-Process -Id 123"}}
    seen = []

    class Channel:
        async def ask(self, prompt):
            store = _store.build_store(tmp_path / "jobs.sqlite")
            ticket = _store.build_approval_queue(store).create_ticket(task_id="job", prompt=prompt)
            seen.append(ticket)
            return False

    monkeypatch.setattr(_jobs, "_store_channel", lambda *a: Channel())
    result = _jobs.run_job(engine_name="codex", cwd=str(tmp_path), root=str(tmp_path),
                           task="x", db_path=tmp_path / "jobs.sqlite")
    assert result.status == "failed"
    assert seen[0].status == "pending"
    assert "Stop-Process" in seen[0].prompt


def test_job_uses_routine_tier_by_default(policy, tmp_path, monkeypatch):
    monkeypatch.setenv("LAZYBRIDGE_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    script = fakes.install(monkeypatch)
    script.request_approval = True
    script.approval_overrides = {"arguments": {"command": WRAPPER + '"git fetch origin"'}}
    result = _jobs.run_job(engine_name="codex", cwd=str(tmp_path), root=str(tmp_path),
                           task="x", db_path=tmp_path / "jobs.sqlite")
    assert result.status == "done"
    store = _store.build_store(tmp_path / "jobs.sqlite")
    tickets = [raw for _, raw in store.items(prefix=_store.APPROVAL_PREFIX)]
    assert tickets[0]["approved_by"] == "routine-auto"
    assert tickets[0]["matched_rule_id"] == "git-fetch"


def test_disable_and_extension_config(policy, tmp_path):
    extra = tmp_path / "routine-extra.toml"
    extra.write_text('[[rules]]\nid = "git-status"\npattern = "git status --short"\n', encoding="utf-8")
    with _concurrency.config_path().open("a", encoding="utf-8") as stream:
        stream.write(f"allowlist = '{extra.as_posix()}'\n")
    extended = _routine.load_policy()
    assert extended.match("git status --short") == "git-status"
    assert extended.match("git status --short; Stop-Process") is None
    assert _routine.load_policy(enabled=False).match("git fetch origin") is None
    assert _routine.load_policy(scratch_root="E:/Scratch").match(f"{PYTHON} -m pytest --basetemp D:/Temp/test") is None
    assert _routine.load_policy(scratch_root="E:/Scratch").match(f"{PYTHON} -m pytest --basetemp E:/Scratch/test") == "pytest-scratch"


@pytest.mark.parametrize("config", ['enabled = "yes"', 'scratch_root = "relative"', 'unknown = true'])
def test_bad_config_refused_before_spawn(tmp_path, monkeypatch, config):
    path = tmp_path / "bad.toml"
    path.write_text("[routine_auto]\n" + config, encoding="utf-8")
    monkeypatch.setenv(_concurrency.CONFIG_ENV, str(path))
    monkeypatch.setattr(cli, "_spawn_detached", lambda *a: pytest.fail("spawned"))
    assert cli.main(["run", "--detach", "--engine", "codex", "--cwd", str(tmp_path), "--task", "x"]) == 2


def test_symlink_output_escape_is_not_allowed(tmp_path):
    root = tmp_path / "scratch"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "escape").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    assert not _routine._inside((root / "escape" / "output").as_posix(), root.as_posix())


def test_packaged_rules_present():
    assert Path(_routine.__file__).with_name("routine_allowlist.toml").is_file()
    assert _routine.load_policy().enabled
    assert _routine.load_policy().scratch_root == ("D:/Temp" if os.name == "nt" else _routine._slash(_routine.tempfile.gettempdir()))
