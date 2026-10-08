from __future__ import annotations

import pytest
from lazybridge import Store
from lazybridge.ext.planners import DurableBlackboard

from _projects_parity import original_function
from lazytools.projects import intake, records

SUBTASKS = [
    {"text": "one", "acceptance_criteria": ["one file exists"]},
    {"text": "two", "acceptance_criteria": ["two files exist"]},
]


@pytest.mark.parametrize("status,review,current", [("draft", None, "a"), ("draft", "a", "b"), ("draft", "a", "a"), ("open", "a", "a"), ("open", "a", "b"), ("open", None, "b"), ("paused", "a", "a"), ("done", "a", "a"), (None, None, "a")])
def test_digest_transition_matches_original(status, review, current):
    stores = [Store(), Store()]
    for store in stores:
        if status is not None:
            records.open_project(store, project_id="alpha", title="Alpha", objective="files")
            records._apply(store, "alpha", {"status": status, "reviewed_digest": review}, allowed_from=("draft",))
    original = original_function("promote_project", get_project=records.get_project, _apply=records._apply, promotion_refusal=intake.promotion_refusal)
    assert original(stores[0], "alpha", current_digest=current) == intake.promote_project(stores[1], "alpha", current_digest=current)
    assert getattr(records.get_project(stores[0], "alpha"), "status", None) == getattr(records.get_project(stores[1], "alpha"), "status", None)


@pytest.mark.parametrize("findings,performed", [([], True), (["decorative"], True), (["outage"], False)])
async def test_async_promotion_matches_original_tool(findings, performed):
    stores = [Store(), Store()]
    seen = []

    async def reviewer(**kwargs):
        seen.append(kwargs)
        return findings, performed

    for store in stores:
        records.open_project(store, project_id="alpha", title="Alpha", objective="files")
    original = original_function(
        "promote_project_tool", store=stores[0], workspace_root=None, reviewer=reviewer,
        _default_reviewer=reviewer, get_project=records.get_project,
        check_project_intake=intake.check_project_intake, plan_digest=intake.plan_digest,
        record_intake_review=intake.record_project_review, _apply_intake_fields=intake._apply_intake_fields,
        _promote=intake.promote_project, DurableBlackboard=DurableBlackboard,
    )
    before = await original("alpha", "files exist", "2026-12-01", SUBTASKS)
    after = await intake.review_and_promote_project_plan(stores[1], "alpha", observable_result="files exist", deadline="2026-12-01", subtasks=SUBTASKS, reviewer=reviewer, reviewer_kwargs={"root": None})
    assert before == after
    for key in ("observable_result", "acceptance_criteria", "required_checks", "allowed_effects", "root"):
        assert seen[0][key] == seen[1][key]
    for field in ("status", "reviewed_digest", "observable_result", "acceptance_criteria", "target_completion_at"):
        assert getattr(records.get_project(stores[0], "alpha"), field) == getattr(records.get_project(stores[1], "alpha"), field)
    assert DurableBlackboard(stores[0], "project:alpha").snapshot().tasks == DurableBlackboard(stores[1], "project:alpha").snapshot().tasks


async def test_reviewer_not_performed_and_checklist_fail_closed():
    store = Store()
    records.open_project(store, project_id="alpha", title="Alpha", objective="files")
    called = []

    async def reviewer(**kwargs):
        called.append(kwargs)
        return [], False

    kwargs = dict(observable_result="files exist", deadline="2026-12-01", subtasks=SUBTASKS, reviewer=reviewer)
    assert "not defined" in await intake.review_and_promote_project_plan(store, "alpha", **{**kwargs, "deadline": "bad"})
    assert called == []
    assert "could not be performed" in await intake.review_and_promote_project_plan(store, "alpha", **kwargs)
    assert records.get_project(store, "alpha").reviewed_digest is None
    assert not DurableBlackboard(store, "project:alpha").snapshot().tasks


async def test_review_fences_project_changes_and_copies_plan():
    store = Store()
    records.open_project(store, project_id="alpha", title="Alpha", objective="files")

    async def reviewer(**kwargs):
        records._apply(store, "alpha", {"objective": "changed"}, allowed_from=("draft",))
        return [], True

    result = await intake.review_and_promote_project_plan(store, "alpha", observable_result="files exist", deadline="2026-12-01", subtasks=SUBTASKS, reviewer=reviewer)
    assert "changed while it was being reviewed" in result
    assert not DurableBlackboard(store, "project:alpha").snapshot().tasks


async def test_open_same_plan_is_idempotent_and_different_plan_refused():
    store = Store()
    records.open_project(store, project_id="alpha", title="Alpha", objective="files")
    calls = []

    async def reviewer(**kwargs):
        calls.append(kwargs)
        return [], True

    kwargs = dict(observable_result="files exist", deadline="2026-12-01", subtasks=SUBTASKS, reviewer=reviewer)
    assert "promoted" in await intake.review_and_promote_project_plan(store, "alpha", **kwargs)
    board = DurableBlackboard(store, "project:alpha")
    board.claim_next(owner="worker")
    raw = store.read(board.key)
    assert "promoted" in await intake.review_and_promote_project_plan(store, "alpha", **kwargs)
    assert len(calls) == 1 and store.read(board.key) == raw
    assert "changed after it was reviewed" in await intake.review_and_promote_project_plan(store, "alpha", **{**kwargs, "observable_result": "different"})


def test_digest_transition_cas_does_not_accept_concurrent_review(monkeypatch):
    store = Store()
    records.open_project(store, project_id="alpha", title="Alpha", objective="files")
    intake.record_project_review(store, "alpha", reviewed_digest="a")
    cas = store.compare_and_swap

    def race(key, expected, value):
        store.write(key, {**expected, "reviewed_digest": "b"})
        return cas(key, expected, value)

    monkeypatch.setattr(store, "compare_and_swap", race)
    assert "changed while" in intake.promote_project(store, "alpha", current_digest="a")
    assert records.get_project(store, "alpha").status == "draft"


async def test_reviewer_cannot_change_the_caller_plan_being_installed():
    store = Store()
    records.open_project(store, project_id="alpha", title="Alpha", objective="files")
    plan = [{"text": s["text"], "acceptance_criteria": list(s["acceptance_criteria"])} for s in SUBTASKS]

    async def reviewer(**kwargs):
        plan[0]["text"] = "changed while awaiting"
        return [], True

    result = await intake.review_and_promote_project_plan(store, "alpha", observable_result="files exist", deadline="2026-12-01", subtasks=plan, reviewer=reviewer)
    assert "promoted" in result
    assert DurableBlackboard(store, "project:alpha").snapshot().tasks[0]["text"] == "one"
