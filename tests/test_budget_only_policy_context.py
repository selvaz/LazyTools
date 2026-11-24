"""A budget-only LazyBridge policy context (tree budget, no policy) carries no authority ceiling."""

from __future__ import annotations

from lazybridge.policy import TreeBudget, current_policy_context, policy_scope

from lazytools.code_bridge import _launch
from lazytools.connectors.code_support._policy import authorize


def test_budget_only_context_is_treated_as_no_policy(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    with policy_scope(None, tree_budget=TreeBudget(max_depth=2)):
        context = current_policy_context()
        assert context is not None and context.delegable_grants is None
        assert authorize("codex_ask", repo) is None
        launched = _launch.resolve_launch("codex", repo, "review", None, "auto")
        assert launched is not None
