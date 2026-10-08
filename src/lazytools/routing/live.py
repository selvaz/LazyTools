"""Bounded live telemetry reads with a cache shared by short-lived CLI processes.

An unreadable engine carries its failure cause, never spare capacity. Cache trouble is
non-fatal; an expired or malformed entry never substitutes for a failed live read.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lazytools.projects import quota_telemetry
from lazytools.projects.admission import ENGINES, Engine, TelemetryReading, WindowReading, budget_for
from lazytools.routing.catalogue import TierCatalogue
from lazytools.routing.router import CapabilityRequirement, ContinuityHint, RoutingDecision, route

CACHE_MAX_AGE_SECONDS = 120.0
READ_TIMEOUT_SECONDS = 45.0
CACHE_PATH_ENV = "LAZYTOOLS_QUOTA_CACHE"


def default_cache_path() -> Path:
    override = os.environ.get(CACHE_PATH_ENV)
    return Path(override).expanduser() if override else Path.home() / ".lazytools" / "quota-cache.json"


def reading_record(reading: TelemetryReading) -> dict[str, Any]:
    """JSON-safe telemetry, used by the file cache and the bridge's dry run."""
    return {
        "engine": reading.engine,
        "source": reading.source,
        "observed_at": reading.observed_at.isoformat(),
        "error": reading.error,
        "windows": [
            {
                "window_id": window.window_id,
                "used_percent": window.used_percent,
                "duration_minutes": window.duration_minutes,
                "resets_at": window.resets_at.isoformat() if window.resets_at else None,
            }
            for window in reading.windows
        ],
    }


def _datetime(raw: str) -> datetime:
    moment = datetime.fromisoformat(raw)
    if moment.tzinfo is None:
        raise ValueError("cache timestamps must have a timezone")
    return moment


def _cached_readings(path: Path, moment: datetime) -> dict[Engine, TelemetryReading]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return {}
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("readings"), dict):
        return {}
    result: dict[Engine, TelemetryReading] = {}
    for engine in ENGINES:
        try:
            entry = data["readings"][engine]
            if entry["engine"] != engine or entry.get("error") is not None or not isinstance(entry["source"], str):
                continue
            observed_at = _datetime(entry["observed_at"])
            if not 0 <= (moment - observed_at).total_seconds() <= CACHE_MAX_AGE_SECONDS:
                continue
            windows = []
            for raw in entry["windows"]:
                used = float(raw["used_percent"])
                duration = raw["duration_minutes"]
                if not math.isfinite(used) or used < 0 or not isinstance(raw["window_id"], str):
                    raise ValueError("bad cached window")
                if duration is not None and (type(duration) is not int or duration <= 0):
                    raise ValueError("bad cached duration")
                windows.append(
                    WindowReading(
                        window_id=raw["window_id"],
                        used_percent=used,
                        duration_minutes=duration,
                        resets_at=_datetime(raw["resets_at"]) if raw["resets_at"] is not None else None,
                    )
                )
            if windows:
                result[engine] = TelemetryReading(engine, entry["source"], observed_at, tuple(windows))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return result


def _write_cache(path: Path, readings: dict[Engine, TelemetryReading]) -> None:
    temporary: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "readings": {engine: reading_record(reading) for engine, reading in readings.items()}}
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", delete=False
        ) as out:
            temporary = out.name
            json.dump(payload, out, allow_nan=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    except (OSError, ValueError):
        pass
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def read_readings(
    *,
    cache_path: Path | None = None,
    timeout: float = READ_TIMEOUT_SECONDS,
    now: datetime | None = None,
) -> dict[Engine, TelemetryReading]:
    """Read both engines, with a per-engine timeout and a 120-second file cache.

    ``cache_path`` or LAZYTOOLS_QUOTA_CACHE makes tests and isolated callers independent
    of the operator's cache. Misses run concurrently; failed reads retain their
    diagnostics for routing and human errors, but are never cached.
    """
    moment = now or datetime.now(UTC)
    path = cache_path if cache_path is not None else default_cache_path()
    readings = _cached_readings(path, moment)
    def fetch(engine: Engine) -> TelemetryReading:
        source = quota_telemetry.CODEX_SOURCE if engine == "codex" else quota_telemetry.CLAUDE_SOURCE
        try:
            reading = quota_telemetry.read_quota_sync(engine, timeout=timeout)
            if reading.engine != engine:
                raise ValueError(f"quota reader returned {reading.engine!r} for {engine!r}")
            if reading.error is not None:
                return replace(reading, windows=())
            if not reading.windows:
                return replace(reading, error=f"{reading.source} reported no usage window")
            return reading
        except Exception as exc:
            # Provider/transport failures must not escape recommend().
            return TelemetryReading(engine, source, moment, error=f"{type(exc).__name__}: {exc}")

    async def fetch_missing() -> list[TelemetryReading]:
        return await asyncio.gather(*(asyncio.to_thread(fetch, engine) for engine in ENGINES if engine not in readings))

    if len(readings) < len(ENGINES):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            fresh = asyncio.run(fetch_missing())
        else:
            # This synchronous API may be called inside a running agent's loop.
            # Run gather in its own loop rather than nesting asyncio.run there.
            with ThreadPoolExecutor(max_workers=1) as pool:
                fresh = pool.submit(lambda: asyncio.run(fetch_missing())).result()
        for reading in fresh:
            readings[reading.engine] = reading
    _write_cache(path, {engine: reading for engine, reading in readings.items() if reading.error is None})
    return readings


def recommend(
    tier: str,
    *,
    catalogue: dict[str, TierCatalogue],
    in_flight: dict[Engine, int],
    continuity: ContinuityHint | None = None,
    capability: CapabilityRequirement | None = None,
    writer_provider_for_review: Engine | None = None,
    available: frozenset[Engine] | None = None,
    readings: dict[Engine, TelemetryReading] | None = None,
    now: datetime | None = None,
    operator_directed: bool = False,
) -> RoutingDecision:
    """Read quota, obtain admission budgets, and route in the caller's admission mode.

    Autonomous routing remains the default; direct operator work opts in explicitly.
    """
    current = read_readings(now=now) if readings is None else readings
    return route(
        tier,
        catalogue=catalogue,
        readings=current,
        budgets={engine: budget_for(engine) for engine in ENGINES},
        in_flight=in_flight,
        continuity=continuity,
        capability=capability,
        writer_provider_for_review=writer_provider_for_review,
        available=available,
        now=now,
        operator_directed=operator_directed,
    )
