"""Whether a new engine call may start, given what the quota has left.

Ported from ``lazyceo.admission``, mechanism only. Four decisions shape it
(unchanged from the original): the ceiling is per engine; below it sits an
autonomous boundary; unknown telemetry is never free capacity; the only
actuator is refusing to START something, never cancelling what is already
running.

Left behind, as CEO/Telegram policy: LazyCEO's own ``_human_approval_exists``/
``_consume_human_approval``/``restore_human_approval`` resolve an
"operator-directed" claim against a Telegram approval-ticket queue
(``lazyceo.approvals``). This module instead takes ``operator_directed``
as a plain, already-resolved ``bool`` -- the caller (LazyCEO's own policy,
today; a Claude Code session's own judgement, tomorrow) decides what counts
as operator-directed and does not get to spend/restore a ticket through this
module at all.

New with this package: :func:`project_admit`, the per-project brake switch
(``brake.get_project_brake_enabled``) -- see docs/projects.md's "brake rule".
This is where the task's requirement that the brake "applies to PROJECT
work... never to a Claude Code session's own direct work" is implemented:
``project_admit`` is the one entry point that checks a project's own
brake-enabled flag before deferring to :func:`admit`; a caller doing its own
direct work (no project attribution) calls :func:`admit` directly, or does
not call this module at all.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from lazybridge import Store

Engine = Literal["codex", "claude_code"]
ENGINES: tuple[Engine, ...] = ("codex", "claude_code")

ADMISSION_PREFIX = "ceo:admission:"

MAX_RECORDED_DECISIONS = 200

MIN_ELAPSED_FOR_FORECAST = 0.05

LONG_WINDOW_MINUTES = 24 * 60
MIN_ELAPSED_FOR_FORECAST_LONG_WINDOW = 0.20

CAS_ATTEMPTS = 8

MAX_TELEMETRY_SKEW_SECONDS = 120.0


@dataclass(frozen=True)
class EngineBudget:
    """What this engine may spend, and how stale its telemetry may be.

    Every field is configuration, not a constant at the call site --
    ceiling/boundary numbers belong to whoever operates the fleet (LazyCEO's
    policy today), never to this module.
    """

    engine: Engine
    ceiling_percent: float = 95.0
    autonomous_fraction: float = 0.8
    per_job_reserve_percent: float = 1.0
    forecast_buffer_fraction: float = 0.10
    max_telemetry_age_seconds: float = 6 * 3600.0
    reservation_ttl_seconds: float = 2 * 3600.0
    #: Decide and record, but never actually refuse -- for calibrating ceiling/boundary
    #: numbers against real data before enforcing them.
    shadow: bool = False

    @property
    def autonomous_percent(self) -> float:
        return self.ceiling_percent * self.autonomous_fraction

    def forecast_limit_percent(self, elapsed: float) -> float:
        b = self.forecast_buffer_fraction
        return self.ceiling_percent * (1 - b + b / elapsed)


@dataclass(frozen=True)
class WindowReading:
    """One provider window: how much is gone and when it comes back."""

    window_id: str
    used_percent: float
    duration_minutes: int | None = None
    resets_at: datetime | None = None

    def elapsed_fraction(self, *, now: datetime) -> float | None:
        if self.resets_at is None or not self.duration_minutes:
            return None
        remaining_hours = (self.resets_at - now).total_seconds() / 3600.0
        total_hours = self.duration_minutes / 60.0
        return min(max((total_hours - remaining_hours) / total_hours, 0.0), 1.0)

    def projected_end_percent(self, *, now: datetime) -> float | None:
        elapsed = self.elapsed_fraction(now=now)
        floor = (
            MIN_ELAPSED_FOR_FORECAST_LONG_WINDOW
            if (self.duration_minutes or 0) >= LONG_WINDOW_MINUTES
            else MIN_ELAPSED_FOR_FORECAST
        )
        if elapsed is None or elapsed < floor:
            return None
        return self.used_percent / elapsed


@dataclass(frozen=True)
class TelemetryReading:
    """What one engine's telemetry said, and when."""

    engine: Engine
    source: str
    observed_at: datetime
    windows: tuple[WindowReading, ...] = ()
    error: str | None = None

    def age_seconds(self, *, now: datetime) -> float:
        return (now - self.observed_at).total_seconds()


@dataclass(frozen=True)
class AdmissionDecision:
    """One answer, with everything needed to explain it afterwards."""

    allowed: bool
    reason: str
    engine: Engine
    operator_directed: bool
    decided_at: datetime
    admission_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    window_id: str | None = None
    used_percent: float | None = None
    effective_percent: float | None = None
    ceiling_percent: float | None = None
    autonomous_percent: float | None = None
    telemetry_age_seconds: float | None = None
    telemetry_source: str | None = None
    resets_at: datetime | None = None
    shadow: bool = False
    detail: str | None = None

    def as_record(self) -> dict[str, Any]:
        return {
            "admission_id": self.admission_id,
            "allowed": self.allowed,
            "reason": self.reason,
            "engine": self.engine,
            "operator_directed": self.operator_directed,
            "decided_at": self.decided_at.isoformat(),
            "window_id": self.window_id,
            "used_percent": self.used_percent,
            "effective_percent": self.effective_percent,
            "ceiling_percent": self.ceiling_percent,
            "autonomous_percent": self.autonomous_percent,
            "telemetry_age_seconds": self.telemetry_age_seconds,
            "telemetry_source": self.telemetry_source,
            "resets_at": self.resets_at.isoformat() if self.resets_at else None,
            "detail": self.detail,
            "shadow": self.shadow,
        }

    def rejection_text(self) -> str:
        if self.allowed:
            raise ValueError("rejection_text() on an allowed decision")
        parts = [f"REJECTED: {self.engine} admission refused ({self.reason})"]
        if self.used_percent is not None and self.window_id is not None:
            parts.append(f"window {self.window_id} at {self.used_percent:.0f}%")
        if self.ceiling_percent is not None:
            limit = self.autonomous_percent if self.reason == "autonomous_boundary" else self.ceiling_percent
            parts.append(f"limit {limit:.0f}%")
        if self.resets_at is not None:
            parts.append(f"resets {self.resets_at:%d/%m %H:%M} UTC")
        if self.detail:
            parts.append(self.detail)
        return " -- ".join(parts) + ". Nothing was started, so nothing was spent."


def decide(
    reading: TelemetryReading, budget: EngineBudget, *, operator_directed: bool, in_flight: int = 0, now: datetime | None = None
) -> AdmissionDecision:
    """Decide, without touching the store. Pure."""
    moment = now or datetime.now(UTC)
    common: dict[str, Any] = {
        "engine": budget.engine,
        "operator_directed": operator_directed,
        "decided_at": moment,
        "ceiling_percent": budget.ceiling_percent,
        "autonomous_percent": budget.autonomous_percent,
        "telemetry_source": reading.source,
        "telemetry_age_seconds": reading.age_seconds(now=moment),
    }

    if reading.error is not None:
        return AdmissionDecision(allowed=False, reason="telemetry_unreadable", detail=reading.error, **common)
    if not reading.windows:
        return AdmissionDecision(allowed=False, reason="telemetry_unreadable", detail=f"{reading.source} reported no usage window", **common)
    age = reading.age_seconds(now=moment)
    if age < -MAX_TELEMETRY_SKEW_SECONDS:
        return AdmissionDecision(
            allowed=False, reason="telemetry_unreadable",
            detail=f"{reading.source} is stamped {-age / 60:.0f} minutes in the future; the clocks disagree", **common,
        )
    if age > budget.max_telemetry_age_seconds:
        return AdmissionDecision(
            allowed=False, reason="telemetry_stale",
            detail=f"{reading.source} last read {age / 3600:.1f}h ago, limit {budget.max_telemetry_age_seconds / 3600:.1f}h", **common,
        )

    reserved = (max(in_flight, 0) + 1) * budget.per_job_reserve_percent

    usable = [w for w in reading.windows if math.isfinite(w.used_percent) and w.used_percent >= 0]
    if len(usable) != len(reading.windows):
        return AdmissionDecision(
            allowed=False, reason="telemetry_unreadable",
            detail=f"{reading.source} reported {len(reading.windows) - len(usable)} of {len(reading.windows)} window(s) with no usable percentage",
            **common,
        )
    worst = max(usable, key=lambda w: w.used_percent)
    effective = worst.used_percent + reserved
    window_common = {**common, "window_id": worst.window_id, "used_percent": worst.used_percent, "effective_percent": effective, "resets_at": worst.resets_at}

    if effective >= budget.ceiling_percent:
        return AdmissionDecision(allowed=False, reason="absolute_ceiling", **window_common)
    if not operator_directed and effective >= budget.autonomous_percent:
        return AdmissionDecision(
            allowed=False, reason="autonomous_boundary", detail="the remaining capacity is reserved for operator-directed work", **window_common
        )

    forecasts = [(w, w.projected_end_percent(now=reading.observed_at)) for w in usable]
    ranked = []
    for w, p in forecasts:
        elapsed = w.elapsed_fraction(now=reading.observed_at)
        if p is not None and elapsed is not None:
            ranked.append((w, p + reserved, budget.forecast_limit_percent(elapsed)))
    if not operator_directed and ranked:
        fastest, projected, limit = max(ranked, key=lambda entry: entry[1] - entry[2])
        if projected >= limit:
            return AdmissionDecision(
                allowed=False,
                reason="forecast_breach",
                detail=(
                    f"at this pace {fastest.window_id} ends near {projected:.0f}%, "
                    f"past the {limit:.0f}% this point in the window allows (the {budget.ceiling_percent:.0f}% "
                    "ceiling plus a buffer that shrinks as the window runs out)"
                ),
                **{**window_common, "window_id": fastest.window_id, "used_percent": fastest.used_percent, "effective_percent": fastest.used_percent + reserved, "resets_at": fastest.resets_at},
            )
    return AdmissionDecision(allowed=True, reason="admitted", **window_common)


SHADOW_ENV = "LAZYTOOLS_PROJECTS_ADMISSION_SHADOW"


def budget_for(engine: Engine) -> EngineBudget:
    """The default budget for ``engine``, with shadow mode read from the environment."""
    import os

    shadow = os.environ.get(SHADOW_ENV, "").strip().lower() in ("1", "true", "yes", "on")
    return EngineBudget(engine=engine, shadow=shadow)


def _doc_key(engine: Engine, *, prefix: str = ADMISSION_PREFIX) -> str:
    return f"{prefix}{engine}"


def shadow_findings(store: Store, *, prefix: str = ADMISSION_PREFIX) -> list[str]:
    """What the brake WOULD have refused, per engine, while shadow was on."""
    lines: list[str] = []
    for engine in ENGINES:
        try:
            doc = store.read(_doc_key(engine, prefix=prefix))
        except Exception as exc:
            lines.append(f"{engine}: shadow trail unreadable ({type(exc).__name__}) -- this engine is NOT measured.")
            continue
        if not isinstance(doc, dict):
            lines.append(f"{engine}: no decisions recorded while shadow was on -- NOT measured.")
            continue
        decisions = [d for d in doc.get("decisions", []) if isinstance(d, dict) and d.get("shadow")]
        if not decisions:
            lines.append(f"{engine}: no decisions recorded while shadow was on -- NOT measured.")
            continue
        would_refuse: dict[str, int] = {}
        for record in decisions:
            detail = str(record.get("detail") or "")
            if detail.startswith(f"{SHADOW_MARK} ("):
                reason = detail.split("(", 1)[1].split(")", 1)[0]
                would_refuse[reason] = would_refuse.get(reason, 0) + 1
        seen = [
            float(d["effective_percent"]) for d in decisions if isinstance(d.get("effective_percent"), (int, float)) and math.isfinite(float(d["effective_percent"]))
        ]
        peak = f"{max(seen):.0f}%" if seen else "unknown"
        if would_refuse:
            detail = ", ".join(f"{count}x {reason}" for reason, count in sorted(would_refuse.items()))
            lines.append(f"{engine}: {sum(would_refuse.values())} of {len(decisions)} recorded decisions would have been refused ({detail}); highest effective reading {peak}.")
        else:
            lines.append(f"{engine}: none of {len(decisions)} recorded decisions would have been refused; highest effective reading {peak}.")
    return lines


SHADOW_MARK = "SHADOW: would have refused"


def _unenforced_in_shadow(decision: AdmissionDecision, budget: EngineBudget) -> AdmissionDecision:
    if not budget.shadow:
        return decision
    if decision.allowed:
        return replace(decision, shadow=True)
    return replace(decision, allowed=True, shadow=True, detail=f"{SHADOW_MARK} ({decision.reason}). {decision.detail or ''}".strip())


def _live_reservations(doc: dict[str, Any], *, now: datetime, ttl: float) -> list[dict[str, Any]]:
    cutoff = now - timedelta(seconds=ttl)
    live = []
    for entry in doc.get("in_flight", []):
        if not isinstance(entry, dict):
            continue
        try:
            started = datetime.fromisoformat(str(entry.get("started_at")))
        except (TypeError, ValueError):
            continue
        if started > cutoff:
            live.append(entry)
    return live


def admit(
    store: Store,
    *,
    budget: EngineBudget,
    reading: TelemetryReading,
    operator_directed: bool = False,
    review: bool = False,
    now: datetime | None = None,
    prefix: str = ADMISSION_PREFIX,
) -> AdmissionDecision:
    """Decide and, if admitted, reserve -- in one compare-and-swap.

    ``operator_directed``: already resolved by the caller (an approval
    ticket, a time boost, whatever the caller's own policy recognises as a
    human decision) -- this module spends/restores nothing of its own.

    ``review``: an independent review of work already done. Admitted like
    operator-directed work -- on the CURRENT position against the absolute
    ceiling only, never the forecast or the autonomous boundary -- because
    the writing work it checks has already been spent and a review is
    small; past the ceiling it is still refused.
    """
    moment = now or datetime.now(UTC)
    effective_operator_directed = operator_directed or review

    for _ in range(CAS_ATTEMPTS):
        try:
            doc = store.read(_doc_key(budget.engine, prefix=prefix)) or {}
        except Exception:
            doc = {}
        if not isinstance(doc, dict):
            doc = {}
        live = _live_reservations(doc, now=moment, ttl=budget.reservation_ttl_seconds)

        decision = decide(reading, budget, operator_directed=effective_operator_directed, in_flight=len(live), now=moment)
        decision = _unenforced_in_shadow(decision, budget)

        new_doc = {
            **doc,
            "in_flight": ([*live, {"admission_id": decision.admission_id, "started_at": moment.isoformat()}] if decision.allowed else live),
            "decisions": [*doc.get("decisions", []), decision.as_record()][-MAX_RECORDED_DECISIONS:],
        }
        try:
            if store.compare_and_swap(_doc_key(budget.engine, prefix=prefix), doc or None, new_doc):
                return decision
        except Exception:
            break

    return AdmissionDecision(
        allowed=False, reason="admission_contended", engine=budget.engine, operator_directed=effective_operator_directed,
        decided_at=moment, telemetry_source=reading.source, detail="could not record the decision against current state; try again",
    )


def preflight(
    store: Store, *, budget: EngineBudget, reading: TelemetryReading, operator_directed: bool = False, now: datetime | None = None, prefix: str = ADMISSION_PREFIX
) -> AdmissionDecision:
    """The same answer as :func:`admit`, without reserving anything -- for a caller
    deciding whether it is worth asking a human first."""
    moment = now or datetime.now(UTC)
    try:
        doc = store.read(_doc_key(budget.engine, prefix=prefix)) or {}
    except Exception:
        doc = {}
    live = _live_reservations(doc if isinstance(doc, dict) else {}, now=moment, ttl=budget.reservation_ttl_seconds)
    return _unenforced_in_shadow(decide(reading, budget, operator_directed=operator_directed, in_flight=len(live), now=moment), budget)


def in_flight_count(store: Store, engine: Engine, *, now: datetime | None = None, prefix: str = ADMISSION_PREFIX) -> int:
    """How many reservations are live for ``engine`` right now. Best-effort: an unreadable
    store reads as zero rather than raising."""
    moment = now or datetime.now(UTC)
    try:
        doc = store.read(_doc_key(engine, prefix=prefix))
    except Exception:
        return 0
    if not isinstance(doc, dict):
        return 0
    return len(_live_reservations(doc, now=moment, ttl=EngineBudget(engine=engine).reservation_ttl_seconds))


def release(store: Store, *, engine: Engine, admission_id: str, prefix: str = ADMISSION_PREFIX) -> bool:
    """Give back a reservation once its job has reached a terminal state. Best-effort."""
    for _ in range(CAS_ATTEMPTS):
        try:
            doc = store.read(_doc_key(engine, prefix=prefix))
        except Exception:
            return False
        if not isinstance(doc, dict):
            return False
        remaining = [e for e in doc.get("in_flight", []) if not (isinstance(e, dict) and e.get("admission_id") == admission_id)]
        if len(remaining) == len(doc.get("in_flight", [])):
            return False
        try:
            if store.compare_and_swap(_doc_key(engine, prefix=prefix), doc, {**doc, "in_flight": remaining}):
                return True
        except Exception:
            return False
    return False


def under_plan_warning(reading: TelemetryReading, budget: EngineBudget, *, now: datetime | None = None) -> str | None:
    """A note when the pace is far below plan, or None. Never starts work on its own."""
    moment = now or datetime.now(UTC)
    if reading.error or not reading.windows:
        return None
    usable = [w for w in reading.windows if math.isfinite(w.used_percent) and w.used_percent >= 0]
    if len(usable) != len(reading.windows):
        return None
    ranked = [(w, w.projected_end_percent(now=reading.observed_at)) for w in usable]
    live = [(w, p) for w, p in ranked if p is not None]
    if not live:
        return None
    worst, projected = max(live, key=lambda pair: pair[1])
    if projected >= budget.ceiling_percent * 0.5:
        return None
    hours = (worst.resets_at - moment).total_seconds() / 3600.0 if worst.resets_at else None
    when = f", {hours:.0f}h before it resets" if hours is not None else ""
    return (
        f"{budget.engine}: at this pace the window ends near {projected:.0f}% of a "
        f"{budget.ceiling_percent:.0f}% ceiling{when}. Nothing is wrong -- but if there is "
        "work worth doing, there is room for it."
    )


def project_admit(
    store: Store,
    project_id: str,
    *,
    budget: EngineBudget,
    reading: TelemetryReading,
    operator_directed: bool = False,
    review: bool = False,
    now: datetime | None = None,
    admission_prefix: str = ADMISSION_PREFIX,
) -> AdmissionDecision:
    """``admit``, gated by this PROJECT's own brake switch.

    The quota brake applies to project work -- delegated jobs attributed to
    a project, whoever launches them -- never to a session's own direct
    work (a caller doing direct work does not call this at all, or calls
    :func:`admit` directly with no project attribution). On by default;
    :func:`lazytools.projects.brake.set_project_brake_enabled` turns it off
    for one project without touching the engine-level ceiling/boundary
    numbers, which stay fleet-wide configuration.
    """
    from lazytools.projects.brake import get_project_brake_enabled

    moment = now or datetime.now(UTC)
    if not get_project_brake_enabled(store, project_id):
        return AdmissionDecision(
            allowed=True,
            reason="project_brake_disabled",
            engine=budget.engine,
            operator_directed=operator_directed,
            decided_at=moment,
            telemetry_source=reading.source,
            detail=f"project {project_id!r} has the quota brake switched off",
        )
    return admit(store, budget=budget, reading=reading, operator_directed=operator_directed, review=review, now=now, prefix=admission_prefix)


def project_preflight(
    store: Store,
    project_id: str,
    *,
    budget: EngineBudget,
    reading: TelemetryReading,
    operator_directed: bool = False,
    now: datetime | None = None,
    admission_prefix: str = ADMISSION_PREFIX,
) -> AdmissionDecision:
    """``preflight``, gated by this PROJECT's own brake switch -- the
    no-reservation twin of :func:`project_admit`.

    Without it a caller deciding whether to ask a human first got the bare
    :func:`preflight`, which judged a brake-disabled project on a reading
    with no windows and refused it as "telemetry_unreadable" -- the opposite
    of what :func:`project_admit` then does for the same project. Found by
    review.
    """
    from lazytools.projects.brake import get_project_brake_enabled

    moment = now or datetime.now(UTC)
    if not get_project_brake_enabled(store, project_id):
        return AdmissionDecision(
            allowed=True,
            reason="project_brake_disabled",
            engine=budget.engine,
            operator_directed=operator_directed,
            decided_at=moment,
            telemetry_source=reading.source,
            detail=f"project {project_id!r} has the quota brake switched off",
        )
    return preflight(store, budget=budget, reading=reading, operator_directed=operator_directed, now=now, prefix=admission_prefix)


__all__ = [
    "ADMISSION_PREFIX",
    "AdmissionDecision",
    "CAS_ATTEMPTS",
    "ENGINES",
    "Engine",
    "EngineBudget",
    "SHADOW_ENV",
    "SHADOW_MARK",
    "TelemetryReading",
    "WindowReading",
    "admit",
    "budget_for",
    "decide",
    "in_flight_count",
    "preflight",
    "project_preflight",
    "project_admit",
    "release",
    "shadow_findings",
    "under_plan_warning",
]
