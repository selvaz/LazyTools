"""Bridge facts and constraints around the shared router; never launches an engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lazytools.code_bridge import _jobs, _store
from lazytools.projects.admission import ENGINES, Engine, TelemetryReading
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


@dataclass(frozen=True)
class Selection:
    decision: RoutingDecision
    readings: dict[Engine, TelemetryReading]
    model: str | None
    effort: str | None
    override: dict[str, str] = field(default_factory=dict)
    notice: str | None = None

    @property
    def rung(self) -> int | None:
        if self.decision.provider is None:
            return None
        reason = self.decision.reason
        return int(reason.rsplit("_step", 1)[1]) + 1 if "_step" in reason else 1

    def record(self) -> dict[str, Any]:
        return {**self.decision.as_record(), "override": self.override, "rung": self.rung}

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

    def error(self) -> str:
        exclusions = "; ".join(f"{engine}: {why}" for engine, why in self.decision.ineligible.items())
        notice = f" {self.notice}" if self.notice else ""
        return f"{self.decision.reason}; {exclusions}. Try an explicit --engine for manual selection.{notice}"


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
    available = frozenset((provider(engine),)) if engine else frozenset(ENGINES)
    # Native conversations cannot migrate providers, including after two failures.
    if continuity.engine is not None:
        available &= frozenset((continuity.engine,))
    writer: Engine | None = None
    if review_of:
        job = _jobs.find_job(store, _store.build_job_registry(store), review_of)
        if job is None:
            raise ValueError(f"no job found matching {review_of!r}")
        writer = provider(str(job.get("engine", job.get("kind"))))
    in_flight: dict[Engine, int] = dict.fromkeys(ENGINES, 0)
    for row in rows:
        if row.get("status") == "running":
            in_flight[provider(str(row.get("engine", row.get("kind"))))] += 1
    readings = read_readings()
    decision = recommend(
        tier, catalogue=catalogue, in_flight=in_flight, continuity=continuity,
        capability=CapabilityRequirement(images=needs == "images"),
        writer_provider_for_review=writer, available=available, readings=readings,
    )
    override = {}
    if decision.provider is not None:
        for value, rejection in (
            (model, DEFAULT_POLICY.reject_model(decision.provider, model)),
            (effort, DEFAULT_POLICY.reject_effort(effort, engine=decision.provider)),
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
    notice = None
    if continuity.failed_attempts >= 2:
        notice = f"Session {session!r} has {continuity.failed_attempts} consecutive failures; use a new --session to allow a different engine."
    return Selection(
        decision, readings, override.get("model", decision.model), effective_effort,
        override, notice,
    )


def print_selection(selection: Selection) -> None:
    """Explain the pick, both quota windows (including Claude's session), and exclusions."""
    print(selection.launch_line())
    for engine in ENGINES:
        reading = selection.readings.get(engine)
        print(f"{bridge_engine(engine)} quota:")
        for label, weekly in (("weekly", True), ("5-hour", False)):
            windows = [] if reading is None else [
                window for window in reading.windows
                if ((window.duration_minutes or 0) >= 24 * 60 if weekly else 0 < (window.duration_minutes or 0) <= 360)
            ]
            if not windows:
                print(f"  {label}: unreadable; reset unknown")
            for window in windows:
                reset = window.resets_at.isoformat() if window.resets_at else "unknown"
                print(f"  {label} ({window.window_id}): {window.used_percent:g}% used; reset {reset}")
    for provider_name, why in selection.decision.ineligible.items():
        print(f"excluded {provider_name}: {why}")
    if selection.notice:
        print(selection.notice)
