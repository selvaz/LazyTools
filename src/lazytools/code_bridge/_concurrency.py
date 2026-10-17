"""Per-engine concurrency limit: at most N live jobs per engine, machine-wide.

The per-repository :class:`~lazytools.code_bridge._lockfile.JobLock` stops two
jobs racing on one ``.git`` index; it says nothing about two Codex jobs in two
different repositories, which is what turned 17/10/2026 into fifteen
approval tickets in fifteen minutes. This module adds an engine-wide limit.

Design (after review found the first version could be exceeded three ways):

* **One canonical slot directory per machine**, independent of ``--db``:
  ``%LOCALAPPDATA%\\lazytools\\engine-slots`` on Windows, else
  ``~/.lazytools/engine-slots``. Only an explicit ``slot_dir`` config key
  moves it (meant for tests). Two jobs using different Stores share it.
* **Every inspect / reclaim / acquire / release runs under one OS-backed
  exclusive lock** on ``guard.lock`` in that directory (``msvcrt.locking``
  on Windows, ``fcntl.flock`` elsewhere), held only for that short critical
  section. So two launches can never both reclaim the same dead slot, or both
  see "one free" and both take it.
* **Admission counts every live slot file of the engine**, one file per job
  (``<engine>-<job_id>.slot``) with no slot indices -- lowering a limit from
  2 to 1 while two jobs run still sees both. Every job takes a slot file,
  even for an unlimited engine, so jobs started while the engine was
  unlimited count against a limit configured later.
* A slot file records the pid, job id and **owning Store path**; a slot whose
  pid is gone is reclaimed at the next acquire and returned to the caller,
  which marks that job ``interrupted`` in the right Store.

Limits come from ``~/.lazytools/code-bridge.toml`` (path overridable with
``LAZYTOOLS_CODE_BRIDGE_CONFIG``)::

    [concurrency]
    codex = 1     # default 1; 0 = unlimited
    claude = 0    # default unlimited
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
import tomllib
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lazytools.code_bridge import _lockfile

CONFIG_ENV = "LAZYTOOLS_CODE_BRIDGE_CONFIG"
DEFAULT_CONFIG_PATH = Path.home() / ".lazytools" / "code-bridge.toml"

#: ``None`` = unlimited. Codex defaults to one job at a time machine-wide:
#: each Codex job escalates independently, and two of them at once doubled
#: the ticket rate without doubling what a human could review.
DEFAULT_LIMITS: dict[str, int | None] = {"codex": 1, "claude": None}

#: How often a ``--wait-for-slot`` launch re-checks for a free slot.
WAIT_POLL_SECONDS = 5.0
#: Upper bound on waiting for the guard lock itself (it is only ever held
#: for a directory scan and one small file write).
GUARD_TIMEOUT_SECONDS = 30.0
GUARD_FILE = "guard.lock"


def config_path() -> Path:
    override = os.environ.get(CONFIG_ENV)
    return Path(override).expanduser() if override else DEFAULT_CONFIG_PATH


def default_slot_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA") if sys.platform == "win32" else None
    if local:
        return Path(local) / "lazytools" / "engine-slots"
    return Path.home() / ".lazytools" / "engine-slots"


@dataclass(frozen=True)
class ConcurrencyConfig:
    limits: dict[str, int | None]
    slot_dir: Path


def load_config(path: Path | None = None) -> ConcurrencyConfig:
    """Limits and slot directory: defaults, overridden by the config file's ``[concurrency]`` table."""
    path = path or config_path()
    limits = dict(DEFAULT_LIMITS)
    slot_dir = default_slot_dir()
    if not path.is_file():
        return ConcurrencyConfig(limits, slot_dir)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"{path}: not valid TOML: {exc}") from exc
    table = data.get("concurrency", {})
    if not isinstance(table, dict):
        raise ValueError(f"{path}: [concurrency] must be a table")
    for key, value in table.items():
        if key == "slot_dir":
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{path}: [concurrency] slot_dir must be a non-empty path string")
            slot_dir = Path(value).expanduser()
            continue
        if key not in DEFAULT_LIMITS:
            raise ValueError(
                f"{path}: [concurrency] names unknown key {key!r} (one of {', '.join(DEFAULT_LIMITS)}, slot_dir)"
            )
        if type(value) is not int or value < 0:
            raise ValueError(f"{path}: [concurrency] {key} must be an integer >= 0 (0 = unlimited), got {value!r}")
        limits[key] = value or None
    return ConcurrencyConfig(limits, slot_dir)


def load_limits(path: Path | None = None) -> dict[str, int | None]:
    return load_config(path).limits


class EngineBusy(RuntimeError):
    """The engine already has ``limit`` live jobs."""

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


# --------------------------------------------------------------------------- #
# the guard: one OS-backed exclusive lock around every slot-directory change
# --------------------------------------------------------------------------- #


@contextlib.contextmanager
def _guard(slot_dir: Path, timeout: float = GUARD_TIMEOUT_SECONDS) -> Iterator[None]:
    slot_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(slot_dir / GUARD_FILE), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if sys.platform == "win32":
            import msvcrt

            deadline = time.monotonic() + timeout
            while True:
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"could not lock {slot_dir / GUARD_FILE} within {timeout:g}s") from None
                    time.sleep(0.01)
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _slot_files(slot_dir: Path, engine: str) -> list[Path]:
    return sorted(slot_dir.glob(f"{engine}-*.slot"))


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _scan(slot_dir: Path, engine: str) -> tuple[list[dict], list[tuple[Path, dict]]]:
    """``(live holders, stale (path, meta))`` -- call under the guard."""
    live: list[dict] = []
    stale: list[tuple[Path, dict]] = []
    for path in _slot_files(slot_dir, engine):
        meta = _read(path) or {}
        pid = meta.get("pid")
        if isinstance(pid, int) and _lockfile._pid_alive(pid):
            live.append(meta)
        else:
            stale.append((path, meta))  # dead pid, or unreadable: reclaimable
    return live, stale


def live_holders(engine: str, slot_dir: Path | None = None) -> list[dict]:
    """Every live job holding a slot of ``engine`` (read-only)."""
    slot_dir = slot_dir or load_config().slot_dir
    if not slot_dir.is_dir():
        return []
    with _guard(slot_dir):
        return _scan(slot_dir, engine)[0]


def engine_at_limit(engine: str, config: ConcurrencyConfig | None = None) -> list[dict]:
    """The live holders if ``engine`` has no free slot, else ``[]``."""
    config = config or load_config()
    limit = config.limits.get(engine)
    if not limit:
        return []
    holders = live_holders(engine, config.slot_dir)
    return holders if len(holders) >= limit else []


@dataclass
class EngineSlot:
    """One job's slot of ``engine``; :meth:`release` when the job ends.

    ``limit=None`` (unlimited) never refuses, but still records a slot file,
    so the job counts if a limit is configured while it runs.
    """

    engine: str
    limit: int | None
    slot_dir: Path
    #: The Store this job records into, written into the slot so a later
    #: reclaim can mark this job interrupted in the RIGHT Store.
    db_path: Path | None = None
    #: Metadata of STALE slots (dead pids) this acquire reclaimed.
    reclaimed: list[dict] = field(default_factory=list)
    path: Path | None = None

    def try_acquire(self, job_id: str) -> None:
        """Take a slot, or raise :class:`EngineBusy` naming the running job(s). Atomic machine-wide."""
        with _guard(self.slot_dir):
            live, stale = _scan(self.slot_dir, self.engine)
            for path, meta in stale:
                with contextlib.suppress(OSError):
                    path.unlink()
                if meta.get("job_id") and meta.get("job_id") != job_id:
                    self.reclaimed.append(meta)
            if self.limit and len(live) >= self.limit:
                raise EngineBusy(self.engine, self.limit, live)
            path = self.slot_dir / f"{self.engine}-{job_id}.slot"
            payload = {
                "pid": os.getpid(),
                "job_id": job_id,
                "engine": self.engine,
                "db_path": str(self.db_path) if self.db_path is not None else None,
                "started_at": time.time(),
            }
            tmp = self.slot_dir / f".tmp-{os.getpid()}-{uuid.uuid4().hex}"
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, path)
            self.path = path

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
        """Remove this job's slot file, if it is still this process's."""
        if self.path is None:
            return
        with _guard(self.slot_dir):
            meta = _read(self.path)
            if meta is not None and meta.get("pid") == os.getpid():
                with contextlib.suppress(OSError):
                    self.path.unlink()
        self.path = None


__all__ = [
    "CONFIG_ENV",
    "DEFAULT_LIMITS",
    "ConcurrencyConfig",
    "EngineBusy",
    "EngineSlot",
    "busy_reason",
    "config_path",
    "default_slot_dir",
    "engine_at_limit",
    "live_holders",
    "load_config",
    "load_limits",
]
