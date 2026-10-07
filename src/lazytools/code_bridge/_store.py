"""Durable storage layout for the async code bridge.

One ``lazybridge.Store`` backs the job registry, the approval queue, and
this bridge's own per-job metadata -- one file, one lock discipline,
instead of three. Default location: ``~/.lazytools/code-bridge.sqlite``,
overridable with ``LAZYTOOLS_CODE_BRIDGE_DB`` or the CLI's ``--db``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

JOB_PREFIX = "code-bridge:job:"
META_PREFIX = "code-bridge:meta:"
APPROVAL_PREFIX = "code-bridge:approval:"

DB_ENV_VAR = "LAZYTOOLS_CODE_BRIDGE_DB"
DEFAULT_DB_PATH = Path.home() / ".lazytools" / "code-bridge.sqlite"


def default_db_path() -> Path:
    override = os.environ.get(DB_ENV_VAR)
    return Path(override).expanduser() if override else DEFAULT_DB_PATH


def build_store(db_path: Path | None = None) -> Any:
    from lazybridge import Store

    path = db_path or default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return Store(db=str(path))


def build_job_registry(store: Any) -> Any:
    from lazybridge.ext.delegation import JobRegistry

    return JobRegistry(store, prefix=JOB_PREFIX)


def build_approval_queue(store: Any) -> Any:
    from lazybridge.ext.approval import ApprovalQueue

    return ApprovalQueue(store, prefix=APPROVAL_PREFIX)


def results_dir(db_path: Path | None = None) -> Path:
    path = (db_path or default_db_path()).parent / "results"
    path.mkdir(parents=True, exist_ok=True)
    return path


def locks_dir(db_path: Path | None = None) -> Path:
    return (db_path or default_db_path()).parent / "locks"


def write_meta(store: Any, job_id: str, meta: dict[str, Any]) -> None:
    """This bridge's own per-job fields (cwd, engine, session_name, ...).

    Kept in a SEPARATE Store prefix from ``JobRegistry.write()``'s own
    record, which only knows a fixed set of columns -- this way reusing
    ``JobRegistry`` (as the task asks) never requires widening its schema
    for fields that belong to this bridge, not to every delegation caller.
    """
    store.write(f"{META_PREFIX}{job_id}", meta)


def read_meta(store: Any, job_id: str) -> dict[str, Any] | None:
    raw = store.read(f"{META_PREFIX}{job_id}")
    return raw if isinstance(raw, dict) else None


def all_job_records(store: Any) -> list[dict[str, Any]]:
    return [raw for _key, raw in store.items(prefix=JOB_PREFIX) if isinstance(raw, dict)]


__all__ = [
    "APPROVAL_PREFIX",
    "DB_ENV_VAR",
    "DEFAULT_DB_PATH",
    "JOB_PREFIX",
    "META_PREFIX",
    "all_job_records",
    "build_approval_queue",
    "build_job_registry",
    "build_store",
    "default_db_path",
    "locks_dir",
    "read_meta",
    "results_dir",
    "write_meta",
]
