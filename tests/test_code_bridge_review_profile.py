"""Real-world command matrix for the local review approval profile."""

from __future__ import annotations

import asyncio

import pytest
from lazybridge.engines.coding import ApprovalRequest

from lazytools.code_bridge import _routine, _store
from lazytools.code_bridge._approvals import BridgeGate
from lazytools.code_bridge._policy import CODING_RULES, REVIEW_RULES


class _NoChannel:
    async def ask(self, prompt: str) -> bool:
        raise AssertionError(prompt)


def _tier(command: str, cwd: str = "/work/repo") -> str:
    gate = BridgeGate(channel=_NoChannel(), rules=REVIEW_RULES, cwd=cwd, profile="review")
    request = ApprovalRequest(provider="claude-code", kind="tool", name="Bash",
                              arguments={"command": command}, cwd=cwd)
    rule = gate._match(request)
    assert rule is not None
    return rule.tier


ALLOWED = [
    "git status --short", "git diff --check", "git log --oneline -5", "git show HEAD:README.md",
    "git check-ignore -v f", "git branch --list", "git rev-parse HEAD", "git remote -v",
    "python -m pytest", "pytest -q", "ruff check .", "mypy src", "npm ci", "npm run build",
    "cd x && PYTHONPATH=. python -m pytest -q | tail -3", "timeout 110 python -m pytest",
    "for i in 1 2; do echo $i; done", "rg -n profile src", "find . -name '*.py'", "cat README.md",
    "gh pr view 12", "gh pr checks 12", "gh pr diff 12", "gh run list",
    "gh api repos/o/r/pulls/1/comments --paginate", "gh api --method GET repos/o/r/pulls/1",
    "gh pr comment 12 --repo owner/repo --body-file review.md", "rm -rf node_modules",
    "rm build/output.txt", "mkdir -p build", "touch report.txt", "cp a.txt build/a.txt",
    "echo ok > report.txt", "python -m compileall src", "coverage run -m pytest",
    "powershell -Command \"python -m pytest -q\"", "pwsh -NoProfile -Command 'ruff check .'",
    "sh -c 'git status --short'", "bash -c 'npm run build'", "git grep TODO",
]


@pytest.mark.parametrize("command", ALLOWED)
def test_review_profile_auto_approves_harmless_commands(command: str):
    assert _tier(command) == "profile:review"


def _denied_commands() -> list[str]:
    git_verbs = ["push origin feature", "commit -m x", "merge topic", "rebase main", "pull",
                 "checkout other", "switch other", "cherry-pick HEAD~1"]
    commands = [f"git {verb}" for verb in git_verbs]
    commands += [f"git -c core.fsmonitor=false {verb}" for verb in git_verbs[:4]]
    commands += [
        "git reset --hard HEAD", "git branch -D old", "git --git-dir=.git push origin feature",
        "sh -c 'git push origin feature'", "bash -c 'git commit -m nope'", "; git push origin feature",
        "&& git commit -m nope", "gh pr merge 1", "gh pr close 1", "gh pr edit 1 --title x",
        "gh pr review 1 --approve", "gh pr ready 1", "gh pr comment 1 --body nope",
        "gh pr comment 1 --repo o/r --body-file x --edit-last", "gh api repos/o/r/issues -f title=x",
        "gh api repos/o/r/issues --field title=x", "gh api -X POST repos/o/r/issues",
        "gh api --method=PATCH repos/o/r/issues", "gh api repos/o/r/issues --input payload.json",
        "rm -rf /tmp/other", "rm -rf ..", "echo x > /tmp/out", "touch /tmp/out",
        "cp result.txt /tmp/result.txt", "mv result.txt ../result.txt", "rm -f .env",
        "npm publish", "pnpm publish", "cargo publish", "twine upload dist/*", "docker push o/image",
        "crontab -e", "at now", "schtasks /Create /TN x", "systemctl restart nginx",
        "service nginx stop", "sudo pytest", "powershell -Command \"git push origin feature\"",
        "pwsh -Command 'gh pr merge 1'", "gh release create v1", "git worktree add ../other other",
    ]
    return commands


@pytest.mark.parametrize("command", _denied_commands())
def test_review_profile_holds_every_denied_form(command: str):
    assert _tier(command) == "ask"


def test_default_profile_is_unchanged():
    gate = BridgeGate(channel=_NoChannel(), rules=CODING_RULES, cwd="/work/repo")
    request = ApprovalRequest(provider="claude-code", kind="tool", name="Bash",
                              arguments={"command": "npm ci"}, cwd="/work/repo")
    assert gate._match(request).tier == "ask"


def test_review_auto_approval_records_profile(tmp_path):
    store = _store.build_store(tmp_path / "jobs.sqlite")
    queue = _store.build_approval_queue(store)
    policy = _routine.load_policy(enabled=False).for_job(tmp_path, "job")
    gate = BridgeGate(channel=_NoChannel(), rules=REVIEW_RULES, cwd=str(tmp_path), profile="review",
                      routine=_routine.RoutineAuto(policy, store, queue, "job"))
    request = ApprovalRequest(provider="codex", kind="command", name="codex-shell",
                              arguments={"command": "python -m pytest"}, cwd=str(tmp_path),
                              raw={"command": "python -m pytest"})
    assert asyncio.run(gate(request)).action == "allow"
    records = [value for _, value in store.items(prefix=_store.APPROVAL_PREFIX)]
    assert len(records) == 1
    assert records[0]["approval_profile"] == "review"
    assert records[0]["approved_by"] == "profile:review"
    assert records[0]["consumed_at"]
    assert gate.log[-1].tier == "profile:review"
