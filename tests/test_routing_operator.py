"""Admission mode changes eligibility, while the original margins still rank providers."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from lazytools.projects.admission import EngineBudget, TelemetryReading, WindowReading
from lazytools.routing import load_tiers, recommend, route, router
from lazytools.routing.catalogue import DEFAULT_PATH

NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)


def _inputs():
    readings = {
        engine: TelemetryReading(
            engine,
            "fake",
            NOW,
            (
                WindowReading(
                    "codex/10080m" if engine == "codex" else "weekly/all models",
                    used,
                    10080,
                    NOW + timedelta(days=5, hours=6),
                ),
            ),
        )
        for engine, used in (("codex", 50), ("claude_code", 40))
    }
    return {
        "catalogue": load_tiers(DEFAULT_PATH),
        "readings": readings,
        "budgets": {engine: EngineBudget(engine) for engine in readings},
        "in_flight": {},
        "now": NOW,
    }


def test_route_default_preserves_forecast_brake_and_operator_mode_scores_both():
    kwargs = _inputs()
    autonomous = route("writing", **kwargs)
    assert autonomous.provider is None
    assert all("forecast_breach" in reason for reason in autonomous.ineligible.values())

    direct = route("writing", operator_directed=True, **kwargs)
    assert direct.provider == "claude_code" and direct.reason == "weekly_margin"
    assert direct.ineligible == {}
    assert direct.scores["codex"]["weekly_margin"] == -77.5
    assert direct.scores["claude_code"]["weekly_margin"] == -37.5


def test_operator_mode_crosses_autonomous_boundary_but_preserves_margin_order():
    kwargs = _inputs()
    for engine, used in (("codex", 80), ("claude_code", 90)):
        old = kwargs["readings"][engine]
        kwargs["readings"][engine] = replace(old, windows=(replace(old.windows[0], used_percent=used, resets_at=None),))
    assert route("writing", **kwargs).provider is None
    direct = route("writing", operator_directed=True, **kwargs)
    assert direct.provider == "codex"
    assert direct.scores["codex"]["weekly_margin"] == -5
    assert direct.scores["claude_code"]["weekly_margin"] == -15


@pytest.mark.parametrize("used,in_flight", [(94, 0), (95, 0), (100, 0), (90, 4)])
def test_operator_mode_still_excludes_absolute_ceiling_including_reservations(used, in_flight):
    kwargs = _inputs()
    reading = kwargs["readings"]["codex"]
    kwargs["readings"]["codex"] = replace(reading, windows=(replace(reading.windows[0], used_percent=used),))
    kwargs["in_flight"] = {"codex": in_flight}
    decision = route("writing", operator_directed=True, **kwargs)
    assert decision.provider == "claude_code"
    assert decision.ineligible["codex"].startswith("admission_would_refuse:absolute_ceiling")


@pytest.mark.parametrize(
    "problem,reason",
    [
        ("error", "telemetry_unreadable"),
        ("stale", "telemetry_stale"),
        ("future", "telemetry_unreadable"),
        ("nan", "telemetry_window_unreadable"),
        ("missing_weekly", "telemetry_window_unreadable"),
        ("missing", "telemetry_missing"),
    ],
)
def test_operator_mode_preserves_telemetry_exclusions(problem, reason):
    kwargs = _inputs()
    reading = kwargs["readings"]["codex"]
    if problem == "missing":
        kwargs["readings"].pop("codex")
    elif problem == "error":
        kwargs["readings"]["codex"] = replace(reading, error="offline")
    elif problem in ("stale", "future"):
        kwargs["readings"]["codex"] = replace(
            reading, observed_at=NOW + timedelta(seconds=-21601 if problem == "stale" else 121)
        )
    else:
        window = (
            replace(reading.windows[0], used_percent=float("nan"))
            if problem == "nan"
            else WindowReading("codex/300m", 10, 300)
        )
        kwargs["readings"]["codex"] = replace(reading, windows=(window,))
    decision = route("writing", operator_directed=True, **kwargs)
    assert decision.provider == "claude_code"
    assert decision.ineligible["codex"].startswith(reason)


@pytest.mark.parametrize("operator_directed", [False, True])
def test_admission_receives_caller_mode_without_changing_scores(monkeypatch, operator_directed):
    original = router._admission_decide
    seen = []

    def decide(*args, **kwargs):
        seen.append(kwargs["operator_directed"])
        return original(*args, **kwargs)

    monkeypatch.setattr(router, "_admission_decide", decide)
    inputs = _inputs()
    inputs["catalogue"] = {
        "writing": replace(inputs["catalogue"]["writing"], steps=inputs["catalogue"]["writing"].steps[:1])
    }
    route("writing", operator_directed=operator_directed, **inputs)
    assert seen == [operator_directed, operator_directed]


def test_recommend_defaults_to_autonomous_and_forwards_operator_mode():
    kwargs = _inputs()
    kwargs.pop("budgets")
    assert recommend("writing", **kwargs).provider is None
    decision = recommend("writing", operator_directed=True, **kwargs)
    assert decision.provider == "claude_code" and decision.scores["claude_code"]["weekly_margin"] == -37.5
