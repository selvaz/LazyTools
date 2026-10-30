"""Regression tests for the independent review of the fail-closed Claude Bash gate.

Every confirmed bypass of the review must resolve to ``ask`` (never an unattended
allow/session grant); the nearby legitimate forms must keep working.
"""

import asyncio

import pytest
from lazybridge.engines.coding import ApprovalRequest
from lazybridge.ext.approval import Rule

from lazytools.code_bridge import _readonly
from lazytools.code_bridge._approvals import BridgeGate, is_readonly_gh_query, short_branch
from lazytools.code_bridge._policy import CODING_RULES

BRANCH = "feat/x"


class Unused:
    async def ask(self, prompt: str) -> bool:
        raise AssertionError("_match must not ask")


def _tier(name, arguments, *, job_branch=BRANCH, rules=CODING_RULES):
    gate = BridgeGate(channel=Unused(), rules=rules, job_branch=job_branch)
    rule = gate._match(ApprovalRequest(provider="claude-code", kind="tool", name=name, arguments=arguments))
    assert rule is not None
    return rule.tier


def bash(command, **kwargs):
    return _tier("Bash", {"command": command}, **kwargs)


# --- 1. quote-split protected refs on the push grant -------------------------

QUOTE_SPLIT_PUSHES = [
    'git push origin ma"i"n', "git push origin ma''in", "git push origin HEA''D", 'git push origin HE"A"D',
    'git push origin mast""er', "git push origin 'main'", 'git push origin "master"', "git push origin m''ain",
    'git push o"r"igin feat/x', "git push origin 'feat/x'", 'git push "origin" feat/x', 'g"i"t push origin feat/x',
    "git push origin feat/'x'",
]


@pytest.mark.parametrize("command", QUOTE_SPLIT_PUSHES)
def test_quote_split_or_quoted_push_words_ask(command):
    assert bash(command) == "ask"


def test_quote_split_push_to_main_cannot_ride_an_earlier_feature_push_grant():
    prompts = []

    class Channel:
        answer = True

        async def ask(self, prompt):
            prompts.append(prompt)
            return self.answer

    channel = Channel()
    gate = BridgeGate(channel=channel, rules=CODING_RULES, job_branch=BRANCH)

    def run(command):
        return asyncio.run(gate(ApprovalRequest(provider="claude-code", kind="tool", name="Bash", arguments={"command": command})))

    assert run("git push origin feat/x").action == "allow_session"
    assert run("git push origin feat/x").action == "allow"
    assert len(prompts) == 1
    channel.answer = False
    for command in ('git push origin ma"i"n', "git push origin ma''in", "git push origin HE\"A\"D"):
        assert run(command).action == "deny"
    assert len(prompts) == 4  # every quote-split push reached a human


# --- 2. push grant to any remote ---------------------------------------------


@pytest.mark.parametrize("command", [
    "git push ../other feat/x", "git push //host/x feat/x", "git push evil feat/x", "git push upstream feat/x",
    "git push origin2 feat/x", "git push https://example.com/r.git feat/x", "git push /c/repo feat/x",
    "git push origin other", "git push origin feat/x extra", "git push origin feat/x:feat/x", "git push origin +feat/x",
    "git push origin feat/x:main", "git push -u origin feat/x", "git push --force origin feat/x",
    "git push origin feat/xx", "git push origin FEAT/X", "git push", "git push origin", "git push origin HEAD",
    "git push origin main", "git push origin refs/heads/feat/x",
])
def test_push_grant_covers_only_origin_and_the_job_branch(command):
    assert bash(command) == "ask"


def test_push_grant_positive_and_job_branch_unknown_or_protected():
    assert bash("git push origin feat/x") == "session"
    assert bash("git push origin feat/x", job_branch=None) == "ask"  # detached HEAD / unknown
    assert bash("git push origin feat/x", job_branch="feat/y") == "ask"
    assert bash("git push origin main", job_branch="main") == "ask"
    assert bash("git push origin mainline", job_branch="mainline") == "ask"


def test_short_branch():
    assert short_branch("refs/heads/feat/x") == "feat/x"
    assert short_branch(None) is None
    assert short_branch("HEAD") is None
    assert short_branch("refs/remotes/origin/x") is None
    assert short_branch("refs/heads/we ird") is None


# --- 3. gh pr create flag allow-list -----------------------------------------


@pytest.mark.parametrize("command", [
    "gh pr create --body-file /c/Users/Administrator/.env", "gh pr create --body-file=.env", "gh pr create -F notes.md",
    "gh pr create -F /c/Users/Administrator/.codex/auth.json", "gh pr create -R other/repo", "gh pr create -R=other/repo",
    "gh pr create --repo other/repo", "gh pr create --repo=other/repo", "gh pr create --web", "gh pr create -w",
    "gh pr create --hostname example.com", "gh pr create --template t.md", "gh pr create -T t.md",
    "gh pr create --fill-first", "gh pr create --fill-verbose", "gh pr create --recover key", "gh pr create --draft=false",
    "gh pr create --head other", "gh pr create -H other", "gh pr create --head=other:feat/x", "gh pr create -H owner:feat/x",
    "gh pr create --reviewer someone", "gh pr create --assignee someone", "gh pr create --project p",
    "gh pr create --milestone m", "gh pr create --no-maintainer-edit", "gh pr create positional",
    "gh pr create --title", "gh pr create --title --web", "gh pr create --body -F", "gh pr create --title x --web",
    "gh pr create -t=x", "gh pr create -tx", "gh pr create '--web'", "gh pr create --fill extra",
    "gh pr create --title x --body-file .env", "gh pr create --title x -F .env",
    "gh pr create-evil", "gh  pr create --fill; gh pr merge 1",
])
def test_gh_pr_create_with_unlisted_flags_asks(command):
    assert bash(command) == "ask"


def test_gh_pr_create_with_head_needs_a_known_job_branch():
    assert bash("gh pr create --head feat/x", job_branch=None) == "ask"
    assert bash("gh pr create --head feat/x") == "session"


@pytest.mark.parametrize("command", [
    "gh pr create", "gh pr create --fill", "gh pr create --draft --fill", "gh pr create -d",
    "gh pr create --title 'A title' --body 'Some inline text'", "gh pr create -t title -b body",
    "gh pr create --base develop --head feat/x", "gh pr create -B develop -H feat/x", "gh pr create --head=feat/x",
    "gh pr create --label bug --label docs", "gh pr create -l bug,docs", "gh pr create --title=x --body=y --base=z",
    "gh pr create --title 'A title' --body 'text' --base main --head feat/x --draft --label bug --fill",
])
def test_gh_pr_create_allow_listed_forms_keep_the_session_grant(command):
    assert bash(command) == "session"


# --- 4. Write/Edit into git control files ------------------------------------

GIT_CONTROL_PATHS = [
    ".git/config", ".git\\config", ".git/hooks/pre-commit", ".git/hooks/post-checkout", ".git/info/attributes",
    ".git/HEAD", "sub/.gitattributes", ".gitattributes", ".gitmodules", ".gitconfig", "a/b/.gitmodules",
    ".GIT/config", ".Git/Hooks/x", ".git./config", ".git /config", ".git::$INDEX_ALLOCATION/config",
    ".gitattributes::$DATA", "GIT~1/config", "GITATT~1", "GITMOD~1", "C:\\repo\\.git\\config",
    "C:/repo/.git/config", "/c/repo/.git/config", "src/../.git/config", "./.git/config", "sub/.git",
    "..\\.git\\config", ".GITATTRIBUTES",
]


@pytest.mark.parametrize("path", GIT_CONTROL_PATHS)
@pytest.mark.parametrize("tool", ["Write", "Edit"])
def test_writing_git_control_files_asks(tool, path):
    assert _tier(tool, {"file_path": path, "content": "x"}) == "ask"


def test_edit_gate_covers_notebook_edit_and_other_path_keys():
    rules = (Rule("allow", "NotebookEdit"), Rule("allow", "MultiEdit"), *CODING_RULES)
    assert _tier("NotebookEdit", {"notebook_path": ".git/n.ipynb"}, rules=rules) == "ask"
    assert _tier("NotebookEdit", {"notebook_path": "n.ipynb"}, rules=rules) == "allow"
    assert _tier("MultiEdit", {"file_path": ".gitattributes"}, rules=rules) == "ask"
    # The extra key cannot be used to hide a git path behind a harmless one.
    assert _tier("Write", {"file_path": "ok.txt", "path": ".git/config"}) == "ask"


@pytest.mark.parametrize("arguments", [{}, {"content": "x"}, {"file_path": ""}, {"file_path": 5}, {"file_path": None}])
def test_write_without_a_usable_path_asks(arguments):
    assert _tier("Write", arguments) == "ask"
    assert _tier("Edit", arguments) == "ask"


@pytest.mark.parametrize("path", [
    "src/x.py", "README.md", ".github/workflows/ci.yml", ".gitignore", "docs/git-notes.md", "src/gitutils.py",
    "tests/git/test_x.py", "gitattributes.md", "my.git.txt", "C:\\repo\\src\\x.py",
])
def test_ordinary_writes_stay_allowed(path):
    assert _tier("Write", {"file_path": path, "content": "x"}) == "allow"
    assert _tier("Edit", {"file_path": path}) == "allow"


# --- 5. git fetch is not read-only -------------------------------------------


@pytest.mark.parametrize("command", ["git fetch", "git fetch origin", "git fetch --all", "git fetch origin main"])
def test_git_fetch_is_not_in_the_readonly_allowlist(command):
    assert _readonly.match(command) is None
    assert bash(command) == "ask"
    assert all("fetch" not in rule.id and "fetch" not in rule.command for rule in _readonly.packaged_policy())


# --- 6. credential reads -----------------------------------------------------

CREDENTIAL_READS = [
    "cat .env", "cat .env.local", "cat .ENV", "cat '.env.'", "cat sub/.env", "cat ./.env", "head -n 1 .env",
    "tail -n 1 .env.production", "wc -l .env",
    "cat /c/Users/Administrator/.codex/auth.json", "cat auth.json", "cat sub/auth.json", "cat Auth.JSON",
    "grep -r token /c/Users/Administrator/.codex", "grep -rn token ~/.codex", "grep -r token '~/.codex'",
    "rg --hidden --no-ignore -e key /c/Users/Administrator/x", "rg --hidden -e key .", "rg --no-ignore key .",
    "rg -uu key .", "rg -u key .", "rg -e key --hidden src", "rg --no-ignore-vcs key .",
    "git grep --no-index -e token /c/Users/Administrator/.codex", "git grep --no-index -e token .",
    "git grep --no-index token",
    "find /", "find / -name x", "ls -R /", "ls /", "ls -la /c/Users/Administrator", "ls ..", "ls -R ../..",
    "cat ../x", "cat sub/../../x", "cat sub/../x", "find .. -name x", "grep -rn x ..", "head -n 1 ../README.md",
    "cat a/.ssh/id", "cat .ssh/config", "cat .aws/credentials", "cat .azure/x", "cat .gnupg/x", "cat .claude/settings.json",
    "cat .codex/config.toml", "ls .codex", "ls .claude", "cat .npmrc", "cat .pypirc", "cat .netrc", "cat .git-credentials",
    "cat foo.pem", "cat x.key", "cat id_rsa", "cat id_rsa.pub", "cat id_rsa_old", "cat my_secret.txt", "cat secrets/a",
    "cat token.txt", "cat access_token", "cat credentials.json", "cat Credentials", "cat .git/config", "ls .git",
    "cat C:/x", "cat c:x", "cat file:stream", "cat '~/x'", "cat ~", "cat ENV~1", "cat PROGRA~1/x", "cat GIT~1/config",
    "git show HEAD:.env", "git show 'HEAD:../x'", "git show HEAD:sub/.ssh/id", "git diff -- .env",
    "git diff -- /etc/passwd", "git log -- /etc/passwd", "git log -- ../x", "git ls-files /x", "git ls-files ../x",
    "git ls-files .env", "git show HEAD:secrets.txt", "git status /c/Users/Administrator",
    "git grep -e k -- .env", "git grep -e k -- ../x", "grep -e k .env", "grep -n k .env", "grep -rn k .ssh",
    "rg -e key -g '.env' .", "rg -e key -g '*' .", "rg -e key -g '.*' .", "rg -e key --glob=secrets/** .",
    "rg -e key -g '**' .", "rg -e key -g 'src/*' .", "rg -e key -g '*.pem' .", "rg -e key -g '.ssh/config' .",
    "rg -e key -g 'src/.*/x' .", "rg -e key -g '**/.env' .", "rg -e key -g '*/*.py' .", "rg -e key -g 'a[b].py' .",
    "rg --files /", "rg --files ..", "rg --files .ssh", "rg key /c/Users/Administrator/.codex",
]


@pytest.mark.parametrize("command", CREDENTIAL_READS)
def test_credential_and_out_of_tree_reads_ask(command):
    assert bash(command) == "ask"


@pytest.mark.parametrize("command", [
    "cat README.md", "cat src/lazytools/code_bridge/_policy.py", "cat -n pyproject.toml", "head -n 5 docs/code-bridge.md",
    "tail -n 5 CHANGELOG.md", "wc -l README.md src/x.py", "ls", "ls -la", "ls src", "ls -la src/lazytools", "ls .github",
    "ls -R src", "find . -name '*.py'", "find src -type f -name 'x*'", "find . -name '.env'", "find",
    "grep foo README.md", "grep -e foo README.md", "grep -r -n foo src", "grep -r foo", "grep -r -n token src",
    "grep -r -n secret src", "grep -r -e token .", "grep --include '*.py' -r -n x src",
    "rg foo src", "rg foo .", "rg foo README.md", "rg --files", "rg --files src", "rg -n token src", "rg -e token src",
    "rg -e foo -g '*.py' src", "rg foo -g '!tests' src", "rg foo -g 'src/**/*.py' .", "rg foo '--glob=*.md' docs",
    "rg foo -g 'README.md' .", "rg foo -g '!*.min.js' -g '*.js' src", "rg -t py foo src",
    "git grep -n text", "git grep -n -e token", "git grep -e token -- src", "git grep text -- src/x.py",
    "git show HEAD:README.md", "git show HEAD:src/x.py", "git show --stat HEAD", "git diff -- src/x.py",
    "git diff --stat", "git log --oneline -5 -- src", "git log origin/main..HEAD --oneline", "git ls-files src",
    "git status --short", "git branch --list 'feat/*'", "git describe --tags", "git rev-parse --show-toplevel",
    "git log --oneline | head -n 5", "git ls-files | grep x", "cat README.md | grep foo | wc -l", "ls | wc -l",
    "git diff | grep token", "git log -p | head -n 50", "git ls-files | wc -l", "ls src | cat",
])
def test_ordinary_in_tree_reads_stay_allowed(command):
    assert bash(command) == "allow"


# --- 7. blocking / DoS reads -------------------------------------------------

BLOCKING = [
    "cat /dev/zero", "cat /dev/stdin", "cat /dev/tty", "head -c 10 /dev/urandom", "head -n 1 /dev/stdin",
    "tail -n 1 /dev/zero", "wc -c /dev/zero", "wc -l /dev/stdin", "grep x /dev/zero", "cat CON", "cat con", "cat NUL",
    "cat nul.txt", "cat sub/AUX", "cat COM1", "cat lpt1.txt", "cat 'CONIN$'", "head -n 1 CONOUT$.txt",
    "cat", "cat -n", "cat -A", "head", "head -n 5", "head -c 10", "tail", "tail -n 5", "wc", "wc -l", "wc -c",
    "grep foo", "grep -n foo", "grep -e foo", "grep -i -e foo -e bar", "rg foo", "rg -n foo", "rg -e foo",
    "cat -", "cat -- -", "head -n 1 -", "head -n 1 -- -", "wc -l -", "grep foo -", "grep -r foo -",
    "gh pr checks 1 --watch", "gh pr checks --watch", "gh pr checks 1 --watch --interval 5", "gh pr checks 1 --interval 5",
    "gh pr checks 1 -i 5", "gh run watch 1", "gh run watch", "gh run watch 1 --exit-status", "gh run view 1 --watch",
    "gh run view 1 --log --watch", "gh pr checks --watch=true",
]


@pytest.mark.parametrize("command", BLOCKING)
def test_blocking_reads_ask(command):
    assert bash(command) == "ask"


def test_stdin_readers_are_allowed_only_as_the_right_side_of_a_pipe():
    assert bash("cat README.md | head -n 5") == "allow"
    assert bash("git log --oneline | grep fix | wc -l") == "allow"
    assert bash("head -n 5") == "ask"
    assert bash("git log | wc") == "allow"
    # The left side of a pipe still needs its own operand.
    assert bash("cat | head -n 5") == "ask"
    assert bash("grep foo | head -n 5") == "ask"
    # ...and the right side still has to pass the path checks for any operand it has.
    assert bash("git log | cat /dev/zero") == "ask"
    assert bash("git log | cat .env") == "ask"
    assert bash("git log | grep -e k /c/Users/Administrator/.codex") == "ask"


def test_gh_pr_checks_without_watch_still_reads():
    assert bash("gh pr checks 1") == "allow"
    assert bash("gh pr checks 1 --required --json state") == "allow"
    assert is_readonly_gh_query("gh pr checks 1 --required")
    for command in ("gh pr checks 1 --watch", "gh pr checks 1 --interval 5", "gh pr checks --watch --interval 10"):
        assert not is_readonly_gh_query(command)


# --- the policy loader knows the new fields ----------------------------------


@pytest.mark.parametrize("text", [
    '[[rules]]\nid="x"\ncommand=["cat"]\noperands="anything"',
    '[[rules]]\nid="x"\ncommand=["cat"]\nstdin="yes"',
    '[[rules]]\nid="x"\ncommand=["cat"]\nglobs=["-g"]',
    '[[rules]]\nid="x"\ncommand=["cat"]\nvalidator="fetch"',
    '[[rules]]\nid="x"\ncommand=["cat"]\nno_stdin_flags="-r"',
])
def test_policy_loader_rejects_invalid_new_fields(tmp_path, text):
    source = tmp_path / "readonly.toml"
    source.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        _readonly.load_policy(source)


def test_stdin_and_operand_policy_is_toml_driven(tmp_path):
    source = tmp_path / "readonly.toml"
    source.write_text('[[rules]]\nid="c"\ncommand=["cat"]\nstdin=true\n', encoding="utf-8")
    rule = _readonly.load_policy(source)[0]
    assert rule.matches(["cat", "README.md"])
    assert not rule.matches(["cat"])
    assert rule.matches(["cat"], piped=True)
    assert not rule.matches(["cat", ".env"])
