"""One job per ``cwd`` at a time -- a reclaimable file lock, Windows-safe.

Not a ``Store`` CAS: the thing this lock protects (a real subprocess running
a coding engine against a real working directory) lives in THIS process, not
in the Store, so the natural liveness check is "is the pid that wrote this
lock still alive" rather than a lease someone has to keep refreshing. A lock
file next to the lock directory, keyed by a hash of the resolved ``cwd``, is
the Windows-safe primitive LazyTools already recommends over a POSIX
``flock``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class LockHeld(RuntimeError):
    """Another live process already holds the lock for this ``cwd``."""

    def __init__(self, cwd: Path, pid: int, job_id: str) -> None:
        self.cwd = cwd
        self.pid = pid
        self.job_id = job_id
        super().__init__(
            f"a job is already running in {cwd} (job {job_id}, pid {pid}). "
            "Only one `run` at a time per repository."
        )


def _windows_kernel32() -> Any:
    """``kernel32`` with explicit, pointer-correct signatures, built once.

    Without declared ``argtypes``/``restype``, ctypes guesses a plain 32-bit
    ``c_int`` for ``OpenProcess``'s ``HANDLE`` return value -- on 64-bit
    Windows a handle needing the upper bits truncates, so a live owner's
    handle can come back looking like a small/garbage value and
    ``GetExitCodeProcess`` then fails on it, making ``_pid_alive`` report a
    LIVE owner as dead and let a second job reclaim its lock. ``wintypes.
    HANDLE`` is pointer-sized; declaring it here, once, fixes every call
    through this object. Found by Codex review.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, wintypes.LPDWORD)
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


_KERNEL32 = _windows_kernel32() if os.name == "nt" else None


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check, Windows and POSIX.

    Windows has no signal-0 equivalent to ``os.kill(pid, 0)`` -- opening the
    process handle (without actually touching it) is the standard
    substitute, BUT a successful ``OpenProcess`` alone is not enough: the
    pid cannot be reused while any handle to it is still open anywhere on
    the system (e.g. a launcher/watchdog that holds one for its own
    bookkeeping), so a process object can stay opens-able well after the
    process itself has exited. ``GetExitCodeProcess`` is what actually
    distinguishes "still running" (``STILL_ACTIVE``) from "exited, handle
    just hasn't been released yet". Found by Codex review.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = _KERNEL32
        assert kernel32 is not None  # built at import time, guarded by the same os.name == "nt" check
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD(0)
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False  # couldn't determine -- treat as not reliably alive
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    return True


def _lock_key(cwd: Path) -> str:
    return hashlib.sha256(str(cwd).encode()).hexdigest()[:16]


@dataclass
class JobLock:
    """A reclaimable, per-``cwd`` exclusive lock.

    Known accepted race: the "does the lock file already exist" check and a
    stale reclaim's replace are two separate steps, not one atomic
    operation -- two processes racing to acquire the SAME stale lock at the
    SAME instant could both decide to reclaim it. Given this bridge's actual
    use (a human-driven Claude Code session launching at most a handful of
    `run`s a day per repository), this is a theoretical gap, not a practical
    one; a stricter fix would need a real cross-process mutex (e.g. a
    Windows named mutex), which is more than this single-user tool needs.
    """

    lock_dir: Path
    cwd: Path
    _path: Path | None = None

    @property
    def path(self) -> Path:
        if self._path is None:
            self._path = self.lock_dir / f"{_lock_key(self.cwd)}.lock"
        return self._path

    def acquire(self, job_id: str) -> str | None:
        """Raise :class:`LockHeld` if a live process holds this lock; else take it.

        Returns the job id of a STALE lock this call just reclaimed (so the
        caller can mark that old job record ``interrupted`` instead of
        leaving it stuck at ``running``/``awaiting_approval`` forever), or
        ``None`` for an uncontended acquire.
        """
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"pid": os.getpid(), "job_id": job_id, "cwd": str(self.cwd), "started_at": time.time()}
        ).encode()
        # Written COMPLETE to a private temp file first, then linked/renamed
        # into place -- never created empty at `self.path` and filled in
        # after. The previous version did `os.open(path, O_CREAT|O_EXCL)`
        # then a separate `os.write`: a second process racing `acquire()` in
        # that window could read the file back empty, find no live pid in
        # it, and "reclaim" a lock that was never actually abandoned. Found
        # by Codex review.
        tmp = self.lock_dir / f".tmp-{os.getpid()}-{uuid.uuid4().hex}"
        tmp.write_bytes(payload)
        try:
            # os.link: atomic "create this path if, and only if, it doesn't
            # already exist" -- same existence-check semantics as the old
            # O_EXCL open, but the content linked in is already complete
            # (the SAME inode `tmp` just wrote), so there is no window where
            # `self.path` exists but is still empty.
            try:
                os.link(str(tmp), str(self.path))
            except FileExistsError:
                pass
            else:
                return None  # fresh, uncontended acquire

            existing: dict = {}
            with contextlib.suppress(OSError, ValueError):
                existing = json.loads(self.path.read_text())
            pid = existing.get("pid")
            if isinstance(pid, int) and _pid_alive(pid):
                raise LockHeld(self.cwd, pid, str(existing.get("job_id", "?")))

            # Stale: the pid that wrote it is gone. Reclaim via an atomic
            # rename of the SAME already-fully-written tmp file -- not a
            # second create, so there is still no empty-content window.
            os.replace(str(tmp), str(self.path))
            stale_job_id = existing.get("job_id")
            return str(stale_job_id) if stale_job_id and stale_job_id != job_id else None
        finally:
            # Harmless if `tmp` was already consumed by `os.replace` above
            # (unlink on a gone path is just suppressed), and necessary if
            # the fast path linked it (two directory entries, same inode --
            # only `self.path` should remain) or if a live-owner check
            # raised `LockHeld` (the orphaned tmp must not leak).
            with contextlib.suppress(OSError):
                tmp.unlink()

    def release(self) -> None:
        """Remove the lock, but only if THIS process still owns it."""
        current: dict = {}
        with contextlib.suppress(OSError, ValueError):
            current = json.loads(self.path.read_text())
        if current.get("pid") == os.getpid():
            with contextlib.suppress(OSError):
                self.path.unlink()


__all__ = ["JobLock", "LockHeld"]
