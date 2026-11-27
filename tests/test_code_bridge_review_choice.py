"""Every row of the ``choose_reviewer`` decision table (see its docstring)."""

from __future__ import annotations

import pytest

from lazytools.code_bridge._review_choice import ReviewerChoice, choose_reviewer, opposite_engine

BOTH = frozenset({"codex", "claude"})
CODEX = frozenset({"codex"})


@pytest.mark.parametrize(
    "row,writer,installed,explicit,history,writer_id,reviewer_id,engine,independence,refusal",
    [
        ("1 same native id", "claude", BOTH, None, False, "id-1", "id-1", None, None, "not independent"),
        ("1 needs both ids", "claude", BOTH, None, False, None, None, "codex", "opposite-engine", None),
        ("2 opposite installed", "codex", BOTH, None, False, "id-1", "id-2", "claude", "opposite-engine", None),
        ("2 opposite installed alone", "claude", CODEX, None, True, None, None, "codex", "opposite-engine", None),
        ("2 explicit opposite", "codex", CODEX, "claude", False, None, None, "claude", "opposite-engine", None),
        ("3 writer only installed", "codex", CODEX, None, False, None, None, "codex", "same-provider-fresh", None),
        ("3 explicit writer engine", "codex", CODEX, "codex", False, None, None, "codex", "same-provider-fresh", None),
        ("4 nothing installed", "codex", frozenset(), None, False, None, None, None, None, "is unavailable"),
        ("5 explicit writer engine", "codex", BOTH, "codex", False, None, None, None, None,
         "runs on claude, not codex"),
        ("6 session with history", "codex", CODEX, None, True, None, None, None, None, "already has a conversation"),
    ],
)
def test_decision_table(row, writer, installed, explicit, history, writer_id, reviewer_id, engine, independence,
                        refusal):
    choice = choose_reviewer(writer, installed, explicit, history, writer_id, reviewer_id)
    assert (choice.engine, choice.independence) == (engine, independence), row
    if refusal is None:
        assert choice == ReviewerChoice(engine, independence), row
    else:
        assert choice.refusal_reason is not None and refusal in choice.refusal_reason, row


def test_opposite_engine():
    assert opposite_engine("codex") == "claude" and opposite_engine("claude") == "codex"
    with pytest.raises(ValueError):
        opposite_engine("other")
