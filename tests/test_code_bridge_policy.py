"""Direct tests of the ``CODING_RULES`` table against a real ``TieredGate``.

No engine involved: these drive ``TieredGate._match`` straight, the same way
``lazybridge``'s own ``tests/unit/ext/approval/test_tiered.py`` tests a rule
table, to pin tier decisions for the dangerous Bash forms independently of
whether an engine ever actually asks for them.
"""

from __future__ import annotations

from lazybridge.engines.coding import ApprovalRequest
from lazybridge.ext.approval import TieredGate

from lazytools.code_bridge._policy import CODING_RULES


def _tier(command: str, *, kind: str = "tool", name: str = "Bash") -> str:
    gate = TieredGate(channel=None, rules=CODING_RULES)  # channel unused: _match never asks
    request = ApprovalRequest(provider="claude-code", kind=kind, name=name, arguments={"command": command})
    rule = gate._match(request)
    return rule.tier if rule is not None else "unmatched"


def test_reads_and_edits_run_free():
    assert _tier("", kind="tool", name="Read") == "allow"
    assert _tier("", kind="tool", name="Write") == "allow"
    assert _tier("", kind="tool", name="Edit") == "allow"


def test_plain_git_status_diff_log_run_free_via_the_bash_catchall():
    assert _tier("git status --short") == "allow"
    assert _tier("git diff") == "allow"
    assert _tier("git log --oneline -5") == "allow"


def test_add_and_commit_are_session_not_allow():
    assert _tier("git add -A") == "session"
    assert _tier("git commit -m 'msg'") == "session"


def test_plain_push_is_session_but_dangerous_forms_always_ask():
    # Implicit destination: wherever the checked-out branch goes, main
    # included, so never session-grantable. Found by Codex review.
    assert _tier("git push") == "ask"
    assert _tier("git push origin") == "ask"
    assert _tier("git push origin HEAD") == "ask"
    assert _tier("git push origin feature") == "session"
    assert _tier("git push --force origin feature") == "ask"
    assert _tier("git push -f origin feature") == "ask"
    assert _tier("git push origin :feature") == "ask"


def test_push_to_default_branch_always_asks_even_with_extra_refspecs():
    assert _tier("git push origin main") == "ask"
    assert _tier("git push origin master") == "ask"
    # A multi-refspec push where main/master is not the LAST token: the
    # narrower "git push * main" pattern alone would miss this. Regression
    # for the Codex review finding that hardened these to "*main*"/"*master*".
    assert _tier("git push origin main feature") == "ask"
    assert _tier("git push origin feature main") == "ask"
    assert _tier("git push origin refs/heads/main") == "ask"


def test_merge_reset_rm_delete_always_ask():
    assert _tier("gh pr merge 42") == "ask"
    assert _tier("git reset --hard HEAD~1") == "ask"
    assert _tier("rm -rf scratch") == "ask"
    assert _tier("git push origin --delete feature") == "ask"


def test_codex_sandbox_escalation_always_asks():
    assert _tier("anything at all", kind="command", name="codex-shell") == "ask"


def test_unknown_tool_name_is_unmatched_default_deny():
    assert _tier("", kind="tool", name="some_custom_tool") == "unmatched"
