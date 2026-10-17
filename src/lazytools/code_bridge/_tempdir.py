"""A dedicated, per-job temp directory -- the ONLY extra writable root a job gets.

``<os temp>/lazytools-bridge/<job_id>``, created at job start and removed
(best effort) at job end. The Codex sandbox gets it as its one extra
writable root, Claude's file tools get it as a second file root, and the
engine subprocess sees ``TEMP``/``TMP``/``TMPDIR`` pointing at it, so pytest's
``tmp_path`` and ``tempfile`` land there without escalating.

The OS temp base comes from the environment, and an environment can say
anything: ``TMP=C:\\`` once turned the whole drive into a writable root
(found by review). So the base is checked first and the job gets NO extra
root -- fail closed -- if any configured temp location is:

* a drive / filesystem root, or a UNC share root;
* the Windows directory or ``ProgramData`` (or anything inside them);
* the user profile root itself;
* an ancestor of (or equal to) the job's cwd or the bridge's DB file;
* fewer than 3 path components (``C:\\Temp``, ``/tmp``).

On Linux/macOS ``/tmp`` is therefore refused; Codex's own workspace-write
sandbox already allows ``/tmp`` and ``$TMPDIR`` there, so nothing is lost.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

log = logging.getLogger(__name__)

TEMP_ENV_VARS = ("TMPDIR", "TEMP", "TMP")
SUBDIR = "lazytools-bridge"


def _resolve(raw: str | os.PathLike[str]) -> Path:
    return Path(raw).expanduser().resolve()


def _inside(path: Path, parent: Path) -> bool:
    """``path`` equals or is inside ``parent``, case-insensitively on Windows."""
    a, b = os.path.normcase(str(path)), os.path.normcase(str(parent))
    return a == b or a.startswith(b.rstrip("\\/") + os.sep)


def unsafe_reason(base: Path, *, cwd: Path, db_path: Path) -> str | None:
    """Why ``base`` must not host job temp dirs, or ``None`` if it is acceptable."""
    if base.parent == base or base == Path(base.anchor):
        return f"{base} is a drive, filesystem or share root"
    if len(base.parts) < 3:
        return f"{base} has fewer than 3 path components"
    for env_name in ("SystemRoot", "WINDIR", "ProgramData", "ALLUSERSPROFILE"):
        value = os.environ.get(env_name)
        if value and _inside(base, _resolve(value)):
            return f"{base} is inside %{env_name}% ({value})"
    with contextlib.suppress(RuntimeError, OSError):
        if os.path.normcase(str(base)) == os.path.normcase(str(Path.home().resolve())):
            return f"{base} is the user profile root"
    for label, other in (("job cwd", cwd), ("bridge DB", db_path)):
        if _inside(other.resolve(), base):
            return f"{base} is an ancestor of the {label} ({other})"
    return None


def os_temp_bases() -> list[Path]:
    """Every temp location the environment names, plus Python's own default."""
    raw = [os.environ.get(name) for name in TEMP_ENV_VARS]
    bases: list[Path] = []
    for value in [*raw, tempfile.gettempdir()]:
        if not value:
            continue
        with contextlib.suppress(OSError):
            path = _resolve(value)
            if path not in bases:
                bases.append(path)
    return bases


def create_job_temp_dir(job_id: str, *, cwd: Path, db_path: Path) -> tuple[Path | None, str | None]:
    """``(<base>/lazytools-bridge/<job_id>, None)``, or ``(None, why)`` when any base is unsafe.

    EVERY configured temp variable is checked, not only the one Python would
    pick: the engine subprocess and the tools it runs may read a different
    one, and an unsafe value anywhere means the environment is not one to
    widen a sandbox in.
    """
    bases = os_temp_bases()
    if not bases:
        return None, "no OS temp directory could be determined"
    for base in bases:
        reason = unsafe_reason(base, cwd=cwd, db_path=db_path)
        if reason is not None:
            log.warning("code bridge: no per-job temp dir for job %s: %s", job_id, reason)
            return None, reason
    path = bases[0] / SUBDIR / job_id
    try:
        path.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        reason = f"could not create {path}: {exc}"
        log.warning("code bridge: no per-job temp dir for job %s: %s", job_id, reason)
        return None, reason
    return path, None


def remove_job_temp_dir(path: Path | None) -> None:
    """Best effort: a cleanup failure never fails the job."""
    if path is None:
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception as exc:  # cleanup must never turn a finished job into a failure
        log.warning("code bridge: could not remove job temp dir %s: %s", path, exc)


@contextlib.contextmanager
def temp_env(path: Path | None) -> Iterator[None]:
    """Point ``TEMP``/``TMP``/``TMPDIR`` at ``path`` for this process (and so its subprocesses).

    Neither LazyBridge engine takes a subprocess ``env`` argument; both inherit
    this process's environment, and a bridge job runs in a process of its
    own (``run`` / ``run --detach``), so scoping the variables to the job's
    lifetime here IS the engine subprocess environment. Restored on exit.
    """
    if path is None:
        yield
        return
    saved = {name: os.environ.get(name) for name in TEMP_ENV_VARS}
    try:
        for name in TEMP_ENV_VARS:
            os.environ[name] = str(path)
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


__all__ = [
    "create_job_temp_dir",
    "os_temp_bases",
    "remove_job_temp_dir",
    "temp_env",
    "unsafe_reason",
]
