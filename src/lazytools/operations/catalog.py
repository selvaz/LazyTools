"""SQLite-backed task/run/artifact catalog.

This module deliberately uses only the Python standard library. It is safe for
several scheduled processes on one workstation: SQLite WAL mode is enabled,
transactions are short, and writes wait briefly for another writer.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_name TEXT PRIMARY KEY,
    description TEXT,
    schedule TEXT,
    owner_repo TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    task_name TEXT NOT NULL REFERENCES tasks(task_name),
    status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','skipped')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    parameters_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    source_repo TEXT,
    source_db TEXT,
    parent_run_id TEXT REFERENCES runs(run_id),
    error TEXT,
    UNIQUE(task_name, run_id)
);
CREATE INDEX IF NOT EXISTS idx_runs_task_started ON runs(task_name, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    storage_path TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(sha256, name, kind)
);
CREATE INDEX IF NOT EXISTS idx_artifacts_kind_created ON artifacts(kind, created_at DESC);
CREATE TABLE IF NOT EXISTS run_artifacts (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
    role TEXT,
    PRIMARY KEY(run_id, artifact_id, role)
);
CREATE TABLE IF NOT EXISTS models (
    model_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    model_name TEXT NOT NULL,
    version TEXT,
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
    report_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    title TEXT NOT NULL,
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS portfolio_nodes (
    record_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    node_key TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    node_type TEXT NOT NULL DEFAULT 'portfolio',
    config_artifact_id TEXT REFERENCES artifacts(artifact_id),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_portfolio_nodes_run ON portfolio_nodes(run_id);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _json(value: dict[str, Any] | None) -> str:
    return json.dumps(value or {}, sort_keys=True, default=str)


def _safe_name(name: str) -> str:
    value = Path(str(name)).name
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".- ")
    return value or "artifact"


def default_db_path() -> Path:
    return Path(os.environ.get("LAZYTOOLS_OPERATIONS_DB", "~/.lazytools/operations.sqlite")).expanduser()


def default_artifact_dir() -> Path:
    return Path(os.environ.get("LAZYTOOLS_ARTIFACTS_DIR", "~/.lazytools/artifacts")).expanduser()


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    task_name: str
    status: str
    started_at: str
    finished_at: str | None
    parameters: dict[str, Any]
    metadata: dict[str, Any]
    source_repo: str | None
    source_db: str | None
    error: str | None


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    run_id: str
    name: str
    kind: str
    mime_type: str
    size_bytes: int
    storage_path: str
    sha256: str
    role: str | None


class OperationsCatalog:
    """Central catalog for scheduled task executions and their outputs."""

    def __init__(self, db_path: str | os.PathLike[str] | None = None,
                 artifact_dir: str | os.PathLike[str] | None = None) -> None:
        self.db_path = Path(db_path).expanduser() if db_path else default_db_path()
        self.artifact_dir = Path(artifact_dir).expanduser() if artifact_dir else default_artifact_dir()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=30000")
        return con

    def initialize(self) -> None:
        """Create or upgrade the catalog schema idempotently."""
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(SCHEMA)

    def start_run(self, task_name: str, *, parameters: dict[str, Any] | None = None,
                  metadata: dict[str, Any] | None = None, source_repo: str | None = None,
                  source_db: str | None = None, parent_run_id: str | None = None,
                  description: str | None = None, schedule: str | None = None) -> str:
        """Register a task and return its new run ID."""
        self.initialize()
        run_id = f"run_{uuid.uuid4().hex}"
        now = _now()
        with self._connect() as con:
            con.execute(
                """INSERT INTO tasks(task_name, description, schedule, owner_repo, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(task_name) DO UPDATE SET
                     description=coalesce(excluded.description, tasks.description),
                     schedule=coalesce(excluded.schedule, tasks.schedule),
                     updated_at=excluded.updated_at""",
                (task_name, description, schedule, source_repo, now, now),
            )
            con.execute(
                """INSERT INTO runs(run_id, task_name, status, started_at, parameters_json,
                   metadata_json, source_repo, source_db, parent_run_id)
                   VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?)""",
                (run_id, task_name, now, _json(parameters), _json(metadata), source_repo, source_db, parent_run_id),
            )
        return run_id

    def finish_run(self, run_id: str, status: str = "succeeded", *, error: str | None = None) -> None:
        if status not in {"succeeded", "failed", "skipped"}:
            raise ValueError("status must be succeeded, failed, or skipped")
        with self._connect() as con:
            updated = con.execute(
                "UPDATE runs SET status=?, finished_at=?, error=? WHERE run_id=? AND status='running'",
                (status, _now(), error, run_id),
            ).rowcount
        if not updated:
            raise KeyError(f"unknown or already finished run: {run_id}")

    def fail_run(self, run_id: str, error: str) -> None:
        self.finish_run(run_id, "failed", error=error)

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        return RunRecord(row["run_id"], row["task_name"], row["status"], row["started_at"], row["finished_at"],
                         json.loads(row["parameters_json"]), json.loads(row["metadata_json"]),
                         row["source_repo"], row["source_db"], row["error"])

    def list_runs(self, *, task_name: str | None = None, limit: int = 50) -> list[RunRecord]:
        query = "SELECT * FROM runs"
        params: list[Any] = []
        if task_name:
            query += " WHERE task_name=?"
            params.append(task_name)
        query += " ORDER BY started_at DESC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        with self._connect() as con:
            rows = con.execute(query, params).fetchall()
        records: list[RunRecord] = []
        for row in rows:
            record = self.get_run(row["run_id"])
            if record is not None:
                records.append(record)
        return records

    def register_bytes(self, run_id: str, name: str, content: bytes, *, kind: str = "artifact",
                       mime_type: str | None = None, role: str | None = None,
                       metadata: dict[str, Any] | None = None) -> ArtifactRecord:
        """Persist bytes once by SHA-256 and attach them to a run."""
        self.initialize()
        safe_name = _safe_name(name)
        digest = hashlib.sha256(content).hexdigest()
        suffix = Path(safe_name).suffix
        target = self.artifact_dir / digest[:2] / digest[2:4] / f"{digest}{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_bytes(content)
            temporary.replace(target)
        artifact_id = f"artifact_{uuid.uuid4().hex}"
        now = _now()
        with self._connect() as con:
            # INSERT OR IGNORE makes the content-addressed identity safe when
            # two scheduled processes publish the same payload concurrently.
            con.execute(
                """INSERT OR IGNORE INTO artifacts(artifact_id, sha256, name, kind, mime_type, size_bytes,
                   storage_path, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (artifact_id, digest, safe_name, kind,
                 mime_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream",
                 len(content), str(target), _json(metadata), now),
            )
            existing = con.execute(
                "SELECT artifact_id FROM artifacts WHERE sha256=? AND name=? AND kind=?",
                (digest, safe_name, kind),
            ).fetchone()
            if existing:
                artifact_id = existing["artifact_id"]
            con.execute(
                "INSERT OR IGNORE INTO run_artifacts(run_id, artifact_id, role) VALUES (?, ?, ?)",
                (run_id, artifact_id, role),
            )
            row = con.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        return ArtifactRecord(artifact_id, run_id, row["name"], row["kind"], row["mime_type"],
                              row["size_bytes"], row["storage_path"], row["sha256"], role)

    def register_text(self, run_id: str, name: str, content: str, **kwargs: Any) -> ArtifactRecord:
        return self.register_bytes(run_id, name, content.encode("utf-8"), **kwargs)

    def register_json(self, run_id: str, name: str, value: Any, **kwargs: Any) -> ArtifactRecord:
        return self.register_text(run_id, name, json.dumps(value, indent=2, sort_keys=True, default=str),
                                   mime_type="application/json", **kwargs)

    def register_file(self, run_id: str, path: str | os.PathLike[str], **kwargs: Any) -> ArtifactRecord:
        source = Path(path)
        return self.register_bytes(run_id, source.name, source.read_bytes(), **kwargs)

    def register_report(self, run_id: str, title: str, content: str, *, name: str = "report.md",
                        mime_type: str = "text/markdown") -> ArtifactRecord:
        artifact = self.register_text(run_id, name, content, kind="report", mime_type=mime_type, role="report")
        with self._connect() as con:
            con.execute("INSERT INTO reports(report_id, run_id, title, artifact_id, created_at) VALUES (?, ?, ?, ?, ?)",
                         (f"report_{uuid.uuid4().hex}", run_id, title, artifact.artifact_id, _now()))
        return artifact

    def register_model(self, run_id: str, model_name: str, content: bytes, *, version: str | None = None,
                       name: str = "model.bin", mime_type: str = "application/octet-stream",
                       metadata: dict[str, Any] | None = None) -> ArtifactRecord:
        artifact = self.register_bytes(run_id, name, content, kind="model", mime_type=mime_type,
                                       role="model", metadata=metadata)
        with self._connect() as con:
            con.execute("""INSERT INTO models(model_id, run_id, model_name, version, artifact_id,
                         metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                         (f"model_{uuid.uuid4().hex}", run_id, model_name, version, artifact.artifact_id,
                          _json(metadata), _now()))
        return artifact

    def register_node(self, run_id: str, node_key: str, *, name: str | None = None,
                      description: str | None = None, node_type: str = "portfolio",
                      config: dict[str, Any] | None = None,
                      metadata: dict[str, Any] | None = None) -> str:
        """Register a constructed portfolio/free node and its full config."""
        config_artifact = None
        if config is not None:
            config_artifact = self.register_json(
                run_id, f"node-{_safe_name(node_key)}.json", config,
                kind="node_config", role="node-config", metadata=metadata,
            )
        record_id = f"node_{uuid.uuid4().hex}"
        with self._connect() as con:
            con.execute(
                """INSERT INTO portfolio_nodes(record_id, run_id, node_key, name, description,
                   node_type, config_artifact_id, metadata_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (record_id, run_id, node_key, name or node_key, description, node_type,
                 config_artifact.artifact_id if config_artifact else None, _json(metadata), _now()),
            )
        return record_id

    def artifacts_for_run(self, run_id: str) -> list[ArtifactRecord]:
        with self._connect() as con:
            rows = con.execute("""SELECT a.*, ra.role FROM artifacts a JOIN run_artifacts ra
                         ON ra.artifact_id=a.artifact_id WHERE ra.run_id=? ORDER BY a.created_at""", (run_id,)).fetchall()
        return [ArtifactRecord(r["artifact_id"], run_id, r["name"], r["kind"], r["mime_type"],
                               r["size_bytes"], r["storage_path"], r["sha256"], r["role"]) for r in rows]
