"""Per-engine concurrency limit: at most N live jobs per engine, across every repository.

The per-repository :class:`~lazytools.code_bridge._lockfile.JobLock` stops two
jobs racing on one ``.git`` index; it says nothing about two Codex jobs in two
different repositories, which is what turned 17/10/2026 into fifteen
approval tickets in fifteen minutes. This module adds an engine-wide limit on
top, built from the SAME reclaimable lock file: one slot = one lock file
(``engine-<name>-<i>.lock`` in the bridge's lock directory), owned by the pid
running the job, reclaimed when that pid is gone -- so a killed or crashed
job never leaves its engine blocked, and a ``--detach``-ed child holds its slot
for exactly as long as it runs.

Limits come from the ``[concurrency]`` table of ``~/.lazytools/code-bridge.toml``
(path overridable with ``LAZYTOOLS_CODE_BRIDGE_CONFIG``)::

    [concurrency]
    codex = 1     # default 1
    claude = 0    # 0 (or absent) = unlimited; the default

Slots live next to the job store (``<store dir>/locks``), so every launch that
shares a store -- the default ``~/.lazytools/code-bridge.sqlite`` -- shares
the limit.
"""

from __future__ import annotations

import os
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from lazytools.code_bridge._lockfile import JobLock

CONFIG_ENV = "LAZYTOOLS_CODE_BRIDGE_CONFIG"
DEFAULT_CONFIG_PATH = Path.home() / ".lazytools" / "code-bridge.toml"

#: ``None`` = unlimited. Codex defaults to one job at a time machine-wide:
#: each Codex job escalates independently, and two of them at once doubled
#: the ticket rate without doubling what a human could review.
DEFAULT_LIMITS: dict[str, int | None] = {"codex": 1, "claude": None}

#: How often a ``--wait-for-slot`` launch re-checks for a free slot.
WAIT_POLL_SECONDS = 5.0


def config_path() -> Path:
    override = os.environ.get(CONFIG_ENV)
    return Path(override).expanduser() if override else DEFAULT_CONFIG_PATH


def load_limits(path: Path | None = None) -> dict[str, int | None]:
    """Per-engine limits: defaults, overridden by the config file's ``[concurrency]`` table."""
    path = path or config_path()
    limits = dict(DEFAULT_LIMITS)
    if not path.is_file():
        return limits
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"{path}: not valid TOML: {exc}") from exc
    table = data.get("concurrency", {})
    if not isinstance(table, dict):
        raise ValueError(f"{path}: [concurrency] must be a table")
    for engine, value in table.items():
        if engine not in DEFAULT_LIMITS:
            raise ValueError(f"{path}: [concurrency] names unknown engine {engine!r} (one of {', '.join(DEFAULT_LIMITS)})")
        if type(value) is not int or value < 0:
            raise ValueError(f"{path}: [concurrency] {engine} must be an integer >= 0 (0 = unlimited), got {value!r}")
        limits[engine] = value or None
    return limits


class EngineBusy(RuntimeError):
    """Every slot of this engine is held by a live job."""

    def __init__(self, engine: str, limit: int, holders: list[dict]) -> None:
        self.engine = engine
        self.limit = limit
        self.holders = holders
        jobs = ", ".join(f"job {h.get('job_id', '?')} (pid {h.get('pid', '?')})" for h in holders) or "unknown job"
        super().__init__(
            f"engine {engine} is at its concurrency limit ({limit}): {jobs} is running. "
            f"Wait for it (`lazytools-code-bridge wait <job>`), pass --wait-for-slot to queue, "
            f"or change [concurrency] {engine} in {config_path()}."
        )


def busy_reason(engine: str, limit: int, holders: list[dict]) -> str:
    """One-line router exclusion text for an engine at its limit."""
    jobs = ", ".join(str(h.get("job_id", "?")) for h in holders)
    return f"engine_at_concurrency_limit: {engine} {len(holders)}/{limit} (running job {jobs})"


@dataclass
class _SlotLock(JobLock):
    """One engine slot: a ``JobLock`` at a fixed path, with its own "held" error."""

    engine: str = ""
    limit: int = 1
    _all_holders: Callable[[], list[dict]] = field(default=lambda: [], repr=False)

    def _held(self, pid: int, job_id: str) -> Exception:
        return EngineBusy(self.engine, self.limit, self._all_holders() or [{"job_id": job_id, "pid": pid}])


def _slot(lock_dir: Path, engine: str, index: int, limit: int) -> _SlotLock:
    path = lock_dir / f"engine-{engine}-{index}.lock"
    return _SlotLock(lock_dir, Path(f"<engine slot {engine}#{index}>"), _path=path, engine=engine, limit=limit)


def live_holders(lock_dir: Path, engine: str, limit: int) -> list[dict]:
    """The live jobs currently holding a slot of ``engine`` (read-only)."""
    holders = []
    for index in range(limit):
        holder = _slot(lock_dir, engine, index, limit).holder()
        if holder is not None:
            holders.append(holder)
    return holders


def engine_at_limit(lock_dir: Path, engine: str, limits: dict[str, int | None] | None = None) -> list[dict]:
    """The live holders if ``engine`` has no free slot, else ``[]``."""
    limit = (limits if limits is not None else load_limits()).get(engine)
    if not limit:
        return []
    holders = live_holders(lock_dir, engine, limit)
    return holders if len(holders) >= limit else []


@dataclass
class EngineSlot:
    """A held slot for one job; :meth:`release` when the job ends. No-op for an unlimited engine."""

    lock_dir: Path
    engine: str
    limit: int | None
    _held_lock: _SlotLock | None = None
    #: Job ids of STALE slot holders this acquire reclaimed (dead pids).
    reclaimed: list[str] = field(default_factory=list)

    def try_acquire(self, job_id: str) -> None:
        """Take a free (or stale) slot, or raise :class:`EngineBusy` naming the running job(s)."""
        if not self.limit:
            return
        limit = self.limit
        for index in range(limit):
            lock = _slot(self.lock_dir, self.engine, index, limit)
            lock._all_holders = lambda: live_holders(self.lock_dir, self.engine, limit)
            try:
                stale = lock.acquire(job_id)
            except EngineBusy:
                continue
            if stale is not None:
                self.reclaimed.append(stale)
            self._held_lock = lock
            return
        raise EngineBusy(self.engine, limit, live_holders(self.lock_dir, self.engine, limit))

    def acquire(
        self,
        job_id: str,
        *,
        wait: bool = False,
        poll_seconds: float | None = None,
        on_wait: Callable[[EngineBusy], None] | None = None,
    ) -> None:
        """:meth:`try_acquire`, or with ``wait`` keep retrying until a slot frees up."""
        announced = False
        while True:
            try:
                self.try_acquire(job_id)
                return
            except EngineBusy as busy:
                if not wait:
                    raise
                if on_wait is not None and not announced:
                    on_wait(busy)
                    announced = True
                time.sleep(poll_seconds if poll_seconds is not None else WAIT_POLL_SECONDS)

    def release(self) -> None:
        if self._held_lock is not None:
            self._held_lock.release()
            self._held_lock = None


__all__ = [
    "CONFIG_ENV",
    "DEFAULT_LIMITS",
    "EngineBusy",
    "EngineSlot",
    "busy_reason",
    "config_path",
    "engine_at_limit",
    "live_holders",
    "load_limits",
]
