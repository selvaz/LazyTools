"""lazytools.projects.admission -- the engine quota brake, including the
per-project on/off switch (``project_admit``) new to this package."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from lazybridge import Store

from lazytools.projects import admission, brake


def _reading(used_percent: float, *, duration_minutes: int = 300, elapsed_fraction: float = 0.5, now: datetime | None = None) -> admission.TelemetryReading:
    now = now or datetime.now(UTC)
    resets_at = now + timedelta(minutes=duration_minutes * (1 - elapsed_fraction))
    return admission.TelemetryReading(
        engine="codex", source="test", observed_at=now,
        windows=(admission.WindowReading(window_id="w", used_percent=used_percent, duration_minutes=duration_minutes, resets_at=resets_at),),
    )


def test_decide_admits_comfortably_below_boundary() -> None:
    budget = admission.EngineBudget(engine="codex", ceiling_percent=95.0, autonomous_fraction=0.8)
    reading = _reading(10.0, elapsed_fraction=0.5)
    decision = admission.decide(reading, budget, operator_directed=False)
    assert decision.allowed is True
    assert decision.reason == "admitted"


def test_decide_refuses_at_absolute_ceiling() -> None:
    budget = admission.EngineBudget(engine="codex", ceiling_percent=95.0)
    reading = _reading(96.0, elapsed_fraction=0.9)
    decision = admission.decide(reading, budget, operator_directed=True)  # even operator-directed
    assert decision.allowed is False
    assert decision.reason == "absolute_ceiling"


def test_decide_refuses_at_autonomous_boundary_unless_operator_directed() -> None:
    budget = admission.EngineBudget(engine="codex", ceiling_percent=95.0, autonomous_fraction=0.8)  # boundary=76
    reading = _reading(80.0, elapsed_fraction=0.9)
    refused = admission.decide(reading, budget, operator_directed=False)
    assert refused.allowed is False and refused.reason == "autonomous_boundary"
    allowed = admission.decide(reading, budget, operator_directed=True)
    assert allowed.allowed is True


def test_decide_telemetry_unreadable_on_error() -> None:
    budget = admission.EngineBudget(engine="codex")
    reading = admission.TelemetryReading(engine="codex", source="test", observed_at=datetime.now(UTC), error="boom")
    decision = admission.decide(reading, budget, operator_directed=False)
    assert decision.allowed is False and decision.reason == "telemetry_unreadable"


def test_decide_telemetry_stale_refuses() -> None:
    budget = admission.EngineBudget(engine="codex", max_telemetry_age_seconds=60.0)
    old = datetime.now(UTC) - timedelta(hours=1)
    reading = _reading(5.0, now=old)
    decision = admission.decide(reading, budget, operator_directed=False)
    assert decision.allowed is False and decision.reason == "telemetry_stale"


def test_decide_forecast_breach_refuses_early_overpace() -> None:
    budget = admission.EngineBudget(engine="codex", ceiling_percent=95.0)
    # 10% elapsed of a long (>=1 day) window at 50% used -> projects way past ceiling,
    # and elapsed is above the long-window forecast floor (20%) only if >=0.2; use 0.25.
    reading = _reading(50.0, duration_minutes=24 * 60, elapsed_fraction=0.25)
    decision = admission.decide(reading, budget, operator_directed=False)
    assert decision.allowed is False
    assert decision.reason == "forecast_breach"


def test_decide_malformed_window_is_unreadable() -> None:
    budget = admission.EngineBudget(engine="codex")
    reading = admission.TelemetryReading(
        engine="codex", source="test", observed_at=datetime.now(UTC),
        windows=(admission.WindowReading(window_id="w", used_percent=float("nan")),),
    )
    decision = admission.decide(reading, budget, operator_directed=False)
    assert decision.allowed is False and decision.reason == "telemetry_unreadable"


def test_admit_reserves_and_release_frees_it() -> None:
    store = Store()
    budget = admission.EngineBudget(engine="codex", ceiling_percent=95.0)
    reading = _reading(10.0)
    decision = admission.admit(store, budget=budget, reading=reading)
    assert decision.allowed is True
    assert admission.in_flight_count(store, "codex") == 1
    assert admission.release(store, engine="codex", admission_id=decision.admission_id) is True
    assert admission.in_flight_count(store, "codex") == 0


def test_admit_reservation_raises_effective_percent_for_concurrent_callers() -> None:
    store = Store()
    budget = admission.EngineBudget(engine="codex", ceiling_percent=95.0, autonomous_fraction=0.8, per_job_reserve_percent=5.0)
    # at 70% used, boundary is 76: first admit reserves +5 (1 in-flight incl self, effective 75 < 76).
    # elapsed near the end of the window so the pace forecast (~73.7%) stays well under its own
    # limit (~95.5%) and does not refuse first on forecast_breach instead of boundary.
    reading = _reading(70.0, elapsed_fraction=0.95)
    first = admission.admit(store, budget=budget, reading=reading)
    assert first.allowed is True
    second = admission.admit(store, budget=budget, reading=reading)
    # second sees 1 in-flight already, reserves (1+1)*5=10 -> effective 80 >= boundary 76
    assert second.allowed is False
    assert second.reason == "autonomous_boundary"


def test_admit_shadow_mode_never_refuses_but_records_would_have() -> None:
    store = Store()
    budget = admission.EngineBudget(engine="codex", ceiling_percent=95.0, shadow=True)
    reading = _reading(99.0, elapsed_fraction=0.9)
    decision = admission.admit(store, budget=budget, reading=reading)
    assert decision.allowed is True
    assert decision.shadow is True
    assert decision.detail is not None and admission.SHADOW_MARK in decision.detail
    findings = admission.shadow_findings(store)
    assert any("codex" in line and "would have been refused" in line for line in findings)


def test_preflight_does_not_reserve() -> None:
    store = Store()
    budget = admission.EngineBudget(engine="codex", ceiling_percent=95.0)
    reading = _reading(10.0)
    decision = admission.preflight(store, budget=budget, reading=reading)
    assert decision.allowed is True
    assert admission.in_flight_count(store, "codex") == 0


def test_review_flag_admits_past_boundary_but_not_past_ceiling() -> None:
    store = Store()
    budget = admission.EngineBudget(engine="codex", ceiling_percent=95.0, autonomous_fraction=0.5)  # boundary 47.5
    reading = _reading(60.0, elapsed_fraction=0.5)
    decision = admission.admit(store, budget=budget, reading=reading, review=True)
    assert decision.allowed is True

    store2 = Store()
    reading_over = _reading(96.0, elapsed_fraction=0.9)
    decision_over = admission.admit(store2, budget=budget, reading=reading_over, review=True)
    assert decision_over.allowed is False
    assert decision_over.reason == "absolute_ceiling"


def test_project_admit_bypasses_quota_when_brake_disabled() -> None:
    store = Store()
    brake.set_project_brake_enabled(store, "alpha", False)
    budget = admission.EngineBudget(engine="codex", ceiling_percent=95.0)
    reading = admission.TelemetryReading(engine="codex", source="test", observed_at=datetime.now(UTC), error="unreachable")
    decision = admission.project_admit(store, "alpha", budget=budget, reading=reading)
    assert decision.allowed is True
    assert decision.reason == "project_brake_disabled"


def test_project_admit_defers_to_admit_when_brake_enabled() -> None:
    store = Store()
    # no explicit brake record -> defaults enabled
    budget = admission.EngineBudget(engine="codex", ceiling_percent=95.0)
    reading = admission.TelemetryReading(engine="codex", source="test", observed_at=datetime.now(UTC), error="unreachable")
    decision = admission.project_admit(store, "alpha", budget=budget, reading=reading)
    assert decision.allowed is False
    assert decision.reason == "telemetry_unreadable"


def test_under_plan_warning_only_fires_well_below_plan() -> None:
    budget = admission.EngineBudget(engine="codex", ceiling_percent=95.0)
    low = _reading(2.0, duration_minutes=24 * 60, elapsed_fraction=0.3)
    assert admission.under_plan_warning(low, budget) is not None
    high = _reading(80.0, duration_minutes=24 * 60, elapsed_fraction=0.5)
    assert admission.under_plan_warning(high, budget) is None


def test_claude_reading_includes_the_session_window_beside_the_weekly_ones(monkeypatch):
    """The 5-hour session window is the first to run out under a burst of
    delegated work; a reading with only the weekly figure would let the brake
    admit that burst blind."""
    import asyncio
    from datetime import UTC, datetime
    from types import SimpleNamespace

    import lazybridge.engines.claude_code.usage as usage

    from lazytools.projects import quota_telemetry

    snapshot = SimpleNamespace(
        session=SimpleNamespace(used_percent=91, resets_at=None),
        weekly={"all models": SimpleNamespace(used_percent=40, resets_at=None)},
    )

    async def fake_fetch(**_kwargs):
        return snapshot

    monkeypatch.setattr(usage, "fetch_claude_usage", fake_fetch)
    reading = asyncio.run(quota_telemetry._read_claude(datetime.now(UTC)))
    assert reading.error is None
    by_id = {w.window_id: w for w in reading.windows}
    assert by_id["session"].used_percent == 91.0
    assert by_id["session"].duration_minutes == quota_telemetry.CLAUDE_SESSION_MINUTES
    assert by_id["weekly/all models"].used_percent == 40.0


def test_claude_reading_with_only_a_session_window_is_still_usable(monkeypatch):
    import asyncio
    from datetime import UTC, datetime
    from types import SimpleNamespace

    import lazybridge.engines.claude_code.usage as usage

    from lazytools.projects import quota_telemetry

    async def fake_fetch(**_kwargs):
        return SimpleNamespace(session=SimpleNamespace(used_percent=10, resets_at=None), weekly={})

    monkeypatch.setattr(usage, "fetch_claude_usage", fake_fetch)
    reading = asyncio.run(quota_telemetry._read_claude(datetime.now(UTC)))
    assert reading.error is None
    assert [w.window_id for w in reading.windows] == ["session"]


def test_project_preflight_agrees_with_project_admit_for_a_brake_disabled_project() -> None:
    """The no-reservation check must not refuse what admission would allow:
    a brake-disabled project's reading carries no windows, and the bare
    preflight called that 'telemetry_unreadable'."""
    store = Store()
    brake.set_project_brake_enabled(store, "alpha", False)
    budget = admission.EngineBudget(engine="codex", ceiling_percent=95.0)
    reading = admission.TelemetryReading(engine="codex", source="project brake disabled", observed_at=datetime.now(UTC))
    decision = admission.project_preflight(store, "alpha", budget=budget, reading=reading)
    assert decision.allowed is True
    assert decision.reason == "project_brake_disabled"
    assert admission.in_flight_count(store, "codex") == 0  # nothing reserved


def test_project_preflight_defers_to_preflight_when_brake_enabled() -> None:
    store = Store()
    budget = admission.EngineBudget(engine="codex", ceiling_percent=95.0)
    reading = admission.TelemetryReading(engine="codex", source="test", observed_at=datetime.now(UTC), error="unreachable")
    decision = admission.project_preflight(store, "alpha", budget=budget, reading=reading)
    assert decision.allowed is False
    assert decision.reason == "telemetry_unreadable"
