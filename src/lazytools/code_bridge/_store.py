"""Durable storage layout for the async code bridge.

One ``lazybridge.Store`` backs the job registry, the approval queue, and
this bridge's own per-job metadata -- one file, one lock discipline,
instead of three. Default location: ``~/.lazytools/code-bridge.sqlite``,
overridable with ``LAZYTOOLS_CODE_BRIDGE_DB`` or the CLI's ``--db``.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any

JOB_PREFIX = "code-bridge:job:"
META_PREFIX = "code-bridge:meta:"
APPROVAL_PREFIX = "code-bridge:approval:"

DB_ENV_VAR = "LAZYTOOLS_CODE_BRIDGE_DB"
DEFAULT_DB_PATH = Path.home() / ".lazytools" / "code-bridge.sqlite"
TERMINAL_STATUSES = {"done", "partial", "failed", "interrupted"}


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
    key = f"{META_PREFIX}{job_id}"
    for _ in range(20):
        raw = store.read(key)
        updated = dict(meta)
        # Callers commonly read, modify, then write metadata.  An intervention
        # can be appended between those operations, so always retain entries
        # that are present in the latest stored value but absent from the
        # caller's snapshot.
        stored = raw.get("manual_interventions", []) if isinstance(raw, dict) else []
        supplied = updated.get("manual_interventions", [])
        if stored or supplied:
            updated["manual_interventions"] = [
                *stored,
                *(item for item in supplied if item not in stored),
            ]
        if store.compare_and_swap(key, raw, updated):
            return
    raise RuntimeError(f"could not write metadata for job {job_id}: concurrent updates did not settle")


def read_meta(store: Any, job_id: str) -> dict[str, Any] | None:
    raw = store.read(f"{META_PREFIX}{job_id}")
    return raw if isinstance(raw, dict) else None


def mark_completion(meta: dict[str, Any], status: str, *, now: float | None = None) -> None:
    """Set the immutable terminal timestamp on the first terminal transition."""
    if status in TERMINAL_STATUSES:
        meta.setdefault("completed_at", time.time() if now is None else now)


def record_intervention(store: Any, job_id: str, kind: str, reason: str | None = None) -> None:
    """Append one operator intervention without losing concurrent metadata writes."""
    key = f"{META_PREFIX}{job_id}"
    for _ in range(20):
        raw = store.read(key)
        current = raw if isinstance(raw, dict) else {"job_id": job_id}
        updated = dict(current)
        interventions = list(updated.get("manual_interventions") or [])
        intervention = {"kind": kind, "at": time.time()}
        if reason:
            intervention["reason"] = reason
        interventions.append(intervention)
        updated["manual_interventions"] = interventions
        if store.compare_and_swap(key, raw, updated):
            return
    raise RuntimeError(f"could not record intervention for job {job_id}: concurrent updates did not settle")


def brief_hash(brief: str) -> str:
    return hashlib.sha256(brief.encode("utf-8")).hexdigest()


def record_relaunch(store: Any, job_id: str, brief: str, repo: str, *, now: float | None = None) -> str | None:
    """Mark the newest matching recent failed job as the source of a relaunch."""
    now = time.time() if now is None else now
    digest = brief_hash(brief)
    candidates = []
    for record in all_job_records(store):
        old_id = str(record.get("job_id") or "")
        if not old_id or old_id == job_id or record.get("status") not in {"failed", "interrupted"}:
            continue
        meta = read_meta(store, old_id) or {}
        completed = meta.get("completed_at") or meta.get("updated_at")
        old_repo = meta.get("repo") or meta.get("repo_root") or meta.get("cwd")
        old_digest = meta.get("brief_hash") or brief_hash(str(record.get("objective") or meta.get("objective") or ""))
        if isinstance(completed, (int, float)) and now - 86400 <= completed <= now and old_repo == repo and old_digest == digest:
            candidates.append((completed, old_id))
    if not candidates:
        return None
    original = max(candidates)[1]
    record_intervention(store, original, "relaunch")
    return original


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
    "brief_hash",
    "default_db_path",
    "locks_dir",
    "mark_completion",
    "read_meta",
    "record_relaunch",
    "record_intervention",
    "results_dir",
    "write_meta",
]
