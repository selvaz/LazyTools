"""Bridge facts and constraints around the shared router; never launches an engine."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from lazytools.code_bridge import _jobs, _lockfile, _store
from lazytools.projects.admission import (
    ENGINES,
    LONG_WINDOW_MINUTES,
    Engine,
    TelemetryReading,
    WindowReading,
    budget_for,
    decide,
)
from lazytools.routing import (
    CapabilityRequirement,
    ContinuityHint,
    RoutingDecision,
    load_default_tiers,
    load_tiers,
    recommend,
)
from lazytools.routing.live import read_readings, reading_record
from lazytools.routing.policy import DEFAULT_POLICY
from lazytools.routing.router import FIVE_HOUR_WINDOW_MAX_MINUTES, _five_hour_window_id, _weekly_window_id


def provider(engine: str) -> Engine:
    if engine == "codex":
        return "codex"
    if engine in ("claude", "claude_code"):
        return "claude_code"
    raise ValueError(f"unknown bridge engine: {engine!r}")


def bridge_engine(engine: Engine) -> str:
    return "claude" if engine == "claude_code" else "codex"


def _repo(cwd: str) -> Path:
    return _jobs._git_repo_root(Path(cwd).expanduser().resolve())


def session_hint(rows: list[dict[str, Any]], cwd: str, session: str | None) -> ContinuityHint:
    """Most recent session engine and its consecutive terminal failures in this repo."""
    if not session:
        return ContinuityHint()
    history = [row for row in rows if row.get("session_name") == session and row.get("cwd") and _repo(row["cwd"]) == _repo(cwd)]
    history.sort(key=lambda row: row.get("created_at") or 0, reverse=True)
    if not history:
        return ContinuityHint()
    engine = provider(str(history[0].get("engine", history[0].get("kind"))))
    failures = 0
    for row in history:
        if row.get("status") in ("failed", "interrupted"):
            failures += 1
        elif row.get("status") == "done":
            break
    return ContinuityHint(engine=engine, failed_attempts=failures)


def session_conflict_message(session: str, pinned: Engine, requested: Engine) -> str:
    return (
        f"session_conflict: Session {session!r} is pinned to {bridge_engine(pinned)}; "
        f"the requested {bridge_engine(requested)} engine conflicts with it. "
        f"Use a new --session to start a {bridge_engine(requested)} conversation."
    )


def check_session_engine(*, cwd: str, session: str, engine: str, db_path: Path | None = None) -> None:
    """Manual and detached launches obey the same native-session pin as tier routing."""
    rows = _jobs.list_jobs(_store.build_store(db_path), all_jobs=True)
    pinned = session_hint(rows, cwd, session).engine
    requested = provider(engine)
    if pinned is not None and pinned != requested:
        raise ValueError(session_conflict_message(session, pinned, requested))


def model_provider(model: str) -> Engine:
    stripped = model.strip()
    if stripped in DEFAULT_POLICY.codex_models:
        return "codex"
    if stripped.lower().startswith("claude-") or stripped.lower() in ("sonnet", "opus", "haiku", "fable"):
        return "claude_code"
    raise ValueError(f"cannot infer engine from model {model!r}; choose --engine explicitly")


def _admission_reading(reading: TelemetryReading, model: str) -> TelemetryReading:
    """Only the effective model's weekly bucket and its engine's short window.

    Match telemetry bucket identities exactly: a Codex per-model limit whose
    name contains 'codex' must not gate another model's account-wide limits.
    Claude's session gate is separate from the router's preserved tie-break.
    """
    weekly_bucket = _weekly_window_id(reading.engine, model)
    short_bucket = _five_hour_window_id(reading.engine) or "session"

    def matches(window: WindowReading, bucket: str, *, weekly: bool) -> bool:
        identity = window.window_id.lower().strip()
        if reading.engine == "codex":
            return identity in (bucket, f"{bucket}/{window.duration_minutes}m")
        return identity in (bucket, f"weekly/{bucket}") if weekly else identity == bucket

    weekly = tuple(window for window in reading.windows
                   if (window.duration_minutes or 0) >= LONG_WINDOW_MINUTES
                   and matches(window, weekly_bucket, weekly=True))
    short = tuple(window for window in reading.windows
                  if 0 < (window.duration_minutes or 0) <= FIVE_HOUR_WINDOW_MAX_MINUTES
                  and matches(window, short_bucket, weekly=False))
    error = reading.error
    if not weekly and error is None:
        error = f"no weekly quota window for {reading.engine}/{model!r} ({weekly_bucket!r})"
    return replace(reading, windows=(*weekly, *short), error=error)


@dataclass(frozen=True)
class Selection:
    decision: RoutingDecision
    readings: dict[Engine, TelemetryReading]
    model: str | None
    effort: str | None
    override: dict[str, str] = field(default_factory=dict)
    notice: str | None = None
    in_flight: dict[Engine, int] = field(default_factory=dict)
    operator_directed: bool = True
    review_of: str | None = None
    session: str | None = None
    pinned_engine: Engine | None = None
    requested_engine: Engine | None = None
    ceiling_windows: dict[Engine, tuple[WindowReading, ...]] = field(default_factory=dict)

    @property
    def rung(self) -> int | None:
        if self.decision.provider is None:
            return None
        reason = self.decision.reason
        return int(reason.rsplit("_step", 1)[1]) + 1 if "_step" in reason else 1

    def record(self) -> dict[str, Any]:
        return {**self.decision.as_record(), "override": self.override, "rung": self.rung, "operator_directed": self.operator_directed}

    def forecast(self, engine: Engine, window: WindowReading) -> tuple[float, float] | None:
        """Projected usage including the same reservations used in the router's margins."""
        reading = self.readings[engine]
        projected = window.projected_end_percent(now=reading.observed_at)
        elapsed = window.elapsed_fraction(now=reading.observed_at)
        if projected is None or elapsed is None or elapsed <= 0:
            return None
        budget = budget_for(engine)
        reserved = (max(self.in_flight.get(engine, 0), 0) + 1) * budget.per_job_reserve_percent
        return projected + reserved, budget.forecast_limit_percent(elapsed)

    def payload(self) -> dict[str, Any]:
        return {
            **self.record(), "model": self.model, "effort": self.effort,
            "engine": bridge_engine(self.decision.provider) if self.decision.provider else None,
            "readings": {engine: reading_record(reading) for engine, reading in self.readings.items()},
            "notice": self.notice,
        }

    def launch_line(self) -> str:
        chosen = self.decision
        overrides = f"; override={self.override}" if self.override else ""
        return (
            f"chosen: {bridge_engine(chosen.provider) if chosen.provider else '(none)'}/"
            f"{self.model or 'default'} effort={self.effort or 'default'} "
            f"tier={chosen.tier} rung={self.rung} because {chosen.reason}{overrides}"
        )

    def _ceiling_guidance(self) -> str:
        if not any("admission_would_refuse:absolute_ceiling" in why for why in self.decision.ineligible.values()):
            return ""
        details = []
        for engine, windows in self.ceiling_windows.items():
            budget = budget_for(engine)
            reserved = (max(self.in_flight.get(engine, 0), 0) + 1) * budget.per_job_reserve_percent
            for window in windows:
                reset = window.resets_at.isoformat() if window.resets_at else "unknown"
                details.append(
                    f"{bridge_engine(engine)} {window.window_id} at {window.used_percent:g}% used "
                    f"({window.used_percent + reserved:g}% including reservations; ceiling {budget.ceiling_percent:g}%); reset {reset}"
                )
        ceilings = f" At ceiling: {'; '.join(details)}." if details else " An applicable quota window is at its ceiling."
        return f"{ceilings} Wait for quota to reset before retrying."

    def error(self) -> str:
        grouped: dict[str, list[str]] = {}
        for key, why in self.decision.ineligible.items():
            reasons = grouped.setdefault(key.rsplit(":", 1)[-1], [])
            if why not in reasons:
                reasons.append(why)
        exclusions = "; ".join(f"{engine}: {'; '.join(reasons)}" for engine, reasons in grouped.items())
        notice = f" {self.notice}" if self.notice else ""
        ceilings = self._ceiling_guidance()
        if self.decision.reason == "session_conflict":
            return self.notice or exclusions
        if self.decision.reason == "human_review_required":
            return (
                f"human_review_required: no opposite-engine reviewer is eligible for job {self.review_of!r}; "
                f"{exclusions}. A human review is required.{ceilings}{notice}"
            )
        # Pure route() embeds rung exclusions in this reason. Use the structured
        # exclusions once here, leaving the router's parity contract untouched.
        reason = self.decision.reason.split(" (", 1)[0]
        if ceilings:
            return f"{reason}; {exclusions}.{ceilings}{notice}"
        if self.pinned_engine is not None:
            return (
                f"{reason}; {exclusions}. Session {self.session!r} is pinned to {bridge_engine(self.pinned_engine)}; "
                f"use a new --session to consider another engine.{notice}"
            )
        hint = " Try an explicit --engine for manual selection." if self.requested_engine is None else ""
        return f"{reason}; {exclusions}.{hint}{notice}"


def choose(
    tier: str, *, cwd: str, db_path: Path | None = None, engine: str | None = None,
    session: str | None = None, needs: str | None = None, review_of: str | None = None,
    model: str | None = None, effort: str | None = None, tiers_path: Path | None = None,
) -> Selection:
    catalogue = load_tiers(tiers_path) if tiers_path is not None else load_default_tiers()
    # Unknown tiers should fail before the slow telemetry read.
    if tier not in catalogue:
        raise ValueError(f"unknown tier {tier!r}; choose one of {', '.join(catalogue)}")
    store = _store.build_store(db_path)
    rows = _jobs.list_jobs(store, all_jobs=True)
    continuity = session_hint(rows, cwd, session)
    requested = provider(engine) if engine else model_provider(model) if model is not None else None
    if requested is not None and model is not None:
        rejection = DEFAULT_POLICY.reject_model(requested, model)
        if rejection is not None:
            raise ValueError(rejection)
    available = frozenset((requested,)) if requested is not None else frozenset(ENGINES)
    # Native conversations cannot migrate providers, including after two failures.
    if continuity.engine is not None:
        available &= frozenset((continuity.engine,))
    writer: Engine | None = None
    if review_of:
        job = _jobs.find_job(store, _store.build_job_registry(store), review_of)
        if job is None:
            raise ValueError(f"no job found matching {review_of!r}")
        writer = provider(str(job.get("engine", job.get("kind"))))
    notice = None
    requested_for_session = requested or ("codex" if needs == "images" else None)
    if needs == "images" and continuity.engine != "codex":
        requested_for_session = "codex"
    if session and continuity.engine is not None and requested_for_session is not None and requested_for_session != continuity.engine:
        if needs == "images" and continuity.engine != "codex":
            notice = (
                f"session_conflict: images need codex, but Session {session!r} is pinned to {bridge_engine(continuity.engine)}. "
                "Use a new --session with --engine codex for images."
            )
        else:
            notice = session_conflict_message(session, continuity.engine, requested_for_session)
        if writer is None:
            return Selection(
                RoutingDecision(tier, None, None, None, "session_conflict", False,
                                ineligible={requested_for_session: notice}),
                {}, None, None, notice=notice,
            )
    in_flight: dict[Engine, int] = dict.fromkeys(ENGINES, 0)
    for row in rows:
        pid = row.get("pid")
        # A paused job awaiting approval still owns a live engine and resumes without
        # another admission check, so it keeps its reservation (Codex review on #186).
        if row.get("status") in ("running", "awaiting_approval") and type(pid) is int and _lockfile._pid_alive(pid):
            in_flight[provider(str(row.get("engine", row.get("kind"))))] += 1
    readings = read_readings()
    routing_catalogue = catalogue
    if model is not None:
        # Score/admit the explicit model's bucket, even when the catalogue model
        # uses another one (Fable versus all models). Keep the original pick in
        # the job's routing record and the effective model in routing.override.
        ladder = catalogue[tier]
        routing_catalogue = {**catalogue, tier: replace(ladder, steps=tuple(
            replace(step, providers=tuple(
                replace(candidate, model=model.strip()) if candidate.provider == requested else candidate
                for candidate in step.providers
            )) for step in ladder.steps
        ))}
    routing_readings = {}
    for reading_engine, reading in readings.items():
        models = {candidate.model for step in routing_catalogue[tier].steps for candidate in step.providers
                  if candidate.provider == reading_engine}
        applicable = {window.window_id for candidate_model in models
                      for window in _admission_reading(reading, candidate_model).windows}
        # The pure router's historical substring matching must not mistake an
        # extra per-model limit for an account bucket before the post-pick gate.
        routing_readings[reading_engine] = replace(reading, windows=tuple(
            window for window in reading.windows if window.window_id in applicable
        ))
    # The bridge has no project attribution today: these are direct, operator-directed
    # jobs. If attribution is introduced, a project with its brake enabled must pass
    # operator_directed=False here, using the project's existing brake setting.
    operator_directed = True
    refused: dict[str, str] = {}
    scores: dict[str, dict[str, Any]] = {}
    while True:
        decision = recommend(
            tier, catalogue=routing_catalogue, in_flight=in_flight, continuity=continuity,
            capability=CapabilityRequirement(images=needs == "images"),
            writer_provider_for_review=writer, available=available, readings=routing_readings,
            operator_directed=operator_directed,
        )
        scores.update(decision.scores)
        if decision.provider is None:
            break
        chosen = decision.provider
        # Preserve weekly scoring, but gate the launch on the effective model's
        # weekly bucket and its engine's 5-hour/session window. Other models'
        # buckets (such as Claude Fable) do not apply to this job.
        chosen_model = model.strip() if model is not None else decision.model
        assert chosen_model is not None
        admission = decide(_admission_reading(readings[chosen], chosen_model), budget_for(chosen),
                           operator_directed=operator_directed, in_flight=in_flight[chosen])
        if admission.allowed:
            break
        refused[chosen] = f"bridge_admission_would_refuse:{admission.reason} ({admission.rejection_text()})"
        available -= frozenset((chosen,))
    exclusions = dict(decision.ineligible)
    for key, why in exclusions.items():
        key_engine = key.rsplit(":", 1)[-1]
        if key_engine in refused:
            exclusions[key] = refused[key_engine]
        elif why == "not_available_to_this_agent":
            if continuity.engine is not None and key_engine != continuity.engine:
                exclusions[key] = f"session {session!r} is pinned to {bridge_engine(continuity.engine)}"
            elif requested is not None and key_engine != requested:
                exclusions[key] = f"requested engine/model restricts routing to {bridge_engine(requested)}"
    exclusions.update(refused)
    decision = replace(decision, scores=scores, ineligible=exclusions)
    if model is not None and decision.provider is not None:
        step_index = int(decision.reason.rsplit("_step", 1)[1]) if "_step" in decision.reason else 0
        original_pick = catalogue[tier].steps[step_index].for_provider(decision.provider)
        assert original_pick is not None
        decision = replace(decision, model=original_pick.model)
    if decision.provider is None and decision.reason.startswith("no_eligible_provider"):
        decision = replace(decision, reason="no_eligible_provider")
    override = {}
    if decision.provider is not None:
        effective_model = model.strip() if model is not None else decision.model
        for value, rejection in (
            (model, DEFAULT_POLICY.reject_model(decision.provider, model)),
            (effort if effort is not None else decision.effort,
             DEFAULT_POLICY.reject_effort(effort if effort is not None else decision.effort,
                                          engine=decision.provider, model=effective_model)),
        ):
            if value is not None and rejection is not None:
                raise ValueError(rejection)
    if model is not None:
        override["model"] = model.strip()
    if effort is not None:
        override["effort"] = effort.strip()
    effective_effort = override.get("effort", decision.effort)
    if effective_effort is not None:
        # Catalogue validation intentionally preserves the original loader's raw
        # effort. Engines accept exact levels, so forward the validated stripped value.
        effective_effort = effective_effort.strip()
    if continuity.failed_attempts >= 2:
        failure_notice = f"Session {session!r} has {continuity.failed_attempts} consecutive failures; use a new --session to allow a different engine."
        notice = f"{notice} {failure_notice}" if notice else failure_notice
    ceiling_windows: dict[Engine, tuple[WindowReading, ...]] = {}
    for candidate_engine in ENGINES:
        if candidate_engine not in readings or not any(
            key.rsplit(":", 1)[-1] == candidate_engine and "admission_would_refuse:absolute_ceiling" in why
            for key, why in exclusions.items()
        ):
            continue
        models = {model.strip()} if model is not None else {
            candidate.model for step in catalogue[tier].steps for candidate in step.providers
            if candidate.provider == candidate_engine
        }
        windows = {window.window_id: window for candidate_model in sorted(models)
                   for window in _admission_reading(readings[candidate_engine], candidate_model).windows}
        budget = budget_for(candidate_engine)
        reserved = (max(in_flight[candidate_engine], 0) + 1) * budget.per_job_reserve_percent
        ceiling_windows[candidate_engine] = tuple(
            window for window in windows.values() if window.used_percent + reserved >= budget.ceiling_percent
        )
    return Selection(
        decision, readings, override.get("model", decision.model), effective_effort,
        override, notice, in_flight=in_flight, operator_directed=operator_directed, review_of=review_of,
        session=session, pinned_engine=continuity.engine,
        requested_engine=requested, ceiling_windows=ceiling_windows,
    )


def print_selection(selection: Selection) -> None:
    """Explain the pick, both quota windows (including Claude's session), and exclusions."""
    print(selection.launch_line(), flush=True)
    if selection.decision.reason == "session_conflict":
        return  # Conflict validation deliberately runs before telemetry.
    for engine in ENGINES:
        reading = selection.readings.get(engine)
        print(f"{bridge_engine(engine)} quota:")
        for label in ("weekly", "5-hour", "other"):
            windows = [] if reading is None else [
                window for window in reading.windows
                if ("weekly" if (window.duration_minutes or 0) >= 24 * 60
                    else "5-hour" if 0 < (window.duration_minutes or 0) <= 360 else "other") == label
            ]
            if not windows and label != "other":
                print(f"  {label}: unreadable; reset unknown")
            for window in windows:
                reset = window.resets_at.isoformat() if window.resets_at else "unknown"
                forecast = selection.forecast(engine, window)
                forecast_text = "projected unavailable" if forecast is None else f"projected {forecast[0]:.0f}% (limit {forecast[1]:.0f}%)"
                print(f"  {label} ({window.window_id}): {window.used_percent:g}% used; reset {reset}; {forecast_text}")
                budget = budget_for(engine)
                effective = window.used_percent + (max(selection.in_flight.get(engine, 0), 0) + 1) * budget.per_job_reserve_percent
                if effective >= budget.ceiling_percent:
                    print(f"warning: {bridge_engine(engine)} {label} ({window.window_id}) at {window.used_percent:g}% used ({effective:g}% including reservations; ceiling {budget.ceiling_percent:g}%)")
                if forecast is not None and forecast[0] > forecast[1]:
                    print(f"warning: {bridge_engine(engine)} {label} projected {forecast[0]:.0f}% (limit {forecast[1]:.0f}%) at the current pace")
    if selection.decision.provider is not None:
        for provider_name, why in selection.decision.ineligible.items():
            print(f"excluded {provider_name}: {why}")
    if selection.notice and selection.decision.provider is not None:
        print(selection.notice)
