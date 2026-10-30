"""Regression tests for the second independent review of the Claude Bash gate."""

import pytest
from lazybridge.engines.coding import ApprovalRequest

from lazytools.code_bridge import _routine
from lazytools.code_bridge._approvals import BridgeGate, is_readonly_gh_query
from lazytools.code_bridge._policy import CODING_RULES


class Unused:
    async def ask(self, prompt: str) -> bool:
        raise AssertionError("_match must not ask")


def tier(name, arguments, *, cwd="C:/repo", temp_dir="C:/tmp/job"):
    gate = BridgeGate(channel=Unused(), rules=CODING_RULES, job_branch="feat/x", cwd=cwd, temp_dir=temp_dir)
    rule = gate._match(ApprovalRequest(provider="claude-code", kind="tool", name=name, arguments=arguments))
    assert rule is not None
    return rule.tier


def bash(command):
    return tier("Bash", {"command": command})


# --- 1. gh --jq / --template print the process environment -------------------

GH_JQ_TEMPLATE = [
    "gh pr view 1 --json title --jq env", "gh pr view 1 --json title --jq=env", "gh pr view 1 --json title -q env",
    "gh pr view 1 --json title -q=env", "gh pr view 1 --template '{{env}}'", "gh pr view 1 -t x", "gh pr view 1 --template=x",
    "gh api repos/a/b --jq 'env|keys'", "gh api repos/a/b --jq env", "gh api repos/a/b -q env",
    "gh api repos/a/b --template '{{env}}'", "gh api repos/a/b -t x", "gh api repos/a/b --paginate --jq '.[].body'",
    "gh pr list --jq env", "gh pr list --json number --jq env", "gh pr list -q env", "gh pr list --template x",
    "gh pr checks 1 --jq env", "gh pr checks 1 --json name -q env", "gh pr diff 1 --jq env",
    "gh run view 1 --json status --jq env", "gh run view 1 --template x", "gh run list --jq env", "gh run list -q env",
    "gh issue view 1 --json title --jq env", "gh issue view 1 -t x", "gh issue list --jq env", "gh issue list --template x",
]


@pytest.mark.parametrize("command", GH_JQ_TEMPLATE)
def test_gh_jq_and_template_always_ask(command):
    assert bash(command) == "ask"
    assert not is_readonly_gh_query(command)
    assert not is_readonly_gh_query(f"powershell.exe -Command '{command}'") or "'" in command
    assert not _routine._gh_read(command)


def test_gh_reads_without_jq_still_allow():
    for command in (
        "gh pr view 1 --json title", "gh pr list --json number", "gh pr checks 1 --json name", "gh run view 1 --json status",
        "gh run list --limit 3 --json status", "gh issue view 1 --json title", "gh api repos/a/b/pulls/1/comments --paginate",
    ):
        assert bash(command) == "allow", command
    # The Codex classifier's default table has no `pr list` / `issue` shapes.
    for command in ("gh pr view 1 --json title", "gh pr checks 1 --json name", "gh api repos/a/b/pulls/1/comments --paginate"):
        assert is_readonly_gh_query(command)
    assert _routine._gh_read("gh pr view 1 --json title")


# --- 2. git pathspec wildcards and magic -------------------------------------

GIT_PATHSPEC = [
    'git grep KEY -- ".e*"', 'git grep -e KEY ".en?"', 'git grep -e KEY -- ":(top).env"', 'git log -p -- ".en?"',
    'git show HEAD -- ".en?"', 'git diff HEAD -- ".e*"', 'git grep -e KEY -- "auth.js?n"', 'git grep -e KEY -- "*.ke?"',
    'git grep -e KEY -- ".[e]nv"', 'git grep -e KEY -- ".{e,x}nv"', 'git grep -e KEY -- ":!src"', 'git grep -e KEY ":/.env"',
    'git grep KEY -- "*"', 'git grep KEY -- "src/*"', 'git grep KEY ":(glob)**/.env"', 'git log -- "*.pem"',
    'git diff -- "*"', 'git diff --cached -- ".e*"', 'git show "HEAD@{1}"', 'git show ":.env"', 'git show ":0:.env"',
    'git ls-files ".e*"', 'git ls-files "*.pem"', 'git ls-files ":(top).env"', 'git status ".e*"', 'git status -- "*"',
    'git rev-parse "HEAD^{tree}"', 'git describe "v1.*"', 'git log "feat/*"', 'git diff "a{1,2}"', 'git log -- "[.]env"',
]


@pytest.mark.parametrize("command", GIT_PATHSPEC)
def test_git_pathspec_wildcards_and_magic_ask(command):
    assert bash(command) == "ask"


@pytest.mark.parametrize("command", [
    "git grep -n -e KEY -- src", "git grep KEY -- src/x.py", "git grep -n text", "git log -p -- src/x.py",
    "git show HEAD:README.md", "git diff HEAD -- src", "git ls-files src", "git branch --list 'feat/*'",
    "git branch --list 'release-?'", "git branch --contains HEAD",
])
def test_literal_git_operands_and_branch_patterns_stay_allowed(command):
    assert bash(command) == "allow"


# --- 3. git remote -v prints credentialed URLs -------------------------------


@pytest.mark.parametrize("command", ["git remote -v", "git remote", "git remote --verbose", "git remote get-url origin", "git remote show origin"])
def test_git_remote_asks(command):
    assert bash(command) == "ask"


# --- 4. Write/Edit scope -----------------------------------------------------

PROTECTED_WRITES = [
    ".claude/settings.json", ".claude/hooks/x.py", "C:/repo/.claude/settings.local.json", "sub/.claude/x", ".CLAUDE/x",
    ".mcp.json", "C:/repo/.mcp.json", "pkg/.mcp.json", ".husky/pre-commit", "C:/repo/.husky/_/husky.sh", ".HUSKY/x",
    ".env", ".env.local", ".env.example", "sub/.env", "C:/repo/.env.production", ".ENV", ".env.", ".env::$DATA",
    ".vscode/settings.json", "C:/repo/.vscode/settings.json", ".VSCODE/Settings.json",
    ".bashrc", ".profile", ".zshrc", ".bash_profile", ".zprofile", ".zshenv", "Microsoft.PowerShell_profile.ps1",
    "C:/repo/Microsoft.PowerShell_profile.ps1", "microsoft.powershell_profile.ps1", ".bashrc.",
    # outside the job: absolute or ..
    "C:/Users/Administrator/Documents/PowerShell/Microsoft.PowerShell_profile.ps1", "C:/other/x.py", "C:\\other\\x.py",
    "D:/repo/x.py", "/etc/passwd", "/c/other/x.py", "/c/Users/Administrator/.bashrc", "../x.py", "sub/../../x.py",
    "..\\x.py", "C:/repo/../other/x.py", "C:/repo2/x.py", "C:\\repo2\\x.py", "C:/repoevil/x.py", "C:/tmp/job2/x.py",
    "C:/tmp/x.py", "//host/share/x", "\\\\host\\share\\x", "/repo/x.py", "C:/repo/sub/../../etc/x", "C:/",
]


@pytest.mark.parametrize("path", PROTECTED_WRITES)
@pytest.mark.parametrize("tool", ["Write", "Edit"])
def test_protected_or_out_of_job_writes_ask(tool, path):
    assert tier(tool, {"file_path": path, "content": "x"}) == "ask"


@pytest.mark.parametrize("path", [
    "src/x.py", "README.md", "C:/repo/src/x.py", "C:\\repo\\src\\x.py", "c:/REPO/src/x.py", "/c/repo/src/x.py", "C:/repo/x.py",
    "C:/tmp/job/helper.py", "C:\\tmp\\job\\sub\\h.py", "/c/tmp/job/h.py", ".vscode/launch.json", ".github/workflows/ci.yml",
    "docs/claude.md", "src/env.py", "src/environment.md", "docs/husky.md", "mcp.json", ".venv/x", "./src/x.py", "src/./x.py",
    "src/.profile.md", "src/profile.py",
])
def test_ordinary_in_job_writes_stay_allowed(path):
    assert tier("Write", {"file_path": path, "content": "x"}) == "allow"
    assert tier("Edit", {"file_path": path}) == "allow"


def test_unknown_roots_make_every_absolute_write_ask_but_not_relative_ones():
    for tool in ("Write", "Edit"):
        assert tier(tool, {"file_path": "C:/repo/src/x.py"}, cwd=None, temp_dir=None) == "ask"
        assert tier(tool, {"file_path": "/tmp/x"}, cwd=None, temp_dir=None) == "ask"
        assert tier(tool, {"file_path": "src/x.py"}, cwd=None, temp_dir=None) == "allow"
    assert tier("Write", {"file_path": "C:/tmp/job/x.py"}, cwd="C:/repo", temp_dir=None) == "ask"


# --- 5. more credential names ------------------------------------------------


@pytest.mark.parametrize("command", [
    "cat id_ed25519", "cat id_ed25519.pub", "cat .ssh2/id_ed25519_sk", "cat id_ecdsa", "cat id_ecdsa.pub", "cat id_dsa", "cat id_dsa.pub",
    "cat .pgpass", "cat terraform.tfstate", "cat terraform.tfstate.backup", "cat prod.tfstate", "cat prod.tfvars",
    "cat prod.tfvars.json", "cat cert.pfx", "cat cert.p12", "cat kubeconfig", "cat .kube/config", "ls .kube", "cat .docker/config.json",
    "ls .docker", "cat KUBECONFIG", "cat Cert.PFX",
    "cat auth.json.bak", "cat .env.bak", "cat .env.local.bak", "cat x.pem.bak", "cat id_rsa.bak", "cat .npmrc.bak", "cat sub/.netrc.bak",
    "cat credentials.json.bak", "cat server.key.bak", "cat .pgpass.bak", "cat prod.tfvars.bak", "cat auth.json.bak.bak",
    "cat AUTH.JSON.BAK", "cat auth.json.bak.", "cat .ssh.bak", "grep -r -e KEY auth.json.bak", "head -n 1 .pgpass",
    "git show HEAD:auth.json.bak", "git show HEAD:id_ed25519", "git log -p -- terraform.tfstate", "git diff -- prod.tfvars",
    "rg -e KEY auth.json.bak", "rg -e KEY .kube",
])
def test_additional_credential_names_and_backup_copies_ask(command):
    assert bash(command) == "ask"


@pytest.mark.parametrize("command", [
    "cat notes.bak", "cat config.bak", "cat README.md.bak", "cat src/x.py", "cat docs/docker.md", "cat kubernetes.md", "cat src/pgp.py",
    "ls src", "git show HEAD:docs/docker.md",
])
def test_non_credential_names_stay_allowed(command):
    assert bash(command) == "allow"
