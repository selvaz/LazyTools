"""lazytools.projects.intake -- the deterministic checklist, digest, and
draft-to-open promotion."""

from __future__ import annotations

from lazybridge import Store
from lazybridge.ext.planners import DurableBlackboard

from lazytools.projects import intake, records

_GOOD_SUBTASKS = [
    {"text": "step one", "acceptance_criteria": ["pytest tests/test_x.py passes"]},
    {"text": "step two", "acceptance_criteria": ["script runs without error"]},
]


def test_check_project_intake_flags_missing_fields() -> None:
    verdict = intake.check_project_intake(objective="", observable_result="", deadline="", subtasks=[])
    assert verdict.ok is False
    assert any("objective" in f for f in verdict.findings)
    assert any("deadline" in f for f in verdict.findings)
    assert any("subtasks" in f for f in verdict.findings)


def test_check_project_intake_flags_unparseable_deadline() -> None:
    verdict = intake.check_project_intake(objective="x", observable_result="y", deadline="eventually", subtasks=_GOOD_SUBTASKS)
    assert verdict.ok is False
    assert any("names no date" in f for f in verdict.findings)


def test_check_project_intake_flags_empty_assurance_criteria() -> None:
    subtasks = [{"text": "t", "acceptance_criteria": ["it works correctly"]}, {"text": "t2", "acceptance_criteria": ["y passes"]}]
    verdict = intake.check_project_intake(objective="x", observable_result="y", deadline="2026-12-01", subtasks=subtasks)
    assert verdict.ok is False
    assert any("assurance" in f for f in verdict.findings)


def test_check_project_intake_passes_on_well_formed_plan() -> None:
    verdict = intake.check_project_intake(objective="x", observable_result="y", deadline="2026-12-01", subtasks=_GOOD_SUBTASKS)
    assert verdict.ok is True
    assert verdict.findings == []


def test_plan_digest_stable_and_sensitive() -> None:
    d1 = intake.plan_digest(objective="x", observable_result="y", deadline="2026-12-01", subtasks=_GOOD_SUBTASKS)
    d2 = intake.plan_digest(objective="x", observable_result="y", deadline="2026-12-01", subtasks=_GOOD_SUBTASKS)
    assert d1 == d2
    d3 = intake.plan_digest(objective="x", observable_result="y changed", deadline="2026-12-01", subtasks=_GOOD_SUBTASKS)
    assert d1 != d3


def test_promotion_refusal_requires_matching_digest() -> None:
    assert intake.promotion_refusal(reviewed_digest=None, current_digest="abc") is not None
    assert intake.promotion_refusal(reviewed_digest="abc", current_digest="def") is not None
    assert intake.promotion_refusal(reviewed_digest="abc", current_digest="abc") is None


def test_promote_project_plan_full_flow() -> None:
    store = Store()
    records.open_project(store, project_id="alpha", title="Alpha", objective="do a thing")

    rejected = intake.promote_project_plan(store, "alpha", observable_result="y", deadline="2026-12-01", subtasks=_GOOD_SUBTASKS)
    assert rejected.startswith("REJECTED") and "not been reviewed" in rejected

    digest = intake.plan_digest(objective="do a thing", observable_result="y", deadline="2026-12-01", subtasks=_GOOD_SUBTASKS)
    assert intake.record_project_review(store, "alpha", reviewed_digest=digest) is True

    result = intake.promote_project_plan(store, "alpha", observable_result="y", deadline="2026-12-01", subtasks=_GOOD_SUBTASKS)
    assert "promoted project" in result
    record = records.get_project(store, "alpha")
    assert record.status == "open"
    assert record.observable_result == "y"
    assert record.target_completion_at is not None
    board = DurableBlackboard(store, plan_id="project:alpha").snapshot()
    assert [t["text"] for t in board.tasks] == ["step one", "step two"]


def test_promote_project_plan_refuses_checklist_failure() -> None:
    store = Store()
    records.open_project(store, project_id="alpha", title="Alpha", objective="do a thing")
    result = intake.promote_project_plan(store, "alpha", observable_result="y", deadline="not a date", subtasks=_GOOD_SUBTASKS)
    assert result.startswith("REJECTED") and "not defined well enough" in result


def test_promote_project_plan_refuses_digest_mismatch_after_edit() -> None:
    store = Store()
    records.open_project(store, project_id="alpha", title="Alpha", objective="do a thing")
    digest = intake.plan_digest(objective="do a thing", observable_result="y", deadline="2026-12-01", subtasks=_GOOD_SUBTASKS)
    intake.record_project_review(store, "alpha", reviewed_digest=digest)

    edited_subtasks = [*_GOOD_SUBTASKS, {"text": "sneaky new step", "acceptance_criteria": ["z passes"]}]
    result = intake.promote_project_plan(store, "alpha", observable_result="y", deadline="2026-12-01", subtasks=edited_subtasks)
    assert result.startswith("REJECTED") and "changed after it was reviewed" in result
