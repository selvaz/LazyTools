"""Reading each engine's quota, and never raising while doing it.

Ported from ``lazyceo.quota``, mechanism only -- this module already had no
CEO policy in it: it reads ``lazybridge.engines.codex.usage``/
``lazybridge.engines.claude_code.usage`` (LazyBridge primitives, unchanged)
and hides provider differences behind one shape, same as before.

The cache exists because admission is consulted on every delegation while
the underlying numbers move in minutes, not milliseconds.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from datetime import UTC, datetime

from lazytools.projects.admission import Engine, TelemetryReading, WindowReading

CACHE_SECONDS = 60.0

CODEX_SOURCE = "codex app-server account/rateLimits/read"
CLAUDE_SOURCE = "claude code /usage report"

_cache: dict[Engine, TelemetryReading] = {}
_locks: dict[Engine, asyncio.Lock] = {}


async def _read_codex(now: datetime) -> TelemetryReading:
    try:
        from lazybridge.engines.codex.usage import fetch_codex_usage

        snapshot = await fetch_codex_usage()
    except Exception as exc:
        return TelemetryReading(engine="codex", source=CODEX_SOURCE, observed_at=now, error=f"{type(exc).__name__}: {exc}")

    windows = tuple(
        WindowReading(
            window_id=f"{window.limit_id}/{window.window_duration_minutes}m",
            used_percent=window.used_percent,
            duration_minutes=window.window_duration_minutes,
            resets_at=window.resets_at,
        )
        for window in snapshot.windows
    )
    if not windows:
        return TelemetryReading(
            engine="codex", source=CODEX_SOURCE, observed_at=now, error="the App Server answered with no usage window"
        )
    return TelemetryReading(engine="codex", source=CODEX_SOURCE, observed_at=now, windows=windows)


async def _read_claude(now: datetime) -> TelemetryReading:
    try:
        from lazybridge.engines.claude_code.usage import fetch_claude_usage

        snapshot = await fetch_claude_usage()
    except Exception as exc:
        return TelemetryReading(engine="claude_code", source=CLAUDE_SOURCE, observed_at=now, error=f"{type(exc).__name__}: {exc}")

    windows: list[WindowReading] = []
    for label, window in (getattr(snapshot, "weekly", None) or {}).items():
        used = getattr(window, "used_percent", None)
        if used is None:
            continue
        windows.append(
            WindowReading(
                window_id=f"weekly/{label}",
                used_percent=float(used),
                duration_minutes=7 * 24 * 60,
                resets_at=getattr(window, "resets_at", None),
            )
        )
    if not windows:
        return TelemetryReading(
            engine="claude_code", source=CLAUDE_SOURCE, observed_at=now, error="no weekly percentage could be parsed out of the usage report"
        )
    return TelemetryReading(engine="claude_code", source=CLAUDE_SOURCE, observed_at=now, windows=tuple(windows))


_READERS = {"codex": _read_codex, "claude_code": _read_claude}


async def read_quota(engine: Engine, *, now: datetime | None = None, cache_seconds: float = CACHE_SECONDS) -> TelemetryReading:
    """The current reading for one engine, cached briefly."""
    moment = now or datetime.now(UTC)
    cached = _cache.get(engine)
    if cached is not None and cached.age_seconds(now=moment) <= cache_seconds:
        return cached

    lock = _locks.setdefault(engine, asyncio.Lock())
    async with lock:
        cached = _cache.get(engine)
        if cached is not None and cached.age_seconds(now=moment) <= cache_seconds:
            return cached
        reading = await _READERS[engine](moment)
        _cache[engine] = reading
        return reading


def cache_quota_reading(reading: TelemetryReading) -> None:
    """Install a reading directly, bypassing the provider. For tests."""
    _cache[reading.engine] = reading


def forget_cached_quota(engine: Engine | None = None) -> None:
    """Drop the cache. For tests, and for a caller just told the numbers moved."""
    if engine is None:
        _cache.clear()
    else:
        _cache.pop(engine, None)


def read_quota_sync(engine: Engine, *, timeout: float = 60.0) -> TelemetryReading:
    """``read_quota`` for a caller with no event loop of its own.

    A cache hit is served straight from ``_cache``. A miss is fetched in a
    throwaway daemon thread running its own one-shot ``asyncio.run`` loop --
    never ``read_quota``'s own coroutine or its per-engine lock, which
    belongs to whatever long-running loop created it.
    """
    moment = datetime.now(UTC)
    cached = _cache.get(engine)
    if cached is not None and cached.age_seconds(now=moment) <= CACHE_SECONDS:
        return cached

    source = CODEX_SOURCE if engine == "codex" else CLAUDE_SOURCE
    box: queue.Queue[TelemetryReading] = queue.Queue(maxsize=1)

    def _fetch() -> None:
        try:
            reading = asyncio.run(_READERS[engine](datetime.now(UTC)))
        except Exception as exc:
            reading = TelemetryReading(engine=engine, source=source, observed_at=datetime.now(UTC), error=f"{type(exc).__name__}: {exc}")
        box.put(reading)

    threading.Thread(target=_fetch, name=f"lazytools-projects-quota-sync-{engine}", daemon=True).start()
    try:
        reading = box.get(timeout=timeout)
    except queue.Empty:
        return TelemetryReading(engine=engine, source=source, observed_at=moment, error=f"timed out after {timeout:.1f}s")
    if reading.error is None:
        cache_quota_reading(reading)
    return reading


__all__ = [
    "CACHE_SECONDS",
    "CLAUDE_SOURCE",
    "CODEX_SOURCE",
    "cache_quota_reading",
    "forget_cached_quota",
    "read_quota",
    "read_quota_sync",
]
