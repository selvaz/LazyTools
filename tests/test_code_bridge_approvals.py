"""Structural approvals: the 17/10/2026 tickets as fixtures, plus adversarial cases.

Every decision here goes through the REAL ``BridgeGate`` + ``CODING_RULES``,
including ``TieredGate``'s own ``_decide`` (session grants, human prompts),
with only the human replaced by a recording channel.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from lazybridge.engines.coding import ApprovalRequest
from lazybridge.ext.approval import TieredGate

from lazytools.code_bridge import _engines
from lazytools.code_bridge._approvals import (
    BridgeGate,
    ClassifiedRule,
    is_readonly_gh_query,
    is_simple_git_add,
    is_simple_git_commit,
    unwrap_powershell,
)
from lazytools.code_bridge._policy import CODING_RULES

REPO = "selvaz/investmentcommittee"


class RecordingChannel:
    name = "test"

    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    async def ask(self, prompt: str) -> bool:
        self.prompts.append(prompt)
        return self.answer


def codex(command: str, *, cwd: str = "C:/repo") -> ApprovalRequest:
    params = {"command": command, "cwd": cwd, "reason": "needs network"}
    return ApprovalRequest(provider="codex", kind="command", name="codex-shell", arguments=params, cwd=cwd, raw=params)


def claude_bash(command: str, *, cwd: str = "C:/repo") -> ApprovalRequest:
    return ApprovalRequest(provider="claude-code", kind="tool", name="Bash", arguments={"command": command}, cwd=cwd)


def tier(request: ApprovalRequest) -> str:
    rule = BridgeGate(channel=RecordingChannel(), rules=CODING_RULES)._match(request)
    return rule.tier if rule is not None else "unmatched"


def decide(gate: BridgeGate, request: ApprovalRequest) -> tuple[str, bool]:
    """``(action, asked_a_human)`` for one request through the full gate."""
    channel = gate.channel
    before = len(channel.prompts)
    decision = asyncio.run(gate(request))
    return decision.action, len(channel.prompts) > before


# --------------------------------------------------------------------------- #
# 17/10 fixtures -- Codex escalations
# --------------------------------------------------------------------------- #

CODEX_PYTEST_ONE = 'powershell.exe -Command "& C:\\ProgramData\\spyder-6\\python.exe -m pytest -q tests\\test_x.py"'
CODEX_PYTEST_ALL = 'powershell.exe -Command "& C:\\ProgramData\\spyder-6\\python.exe -m pytest -q"'
CODEX_GIT_ADD = "powershell.exe -Command 'git add -- config/databases.toml src/ic/archive.py tests/test_archive.py'"
CODEX_READONLY_GH = [
    f"powershell.exe -Command 'gh pr checks 77 --repo {REPO} --json name,state,bucket,link'",
    f"powershell.exe -Command 'gh pr view 77 --repo {REPO} --json url,state,headRefName,baseRefName,body,comments,reviews'",
    f"powershell.exe -Command 'gh api repos/{REPO}/pulls/77/comments'",
    f"powershell.exe -Command 'gh run view 39028190857 --repo {REPO} --json status,conclusion,jobs'",
    f'powershell.exe -Command "gh pr view 77 --repo {REPO} --json reviews,comments --jq '
    "'{reviews: .reviews, comments: [.comments[] | {author: .author.login, body: .body}]}'\"",
]


@pytest.mark.parametrize("command", CODEX_READONLY_GH, ids=["4-pr-checks", "5-pr-view", "6-api", "7-run-view", "8-jq"])
def test_codex_fixtures_4_to_8_readonly_gh_are_auto_allowed(command):
    assert is_readonly_gh_query(command)
    gate = BridgeGate(channel=RecordingChannel(), rules=CODING_RULES)
    assert decide(gate, codex(command)) == ("allow", False)


@pytest.mark.parametrize("command", [CODEX_PYTEST_ONE, CODEX_PYTEST_ALL], ids=["1-pytest-file", "2-pytest-all"])
def test_codex_fixtures_1_2_pytest_still_ask_if_they_ever_escalate(command):
    """The fix for 1-2 is the sandbox's writable temp dir (no escalation at
    all, see test_codex_sandbox_*); an escalated pytest is still a human's call."""
    assert tier(codex(command)) == "ask"


def test_codex_fixture_3_git_add_is_session_granted_after_the_first_approval():
    gate = BridgeGate(channel=RecordingChannel(), rules=CODING_RULES)
    assert decide(gate, codex(CODEX_GIT_ADD)) == ("allow_session", True)
    # Same form, other files, later in the run: granted without asking.
    assert decide(gate, codex("powershell.exe -Command 'git add -- docs/x.md'")) == ("allow", False)
    assert decide(gate, codex("git add -A")) == ("allow", False)
    # ...but the grant covers ONLY a simple git add, nothing chained or different.
    assert decide(gate, codex("powershell.exe -Command 'git add -A; git push origin main'"))[1] is True
    assert decide(gate, codex("git add -A && git push origin feature"))[1] is True
    assert decide(gate, codex("git push origin feature"))[1] is True
    # A commit is its own grant: the first one still asks.
    assert decide(gate, codex("powershell.exe -Command 'git commit -m \"archive: fix\"'")) == ("allow_session", True)
    assert decide(gate, codex("git commit -m 'second'")) == ("allow", False)


#: Round-2 review (P1): Windows PowerShell 5.1 DROPS empty arguments when it
#: starts a native exe, so an empty argument makes the program see a different
#: argument list from the one classified. Any empty argument must ASK.
EMPTY_ARG_GH = [
    "powershell.exe -Command \"gh api --jq '' --template -XDELETE repos/a/b/git/refs/heads/topic\"",
    "gh api --jq '' --template -XDELETE repos/a/b/git/refs/heads/topic",
    'gh api --jq "" --template -XDELETE repos/a/b/git/refs/heads/topic',
    'gh pr view 1 --jq ""',
    "powershell.exe -Command 'gh pr view 1 --jq \"\"'",
    "gh pr view 1 --jq ''",
    "gh api repos/a/b/pulls --jq ''",
    "gh api repos/a/b/pulls --jq ''\"\"",  # empty after quote removal
]
EMPTY_ARG_GIT = [
    "git commit -m '' -m --amend",
    "powershell.exe -Command \"git commit -m '' -m --amend\"",
    "git commit -m '' -m --no-verify",
    "powershell.exe -Command \"git commit -m '' -m --no-verify\"",
    'git commit -m ""',
    "git commit -m ''",
    "powershell.exe -Command 'git commit -m \"\"'",
    "git add '' -f ignored.txt",
    'git add "" --force ignored.txt',
]


#: Round-3 review (P1): Windows PowerShell 5.1 does not escape a `"` inside an
#: argument when it builds the native command line, so a single-quoted value
#: holding double quotes is split again by the program. Fail-closed rule: no
#: argument may contain `"` `'` backtick `\` `$` after quote removal.
NESTED_QUOTE_GH = [
    "gh api --jq '\". -XDELETE -q\" .' repos/a/b/git/refs/heads/topic",
    "powershell.exe -Command 'gh api --jq ''\". -XDELETE -q\" .'' repos/a/b/git/refs/heads/topic'",
    'powershell.exe -Command "gh api --jq \'\\". -XDELETE -q\\" .\' repos/a/b/git/refs/heads/topic"',
    "gh pr view 77 --jq '.title + \"x\"'",
    'gh pr view 77 --jq "\'"',
    "powershell.exe -Command \"gh pr view 77 --jq '.body'\" ; gh pr merge 77",
]
NESTED_QUOTE_GIT = [
    "git commit -m '\"x --amend -m\" y'",
    "powershell.exe -Command 'git commit -m ''\"x --amend -m\" y'''",
    'powershell.exe -Command "git commit -m \'\\"x --amend -m\\" y\'"',
    'git commit -m "it\'s"',
    "git commit -F '\"msg.txt\"'",
    "git add '\"-f\" ignored.txt'",
]


@pytest.mark.parametrize("command", NESTED_QUOTE_GH)
def test_nested_quote_gh_queries_ask(command):
    assert not is_readonly_gh_query(command)
    assert decide(BridgeGate(channel=RecordingChannel(), rules=CODING_RULES), codex(command))[1] is True


@pytest.mark.parametrize("command", NESTED_QUOTE_GIT)
def test_nested_quote_git_forms_ask_even_after_the_grants(command):
    assert not is_simple_git_add(command) and not is_simple_git_commit(command)
    gate = BridgeGate(channel=RecordingChannel(), rules=CODING_RULES)
    assert decide(gate, codex("git add -A")) == ("allow_session", True)
    assert decide(gate, codex("git commit -m ok")) == ("allow_session", True)
    assert decide(gate, codex(command))[1] is True


@pytest.mark.parametrize(
    "command",
    ["gh pr view 77 --repo a/b --json state", "powershell.exe -Command 'gh pr view 77 --repo a/b --json state'"],
)
def test_documented_safe_gh_form_still_auto_allows(command):
    gate = BridgeGate(channel=RecordingChannel(), rules=CODING_RULES)
    assert decide(gate, codex(command)) == ("allow", False)


@pytest.mark.parametrize("command", ["git commit -m fix-typo", "powershell.exe -Command 'git commit -m fix-typo'"])
def test_documented_safe_commit_form_is_still_session_granted(command):
    gate = BridgeGate(channel=RecordingChannel(), rules=CODING_RULES)
    assert decide(gate, codex("git commit -m first")) == ("allow_session", True)
    assert decide(gate, codex(command)) == ("allow", False)


@pytest.mark.parametrize("command", EMPTY_ARG_GH)
def test_empty_argument_gh_queries_are_never_auto_allowed(command):
    assert not is_readonly_gh_query(command)
    gate = BridgeGate(channel=RecordingChannel(), rules=CODING_RULES)
    assert decide(gate, codex(command))[1] is True


@pytest.mark.parametrize("command", EMPTY_ARG_GIT)
def test_empty_argument_git_forms_ask_even_after_the_grants(command):
    assert not is_simple_git_add(command) and not is_simple_git_commit(command)
    gate = BridgeGate(channel=RecordingChannel(), rules=CODING_RULES)
    assert decide(gate, codex("git add -A")) == ("allow_session", True)
    assert decide(gate, codex("git commit -m ok")) == ("allow_session", True)
    assert decide(gate, codex("git commit -m 'still granted'")) == ("allow", False)  # the grants are live
    assert decide(gate, codex(command))[1] is True


def test_codex_git_add_grant_is_scoped_to_the_cwd():
    gate = BridgeGate(channel=RecordingChannel(), rules=CODING_RULES)
    assert decide(gate, codex(CODEX_GIT_ADD, cwd="C:/repo"))[1] is True
    assert decide(gate, codex(CODEX_GIT_ADD, cwd="C:/other-repo"))[1] is True


@pytest.mark.parametrize(
    "command",
    [
        "git commit --amend --no-edit",
        "git commit --no-verify -m x",
        "git commit -nm x",
        # P2-4 review examples: quoted and abbreviated options.
        'git commit "--amend" -m x',
        "git commit '--amend'",
        'git commit "--no-verify" -m x',
        "git commit --amen -m x",
        "git commit --no-ver -m x",
        "powershell.exe -Command 'git commit \"--amend\" -m x'",
        # Everything else outside the allow-list.
        "git commit -n -m x",
        "git commit --fixup HEAD",
        "git commit --fixup=HEAD",
        "git commit -c HEAD",
        "git commit -C HEAD",
        "git commit --author=x -m y",
        "git commit -m",
        "git commit -mx",
        "git commit -ma x",
        "git commit -F /etc/passwd",
        "git commit -F ../outside.txt",
        "git commit --file=C:/outside.txt",
        "git commit -F -",
        "git commit -m x ../other/file.py",
        'git add "--force" ignored.txt',
        "git add --forc ignored.txt",
        "git add -f ignored.txt",
        "git add -p",
        "git add --chmod=+x run.sh",
        "git add --al",
        "git -C ../other add -A",
        "git add $(evil)",
        "git add x > out.txt",
        "git reset --hard HEAD~1",
        "git push origin feature",
        "git merge main",
    ],
)
def test_escalated_git_forms_outside_the_session_grant_always_ask(command):
    gate = BridgeGate(channel=RecordingChannel(), rules=CODING_RULES)
    decide(gate, codex("git add -A"))
    decide(gate, codex("git commit -m ok"))
    assert decide(gate, codex(command))[1] is True


# --------------------------------------------------------------------------- #
# LT-1: Claude Bash wrappers, destructive forms, and routine-work compatibility
# --------------------------------------------------------------------------- #

LT1_NEW_ASK = [
    "bash -c 'git push --force origin main'",
    'sh -c "git push --force origin main"',
    "python -c \"import subprocess;subprocess.run(['git','push','--force','origin','main'])\"",
    "/usr/bin/git push --force origin main",
    "env X=1 git push --force origin main",
    'eval "git push --force origin main"',
    "xargs git push --force origin main < list",
    "git branch -D feature",
    "git checkout -- .",
    "git restore .",
    "git clean -fdx",
    "git stash drop",
    "gh pr close 12",
    "Remove-Item -Recurse -Force build",
    "del /s /q build",
    "truncate -s 0 important.db",
    "> important.db",
    # Found by the lead's independent probe: restoring paths from a ref overwrites them too.
    "git checkout main -- src",
    "git checkout HEAD -- file.py",
    "git checkout HEAD .",
]
LT1_STILL_ASK = [
    "git push --force origin main",
    "cd sub && git push --force origin main",
    "gh pr merge 1",
    "git reset --hard",
    "rm x",
]
LT1_ROUTINE = [
    ("python -m pytest tests -q", "allow"),
    ("ruff check src", "allow"),
    ("git status", "allow"),
    ("git diff", "allow"),
    ("git log -5", "allow"),
    ("git add -A", "session"),
    ("git commit -m x", "session"),
    ("ls", "allow"),
    ("git checkout feature", "allow"),
    ("cat file", "allow"),
    ("pip show x", "allow"),
    ("python scripts/foo.py --dry-run", "allow"),
]


@pytest.mark.parametrize("command", [*LT1_NEW_ASK, *LT1_STILL_ASK])
def test_lt1_destructive_and_wrapped_bash_asks_on_every_call(command):
    assert tier(claude_bash(command)) == "ask"
    channel = RecordingChannel()
    gate = BridgeGate(channel=channel, rules=CODING_RULES)
    # Existing routine grants must never cover an opaque/destructive call.
    for routine in ("git add -A", "git commit -m x", "git push origin feature", "gh pr create --fill"):
        assert decide(gate, claude_bash(routine)) == ("allow_session", True)
    for _ in range(2):
        assert decide(gate, claude_bash(command)) == ("allow", True)
        arguments = channel.prompts[-1].split("  arguments: ", 1)[1].splitlines()[0]
        assert json.loads(arguments) == {"command": command}  # review the original text
    assert decide(BridgeGate(channel=RecordingChannel(answer=False), rules=CODING_RULES), claude_bash(command)) == (
        "deny",
        True,
    )


@pytest.mark.parametrize(("command", "expected"), LT1_ROUTINE)
def test_lt1_routine_bash_keeps_its_existing_tier(command, expected):
    assert tier(claude_bash(command)) == expected
    gate = BridgeGate(channel=RecordingChannel(), rules=CODING_RULES)
    assert decide(gate, claude_bash(command)) == (
        ("allow_session", True) if expected == "session" else ("allow", False)
    )
    assert decide(gate, claude_bash(command)) == ("allow", False)


@pytest.mark.parametrize(
    "command",
    [
        "X=1 git push --force origin main",
        "env X=1 /usr/bin/git push --force origin main",
        "env X=1 env Y=2 /usr/bin/git push --force origin main",
        "env -- X=1 git restore .",
        "./git push --force origin main",
        '"C:/Program Files/Git/bin/git.exe" push --force origin main',
        r"C:\tools\git.exe push --force origin main",
        "cd sub && env X=1 ./git restore .",
        "zsh -c 'ls'",
        "/bin/bash -lc 'ls'",
        "pwsh -Command 'ls'",
        "powershell.exe -NoProfile -c 'ls'",
        'cmd /c "dir"',
        'cmd /c"dir"',
        "python3.11 -c 'print(1)'",
        "pythonw.exe -c 'print(1)'",
        "python -uc 'print(1)'",
        "node -e 'console.log(1)'",
        "node --eval='console.log(1)'",
        "powershell.exe -EncodedCommand ZwBoAA==",
        "env -S 'git push --force origin main'",
        "git branch -d feature",
        "git checkout -- *",
        "git checkout .",
        "git stash clear",
        "gh repo delete a/b --yes",
        "gh release delete v1",
        "remove-item build",
        "rmdir /s /q build",
        "rd /s /q build",
        "echo data > important.db",
        "env X=1 > important.db",
        "> important.db git status",
        "echo data 2>important.db",
        "git add -A > important.db",
        "git commit -m x > important.db",
        "git   restore *",
        "git 'restore' *",
        "git\trestore .",
    ],
)
def test_lt1_normalized_and_additional_destructive_forms_ask(command):
    assert tier(claude_bash(command)) == "ask"


@pytest.mark.parametrize(
    "command",
    [
        "env X=1 /usr/bin/git status",
        "X=1 ./ruff check src",
        "ls *.py",
        "cat 'file with spaces'",
        "echo 'a > b'",
        "python -m pytest tests -q 2>&1",
        "echo log >> output.log",
    ],
)
def test_lt1_prefixes_and_nontruncating_syntax_remain_allowed(command):
    assert tier(claude_bash(command)) == "allow"


# --------------------------------------------------------------------------- #
# 17/10 fixtures -- Claude Bash calls
# --------------------------------------------------------------------------- #

CLAUDE_1 = """cat > /tmp/new_archive.py <<'EOF'
import tempfile

def archive(rows):
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
        fh.write("\\n".join(rows))
    return fh.name
EOF"""
CLAUDE_2 = "python _splice.py && rm _splice.py _new_archive.py.txt && git status --short && git diff --stat"
CLAUDE_3 = """cat > _edit.py <<'PYEOF'
from pathlib import Path
p = Path("src/econ_calendar.py")
s = p.read_text()
s = s.replace("self.rm (cache)", "self.cache.clear()")  # drop the old rm helper; delete stale rows
p.write_text(s)
PYEOF"""
CLAUDE_4 = (
    'python _edit.py && rm _edit.py && python -m pytest -q tests/test_econ_calendar.py -k "calendar" 2>&1 | tail -30'
)
CLAUDE_5 = "python _docs.py && rm _docs.py && git diff --stat docs; cat C:/Users/x/AppData/Local/Temp/job.output | tail"


@pytest.mark.parametrize("command", [CLAUDE_1, CLAUDE_3], ids=["1-heredoc-delete", "3-heredoc-rm"])
def test_claude_fixtures_1_3_heredocs_are_matched_in_full_and_ask(command):
    """Option B (fix round 1): the gate matches the WHOLE Bash text again, so a
    heredoc whose body says delete/rm asks. The fix for 1 and 3 is upstream:
    the writer is told to create files with Write/Edit, never heredocs."""
    assert tier(claude_bash(command)) == "ask"
    stock = TieredGate(channel=None, rules=CODING_RULES)._match(claude_bash(command))
    assert stock is not None and stock.tier == "ask"  # identical to the stock gate


def test_claude_writer_is_told_to_write_files_with_write_edit_and_use_its_temp_dir():
    text = _engines.claude_writer_instructions("C:/Temp/lazytools-bridge/job1")
    assert "Write and Edit tools" in text
    assert "no heredocs" in text and "echo" in text
    assert "C:/Temp/lazytools-bridge/job1" in text
    assert "temp directory" in _engines.claude_writer_instructions(None)


@pytest.mark.parametrize("command", [CLAUDE_2, CLAUDE_4, CLAUDE_5], ids=["2", "4", "5"])
def test_claude_fixtures_2_4_5_still_ask_because_of_rm(command):
    assert tier(claude_bash(command)) == "ask"


#: Bypasses found by the opposite-engine review of the removed heredoc
#: stripping (P1-1, P1-2) and its neighbours. Every one must ASK.
HEREDOC_BYPASSES = [
    # P1-1: bash ignores the commented cat, so the "body" runs as commands.
    "echo ok # ; cat > x <<'EOF'\nrm -rf target\nEOF",
    # P1-2: the heredoc body becomes the program of bash -c "$(...)".
    "bash -c \"$(true; cat <<'EOF'\ngit push origin main\nEOF\n)\"",
    # A data sink that is then executed.
    "cat > x.sh <<'EOF'\nrm -rf target\nEOF\nbash x.sh",
    "cat > x.sh <<'EOF'\ngit push origin main\nEOF\nsh x.sh",
    # Classic code heredocs.
    "bash <<'EOF'\nrm -rf ~\nEOF",
    "python - <<'EOF'\nimport os; os.system('rm -rf x')\nEOF",
    "cat <<'EOF' | bash\nrm -rf x\nEOF",
    "cat > f.txt <<EOF\n$(rm -rf ~)\nEOF",
    "python -c \"import os; os.system('rm -rf x')\"",
    # rm before / after a heredoc.
    CLAUDE_1 + "\nrm -rf /tmp/x",
    "rm -rf build && " + CLAUDE_1,
]


@pytest.mark.parametrize("command", HEREDOC_BYPASSES)
def test_review_heredoc_bypasses_ask(command):
    assert tier(claude_bash(command)) == "ask"
    channel = RecordingChannel(answer=False)
    assert decide(BridgeGate(channel=channel, rules=CODING_RULES), claude_bash(command)) == ("deny", True)


@pytest.mark.parametrize("command", [CLAUDE_1, *HEREDOC_BYPASSES, CLAUDE_2, "ls -la", "git push origin main"])
def test_bridge_gate_matches_bash_exactly_like_the_stock_gate(command):
    stock = TieredGate(channel=None, rules=CODING_RULES)._match(claude_bash(command))
    assert tier(claude_bash(command)) == (stock.tier if stock else "unmatched")


def test_rm_is_not_weakened_globally():
    assert tier(claude_bash("rm _splice.py")) == "ask"
    assert tier(claude_bash("rm -rf scratch")) == "ask"
    assert tier(claude_bash("git branch --delete x")) == "ask"


# --------------------------------------------------------------------------- #
# adversarial escalations -- every one must ASK
# --------------------------------------------------------------------------- #

ADVERSARIAL = [
    "gh pr view 1; gh pr merge 1",
    "gh pr view 1 && git push origin main",
    "gh api -X DELETE repos/a/b/git/refs/heads/x",
    "gh api repos/a/b/pulls -f title=x",
    "gh pr view 1 > C:\\Windows\\x",
    "gh pr view $(rm -rf .)",
    "powershell.exe -Command 'gh pr view 1' ; Remove-Item x",
    "gh pr merge 1",
    "gh release create v1",
    # more of the same families
    "powershell.exe -Command 'gh pr view 1; gh pr merge 1'",
    "powershell.exe -Command 'gh pr view 1' ; Remove-Item 'x'",
    'powershell.exe -Command "gh pr view 1" ; Remove-Item "x"',
    "gh pr view 1 | sh",
    "gh pr view 1 || gh pr merge 1",
    "gh pr view `rm -rf .`",
    "gh pr view 1 >> out.txt",
    "gh pr view 1 2>&1",
    "gh pr view 1\ngh pr merge 1",
    "gh pr view 1 --web",
    "gh pr view 1 --repo a/b --jq",
    "gh pr view '1'",
    "gh pr comment 1 --body hi",
    "gh pr review 1 --approve",
    "gh pr close 1",
    "gh pr edit 1 --title x",
    "gh pr create --fill",
    "gh run rerun 123",
    "gh run cancel 123",
    "gh run delete 123",
    "gh workflow run ci.yml",
    "gh repo delete a/b --yes",
    "gh auth token",
    "gh api --method DELETE repos/a/b/git/refs/heads/x",
    "gh api --method=GET repos/a/b",
    "gh api -XDELETE repos/a/b/git/refs/heads/x",
    "gh api -X=DELETE repos/a/b",
    "gh api repos/a/b/pulls -F title=x",
    "gh api repos/a/b/pulls --field title=x",
    "gh api repos/a/b/pulls --raw-field title=x",
    "gh api repos/a/b/pulls --input body.json",
    "gh api repos/a/b -H X-HTTP-Method-Override:DELETE",
    "gh api graphql -f query=mutation",
    "gh api user",
    "gh api repos/a/b/../../user",
    "gh api repos/a/b repos/c/d",
    "gh api 'repos/a/b'",
    "gh api repos/a/b/pulls --jq .[].body",  # unquoted glob characters
    "gh api repos/a/b?per_page=100",
    "GH_TOKEN=x gh pr view 1",
    "C:/tools/gh.exe pr view 1",
    "git push origin main",
    "Remove-Item -Recurse x",
    "pwsh -Command 'gh pr view 1' -NoExit",
    "powershell.exe -EncodedCommand ZwBoACAAcAByACAAdgBpAGUAdwAgADEA",
    "powershell.exe -Command 'gh pr view 1 \u2019; Remove-Item x'",
    "gh pr view 1 \u037e gh pr merge 1",
]


@pytest.mark.parametrize("command", ADVERSARIAL)
def test_adversarial_escalations_always_ask(command):
    assert not is_readonly_gh_query(command)
    gate = BridgeGate(channel=RecordingChannel(), rules=CODING_RULES)
    action, asked = decide(gate, codex(command))
    assert asked is True and action == "allow"  # the recording human said yes -- but was asked


def test_a_denied_adversarial_escalation_is_denied():
    gate = BridgeGate(channel=RecordingChannel(answer=False), rules=CODING_RULES)
    assert decide(gate, codex("gh pr view 1; gh pr merge 1")) == ("deny", True)


# --------------------------------------------------------------------------- #
# classifier details
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "command",
    [
        "gh pr view 77",
        "gh pr view",
        "gh pr view 77 --comments",
        "gh pr diff 77 --name-only",
        "gh pr checks 77 --watch --interval 10",
        "gh run list --repo a/b --limit 5 --json databaseId,status",
        "gh run view 1 --log-failed",
        "gh api repos/a/b/pulls/77/comments --paginate --jq '.[].body'",
        "gh api /repos/a/b/actions/runs --jq '.workflow_runs[] | .status'",
        "gh.exe pr view 77",
        "pwsh -NoProfile -Command 'gh pr view 77'",
        'powershell -NoProfile -NonInteractive -c "gh run list --limit 3"',
        "gh pr view 77 --json body --jq=.body",
    ],
)
def test_readonly_gh_shapes_recognised(command):
    assert is_readonly_gh_query(command)


def test_unwrap_powershell_only_unwraps_the_exact_wrapper():
    assert unwrap_powershell("powershell.exe -Command 'gh pr view 1'") == "gh pr view 1"
    assert unwrap_powershell('powershell.exe -Command "gh pr view 1"') == "gh pr view 1"
    for not_wrapped in (
        "powershell.exe -Command 'gh pr view 1' ; Remove-Item x",
        "powershell.exe -Command 'a' 'b'",
        "powershell.exe -ExecutionPolicy Bypass -Command 'gh pr view 1'",
        "gh pr view 1",
    ):
        assert unwrap_powershell(not_wrapped) == not_wrapped


def test_git_classifiers():
    assert is_simple_git_add("git add -A")
    assert is_simple_git_add("powershell.exe -Command 'git add -- a.py b/c.py'")
    assert not is_simple_git_add("git add -A; rm -rf x")
    assert not is_simple_git_add("git status")
    assert is_simple_git_commit("git commit -m 'feat: x; not a separator'")
    assert not is_simple_git_commit("git commit --amend")
    assert not is_simple_git_commit("git commit -m x && git push")


@pytest.mark.parametrize(
    "command",
    [
        "git add -A",
        "git add --all",
        "git add -u",
        "git add --update",
        "git add src/a.py tests/b.py",
        "git add -- -weird-name.txt",
        "git add 'docs/with space.md'",
    ],
)
def test_git_add_allow_list_accepts(command):
    assert is_simple_git_add(command)


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m 'msg'",
        'git commit -m "--amend is only the message"',
        "git commit --message=fix",
        "git commit -a -m x",
        "git commit --all -m x",
        "git commit -am 'x'",
        "git commit -F docs/msg.txt",
        "git commit --file=msg.txt",
        "git commit -m x -- src/a.py",
        "git commit -m x src/a.py",
    ],
)
def test_git_commit_allow_list_accepts(command):
    assert is_simple_git_commit(command)


def test_classified_rules_never_match_a_split_segment_under_a_plain_tieredgate():
    """Defence in depth: the table stays safe even without BridgeGate's whole-command step."""
    plain = TieredGate(channel=None, rules=CODING_RULES)
    assert plain._match(codex("gh pr view 1; gh pr merge 1")).tier == "ask"
    assert plain._match(codex("gh pr view 1 && git add -A")).tier == "ask"
    assert plain._match(codex("gh pr view 1")).tier == "allow"


def test_classified_rule_requires_the_original_command():
    request = ApprovalRequest(
        provider="codex", kind="command", name="codex-shell", arguments={"command": "gh pr view 1"}
    )
    assert tier(request) == "ask"  # no raw command to verify against: fail closed


def test_classified_rule_rejects_unknown_classifier_and_arg_pattern():
    with pytest.raises(ValueError):
        ClassifiedRule("allow", "*", kind_pattern="command", classifier="nope")
    with pytest.raises(ValueError):
        ClassifiedRule("allow", "*", "gh *", kind_pattern="command", classifier="readonly_gh_query")


def test_classified_session_grants_fingerprint_separately():
    add, commit = (r for r in CODING_RULES if isinstance(r, ClassifiedRule) and r.tier == "session")
    assert add.fingerprint() != commit.fingerprint()


def test_readonly_gh_is_not_allowed_for_tools_only_for_escalations():
    """Claude's Bash already runs gh reads via the Bash catch-all; the classified
    rules speak only about kind=command and never widen the tool rules."""
    assert tier(claude_bash("gh pr merge 1")) == "ask"


@pytest.mark.parametrize(
    "command, expected",
    [
        # Escaped quotes are literal to Bash: the '>' between them is a real redirect (review P1).
        (r"echo \" > important.db \"", "ask"),
        ("echo x >| important.db", "ask"),
        ("cmd &> important.db", "ask"),
        # Ambiguous input fails closed.
        ('echo "unbalanced', "ask"),
        # Quoted '>' is data; appends and descriptor duplication do not truncate.
        ('echo "a > b"', "allow"),
        ("echo 'x > y'", "allow"),
        ("echo hi >> log.txt", "allow"),
        ("python -m pytest -q 2>&1 | tail -5", "allow"),
        ('git commit -m "don\'t break"', "session"),
    ],
)
def test_redirect_detection_follows_shell_lexing(command, expected):
    assert tier(claude_bash(command)) == expected


@pytest.mark.parametrize(
    "command, expected",
    [
        # Review P1s on #217.
        ("echo replacement >& important.db", "ask"),
        ("python -m pytest -q 2>&1", "allow"),
        ("cmd 1>&2", "allow"),
        ("gh -R owner/repo pr close 12", "ask"),
        ("gh --repo owner/repo pr close 12", "ask"),
        ("gh pr -R owner/repo close 12", "ask"),
        ("gh --repo=owner/repo pr merge 3", "ask"),
        ("gh -R owner/repo pr view 12", "allow"),
    ],
)
def test_review_gh_repo_flags_and_fd_redirects(command, expected):
    assert tier(claude_bash(command)) == expected
