"""``route()``: a PURE, DETERMINISTIC function, tested the way ``lazytools.projects.admission.decide`` is
-- every case named for the way it could fail towards routing something it should not.

PR1 (docs/design-router-modelli-2026-09-24.md, sections 1-7 + v3 A-E + Codex's corrections
in F) plus the 25/09 operator decision (docs/architettura 06+11): the router walks a tier's
ladder IN ORDER, falling through to the next rung when nobody is eligible on the current one
-- no store, no network, no LLM choice -- everything here is built by hand.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from lazytools.projects.admission import MAX_TELEMETRY_SKEW_SECONDS, EngineBudget, TelemetryReading, WindowReading
from lazytools.routing.catalogue import StepModel, TierCatalogue, TierStep
from lazytools.routing.router import (
    HYSTERESIS_MARGIN_POINTS,
    CapabilityRequirement,
    ContinuityHint,
    ProviderScore,
    WindowAvailability,
    _find_window,
    _price_and_order_tiebreak,
    _score_provider,
    _weekly_window_id,
    route,
)

NOW = datetime.now(UTC)


def _budget(engine: str, **kwargs) -> EngineBudget:
    return EngineBudget(
        engine=engine, ceiling_percent=100.0, autonomous_fraction=0.8, per_job_reserve_percent=1.0, **kwargs
    )


def _reading(
    engine: str, *windows: WindowReading, error: str | None = None, observed_at: datetime = NOW
) -> TelemetryReading:
    return TelemetryReading(
        engine=engine, source="test fixture", observed_at=observed_at, windows=tuple(windows), error=error
    )


def _weekly(used: float, *, window_id: str) -> WindowReading:
    # resets_at intentionally None: the position margin (autonomous_percent - used - reserve)
    # does not need it, and leaving it out keeps every test's forecast margin at None so the
    # scenario tests exactly one thing at a time.
    return WindowReading(window_id=window_id, used_percent=used, duration_minutes=10080, resets_at=None)


def _five_hour(used: float, *, window_id: str) -> WindowReading:
    return WindowReading(window_id=window_id, used_percent=used, duration_minutes=300, resets_at=None)


def _writing_catalogue(
    codex_model: str = "gpt-6.1-sol", claude_model: str = "claude-sonnet-5"
) -> dict[str, TierCatalogue]:
    step = TierStep(
        providers=(
            StepModel(provider="codex", model=codex_model, effort="xhigh"),
            StepModel(provider="claude_code", model=claude_model, effort="high"),
        )
    )
    return {"writing": TierCatalogue(name="writing", steps=(step,))}


def _route(catalogue, readings, budgets, in_flight=None, **kwargs):
    return route(
        "writing",
        catalogue=catalogue,
        readings=readings,
        budgets=budgets,
        in_flight=in_flight or {"codex": 0, "claude_code": 0},
        now=NOW,
        **kwargs,
    )


# --- the (tier, model) -> window mapping, and its two distinct sentinels -----------------------------------------


def test_the_weekly_window_mapping_is_explicit_per_engine_and_model() -> None:
    assert _weekly_window_id("codex", "gpt-6.1-sol") == "codex"
    assert _weekly_window_id("codex", "gpt-6-luna") == "codex"  # Codex has no per-model bucket at all
    assert _weekly_window_id("claude_code", "claude-sonnet-5") == "all models"
    assert _weekly_window_id("claude_code", "claude-fable-5-1") == "fable"


def test_a_fable_window_constrains_a_fable_call_but_not_a_sonnet_one_reading_the_same_telemetry() -> None:
    """The design's own example (section 3.1.4): Fable's window is not applicable to Sonnet,
    so a Fable bucket near its ceiling must not drag a Sonnet-tier score down with it."""
    reading = _reading(
        "claude_code",
        _weekly(99.0, window_id="weekly/fable"),
        _weekly(5.0, window_id="weekly/all models"),
    )
    budget = _budget("claude_code")

    sonnet = _score_provider(
        engine="claude_code", model="claude-sonnet-5", reading=reading, budget=budget, in_flight=0, moment=NOW
    )
    fable = _score_provider(
        engine="claude_code", model="claude-fable-5-1", reading=reading, budget=budget, in_flight=0, moment=NOW
    )

    assert isinstance(sonnet, ProviderScore) and sonnet.weekly_position_margin > 0  # untouched by Fable's 99%
    # Its OWN bucket is so nearly exhausted (99%) that admission.decide() itself would refuse
    # ordinary autonomous work there -- ineligible outright (a string reason), not merely a
    # ProviderScore with a negative margin (25/09 fix, Codex review: a margin that would be
    # refused is never just "worse").
    assert isinstance(fable, str) and fable.startswith("admission_would_refuse:")


def test_an_applicable_but_missing_window_is_unreadable_not_not_applicable() -> None:
    """The reading has no 'all models' bucket at all -- unlike the Fable case above, this
    window WOULD apply to Sonnet, so its absence is a real failure, distinct from
    'irrelevant'. Two sentinels, not one None wearing two hats (design section 3.1.4)."""
    empty = _reading("claude_code")
    assert _find_window(empty, bucket="all models", weekly=True) is WindowAvailability.UNREADABLE

    reading = _reading("claude_code", _weekly(50.0, window_id="weekly/fable"))
    assert _find_window(reading, bucket="all models", weekly=True) is WindowAvailability.UNREADABLE


# --- eligibility ----------------------------------------------------------------------------------------------


def test_a_capability_requiring_images_only_leaves_codex_eligible() -> None:
    catalogue = _writing_catalogue()
    readings = {
        "codex": _reading("codex", _weekly(10.0, window_id="codex/10080m")),
        "claude_code": _reading("claude_code", _weekly(1.0, window_id="weekly/all models")),  # far better margin
    }
    budgets = {"codex": _budget("codex"), "claude_code": _budget("claude_code")}

    decision = _route(catalogue, readings, budgets, capability=CapabilityRequirement(images=True))

    assert decision.provider == "codex"
    assert decision.ineligible.get("claude_code") == "capability_requires_codex"


def test_unreadable_telemetry_makes_that_provider_ineligible_not_free_capacity() -> None:
    catalogue = _writing_catalogue()
    readings = {
        "codex": _reading("codex", error="RuntimeError: app-server unreachable"),
        "claude_code": _reading("claude_code", _weekly(50.0, window_id="weekly/all models")),
    }
    budgets = {"codex": _budget("codex"), "claude_code": _budget("claude_code")}

    decision = _route(catalogue, readings, budgets)

    assert decision.provider == "claude_code"
    assert "telemetry_unreadable" in decision.ineligible["codex"]


def test_stale_telemetry_makes_that_provider_ineligible_the_same_way_admission_decide_does() -> None:
    """P2 correction: route() must judge staleness exactly like admission.decide() -- same
    constant (EngineBudget.max_telemetry_age_seconds), same outcome (ineligible, never scored
    on a number nobody can vouch for)."""
    catalogue = _writing_catalogue()
    budget = _budget("codex")
    stale_at = NOW - timedelta(seconds=budget.max_telemetry_age_seconds + 1.0)
    readings = {
        "codex": _reading("codex", _weekly(10.0, window_id="codex/10080m"), observed_at=stale_at),
        "claude_code": _reading("claude_code", _weekly(10.0, window_id="weekly/all models")),
    }
    budgets = {"codex": budget, "claude_code": _budget("claude_code")}

    decision = _route(catalogue, readings, budgets)

    assert decision.provider == "claude_code"
    assert "telemetry_stale" in decision.ineligible["codex"]


def test_a_reading_stamped_in_the_future_beyond_the_skew_tolerance_is_also_ineligible() -> None:
    """The clocks disagree -- unknown, not free capacity (same admission.py rule, same
    MAX_TELEMETRY_SKEW_SECONDS constant, reused rather than reimplemented)."""
    catalogue = _writing_catalogue()
    future = NOW + timedelta(seconds=MAX_TELEMETRY_SKEW_SECONDS + 1.0)
    readings = {
        "codex": _reading("codex", _weekly(10.0, window_id="codex/10080m"), observed_at=future),
        "claude_code": _reading("claude_code", _weekly(10.0, window_id="weekly/all models")),
    }
    budgets = {"codex": _budget("codex"), "claude_code": _budget("claude_code")}

    decision = _route(catalogue, readings, budgets)

    assert decision.provider == "claude_code"
    assert "telemetry_unreadable" in decision.ineligible["codex"]
    assert "future" in decision.ineligible["codex"]


def test_a_reading_within_the_ordinary_skew_tolerance_still_scores_normally() -> None:
    """A few seconds of clock jitter between provider and caller is ordinary, not a refusal
    -- only skew PAST the tolerance (admission.py's own MAX_TELEMETRY_SKEW_SECONDS) is."""
    catalogue = _writing_catalogue()
    slightly_ahead = NOW + timedelta(seconds=5.0)
    readings = {
        "codex": _reading("codex", _weekly(10.0, window_id="codex/10080m"), observed_at=slightly_ahead),
        "claude_code": _reading("claude_code", _weekly(80.0, window_id="weekly/all models")),
    }
    budgets = {"codex": _budget("codex"), "claude_code": _budget("claude_code")}

    decision = _route(catalogue, readings, budgets)

    assert decision.provider == "codex"
    assert "codex" not in decision.ineligible


def test_both_providers_ineligible_fails_safe_with_no_default_fallback() -> None:
    catalogue = _writing_catalogue()
    readings = {
        "codex": _reading("codex", error="boom"),
        "claude_code": _reading("claude_code", error="boom too"),
    }
    budgets = {"codex": _budget("codex"), "claude_code": _budget("claude_code")}

    decision = _route(catalogue, readings, budgets)

    assert decision.provider is None
    assert decision.model is None
    assert decision.reason.startswith("no_eligible_provider")


def test_continuity_keeps_the_last_engine_unless_it_is_ineligible_or_has_failed_twice() -> None:
    catalogue = _writing_catalogue()
    # Codex scores far better on quota, but continuity should win anyway.
    readings = {
        "codex": _reading("codex", _weekly(1.0, window_id="codex/10080m")),
        "claude_code": _reading("claude_code", _weekly(50.0, window_id="weekly/all models")),
    }
    budgets = {"codex": _budget("codex"), "claude_code": _budget("claude_code")}

    kept = _route(catalogue, readings, budgets, continuity=ContinuityHint(engine="claude_code", failed_attempts=0))
    assert kept.provider == "claude_code" and kept.reason == "continuity"

    # Two failures already: continuity no longer applies, quota decides instead.
    scored = _route(catalogue, readings, budgets, continuity=ContinuityHint(engine="claude_code", failed_attempts=2))
    assert scored.provider == "codex" and scored.reason != "continuity"

    # Ineligible continuity engine: falls through to ordinary scoring.
    unreadable = dict(readings)
    unreadable["claude_code"] = _reading("claude_code", error="boom")
    fallback = _route(
        catalogue, unreadable, budgets, continuity=ContinuityHint(engine="claude_code", failed_attempts=0)
    )
    assert fallback.provider == "codex" and fallback.reason != "continuity"


# --- review: only the opposite provider, or nobody --------------------------------------------------------------


def test_review_only_considers_the_provider_opposite_the_writer() -> None:
    catalogue = _writing_catalogue()
    readings = {
        "codex": _reading("codex", _weekly(10.0, window_id="codex/10080m")),
        "claude_code": _reading("claude_code", _weekly(10.0, window_id="weekly/all models")),
    }
    budgets = {"codex": _budget("codex"), "claude_code": _budget("claude_code")}

    decision = _route(catalogue, readings, budgets, writer_provider_for_review="claude_code")

    assert decision.provider == "codex"
    assert decision.ineligible.get("claude_code") == "same_as_writer"


def test_review_with_no_adequate_opposite_provider_fails_safe_to_human_review() -> None:
    step = TierStep(providers=(StepModel(provider="claude_code", model="claude-sonnet-5", effort="high"),))
    catalogue = {"writing": TierCatalogue(name="writing", steps=(step,))}
    readings = {"claude_code": _reading("claude_code", _weekly(1.0, window_id="weekly/all models"))}
    budgets = {"claude_code": _budget("claude_code")}

    decision = _route(catalogue, readings, budgets, writer_provider_for_review="claude_code")

    assert decision.provider is None
    assert decision.reason == "human_review_required"


# --- scoring: hysteresis, and its known blind spot ------------------------------------------------------------


def test_within_the_hysteresis_band_the_provider_with_more_margin_still_wins_deterministically() -> None:
    catalogue = _writing_catalogue()
    readings = {
        "codex": _reading("codex", _weekly(30.0, window_id="codex/10080m")),
        "claude_code": _reading(
            "claude_code", _weekly(30.0 + HYSTERESIS_MARGIN_POINTS / 2, window_id="weekly/all models")
        ),
    }
    budgets = {"codex": _budget("codex"), "claude_code": _budget("claude_code")}

    decision = _route(catalogue, readings, budgets)

    assert decision.reason == "hysteresis_no_tiebreak_data"
    assert decision.provider == "codex"  # codex has the (slightly) larger margin


def test_outside_the_hysteresis_band_the_larger_weekly_margin_wins_outright() -> None:
    catalogue = _writing_catalogue()
    readings = {
        "codex": _reading("codex", _weekly(70.0, window_id="codex/10080m")),  # small margin
        "claude_code": _reading("claude_code", _weekly(10.0, window_id="weekly/all models")),  # large margin
    }
    budgets = {"codex": _budget("codex"), "claude_code": _budget("claude_code")}

    decision = _route(catalogue, readings, budgets)

    assert decision.provider == "claude_code" and decision.reason == "weekly_margin"


def test_the_five_hour_tiebreak_never_fires_today_because_claude_code_has_no_such_window() -> None:
    """KNOWN LIMITATION, tested rather than left implicit: the design's tie-break (section 3,
    'a parita' decide il margine della finestra da 5 ore') needs BOTH candidates to carry a
    five-hour margin. Claude Code's own reader never builds one (only weekly windows --
    lazyceo.quota._read_claude), so a codex-vs-claude_code tie always falls back to
    'hysteresis_no_tiebreak_data', never to the five-hour branch, however the two codex-side
    5h windows below are set. This will only wake up once the writing tier can offer two
    codex-side alternatives at once (not modeled yet) or Claude's telemetry grows a short
    window of its own."""
    catalogue = _writing_catalogue()
    readings = {
        "codex": _reading("codex", _weekly(30.0, window_id="codex/10080m"), _five_hour(5.0, window_id="codex/300m")),
        "claude_code": _reading(
            "claude_code", _weekly(30.0 + HYSTERESIS_MARGIN_POINTS / 2, window_id="weekly/all models")
        ),
    }
    budgets = {"codex": _budget("codex"), "claude_code": _budget("claude_code")}

    decision = _route(catalogue, readings, budgets)

    assert decision.scores["codex"]["five_hour_margin"] is not None
    assert decision.scores["claude_code"]["five_hour_margin"] is None
    assert decision.reason == "hysteresis_no_tiebreak_data"


# --- in_flight moves the reservation, and so the choice ----------------------------------------------------------


def test_in_flight_reservations_can_tip_the_choice_to_the_other_provider() -> None:
    catalogue = _writing_catalogue()
    readings = {
        "codex": _reading("codex", _weekly(50.0, window_id="codex/10080m")),
        "claude_code": _reading("claude_code", _weekly(50.0, window_id="weekly/all models")),
    }
    budgets = {"codex": _budget("codex"), "claude_code": _budget("claude_code")}

    even = _route(catalogue, readings, budgets, in_flight={"codex": 0, "claude_code": 0})
    assert even.reason.startswith("hysteresis") or even.provider is not None  # a tie either way, deterministic

    # A MODERATE load (10 in-flight): tips the margin without pushing codex past the
    # autonomous boundary into outright ineligibility -- see the heavier-load variant below
    # for what happens once it does.
    loaded = _route(catalogue, readings, budgets, in_flight={"codex": 10, "claude_code": 0})

    assert loaded.provider == "claude_code"
    assert loaded.scores["codex"]["weekly_margin"] < even.scores["codex"]["weekly_margin"]


def test_in_flight_reservations_heavy_enough_to_breach_the_boundary_make_that_provider_ineligible() -> None:
    """The 25/09 fix's own consequence (Codex review): in-flight reservations feed the SAME
    margin the admission-consistency check judges, so enough of them do not just make a
    provider score worse -- they make it INELIGIBLE outright, exactly as heavy real
    concurrent load would make admission.decide() itself refuse a new call."""
    catalogue = _writing_catalogue()
    readings = {
        "codex": _reading("codex", _weekly(50.0, window_id="codex/10080m")),
        "claude_code": _reading("claude_code", _weekly(50.0, window_id="weekly/all models")),
    }
    budgets = {"codex": _budget("codex"), "claude_code": _budget("claude_code")}

    decision = _route(catalogue, readings, budgets, in_flight={"codex": 40, "claude_code": 0})

    assert decision.provider == "claude_code"
    assert decision.ineligible.get("codex", "").startswith("admission_would_refuse:")
    assert "codex" not in decision.scores


# --- last resort: price, then fixed provider order ---------------------------------------------------------------


def test_price_tiebreak_prefers_the_cheaper_listed_model_on_an_exact_weekly_tie() -> None:
    """An exact weekly-margin tie, with no five-hour data on either side, is a real (if rare)
    possibility -- e.g. two engines seeded from the same synthetic reading. ``gpt-6-luna`` is
    ranked cheaper than ``claude-sonnet-5`` in ``_API_PRICE_RANK``, so it must win, not
    whichever provider happened to sort first."""
    catalogue = _writing_catalogue(codex_model="gpt-6-luna")
    readings = {
        "codex": _reading("codex", _weekly(30.0, window_id="codex/10080m")),
        "claude_code": _reading("claude_code", _weekly(30.0, window_id="weekly/all models")),
    }
    budgets = {"codex": _budget("codex"), "claude_code": _budget("claude_code")}

    decision = _route(catalogue, readings, budgets)

    assert decision.provider == "codex"
    assert decision.reason == "price_tiebreak"


def test_provider_order_tiebreak_is_the_final_resort_when_price_cannot_separate_either() -> None:
    """Two models absent from ``_API_PRICE_RANK`` (or, as here, priced identically) still need
    a deterministic answer -- the fixed provider order (codex first, Luna's own default-writer
    status) rather than an arbitrary "whichever sorted first"."""
    step = TierStep(
        providers=(
            StepModel(provider="codex", model="gpt-6.1-sol", effort="xhigh"),
            StepModel(provider="claude_code", model="claude-sonnet-5", effort="high"),
        )
    )
    catalogue = {"writing": TierCatalogue(name="writing", steps=(step,))}
    readings = {
        "codex": _reading("codex", _weekly(30.0, window_id="codex/10080m")),
        "claude_code": _reading("claude_code", _weekly(30.0, window_id="weekly/all models")),
    }
    budgets = {"codex": _budget("codex"), "claude_code": _budget("claude_code")}

    decision = _route(catalogue, readings, budgets)

    # gpt-6.1-sol and claude-sonnet-5 are both ranked 2.0 in _API_PRICE_RANK -- price cannot
    # separate them, so this must fall to the fixed provider order.
    assert decision.reason == "provider_order_tiebreak"
    assert decision.provider == "codex"


def test_gpt_6_1_sol_has_the_same_price_rank_as_the_previous_gpt_6_sol() -> None:
    """Same list price (2.0 / 10.0 per 1M tokens): the successor must not be 'missing from the table' (which
    would make it never cheaper than a ranked model) nor ranked differently from its predecessor."""
    from lazytools.routing.router import _API_PRICE_RANK

    assert _API_PRICE_RANK[("codex", "gpt-6.1-sol")] == _API_PRICE_RANK[("codex", "gpt-6-sol")] == 2.0


def test_price_and_order_tiebreak_helper_is_symmetric_and_always_picks_a_winner() -> None:
    luna = StepModel(provider="codex", model="gpt-6-luna", effort="xhigh")
    sonnet = StepModel(provider="claude_code", model="claude-sonnet-5", effort="high")

    reason_ab, winner_ab = _price_and_order_tiebreak(luna, sonnet)
    reason_ba, winner_ba = _price_and_order_tiebreak(sonnet, luna)

    assert reason_ab == reason_ba == "price_tiebreak"
    assert winner_ab == winner_ba == "codex"  # luna is the cheaper listed model either way

    # Two models with no price entry at all: falls through to the fixed provider order.
    unknown_codex = StepModel(provider="codex", model="gpt-6-nova", effort="high")
    unknown_claude = StepModel(provider="claude_code", model="claude-nova-1", effort="high")
    reason, winner = _price_and_order_tiebreak(unknown_codex, unknown_claude)
    assert reason == "provider_order_tiebreak"
    assert winner == "codex"


# --- walking the ladder: a rung with nobody eligible falls through to the next -------------------------------------


def _two_rung_catalogue() -> dict[str, TierCatalogue]:
    """Mirrors config/model_tiers.toml's real 'writing' ladder shape (25/09 decision): rung 0
    is Luna/Sonnet, rung 1 is the escalation pair Sol/Opus."""
    rung0 = TierStep(
        providers=(
            StepModel(provider="codex", model="gpt-6-luna", effort="xhigh"),
            StepModel(provider="claude_code", model="claude-sonnet-5", effort="high"),
        )
    )
    rung1 = TierStep(
        providers=(
            StepModel(provider="codex", model="gpt-6.1-sol", effort="xhigh"),
            StepModel(provider="claude_code", model="claude-opus-5-5", effort="high"),
        )
    )
    return {"writing": TierCatalogue(name="writing", steps=(rung0, rung1))}


def test_a_rung_with_nobody_eligible_falls_through_to_the_next_rung() -> None:
    """Both providers ineligible (stale/unreadable telemetry) on rung 0 -- rung 1's own
    (independently-scored) providers still decide the pick, rather than the whole route
    failing just because the CHEAPEST rung had nothing to offer."""
    catalogue = _two_rung_catalogue()
    readings = {
        "codex": _reading("codex", error="app-server unreachable"),
        "claude_code": _reading("claude_code", error="cli not authenticated"),
    }
    budgets = {"codex": _budget("codex"), "claude_code": _budget("claude_code")}

    decision = route(
        "writing",
        catalogue=catalogue,
        readings=readings,
        budgets=budgets,
        in_flight={"codex": 0, "claude_code": 0},
        now=NOW,
    )

    # Both rungs read the SAME (broken) telemetry here, so both are equally ineligible --
    # this proves the walk actually reaches rung 1 (its own ineligibility is recorded) and
    # still ends with no pick, rather than stopping silently at rung 0.
    assert decision.provider is None
    assert (
        "step0" in decision.reason or "step1" in decision.reason or decision.reason.startswith("no_eligible_provider")
    )
    assert any(k.startswith("step1:") for k in decision.ineligible)


def test_a_rung_with_nobody_eligible_falls_through_and_the_next_rung_actually_decides() -> None:
    """A genuine fall-through where the SECOND rung ends up deciding: rung 0 offers only
    claude_code, whose telemetry is unreadable there -- nobody eligible at rung 0 at all, so
    the walk moves on. Rung 1 offers both providers; claude_code is still unreadable (same
    telemetry), but codex is healthy and is the only one left standing there, so IT decides."""
    rung0 = TierStep(providers=(StepModel(provider="claude_code", model="claude-sonnet-5", effort="high"),))
    rung1 = TierStep(
        providers=(
            StepModel(provider="codex", model="gpt-6.1-sol", effort="xhigh"),
            StepModel(provider="claude_code", model="claude-opus-5-5", effort="high"),
        )
    )
    catalogue = {"writing": TierCatalogue(name="writing", steps=(rung0, rung1))}
    readings = {
        "codex": _reading("codex", _weekly(10.0, window_id="codex/10080m")),
        "claude_code": _reading("claude_code", error="cli not authenticated"),
    }
    budgets = {"codex": _budget("codex"), "claude_code": _budget("claude_code")}

    decision = route(
        "writing",
        catalogue=catalogue,
        readings=readings,
        budgets=budgets,
        in_flight={"codex": 0, "claude_code": 0},
        now=NOW,
    )

    assert decision.provider == "codex"
    assert decision.model == "gpt-6.1-sol"
    assert decision.reason == "only_eligible_provider_step1"
    assert "telemetry_unreadable" in decision.ineligible["claude_code"]  # rung 0's own record
    assert "telemetry_unreadable" in decision.ineligible["step1:claude_code"]  # rung 1 too


def test_no_pick_when_every_rung_is_empty() -> None:
    catalogue = _two_rung_catalogue()
    readings = {
        "codex": _reading("codex", error="boom"),
        "claude_code": _reading("claude_code", error="boom too"),
    }
    budgets = {"codex": _budget("codex"), "claude_code": _budget("claude_code")}

    decision = route(
        "writing",
        catalogue=catalogue,
        readings=readings,
        budgets=budgets,
        in_flight={"codex": 0, "claude_code": 0},
        now=NOW,
    )

    assert decision.provider is None
    assert decision.model is None
    assert decision.reason.startswith("no_eligible_provider")
    assert "step0" in decision.reason and "step1" in decision.reason


def test_the_second_rung_scores_its_own_two_providers_by_their_own_margin() -> None:
    """Rung 0 offers only a Fable model on claude_code -- Fable's own weekly bucket is
    missing from this reading, so rung 0 has nobody eligible and the walk falls through.
    Rung 1 offers codex plus a NON-Fable claude_code model (the 'all models' bucket, which
    IS present and healthy), so both are genuinely eligible there and weekly margin decides
    -- proving rung 1 scores its own candidates independently rather than inheriting rung
    0's verdict."""
    rung0 = TierStep(providers=(StepModel(provider="claude_code", model="claude-fable-5-1", effort="high"),))
    rung1 = TierStep(
        providers=(
            StepModel(provider="codex", model="gpt-6.1-sol", effort="xhigh"),  # tight margin
            StepModel(provider="claude_code", model="claude-opus-5-5", effort="high"),  # ample margin
        )
    )
    catalogue = {"writing": TierCatalogue(name="writing", steps=(rung0, rung1))}
    readings = {
        "codex": _reading("codex", _weekly(70.0, window_id="codex/10080m")),
        "claude_code": _reading("claude_code", _weekly(5.0, window_id="weekly/all models")),  # no fable bucket
    }
    budgets = {"codex": _budget("codex"), "claude_code": _budget("claude_code")}

    decision = route(
        "writing",
        catalogue=catalogue,
        readings=readings,
        budgets=budgets,
        in_flight={"codex": 0, "claude_code": 0},
        now=NOW,
    )

    assert decision.provider == "claude_code"
    assert decision.model == "claude-opus-5-5"  # rung 1's own model, decided on rung 1's own scores
    assert decision.reason == "weekly_margin_step1"
    assert "telemetry_window_unreadable" in decision.ineligible["claude_code"]  # rung 0's own record
    assert "step1:claude_code" not in decision.ineligible  # rung 1's claude_code WAS eligible there


def test_review_mode_also_falls_through_an_empty_rung_before_giving_up() -> None:
    """Review mode (``writer_provider_for_review``) must walk rungs the same way a normal
    route does: rung 0 has only the writer's own provider (excluded as 'same_as_writer'), so
    the reviewer pick must come from rung 1's opposite provider instead of failing early."""
    rung0 = TierStep(providers=(StepModel(provider="codex", model="gpt-6-luna", effort="xhigh"),))
    rung1 = TierStep(
        providers=(
            StepModel(provider="codex", model="gpt-6.1-sol", effort="xhigh"),
            StepModel(provider="claude_code", model="claude-opus-5-5", effort="high"),
        )
    )
    catalogue = {"writing": TierCatalogue(name="writing", steps=(rung0, rung1))}
    readings = {
        "codex": _reading("codex", _weekly(10.0, window_id="codex/10080m")),
        "claude_code": _reading("claude_code", _weekly(10.0, window_id="weekly/all models")),
    }
    budgets = {"codex": _budget("codex"), "claude_code": _budget("claude_code")}

    decision = route(
        "writing",
        catalogue=catalogue,
        readings=readings,
        budgets=budgets,
        in_flight={"codex": 0, "claude_code": 0},
        writer_provider_for_review="codex",
        now=NOW,
    )

    assert decision.provider == "claude_code"
    assert decision.model == "claude-opus-5-5"


# --- availability: a provider this agent process has no engine factory for --------------------------------------


def test_an_unavailable_provider_is_ineligible_even_when_it_would_win_on_budget() -> None:
    """``available`` names the providers THIS AGENT PROCESS actually has an engine factory
    for -- a specialist built with no Codex writer must never have codex recommended, even
    when codex clearly has the better margin. Found by Codex review on this PR."""
    catalogue = _writing_catalogue()
    readings = {
        "codex": _reading("codex", _weekly(5.0, window_id="codex/10080m")),  # far better margin
        "claude_code": _reading("claude_code", _weekly(50.0, window_id="weekly/all models")),
    }
    budgets = {"codex": _budget("codex"), "claude_code": _budget("claude_code")}

    decision = _route(catalogue, readings, budgets, available=frozenset({"claude_code"}))

    assert decision.provider == "claude_code"
    assert decision.ineligible.get("codex") == "not_available_to_this_agent"


def test_available_none_means_every_catalogue_provider_is_assumed_available() -> None:
    """The default (``available=None``) must behave exactly as it did before this parameter
    existed -- every existing caller (and every other test in this file) relies on that."""
    catalogue = _writing_catalogue()
    readings = {
        "codex": _reading("codex", _weekly(5.0, window_id="codex/10080m")),
        "claude_code": _reading("claude_code", _weekly(50.0, window_id="weekly/all models")),
    }
    budgets = {"codex": _budget("codex"), "claude_code": _budget("claude_code")}

    decision = _route(catalogue, readings, budgets)

    assert decision.provider == "codex"
    assert "codex" not in decision.ineligible


# --- a margin admission.decide() would itself refuse makes that provider ineligible ------------------------------


def test_continuity_cannot_force_a_provider_admission_would_refuse() -> None:
    """The task's last engine (codex) is exhausted enough that ``admission.decide()`` would
    refuse ordinary autonomous work there -- continuity must not be able to force it anyway;
    quota scoring (here, the only remaining eligible provider) decides instead."""
    catalogue = _writing_catalogue()
    readings = {
        "codex": _reading("codex", _weekly(95.0, window_id="codex/10080m")),  # past the autonomous boundary
        "claude_code": _reading("claude_code", _weekly(10.0, window_id="weekly/all models")),
    }
    budgets = {"codex": _budget("codex"), "claude_code": _budget("claude_code")}

    decision = _route(catalogue, readings, budgets, continuity=ContinuityHint(engine="codex", failed_attempts=0))

    assert decision.provider == "claude_code"
    assert decision.reason != "continuity"
    assert decision.ineligible.get("codex", "").startswith("admission_would_refuse:")


def test_every_provider_admission_would_refuse_on_rung_one_falls_through_to_rung_two() -> None:
    catalogue = _two_rung_catalogue()  # rung0: Luna/Sonnet, rung1: Sol/Opus
    readings = {
        "codex": _reading("codex", _weekly(96.0, window_id="codex/10080m")),  # past the ceiling
        "claude_code": _reading("claude_code", _weekly(90.0, window_id="weekly/all models")),  # past the boundary
    }
    budgets = {"codex": _budget("codex"), "claude_code": _budget("claude_code")}

    decision = route(
        "writing",
        catalogue=catalogue,
        readings=readings,
        budgets=budgets,
        in_flight={"codex": 0, "claude_code": 0},
        now=NOW,
    )

    # Both rungs read the SAME exhausted telemetry, so rung 1 is exhausted too -- this proves
    # the walk actually reached it (its own ineligible entries are recorded) rather than
    # stopping silently at rung 0.
    assert decision.provider is None
    assert decision.ineligible.get("codex", "").startswith("admission_would_refuse:")
    assert decision.ineligible.get("step1:codex", "").startswith("admission_would_refuse:")
    assert decision.reason.startswith("no_eligible_provider")


def test_every_provider_admission_would_refuse_everywhere_returns_no_eligible_provider() -> None:
    catalogue = _writing_catalogue()
    readings = {
        "codex": _reading("codex", _weekly(99.0, window_id="codex/10080m")),
        "claude_code": _reading("claude_code", _weekly(99.0, window_id="weekly/all models")),
    }
    budgets = {"codex": _budget("codex"), "claude_code": _budget("claude_code")}

    decision = _route(catalogue, readings, budgets)

    assert decision.provider is None
    assert decision.model is None
    assert decision.reason.startswith("no_eligible_provider")
    assert decision.ineligible.get("codex", "").startswith("admission_would_refuse:")
    assert decision.ineligible.get("claude_code", "").startswith("admission_would_refuse:")
