"""Whole-command Bash capability decisions and the closed fallback."""

import asyncio
from pathlib import Path

import pytest
from lazybridge.engines.coding import ApprovalRequest
from lazybridge.ext.approval import TieredGate

from lazytools.code_bridge import _readonly
from lazytools.code_bridge._approvals import BridgeGate
from lazytools.code_bridge._policy import CODING_RULES


def request(command):
    return ApprovalRequest(provider="claude-code", kind="tool", name="Bash", arguments={"command": command})


def tier(command):
    rule = BridgeGate(channel=UnusedChannel(), rules=CODING_RULES)._match(request(command))
    assert rule is not None
    return rule.tier


class UnusedChannel:
    async def ask(self, prompt: str) -> bool:
        raise AssertionError("_match must not ask")


READS = [
    "ls -la", "pwd", "cat README.md", "head -n 20 README.md", "tail --lines=20 README.md", "wc -l README.md",
    "grep -n word README.md", "rg --files", "rg -n 'a b' src", "find . -name '*.py' -type f -print",
    "git status --short", "git log --oneline -5", "git diff --stat", "git show HEAD:README.md",
    "git branch", "git branch --list 'feat/*'", "git branch -a", "git rev-parse --show-toplevel",
    "git describe --tags --always", "git ls-files", "git grep -n text", "git remote -v", "git fetch origin",
    "gh pr view 1 --json state", "gh pr list --state open", "gh pr checks 1", "gh pr diff 1 --name-only",
    "gh run view 1 --log-failed", "gh run list --limit 5", "gh issue view 1", "gh issue list",
    "gh api repos/a/b/pulls/1/comments --paginate --jq '.[].body'",
    "git log --oneline | head -n 5", "cat README.md | grep text | wc -l",
    "git status 2>&1", "git status >/dev/null", "git status 2> /dev/null",
    "gh pr view 1 --json reviews --jq '.reviews[] | .state'",
]


@pytest.mark.parametrize("command", READS)
def test_readonly_capabilities(command):
    assert _readonly.match(command)
    assert tier(command) == "allow"


BYPASSES = [
    r'git status \" > output', 'git status >"output"', "git status >& output", "git status <> output",
    "git status >> output", "git status >| output", "git status &> output", "git status 2>&3",
    "git status 2>&1 > output", "git status > /dev/null/../output", "git status 3>/dev/null",
    "gh -R a/b pr close 1", "gh --repo=a/b pr close 1", "git checkout HEAD file", "git checkout file",
    "git status # ; git restore .", "git status#ignored", "powershell -Com 'git restore .'",
    "pwsh -c 'git status'", r"g\it restore .", "node -p 'fs.unlinkSync(x)'", "node -pe code",
    "git checkout -b x -f", "git checkout -B x", "git restore .", "git switch x", "git clean -fdx",
    "bash -c 'git status'", "sh -c 'git status'", "cmd /c git status", "python -c code", "perl -e code",
    "env git status", "env X=1 git status", "X=1 git status", "nohup git status", "sudo git status",
    "xargs git status", "eval 'git status'", "exec git status", "source x.sh",
    "find . -exec echo x ';'", "find . -delete", "find . -fprint output", "find . -fprintf output x",
    "git status; cat README.md", "git status && cat README.md", "git status || cat README.md",
    "git status | sh", "git status | git add -A", "git status |", "| git status", "git status |& cat",
    "git status $(echo x)", "git status `echo x`", "cat <(git status)", "cat >(git status)",
    "git status\ncat README.md", "git status\t--short", "git status 'unterminated", 'git status ""',
    "git diff --output=output", "git log --output output", "git show --ext-diff", "git diff --textconv",
    "git branch new", "git branch -D old", "git branch --list old -D", "git branch new -- --list",
    "git fetch origin main:main",
    "git fetch --output=output", "git grep --open-files-in-pager text", "tail -f output",
    "rg --pre script text", "rg --pre=script text", "rg --hostname-bin script text", "grep -f patterns x",
    "gh api repos/a/b -X GET", "gh api repos/a/b --method=GET", "gh api repos/a/b -f key=value",
    "gh api repos/a/b -F key=value", "gh api repos/a/b --input input.json", "gh api graphql",
    "gh pr diff --output output", "gh pr view --web", "git add -A && git status",
    "git commit -m x; git restore .", "git addendum x", "gh pr create-evil", "git push origin topic extra",
    "curl example.com", "npm install", "python script.py", "unknown-command", "",
]


@pytest.mark.parametrize("command", BYPASSES)
def test_bypasses_and_unknowns_ask(command):
    assert tier(command) == "ask"


@pytest.mark.parametrize("command", ["git add -A", "git commit -m message", "git push origin topic", "gh pr create --fill"])
def test_session_grants_still_require_the_first_approval(command):
    class Channel:
        prompts = []

        async def ask(self, prompt):
            self.prompts.append(prompt)
            return True

    channel = Channel()
    gate = BridgeGate(channel=channel, rules=CODING_RULES)
    assert asyncio.run(gate(request(command))).action == "allow_session"
    assert asyncio.run(gate(request(command))).action == "allow"
    assert len(channel.prompts) == 1
    assert "matched session rule:" in channel.prompts[0]
    assert asyncio.run(gate(request(command + " && git restore ."))).action == "allow"
    assert len(channel.prompts) == 2


def test_fallback_is_ask_even_under_stock_gate():
    rule = TieredGate(channel=UnusedChannel(), rules=CODING_RULES)._match(request("arbitrary script"))
    assert rule is not None and rule.tier == "ask"
    assert [r for r in CODING_RULES if r.name_pattern == "Bash"][-1].tier == "ask"


def test_ticket_explains_the_closed_fallback():
    class Channel:
        async def ask(self, prompt):
            assert "no allow rule matched (fail closed)" in prompt
            return False

    assert asyncio.run(BridgeGate(channel=Channel(), rules=CODING_RULES)(request("python script.py"))).action == "deny"


@pytest.mark.parametrize("text", [
    'unknown = true\nrules = []', '[[rules]]\nid="x"\ncommand=["cat"]\nextra=true',
    '[[rules]]\nid="x"\ncommand=["cat"]\nvalidator="unknown"',
    '[[rules]]\nid="x"\ncommand=["cat"]\nmax_positionals=true',
    '[[rules]]\nid="x"\ncommand=[]', '[[rules]]\nid="x"\ncommand="cat"',
    '[[rules]]\nid="x"\ncommand=["cat"]\n[[rules]]\nid="x"\ncommand=["pwd"]',
])
def test_policy_loader_rejects_invalid_capabilities(tmp_path, text):
    source = tmp_path / "readonly.toml"
    source.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        _readonly.load_policy(source)


def test_policy_is_extensible_without_code(tmp_path):
    source = tmp_path / "readonly.toml"
    source.write_text('[[rules]]\nid="custom-cat"\ncommand=["cat"]\nflags=["--number"]', encoding="utf-8")
    assert _readonly.load_policy(source)[0].matches(["cat", "--number", "README.md"])
    assert Path(_readonly.__file__).with_name("readonly_allowlist.toml").is_file()
