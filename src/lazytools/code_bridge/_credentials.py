"""Operator-only storage for bridge approval credentials."""

from __future__ import annotations

import csv
import hashlib
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

from lazybridge.ext.approval import ApprovalQueue

from lazytools.code_bridge._concurrency import _guard

CREDENTIAL_ENV = "LAZYTOOLS_CODE_BRIDGE_APPROVER_CREDENTIAL"
_INIT = "lazytools-code-bridge approver-credential init"
_environment_lock = Lock()
_environment_users = 0
_saved_environment: dict[str, str] = {}


def job_environment(env: Mapping[str, str]) -> dict[str, str]:
    return {name: value for name, value in env.items() if name.upper() != CREDENTIAL_ENV}


@contextmanager
def scrub_job_environment() -> Iterator[None]:
    # Native engines inherit the environment of the one-shot bridge process.
    # A library caller may also run multiple jobs in the same process.
    global _environment_users
    with _environment_lock:
        for name in tuple(os.environ):
            if name.upper() == CREDENTIAL_ENV:
                _saved_environment.setdefault(name, os.environ.pop(name))
        _environment_users += 1
    try:
        yield
    finally:
        with _environment_lock:
            _environment_users -= 1
            if _environment_users == 0:
                os.environ.update(_saved_environment)
                _saved_environment.clear()


def credential_directory() -> Path:
    directory = (Path.home() / ".lazytools" / "approver-credentials").resolve()
    if any((parent / ".git").exists() for parent in (directory, *directory.parents)):
        raise ValueError("The approver credential directory must be outside Git checkouts")
    return directory


def _path(registry: Path) -> Path:
    key = hashlib.sha256(os.path.normcase(str(registry.resolve())).encode()).hexdigest()
    return credential_directory() / f"{key}.credential"


def _restrict_windows_file(path: Path) -> None:
    system = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32"
    user = subprocess.run(
        [str(system / "whoami.exe"), "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True,
    )
    _, sid = next(csv.reader(user.stdout.strip().splitlines()))
    subprocess.run(
        [str(system / "icacls.exe"), str(path), "/inheritance:r", "/grant:r", f"*{sid}:(F)"],
        capture_output=True, check=True,
    )


def initialize(registry: Path, queue: ApprovalQueue, *, replace: bool = False) -> bool:
    """Publish a private credential file atomically, once per registry."""
    path = _path(registry)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _guard(path.parent):
        if (path.exists() or path.is_symlink()) and not replace:
            try:
                credential = load(registry)
            except (OSError, UnicodeError, ValueError) as exc:
                raise ValueError(
                    f"Existing owner approver credential is invalid: {exc}; use init --replace"
                ) from exc
            approver = queue._authenticated_approver(credential)
            if approver is None or approver.actor != "owner" or "owner" not in approver.roles:
                raise ValueError("Existing owner approver credential is invalid for this registry; use init --replace")
            return False
        fd, name = tempfile.mkstemp(dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as file:
                if os.name == "nt":
                    _restrict_windows_file(temporary)
                file.write(queue.issue_approver_credential("owner", ("owner",)))
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return True


def load(registry: Path) -> str:
    path = _path(registry)
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise PermissionError("The owner approver credential file must be a regular file")
        if sys.platform != "win32" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise PermissionError("The owner approver credential file must be private to the current user")
        return path.read_text(encoding="ascii")
    except FileNotFoundError:
        raise ValueError(f"Owner approver credential is missing. Run `{_INIT}` for this registry.") from None
