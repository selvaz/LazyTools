"""Prototype coverage and hostile inputs for the routine-auto tier."""

import asyncio
import os
import re
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from lazybridge.engines.coding import ApprovalRequest

import _code_bridge_fakes as fakes
from lazytools.code_bridge import _concurrency, _jobs, _routine, _routine_commands, _store, cli
from lazytools.code_bridge._approvals import BridgeGate
from lazytools.code_bridge._policy import CODING_RULES

PYTHON = "C:/ProgramData/spyder-6/python.exe"
SOURCE = "C:/ProgramData/InvestmentCommittee/live/db/macro_releases.sqlite"
BRIDGE_TEMP = "C:/Users/Administrator/AppData/Local/Temp/lazytools-bridge"
WRAPPER = '"C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command '


@pytest.fixture
def policy(monkeypatch, routine_repo):
    # The prototype fixtures are Windows commands, even on the Linux CI host.
    monkeypatch.setattr(_routine, "os", SimpleNamespace(name="nt"))
    path = _concurrency.config_path()
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f'\n[routine_auto]\npython_executable = "{PYTHON}"\nscratch_root = "D:/Temp"\n')
    return replace(_routine.load_policy(), bridge_temp_root=BRIDGE_TEMP, cwd=routine_repo,
                   job_id="routine-test", origin_repo="selvaz/lazytools")


@pytest.fixture(scope="module")
def routine_repo(tmp_path_factory):
    path = tmp_path_factory.mktemp("routine-repo")
    for args in (("init",), ("symbolic-ref", "HEAD", "refs/heads/feat/routine")):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    return path


POSITIVE = [
    ("gh pr view 123 --json state,mergeable", "gh-pr-read"),
    ("gh pr checks 123 --watch --interval 60", "gh-pr-read"),
    ("gh pr view 123 --json reviews,comments", "gh-pr-read"),
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
    *[(f"git push origin {prefix}/ticket", "git-push-feature") for prefix in ("feat", "fix", "docs")],
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
    *[f"git add -- {path}" for path in (
        ".git/config", "sub/.GIT/config", "sub/.npmrc", ".NETRC", "sub/.pypirc",
        "sub/cert.PFX", "cert.p12", "auth.token", "sub/keys.JKS",
    )],
    "git push origin feat/-x",
    "git merge --no-edit origin/feat/-x",
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
    f"{PYTHON} -m pytest --basetemp D:/Temp/lazytools-bridge",
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
    wrapper = WRAPPER if os.name == "nt" else WRAPPER.replace("\\", "/")
    command = wrapper + '"' + command + '"' if wrapped else command
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
    command = WRAPPER.replace("\\", "/") + '"git push origin feat/ticket"'
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
    script.approval_overrides = {"arguments": {"command": WRAPPER.replace("\\", "/") + '"git fetch origin"'}}
    result = _jobs.run_job(engine_name="codex", cwd=str(tmp_path), root=str(tmp_path),
                           task="x", db_path=tmp_path / "jobs.sqlite")
    assert result.status == "done"
    store = _store.build_store(tmp_path / "jobs.sqlite")
    tickets = [raw for _, raw in store.items(prefix=_store.APPROVAL_PREFIX)]
    assert tickets[0]["approved_by"] == "routine-auto"
    assert tickets[0]["matched_rule_id"] == "git-fetch"


def test_disable_and_extension_config(policy, tmp_path):
    extra = tmp_path / "routine-extra.toml"
    extra.write_text('[[rules]]\nid = "git-status"\npattern = "git status --short"\nvalidator = "git-status"\n', encoding="utf-8")
    with _concurrency.config_path().open("a", encoding="utf-8") as stream:
        stream.write(f"allowlist = '{extra.as_posix()}'\n")
    extended = _routine.load_policy()
    assert extended.match("git status --short") == "git-status"
    assert extended.match("git status --short; Stop-Process") is None
    assert _routine.load_policy(enabled=False).match("git fetch origin") is None
    assert _routine.load_policy(scratch_root="E:/Scratch").match(f"{PYTHON} -m pytest --basetemp D:/Temp/test") is None
    configured = replace(_routine.load_policy(scratch_root="E:/Scratch"), cwd=policy.cwd)
    assert configured.match(f"{PYTHON} -m pytest --basetemp E:/Scratch/test") == "pytest-scratch"


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


def test_windows_backslash_runs_still_normalise(policy, monkeypatch):
    monkeypatch.setattr(_routine, "os", SimpleNamespace(name="nt"))
    command = WRAPPER + '"gh pr view 123 --json state"'
    assert policy.match(command.replace("\\", "\\\\")) == "gh-pr-read"


@pytest.mark.parametrize("command", [
    f"{PYTHON} -m pytest --basetemp '/tmp\\outside'",
    f"{PYTHON} -B scripts/verify_macro_release.py --source {SOURCE} --destination-root '/tmp\\outside'",
    "gh pr edit 123 --body-file '/tmp/lazytools-bridge\\outside.md'",
])
def test_posix_literal_backslash_is_not_a_separator(policy, monkeypatch, command):
    monkeypatch.setattr(_routine, "os", SimpleNamespace(name="posix"))
    policy = replace(policy, scratch_root="/tmp", bridge_temp_root="/tmp/lazytools-bridge")
    assert policy.match(command) is None
    assert policy.match(command.replace("\\", "/")) is not None


def test_posix_rejects_windows_scratch_root_and_paths(policy, monkeypatch):
    monkeypatch.setattr(_routine, "os", SimpleNamespace(name="posix"))
    with pytest.raises(ValueError, match="absolute"):
        _routine.load_policy(scratch_root="D:/Temp")
    assert not _routine._inside("D:/Temp/output", "D:/Temp")


PYTEST_BYPASSES = [
    "--junitxml=C:/Windows/x.xml", "--junitxml C:/Windows/x.xml",
    "--log-file=C:/Windows/x.log", "--log-file C:/Windows/x.log",
    "--debug=C:/Windows/debug.log", "--debug C:/Windows/debug.log",
    "-oaddopts=-pevil", "-o addopts=-pevil", "--override-in addopts=-pevil",
    "--override-ini=addopts=-pevil", "--override-ini addopts=-pevil",
    "-p evilplugin", "-pevilplugin", "-pno:cacheprovider",
    "-c C:/other/pytest.ini", "-cC:/other/pytest.ini",
    "--rootdir C:/", "--rootdir=C:/", "--root C:/",
    "--confcutdir=C:/", "--confcutdir C:/", "--conf C:/",
    "--pyargs os", "--py os", "--pyargs=os",
    "--import-mode=importlib", "--cache-clear", "--co", "--collect-only",
    "--basetemp=C:/outside", "--basetem C:/outside", "-- --basetemp C:/outside",
    "../outside/test_x.py", "C:/outside/test_x.py", "scripts/test_x.py", ".", "-A",
    "'tests/../outside.py'", "'tests/*.py'", "'tests/[ab].py'",
]


@pytest.mark.parametrize("flags", PYTEST_BYPASSES)
def test_review_pytest_bypasses_are_denied(policy, flags):
    assert policy.match(f"{PYTHON} -m pytest --basetemp D:/Temp/rv220-fresh-nonexistent {flags}") is None


@pytest.mark.parametrize("flags", ["-q", "-x", "-v", "-vv", "-rN", "-ra", "--tb=short", "-k 'test_alpha or test_beta'", "-m 'slow and not network'", "--maxfail=2", "-p no:cacheprovider", "--no-header", "-s"])
def test_review_pytest_flag_allowlist(policy, flags):
    assert policy.match(f"{PYTHON} -m pytest tests/test_x.py::TestClass::test_y --basetemp D:/Temp/rv220-fresh-nonexistent {flags}") == "pytest-scratch"


def test_bypass_corpus_detects_a_permissive_pytest_validator(policy, monkeypatch):
    def assert_denied():
        for flags in PYTEST_BYPASSES:
            assert policy.match(f"{PYTHON} -m pytest --basetemp D:/Temp/rv220-fresh-nonexistent {flags}") is None

    assert_denied()
    monkeypatch.setattr(_routine_commands, "pytest_safe", lambda *a: True)
    with pytest.raises(AssertionError):
        assert_denied()


@pytest.fixture
def native_policy(policy, tmp_path, monkeypatch):
    monkeypatch.setattr(_routine, "os", SimpleNamespace(name=os.name))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    bridge = scratch / "lazytools-bridge"
    bridge.mkdir()
    return replace(policy, scratch_root=scratch.as_posix(), bridge_temp_root=bridge.as_posix(), job_id="job-123")


def test_basetemp_is_fresh_or_job_owned_and_never_bridge_state(native_policy):
    root = Path(native_policy.scratch_root)

    def check(path):
        return native_policy.match(f"{PYTHON} -m pytest tests --basetemp '{path.as_posix()}'")
    assert check(root / "fresh-unique") == "pytest-scratch"
    unrelated = root / "existing"
    unrelated.mkdir()
    assert check(unrelated) is None
    assert check(root) is None
    assert check(Path(native_policy.bridge_temp_root)) is None
    assert check(Path(native_policy.bridge_temp_root) / "new-child") is None
    mine = root / "bridge-job-123-unique"
    mine.mkdir()
    assert check(mine) == "pytest-scratch"
    other = root / "bridge-job-1234-other"
    other.mkdir()
    assert check(other) is None
    other = root / "bridge-different-job-owned"
    other.mkdir()
    assert check(other) is None
    prefix_only = root / "bridge-job-123-"
    prefix_only.mkdir()
    assert check(prefix_only) is None


def test_basetemp_job_id_boundary_is_exact(native_policy):
    other = Path(native_policy.scratch_root) / "bridge-job-123-other-unique"
    other.mkdir()
    command = f"{PYTHON} -m pytest tests --basetemp '{other.as_posix()}'"
    assert native_policy.match(command) is None
    assert replace(native_policy, job_id="job-123-other").match(command) == "pytest-scratch"


@pytest.mark.parametrize("char", [
    "\u00a0", "\u1680", "\u2000", "\u2007", "\u2028", "\u2029", "\u202f", "\u205f", "\u3000",
    "\0", "\x1b", "\x7f", "\x85", "\v", "\f", "\r", "\n", "\u200b",
])
@pytest.mark.parametrize("shape", ["{char}git fetch origin", "git fetch origin{char}", 'powershell -Command "git fetch origin{char}"'])
def test_unicode_whitespace_and_controls_are_never_trimmed_or_approved(policy, char, shape):
    command = shape.format(char=char)
    assert _routine.normalise(command) is None
    assert policy.match(command) is None


def test_only_ascii_spaces_and_tabs_can_be_trimmed(policy):
    assert policy.match(" \tgit fetch origin\t ") == "git-fetch"
    assert policy.match(' \tpowershell -Command \t"git fetch origin"\t ') == "git-fetch"


@pytest.mark.parametrize("part", ["lazytools-bridge.", "lazytools-bridge ", "LAZYTO~1", "safe./child", "safe /child", "safe~1/child"])
@pytest.mark.parametrize("command", [
    f"{PYTHON} -m pytest tests --basetemp 'D:/Temp/{{part}}'",
    f"{PYTHON} -B scripts/verify_macro_release.py --source {SOURCE} --destination-root 'D:/Temp/{{part}}'",
    f"gh pr edit 123 --body-file '{BRIDGE_TEMP}/{{part}}/body.md'",
    "git add -- 'src/{part}/file.py'",
    f"{PYTHON} -m pytest 'tests/{{part}}/test_a.py' --basetemp D:/Temp/fresh",
])
def test_path_aliases_are_rejected_by_all_path_validators(policy, part, command):
    assert policy.match(command.format(part=part)) is None


@pytest.mark.parametrize("root", ["D:/Temp.", "D:/Temp ", "D:/TEMP~1"])
def test_containment_rejects_alias_in_root(policy, root):
    assert not _routine._inside(root + "/fresh", root)


@pytest.mark.parametrize("endpoint", ["actions/jobs/123/logs", "pulls/123/comments", "issues/123/reviews"])
def test_gh_api_requires_a_known_origin(policy, endpoint):
    command = f"gh api repos/selvaz/LazyTools/{endpoint}"
    assert policy.match(command) is not None
    assert replace(policy, origin_repo=None).match(command) is None


def test_pytest_test_roots_are_configurable(native_policy):
    policy = replace(native_policy, test_roots=("checks",))
    base = (Path(policy.scratch_root) / "fresh").as_posix()
    assert policy.match(f"{PYTHON} -m pytest checks/test_a.py --basetemp {base}") == "pytest-scratch"
    assert policy.match(f"{PYTHON} -m pytest tests/test_a.py --basetemp {base}") is None


REVIEW_GIT_GH_BYPASSES = [
    "git add -- .", "git add -- ./", "git add -- -A", "git add -- -f",
    "git add -- ../outside", "git add -- tests/../../outside", "git add -- C:/outside",
    "git add -- '/absolute'", "git add -- 'tests/*.py'", "git add -- 'tests/[ab].py'",
    "git add -- .env", "git add -- .env.production", "git add -- tests/.env.local",
    "git add -- keys/server.pem", "git add -- keys/server.KEY", "git add -- keys/id_rsa",
    "git add -- tests/secrets.json", "git add -- config/CREDENTIALS", "git add -- safe.py .env",
    "git push origin release/x", "git push origin feat/.hidden", "git push origin feat/a..b",
    "git push origin feat/x/", "git push origin feat/..",
    "git merge --no-edit origin/feat/.hidden", "git merge --no-edit origin/feat/a..b",
    "gh api repos/selvaz/./actions/jobs/1/logs", "gh api repos/selvaz/../actions/jobs/1/logs",
    "gh api repos/selvaz/A..B/pulls/1/comments",
    "gh pr view 1 -R other/repo", "gh pr view 1 --repo other/repo",
    "gh pr view 1 --repo=other/repo", "gh run view 1 -R other/repo",
    "gh pr list --head feat/x --repo other/repo",
]


@pytest.mark.parametrize("command", REVIEW_GIT_GH_BYPASSES)
def test_review_git_gh_bypasses_stay_pending_in_gate(policy, tmp_path, command):
    store = _store.build_store(tmp_path / "store.sqlite")
    prompts = []

    class Channel:
        async def ask(self, prompt):
            prompts.append(prompt)
            return False

    gate = BridgeGate(channel=Channel(), rules=CODING_RULES,
                      routine=_routine.RoutineAuto(policy, store, _store.build_approval_queue(store), "job"))
    assert policy.match(command) is None
    for kind, name in (("command", "codex-shell"), ("tool", "Bash")):
        request = ApprovalRequest(provider="codex", kind=kind, name=name,
                                  arguments={"command": command}, raw={"command": command})
        assert asyncio.run(gate(request)).action == "deny"
    assert len(prompts) == 2


@pytest.mark.parametrize("branch", ["main", "master"])
def test_merge_on_protected_current_branch_is_manual(policy, branch):
    assert policy.cwd is not None
    subprocess.run(["git", "-C", str(policy.cwd), "symbolic-ref", "HEAD", f"refs/heads/{branch}"], check=True)
    try:
        assert policy.match("git merge --no-edit origin/main") is None
    finally:
        subprocess.run(["git", "-C", str(policy.cwd), "symbolic-ref", "HEAD", "refs/heads/feat/routine"], check=True)


@pytest.mark.parametrize("value", ["selvaz/LazyTools", "github.com/selvaz/LazyTools", "https://github.com/selvaz/LazyTools"])
def test_gh_repo_override_can_only_name_origin(policy, value):
    assert policy.match(f"gh pr view 1 --repo {value}") == "gh-pr-read"
    assert policy.match(f"gh pr view 1 --repo={value}") == "gh-pr-read"


@pytest.mark.parametrize("body", ["", "[other]\nx=1\n", '[[rules]]\nid="bare"\npattern=".*"\n', '[[rules]]\nid="bad"\npattern=".*"\nvalidator="anything"\n'])
def test_extensions_require_rules_and_explicit_known_validator(policy, tmp_path, body):
    extra = tmp_path / "extension.toml"
    extra.write_text(body, encoding="utf-8")
    with _concurrency.config_path().open("a", encoding="utf-8") as stream:
        stream.write(f"allowlist = '{extra.as_posix()}'\n")
    with pytest.raises(ValueError):
        _routine.load_policy()


def test_broad_extension_regex_cannot_override_pytest_capabilities(policy):
    extra = _routine.RoutineRule("broad-pytest", re.compile(".*"), "pytest")
    policy = replace(policy, rules=(extra,))
    for flags in PYTEST_BYPASSES:
        assert policy.match(f"{PYTHON} -m pytest --basetemp D:/Temp/rv220-fresh-nonexistent {flags}") is None
    assert policy.match("git push --force origin main") is None


def test_unknown_top_level_config_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text("[rountine_auto]\nenabled=true\n", encoding="utf-8")
    monkeypatch.setenv(_concurrency.CONFIG_ENV, str(path))
    for loader in (_routine.load_policy, _concurrency.load_config):
        with pytest.raises(ValueError, match="top-level"):
            loader()


def test_test_roots_setting_is_loaded_and_enforced(policy):
    with _concurrency.config_path().open("a", encoding="utf-8") as stream:
        stream.write('test_roots = ["checks"]\n')
    configured = replace(_routine.load_policy(), cwd=policy.cwd)
    prefix = f"{PYTHON} -m pytest --basetemp D:/Temp/rv220-fresh-nonexistent"
    assert configured.match(prefix + " checks/test_a.py") == "pytest-scratch"
    assert configured.match(prefix + " tests/test_a.py") is None


@pytest.mark.parametrize("url", ["https://github.com/selvaz/LazyTools.git", "git@github.com:selvaz/LazyTools.git", "ssh://git@github.com/selvaz/LazyTools.git"])
def test_job_origin_is_bound_from_git_remote(policy, monkeypatch, url):
    monkeypatch.setattr(_routine_commands, "git", lambda *a: url)
    bound = policy.for_job(policy.cwd, "job-123")
    assert bound.origin_repo == "selvaz/lazytools"
    assert bound.job_id == "job-123"
    assert bound.match("gh pr view 1 -R selvaz/LazyTools") == "gh-pr-read"
    assert bound.match("gh pr view 1 -R other/repo") is None


def test_existing_directories_with_hidden_sensitive_files_are_not_staged(policy, tmp_path):
    directory = tmp_path / "data"
    directory.mkdir()
    (directory / ".env").write_text("fixture", encoding="utf-8")
    policy = replace(policy, cwd=tmp_path)
    assert policy.match("git add -- data") is None


def test_legacy_session_grant_cannot_bypass_routine_git_add_validation(policy, tmp_path):
    class Channel:
        answer = True

        async def ask(self, prompt):
            return self.answer

    channel = Channel()
    gate = BridgeGate(channel=channel, rules=CODING_RULES)

    def request(command):
        return ApprovalRequest(provider="codex", kind="command", name="codex-shell",
                               arguments={"command": command}, raw={"command": command})

    assert asyncio.run(gate(request("git add -- safe.py"))).action == "allow_session"
    store = _store.build_store(tmp_path / "store.sqlite")
    gate.routine = _routine.RoutineAuto(policy, store, _store.build_approval_queue(store), "job")
    channel.answer = False
    assert asyncio.run(gate(request("git add -- .env"))).action == "deny"


def test_test_path_symlink_cannot_escape_configured_test_root(native_policy):
    cwd = Path(native_policy.scratch_root).parent
    tests = cwd / "tests"
    tests.mkdir()
    source = cwd / "src"
    source.mkdir()
    (source / "test_a.py").write_text("# fixture", encoding="utf-8")
    try:
        (tests / "link").symlink_to(source, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    policy = replace(native_policy, cwd=cwd)
    base = (Path(policy.scratch_root) / "fresh").as_posix()
    assert policy.match(f"{PYTHON} -m pytest tests/link/test_a.py --basetemp {base}") is None


@pytest.mark.parametrize("command", ["git add -- safe.py", "git merge --no-edit origin/main", "gh pr view 1 -R selvaz/LazyTools"])
def test_different_execution_cwd_cannot_borrow_job_scope(policy, tmp_path, command):
    store = _store.build_store(tmp_path / "store.sqlite")
    routine = _routine.RoutineAuto(policy, store, _store.build_approval_queue(store), "job")
    request = ApprovalRequest(provider="codex", kind="command", name="codex-shell", cwd=str(tmp_path),
                              arguments={"command": command}, raw={"command": command})
    assert routine.match(request) is None
