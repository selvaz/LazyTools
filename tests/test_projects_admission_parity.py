from __future__ import annotations

from dataclasses import fields, replace
from datetime import UTC, datetime

import pytest
from lazybridge import Store

from _projects_parity import original_function
from lazytools.projects import admission, brake

NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)


def approval_hook(reference, consume, budget, reading, observed):
    def post(store, decision, before):
        observed.append(before)
        live = admission._live_reservations(before, now=NOW, ttl=budget.reservation_ttl_seconds)
        autonomous = admission.decide(reading, budget, operator_directed=False, in_flight=len(live), now=NOW)
        if autonomous.allowed:
            return None
        if not consume(store, reference, now=NOW):
            return admission.AdmissionDecision(allowed=False, reason="approval_already_spent", engine=budget.engine,
                operator_directed=True, decided_at=NOW, telemetry_source=reading.source,
                detail="that approval has already been used; ask for a new one")
        return replace(decision, spent_approval=reference.strip())
    return post


@pytest.mark.parametrize("used,spent,boosted,review,disabled", [
    (10, False, False, False, False), (80, False, False, False, False),
    (80, True, False, False, False), (96, False, False, False, False),
    (80, False, True, False, False), (80, False, False, True, False),
    (80, False, False, False, True),
])
def test_approval_hook_matches_current_ceo_wrapper(used, spent, boosted, review, disabled):
    stores = [Store(), Store()]
    consumed, observed = [], []
    budget = admission.EngineBudget(engine="codex")
    reading = admission.TelemetryReading(engine="codex", source="fixture", observed_at=NOW, windows=(admission.WindowReading("w", used),))

    def consume(store, reference, *, now):
        consumed.append(store)
        return not spent

    for store in stores:
        if disabled:
            brake.set_project_brake_enabled(store, "alpha", False)
    observer = original_function("adoption__AdmissionStore")
    original = original_function("adoption_admit", datetime=datetime, UTC=UTC, fields=fields, replace=replace,
        mechanism=admission, AdmissionDecision=admission.AdmissionDecision, _AdmissionStore=observer,
        _doc_key=admission._doc_key, decide=admission.decide, release=admission.release,
        _human_approval_exists=lambda *args: True, _consume_human_approval=consume)
    before = original(stores[0], budget=budget, reading=reading, operator_reference=" ticket ", boosted=boosted, review=review, now=NOW, project_id="alpha")
    hook = None if boosted or review else approval_hook(" ticket ", consume, budget, reading, observed)
    after = admission.project_admit(stores[1], "alpha", budget=budget, reading=reading, operator_directed=True, review=review, now=NOW, post_reservation=hook)
    for field in fields(admission.AdmissionDecision):
        if field.name != "admission_id":
            assert getattr(before, field.name) == getattr(after, field.name), field.name
    assert admission.in_flight_count(stores[0], "codex", now=NOW) == admission.in_flight_count(stores[1], "codex", now=NOW)
    assert len(consumed) == (2 if used == 80 and not boosted and not review and not disabled else 0)


def test_hook_uses_winning_input_and_runs_once_after_retry(monkeypatch):
    store = Store()
    budget = admission.EngineBudget(engine="codex")
    reading = admission.TelemetryReading(engine="codex", source="fixture", observed_at=NOW, windows=(admission.WindowReading("w", 73),))
    key = admission._doc_key("codex")
    cas = store.compare_and_swap
    races, seen = [], []

    def race(k, expected, value):
        if k == key and not races:
            races.append(k)
            store.write(k, {"in_flight": [{"admission_id": "other", "started_at": NOW.isoformat()}], "decisions": []})
            return False
        won = cas(k, expected, value)
        if won and k == key:
            # A further admission between the winning CAS and the callback.
            current = store.read(k)
            store.write(k, {**current, "in_flight": [*current["in_flight"], {"admission_id": "later", "started_at": NOW.isoformat()}]})
        return won

    monkeypatch.setattr(store, "compare_and_swap", race)
    decision = admission.admit(store, budget=budget, reading=reading, operator_directed=True, now=NOW,
        post_reservation=lambda s, d, before: seen.append(before))
    assert decision.allowed and len(seen) == 1
    assert [row["admission_id"] for row in seen[0]["in_flight"]] == ["other"]


def test_hook_exception_releases_reservation_and_propagates():
    store = Store()
    budget = admission.EngineBudget(engine="codex")
    reading = admission.TelemetryReading(engine="codex", source="fixture", observed_at=NOW, windows=(admission.WindowReading("w", 10),))

    def fail(*args):
        raise RuntimeError("ticket failure")

    with pytest.raises(RuntimeError, match="ticket failure"):
        admission.admit(store, budget=budget, reading=reading, now=NOW, post_reservation=fail)
    assert admission.in_flight_count(store, "codex", now=NOW) == 0
    assert "spent_approval" not in store.read(admission._doc_key("codex"))["decisions"][0]
