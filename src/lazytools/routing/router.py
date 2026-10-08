"""A PURE, DETERMINISTIC router: given a tier, telemetry and a few small facts, says which
provider and model it WOULD choose -- and nothing else. No LLM ever picks a model; this
function is plain code, and callers pass a capability ``tier``.

The router walks a tier's ladder ("gradini") IN ORDER: the first rung with at least one eligible
provider decides, and a rung with none (every provider's telemetry stale, unreadable, or
missing a budget) is skipped in favour of the next one -- not a failure of the whole route,
only of that one rung. Only when EVERY rung comes up empty does ``route()`` return no pick.
This is not escalation on a FAILED task -- it is simply "nobody to decide between at this
rung", the same shape ``no_eligible_provider`` already had for a single-rung tier.

**Eligibility**, checked before any score is computed, in this order, per rung:
1. availability -- a provider this AGENT PROCESS has no engine factory for at all
   (``available``, e.g. a specialist built with no Codex writer) is ineligible on every rung;
   found by Codex review on this PR: without this, a tier-only delegation could be routed to a
   provider that will simply be REJECTED once the caller tries to actually use
   it, on a process that never had it to begin with;
2. capability (images/audio -> Codex only, design section 3.1);
3. review mode -- when ``writer_provider_for_review`` is set, only the OPPOSITE provider is
   ever eligible (section 5 / F.2: a reviewer must be the opposite provider, never "whoever
   has quota"; a rung lacking that opposite provider is skipped like any other empty rung, and
   running out of rungs in review mode still ends in ``human_review_required``, never a
   same-provider review);
4. continuity -- a task/contract's last engine is preferred, unless it is ineligible or has
   already failed twice for this task (section 3.1.3); checked at whichever rung continuity's
   engine first turns up eligible;
5. telemetry -- an unreadable reading, a reading stamped stale or further in the future than
   ``MAX_TELEMETRY_SKEW_SECONDS`` (the same two admission.py checks, same constants -- P2
   correction), or an applicable window this account's telemetry does not carry, all make
   that provider ineligible on THAT rung; a window that plainly does not apply to this model
   (Fable's window, for a Sonnet call) is simply excluded rather than treated as a failure.
   Two distinct outcomes, both real: see ``WindowAvailability``.
6. admission -- a margin that ``admission.decide()`` would itself REFUSE for ordinary
   autonomous work (at or past the autonomous boundary, or a forecast breach) makes that
   provider ineligible too, checked by calling ``decide()`` itself rather than re-deriving its
   thresholds (found by Codex review on this PR: a provider already exhausted enough that a
   real delegation would be refused must never be scored as merely "worse" -- continuity could
   otherwise force it, and with every provider exhausted the router would never fall through
   to the next rung or report ``no_eligible_provider`` at all).

**Scoring**, only among what eligibility left standing on the winning rung -- deterministic,
in this order:
1. the weekly window's margin (position, and forecast when one exists) ranks the candidates;
2. within ``HYSTERESIS_MARGIN_POINTS`` of each other, the five-hour window's margin breaks the
   tie (section 3, "Classifica");
3. an exact tie even THAT cannot break falls to the last, documented resort: the cheaper
   listed API price first (``_API_PRICE_RANK`` -- a provisional, per-model ordering, not the
   per-account subscription quota the rest of this router works on -- see the module
   docstring's own note that the router never activates on dollars), then a fixed provider
   order (``_FIXED_PROVIDER_ORDER``: codex before claude_code, matching Codex/Sol's status as
   the tier's own default writer).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from lazytools.projects.admission import (
    LONG_WINDOW_MINUTES,
    MAX_TELEMETRY_SKEW_SECONDS,
    Engine,
    EngineBudget,
    TelemetryReading,
    WindowReading,
)
from lazytools.projects.admission import decide as _admission_decide
from lazytools.routing.catalogue import StepModel, TierCatalogue

#: A single, provisional, visible threshold (design section 3: "una sola soglia, provvisoria
#: e ben visibile (10 punti di margine), da tarare sul numero di cambi osservati"). Two
#: candidates within this many points of margin on the weekly window are a TIE, broken by the
#: five-hour window rather than by the weekly margin's own (noisy, at this distance) ordering.
HYSTERESIS_MARGIN_POINTS = 10.0

#: A window this short (or shorter) is the "five hour" tie-break window (design section 3).
#: Not an exact-300-minutes match: Codex's own bucket duration is read from its telemetry,
#: not assumed, so anything meaningfully shorter than a day is treated as the short window.
FIVE_HOUR_WINDOW_MAX_MINUTES = 6 * 60

#: The LAST-resort tie-break (this PR, 25/09): a candidate that is still exactly tied after
#: both the weekly and five-hour margins have had their say falls to whichever model is
#: cheaper by LISTED API price -- lower number wins. These are relative, provisional ranks
#: (a provisional ordering of listed API prices), never the per-account subscription quota the rest of this router routes
#: on -- the router still never activates on dollars (design F.1); this only breaks a tie
#: that quota margins alone could not. A model missing from this table is simply never
#: cheaper than one that is in it -- see ``_price_and_order_tiebreak``.
_API_PRICE_RANK: dict[tuple[Engine, str], float] = {
    ("codex", "gpt-6-luna"): 1.0,
    ("claude_code", "claude-haiku-4-5"): 1.5,
    ("codex", "gpt-6-sol"): 2.0,
    ("codex", "gpt-6.1-sol"): 2.0,
    ("claude_code", "claude-sonnet-5-5"): 2.0,
    ("claude_code", "claude-sonnet-5"): 2.0,
    ("codex", "gpt-6-astra"): 2.5,
    ("claude_code", "claude-opus-5-5"): 4.0,
}

#: The tie-break of LAST resort, once price cannot separate two candidates either (both
#: missing from ``_API_PRICE_RANK``, or tied there too): a fixed provider order, codex first --
#: an unresolved tie deterministically prefers the default Codex writer.
_FIXED_PROVIDER_ORDER: tuple[Engine, ...] = ("codex", "claude_code")


class WindowAvailability(Enum):
    """Why a weekly-window lookup did not return a usable reading -- the two sentinels the
    design insists must stay distinct (section 3.1.4): a window that simply has nothing to
    do with this model is excluded, never scored as a failure; a window that SHOULD apply
    but is missing or malformed makes the provider ineligible. Collapsing both to ``None``
    is exactly the ambiguity the design calls out by name."""

    #: This (engine, model) pair has nothing to do with this window -- it is not that the
    #: window is missing, it never applied. Excluded from consideration, not a failure.
    NOT_APPLICABLE = "not_applicable"
    #: This window DOES apply to this (engine, model) pair, but the reading has no usable
    #: entry for it (absent, or present with a non-finite/negative percentage). The provider
    #: is not eligible until this is fixed -- unknown is never free capacity (admission.py's
    #: own rule, applied here to the same telemetry).
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class ContinuityHint:
    """What the last attempt at this task/contract did, if anything -- design section
    3.1.3: continuity is held PER TASK, never globally."""

    engine: Engine | None = None
    #: How many times this task has already failed on ``engine``. At 2, continuity no longer
    #: applies -- the design's own words: "salvo che... abbia già fallito due volte".
    failed_attempts: int = 0


@dataclass(frozen=True)
class CapabilityRequirement:
    """What this task needs that only one provider can serve (design section 3.1.1:
    "immagini e audio vanno solo su Codex")."""

    images: bool = False
    audio: bool = False

    @property
    def codex_only(self) -> bool:
        return self.images or self.audio


@dataclass(frozen=True)
class ProviderScore:
    """The margins one provider scored at, kept for the job record (design section 6:
    "tutti i punteggi")."""

    weekly_position_margin: float
    weekly_forecast_margin: float | None
    weekly_margin: float  # the worse (more constraining) of the two above -- what ranks
    five_hour_position_margin: float | None
    five_hour_forecast_margin: float | None
    five_hour_margin: float | None

    def as_record(self) -> dict[str, Any]:
        return {
            "weekly_position_margin": self.weekly_position_margin,
            "weekly_forecast_margin": self.weekly_forecast_margin,
            "weekly_margin": self.weekly_margin,
            "five_hour_position_margin": self.five_hour_position_margin,
            "five_hour_forecast_margin": self.five_hour_forecast_margin,
            "five_hour_margin": self.five_hour_margin,
        }


@dataclass(frozen=True)
class RoutingDecision:
    """The router's verdict: what it would run, and why. ``provider=None`` is a real,
    intended outcome (design section 3.1.5 / F.2), not an error -- both providers ineligible,
    or a review with no adequate opposite-provider reviewer, fail safe this way."""

    tier: str
    provider: Engine | None
    model: str | None
    effort: str | None
    reason: str
    requires_confirmation: bool
    scores: dict[str, dict[str, Any]] = field(default_factory=dict)
    ineligible: dict[str, str] = field(default_factory=dict)
    telemetry_age: dict[str, float | None] = field(default_factory=dict)
    decided_at: datetime | None = None

    def as_record(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "provider": self.provider,
            "model": self.model,
            "effort": self.effort,
            "reason": self.reason,
            "requires_confirmation": self.requires_confirmation,
            "scores": self.scores,
            "ineligible": self.ineligible,
            "telemetry_age": self.telemetry_age,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
        }


def _weekly_window_id(engine: Engine, model: str) -> str:
    """The window this (engine, model) pair's weekly usage is expected under.

    Codex reports one account-wide bucket regardless of which model ran (verified against
    ``lazybridge.engines.codex.usage``: buckets are keyed by ``limitId``, e.g. ``"codex"``,
    never by model) -- so every Codex model shares the same weekly window. Claude Code's own
    ``/usage`` breaks some models out separately (``ClaudeUsageSnapshot.weekly`` is "keyed by
    whatever label the CLI prints... 'all models' plus one entry per model it breaks out
    separately (e.g. 'Fable')") -- Fable gets its own bucket, everything else in this
    catalogue shares "all models". This is the (fascia, modello) -> secchio mapping the
    design asks for (section 3.1.4), made explicit rather than assumed.
    """
    if engine == "codex":
        return "codex"
    return "fable" if "fable" in model.lower() else "all models"


def _five_hour_window_id(engine: Engine) -> str | None:
    """The engine-wide bucket the five-hour tie-break reads, or ``None`` when this engine's
    telemetry never carries one at all -- structurally NOT_APPLICABLE, not merely absent
    today. The original telemetry reader only built Claude weekly windows. Preserve that
    scoring rule even when a newer reader carries a Claude session window: it remains
    excluded from the tie-break, rather than reported unreadable."""
    return "codex" if engine == "codex" else None


def _find_window(reading: TelemetryReading, *, bucket: str, weekly: bool) -> WindowReading | WindowAvailability:
    """Look ``bucket`` up among ``reading.windows``, classified by duration into the weekly
    or five-hour group. Returns the reading itself, or one of the two sentinels above."""
    matches = [w for w in reading.windows if bucket in w.window_id.lower()]
    if weekly:
        candidates = [w for w in matches if (w.duration_minutes or 0) >= LONG_WINDOW_MINUTES]
    else:
        candidates = [
            w for w in matches if w.duration_minutes is not None and w.duration_minutes <= FIVE_HOUR_WINDOW_MAX_MINUTES
        ]
    if not candidates:
        return WindowAvailability.UNREADABLE
    window = candidates[0]
    import math

    if not math.isfinite(window.used_percent) or window.used_percent < 0:
        return WindowAvailability.UNREADABLE
    return window


def _margin_pair(
    window: WindowReading, budget: EngineBudget, *, reserved: float, observed_at: datetime
) -> tuple[float, float | None]:
    """(position margin, forecast margin or None) for one window -- the same two quantities
    ``admission.decide()`` judges, computed at ``observed_at`` the same way it is (design
    section 3: "calcolato a reading.observed_at, come fa il freno")."""
    position_margin = budget.autonomous_percent - (window.used_percent + reserved)
    elapsed = window.elapsed_fraction(now=observed_at)
    projected = window.projected_end_percent(now=observed_at)
    forecast_margin = None
    if projected is not None and elapsed is not None and elapsed > 0:
        forecast_margin = budget.forecast_limit_percent(elapsed) - (projected + reserved)
    return position_margin, forecast_margin


def _score_provider(
    *, engine: Engine, model: str, reading: TelemetryReading, budget: EngineBudget, in_flight: int, moment: datetime
) -> ProviderScore | str:
    """A provider's margins, or the ineligibility reason (a string) if its telemetry cannot
    support a score at all.

    Age is judged the same way ``admission.decide()`` judges it, against the SAME two
    constants: a reading stamped more than ``MAX_TELEMETRY_SKEW_SECONDS`` in the future means
    the clocks disagree (unknown, not free capacity), and one older than
    ``budget.max_telemetry_age_seconds`` is stale. Without this, a provider whose telemetry
    happened to go stale between admission's own read and this one would still get scored and
    ranked on a number nobody can vouch for -- the router would disagree with the brake about
    whether the same reading may be trusted at all.
    """
    if reading.error is not None:
        return f"telemetry_unreadable: {reading.error}"
    age = reading.age_seconds(now=moment)
    if age < -MAX_TELEMETRY_SKEW_SECONDS:
        return f"telemetry_unreadable: {reading.source} is stamped {-age / 60:.0f} minutes in the future; the clocks disagree"
    if age > budget.max_telemetry_age_seconds:
        return (
            f"telemetry_stale: {reading.source} last read {age / 3600:.1f}h ago, "
            f"limit {budget.max_telemetry_age_seconds / 3600:.1f}h"
        )
    weekly_bucket = _weekly_window_id(engine, model)
    weekly = _find_window(reading, bucket=weekly_bucket, weekly=True)
    if weekly is WindowAvailability.UNREADABLE:
        return f"telemetry_window_unreadable: no usable weekly window for {engine}/{model!r} ({weekly_bucket!r})"
    assert isinstance(weekly, WindowReading)

    # A margin that ``admission.decide()`` would itself REFUSE for ordinary autonomous work
    # (at or past the autonomous boundary, or a forecast breach) makes this provider
    # ineligible -- checked by calling ``decide()`` itself, scoped to just THIS weekly window
    # (never the whole account-wide reading: a Fable bucket near its ceiling must not drag a
    # Sonnet-tier score down with it -- the five-hour window is deliberately excluded too, the
    # same way it is excluded from eligibility everywhere else in this function, since it is a
    # tie-break, not a gate). ``operator_directed=False`` because this is what an ordinary
    # AUTONOMOUS delegation would face -- the same boundary ``admission.decide()`` enforces for
    # one. Reusing ``decide()`` rather than re-deriving its ceiling/autonomous_boundary/
    # forecast_breach thresholds keeps the two permanently consistent -- found by Codex review
    # on this PR: a provider already exhausted enough that a real delegation would be refused
    # must never be scored as merely "worse", or continuity could force it and, with every
    # provider exhausted, the router would never fall through to the next rung or report
    # ``no_eligible_provider`` at all.
    scoped_reading = TelemetryReading(
        engine=engine, source=reading.source, observed_at=reading.observed_at, windows=(weekly,)
    )
    admission_result = _admission_decide(
        scoped_reading, budget, operator_directed=False, in_flight=in_flight, now=moment
    )
    if not admission_result.allowed:
        detail = f" ({admission_result.detail})" if admission_result.detail else ""
        return f"admission_would_refuse:{admission_result.reason}{detail}"

    reserved = (max(in_flight, 0) + 1) * budget.per_job_reserve_percent
    weekly_position, weekly_forecast = _margin_pair(weekly, budget, reserved=reserved, observed_at=reading.observed_at)
    weekly_margin = weekly_position if weekly_forecast is None else min(weekly_position, weekly_forecast)

    five_hour_bucket = _five_hour_window_id(engine)
    five_position = five_forecast = five_margin = None
    if five_hour_bucket is not None:
        five = _find_window(reading, bucket=five_hour_bucket, weekly=False)
        if isinstance(five, WindowReading):
            five_position, five_forecast = _margin_pair(
                five, budget, reserved=reserved, observed_at=reading.observed_at
            )
            five_margin = five_position if five_forecast is None else min(five_position, five_forecast)
        # UNREADABLE here only disables the TIE-BREAK, it does not make the provider
        # ineligible -- eligibility is decided on the weekly window alone (design section
        # 3: the five-hour window is explicitly a tie-break, not a gate).

    return ProviderScore(
        weekly_position_margin=weekly_position,
        weekly_forecast_margin=weekly_forecast,
        weekly_margin=weekly_margin,
        five_hour_position_margin=five_position,
        five_hour_forecast_margin=five_forecast,
        five_hour_margin=five_margin,
    )


def _price_and_order_tiebreak(a: StepModel, b: StepModel) -> tuple[str, Engine]:
    """The tie-break of last resort, once weekly AND five-hour margins are both an exact tie
    (or unavailable): cheaper listed API price first, then the fixed provider order. Always
    returns a winner -- one of ``a.provider``/``b.provider`` -- because ``_FIXED_PROVIDER_ORDER``
    covers every provider this router knows about."""
    price_a = _API_PRICE_RANK.get((a.provider, a.model))
    price_b = _API_PRICE_RANK.get((b.provider, b.model))
    if price_a is not None and price_b is not None and price_a != price_b:
        return "price_tiebreak", (a.provider if price_a < price_b else b.provider)
    order = {engine: i for i, engine in enumerate(_FIXED_PROVIDER_ORDER)}
    order_a = order.get(a.provider, len(order))
    order_b = order.get(b.provider, len(order))
    return "provider_order_tiebreak", (a.provider if order_a <= order_b else b.provider)


def route(
    tier: str,
    *,
    catalogue: dict[str, TierCatalogue],
    readings: dict[Engine, TelemetryReading],
    budgets: dict[Engine, EngineBudget],
    in_flight: dict[Engine, int],
    continuity: ContinuityHint | None = None,
    capability: CapabilityRequirement | None = None,
    writer_provider_for_review: Engine | None = None,
    available: frozenset[Engine] | None = None,
    now: datetime | None = None,
) -> RoutingDecision:
    """What the router would choose for ``tier``, right now -- PURE and DETERMINISTIC: no
    store, no network, no side effect, and no LLM ever makes this choice. Walks
    ``catalogue[tier].steps`` IN ORDER; the first rung with at least one eligible provider
    decides (see the module docstring for what "eligible" and "decides" mean, and the
    documented three-level tie-break: weekly margin, then five-hour margin, then price/fixed
    order). A rung with nobody eligible is skipped, not a failure -- only running out of
    rungs entirely returns no pick.

    ``available`` names the providers THIS AGENT PROCESS actually has an engine factory for
    (e.g. a specialist built with no Codex writer at all) -- ``None`` (the default) means every
    provider the catalogue names is assumed available, matching every caller before this
    parameter existed. A provider outside ``available`` is ineligible on every rung with reason
    ``"not_available_to_this_agent"``, checked first, before capability/review/telemetry --
    found by Codex review on this PR: without it, a tier-only delegation could be routed to a
    provider that the caller would then REJECT outright once it tried to actually
    use it, on a process that never had that engine to begin with.

    ``writer_provider_for_review`` turns this into a REVIEWER pick: only the provider
    OPPOSITE it is ever eligible on any rung (design section 5 / F.2). If no rung ever has
    that opposite provider eligible, the result is ``provider=None`` with reason
    ``"human_review_required"`` -- never a same-provider review, and never a "weaker but
    available" one presented as adequate.
    """
    moment = now or datetime.now(UTC)
    continuity = continuity or ContinuityHint()
    capability = capability or CapabilityRequirement()

    common: dict[str, Any] = dict(tier=tier, decided_at=moment)

    tier_catalogue = catalogue.get(tier)
    if tier_catalogue is None or not tier_catalogue.steps:
        return RoutingDecision(
            provider=None, model=None, effort=None, reason=f"unknown_tier:{tier}", requires_confirmation=False, **common
        )

    scores: dict[str, dict[str, Any]] = {}
    ineligible: dict[str, str] = {}
    telemetry_age: dict[str, float | None] = {}
    rung_failures: list[str] = []

    for step_index, step in enumerate(tier_catalogue.steps):
        # Keys namespaced by rung once there is more than one to distinguish -- rung 0 keeps
        # the bare engine name so every existing reader of a single-rung tier's record (every
        # tier before this PR) sees the exact same shape it always did.
        key_prefix = "" if step_index == 0 else f"step{step_index}:"

        step_scores: dict[str, dict[str, Any]] = {}
        step_ineligible: dict[str, str] = {}
        eligible_models: dict[Engine, StepModel] = {}

        for step_model in step.providers:
            engine = step_model.provider
            reading = readings.get(engine)
            if reading is not None:
                telemetry_age[engine] = reading.age_seconds(now=moment)
            elif engine not in telemetry_age:
                telemetry_age[engine] = None

            if available is not None and engine not in available:
                step_ineligible[engine] = "not_available_to_this_agent"
                continue
            if capability.codex_only and engine != "codex":
                step_ineligible[engine] = "capability_requires_codex"
                continue
            if writer_provider_for_review is not None and engine == writer_provider_for_review:
                step_ineligible[engine] = "same_as_writer"
                continue
            if reading is None:
                step_ineligible[engine] = "telemetry_missing"
                continue

            budget = budgets.get(engine)
            if budget is None:
                step_ineligible[engine] = "no_budget_configured"
                continue

            result = _score_provider(
                engine=engine,
                model=step_model.model,
                reading=reading,
                budget=budget,
                in_flight=in_flight.get(engine, 0),
                moment=moment,
            )
            if isinstance(result, str):
                step_ineligible[engine] = result
                continue
            step_scores[engine] = result.as_record()
            eligible_models[engine] = step_model

        for provider_name, why in step_ineligible.items():
            ineligible[f"{key_prefix}{provider_name}"] = why
        for provider_name, record in step_scores.items():
            scores[f"{key_prefix}{provider_name}"] = record

        # Review mode with nobody left standing on THIS rung: try the next one, never fall
        # back to the writer's own provider or to a weaker-but-present one (design F.2).
        if writer_provider_for_review is not None and not eligible_models:
            detail = "; ".join(f"{e}: {w}" for e, w in sorted(step_ineligible.items())) or "no provider at this step"
            rung_failures.append(f"step{step_index}: {detail}")
            continue

        # Continuity: the last attempt's engine is kept, unless it is ineligible on this rung
        # or has already failed twice for this task (design section 3.1.3). Only applies
        # outside review mode -- a reviewer is never "the writer's own last engine".
        if (
            writer_provider_for_review is None
            and continuity.engine is not None
            and continuity.failed_attempts < 2
            and continuity.engine in eligible_models
        ):
            chosen = eligible_models[continuity.engine]
            return RoutingDecision(
                provider=chosen.provider,
                model=chosen.model,
                effort=chosen.effort,
                reason="continuity" if step_index == 0 else f"continuity_step{step_index}",
                requires_confirmation=chosen.provider == "codex",
                scores=scores,
                ineligible=ineligible,
                telemetry_age=telemetry_age,
                **common,
            )

        if not eligible_models:
            detail = "; ".join(f"{e}: {w}" for e, w in sorted(step_ineligible.items())) or "no provider at this step"
            rung_failures.append(f"step{step_index}: {detail}")
            continue

        if len(eligible_models) == 1:
            (engine, chosen) = next(iter(eligible_models.items()))
            return RoutingDecision(
                provider=chosen.provider,
                model=chosen.model,
                effort=chosen.effort,
                reason="only_eligible_provider" if step_index == 0 else f"only_eligible_provider_step{step_index}",
                requires_confirmation=chosen.provider == "codex",
                scores=scores,
                ineligible=ineligible,
                telemetry_age=telemetry_age,
                **common,
            )

        # Two or more eligible candidates on this rung: rank by weekly margin -- the largest
        # remaining budget margin decides, deterministically. A tie within the hysteresis band
        # is broken by the five-hour margin; a tie even THAT cannot break falls to the
        # documented last resort (price, then fixed provider order) -- see
        # ``_price_and_order_tiebreak`` and the module docstring (design section 3,
        # "Classifica", plus this PR's own addition).
        engines_sorted = sorted(eligible_models.keys(), key=lambda e: step_scores[e]["weekly_margin"], reverse=True)
        best, second = engines_sorted[0], engines_sorted[1]
        reason = "weekly_margin"
        winner = best
        if abs(step_scores[best]["weekly_margin"] - step_scores[second]["weekly_margin"]) < HYSTERESIS_MARGIN_POINTS:
            best_five = step_scores[best]["five_hour_margin"]
            second_five = step_scores[second]["five_hour_margin"]
            if best_five is not None and second_five is not None and best_five != second_five:
                reason = "hysteresis_five_hour_tiebreak"
                winner = best if best_five >= second_five else second
            elif step_scores[best]["weekly_margin"] == step_scores[second]["weekly_margin"]:
                # An exact weekly tie the five-hour window also could not break (missing on
                # one side, or tied there too): the documented last resort, never an
                # unexplained "whichever sorted first".
                reason, winner = _price_and_order_tiebreak(eligible_models[best], eligible_models[second])
            else:
                reason = "hysteresis_no_tiebreak_data"
                winner = best  # keep the (already-tied) weekly ranking; nothing better to go on
        if step_index > 0:
            reason = f"{reason}_step{step_index}"

        chosen = eligible_models[winner]
        return RoutingDecision(
            provider=chosen.provider,
            model=chosen.model,
            effort=chosen.effort,
            reason=reason,
            requires_confirmation=chosen.provider == "codex",
            scores=scores,
            ineligible=ineligible,
            telemetry_age=telemetry_age,
            **common,
        )

    # Every rung came up empty.
    if writer_provider_for_review is not None:
        return RoutingDecision(
            provider=None,
            model=None,
            effort=None,
            reason="human_review_required",
            requires_confirmation=False,
            scores=scores,
            ineligible=ineligible,
            telemetry_age=telemetry_age,
            **common,
        )
    detail = " | ".join(rung_failures) or "no provider at any step"
    return RoutingDecision(
        provider=None,
        model=None,
        effort=None,
        reason=f"no_eligible_provider ({detail})",
        requires_confirmation=False,
        scores=scores,
        ineligible=ineligible,
        telemetry_age=telemetry_age,
        **common,
    )
