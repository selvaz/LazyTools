"""SQLite persistence for the IC Report Registry. Stdlib ``sqlite3`` only,
matching :mod:`lazytools.registry.artifacts`' own conventions.

Four tables, deliberately not more:

* ``reports`` -- the logical identity of an ongoing report series (one row
  per (report_type, scope), e.g. "the Europe regional report"), pointing at
  its current version.
* ``report_versions`` -- one row per generation. ``content_json`` is the
  full validated :class:`~lazytools.ic_reports.models.ReportEnvelope`
  (``model_dump_json()``) -- the single source of truth for a version's
  content. Nothing here re-normalizes claims/evidence into their own tables
  (see the module docstring in ``models.py`` for why); only the fields
  genuinely queried *across* reports get their own table.
* ``report_inputs`` -- denormalized from ``content_json.input_refs`` purely
  so "what reports depend on artifact X" can be a plain SQL query instead of
  a full-table JSON scan.
* ``report_changes`` -- denormalized from ``content_json.changes`` for the
  same reason ("what changed between these two versions" without
  re-parsing every version's JSON).

Idempotency: a ``(report_id, run_id)`` pair is unique -- re-running the same
generation returns the EXISTING version_id rather than creating a duplicate.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import urllib.request
import uuid
from datetime import UTC, datetime

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    report_id TEXT PRIMARY KEY,
    report_type TEXT NOT NULL,
    scope_type TEXT,
    scope_key TEXT,
    title TEXT NOT NULL,
    current_version_id TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- IFNULL(...,'') normalizes NULL scope_type/scope_key for uniqueness purposes
-- only (the stored values stay NULL) -- SQLite treats every NULL as distinct
-- in a plain UNIQUE constraint, which would let concurrent get_or_create_report
-- calls for the SAME unscoped report_type each insert their own row.
CREATE UNIQUE INDEX IF NOT EXISTS idx_reports_identity
    ON reports(report_type, IFNULL(scope_type, ''), IFNULL(scope_key, ''));

CREATE TABLE IF NOT EXISTS report_versions (
    version_id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL REFERENCES reports(report_id),
    version_number INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    as_of TEXT NOT NULL,
    content_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    model TEXT,
    prompt_version TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(report_id, run_id),
    UNIQUE(report_id, version_number)
);
CREATE INDEX IF NOT EXISTS idx_report_versions_report ON report_versions(report_id);
CREATE INDEX IF NOT EXISTS idx_report_versions_status ON report_versions(status);
CREATE INDEX IF NOT EXISTS idx_report_versions_as_of ON report_versions(as_of);

CREATE TABLE IF NOT EXISTS report_inputs (
    version_id TEXT NOT NULL REFERENCES report_versions(version_id),
    input_type TEXT NOT NULL,
    input_id TEXT NOT NULL,
    source_repo TEXT,
    role TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_report_inputs_version ON report_inputs(version_id);
CREATE INDEX IF NOT EXISTS idx_report_inputs_input ON report_inputs(input_type, input_id);

CREATE TABLE IF NOT EXISTS report_changes (
    current_version_id TEXT NOT NULL REFERENCES report_versions(version_id),
    previous_version_id TEXT REFERENCES report_versions(version_id),
    change_type TEXT NOT NULL,
    description TEXT NOT NULL,
    drivers TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_report_changes_current ON report_changes(current_version_id);

CREATE TABLE IF NOT EXISTS report_artifacts (
    version_id TEXT NOT NULL REFERENCES report_versions(version_id),
    artifact_id TEXT NOT NULL,
    repo TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'primary'
);
CREATE INDEX IF NOT EXISTS idx_report_artifacts_version ON report_artifacts(version_id);
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _connect_write(db_path: str) -> sqlite3.Connection:
    """Open (creating the file/schema if needed) for a mutating call."""
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def _connect_read(db_path: str) -> sqlite3.Connection | None:
    """Open ``db_path`` read-only for a query, without ever creating the file
    or its schema -- a search/get against a report registry that hasn't been
    written to yet (or a deployment mounting the filesystem read-only) must
    return empty, not silently create a database file. Matches
    ``lazytools.registry.artifacts``' own ``_connect_read``.
    """
    if not os.path.exists(db_path):
        return None
    uri = "file:" + urllib.request.pathname2url(os.path.abspath(db_path)) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reports'"
    ).fetchone()
    if has_table is None:
        conn.close()
        return None
    return conn


def _new_id() -> str:
    return str(uuid.uuid4())


def get_or_create_report(db_path: str, *, report_type: str, scope_type: str | None, scope_key: str | None, title: str) -> str:
    """The logical report identity for (report_type, scope) -- idempotent:
    a second call with the same (report_type, scope_type, scope_key) returns
    the SAME report_id rather than creating a second series, even under
    concurrent callers (the insert-then-select happens as one atomic
    transaction against ``idx_reports_identity``, so two callers racing on a
    previously-unseen scope can't each create a separate row). ``title`` is
    only used on first creation; call sites that want to rename an existing
    series should update it explicitly, not rely on this to do it silently.
    """
    report_id = _new_id()
    now = _now_iso()
    with _connect_write(db_path) as conn:
        conn.execute(
            """
            INSERT INTO reports (report_id, report_type, scope_type, scope_key, title, current_version_id, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)
            ON CONFLICT (report_type, IFNULL(scope_type, ''), IFNULL(scope_key, '')) DO NOTHING
            """,
            (report_id, report_type, scope_type, scope_key, title, "draft", now, now),
        )
        row = conn.execute(
            "SELECT report_id FROM reports WHERE report_type = ? AND scope_type IS ? AND scope_key IS ?",
            (report_type, scope_type, scope_key),
        ).fetchone()
    return str(row["report_id"])


def create_version(
    db_path: str,
    *,
    report_id: str,
    run_id: str,
    as_of: str,
    content_json: str,
    agent_id: str,
    model: str | None,
    prompt_version: str | None,
    input_refs: list[dict],
) -> tuple[str, bool]:
    """Create a new version of ``report_id``. Idempotent on ``(report_id,
    run_id)``: a repeated run with the same run_id returns the EXISTING
    version_id (and does NOT touch its content) rather than creating a
    duplicate -- callers that need to force a genuinely new attempt must
    pass a new run_id.

    Returns ``(version_id, created)`` -- ``created`` is ``False`` when an
    existing version was returned, so callers know whether to also (re-)run
    any denormalized inserts keyed off this version (changes, artifact
    links) or skip them since they'd already be in place.
    """
    with _connect_write(db_path) as conn:
        existing = conn.execute(
            "SELECT version_id FROM report_versions WHERE report_id = ? AND run_id = ?",
            (report_id, run_id),
        ).fetchone()
        if existing is not None:
            return str(existing["version_id"]), False

        version_id = _new_id()
        content_hash = hashlib.sha256(content_json.encode("utf-8")).hexdigest()
        next_number = conn.execute(
            "SELECT COALESCE(MAX(version_number), 0) + 1 AS n FROM report_versions WHERE report_id = ?",
            (report_id,),
        ).fetchone()["n"]
        now = _now_iso()

        conn.execute(
            """
            INSERT INTO report_versions
                (version_id, report_id, version_number, run_id, as_of, content_json,
                 content_hash, agent_id, model, prompt_version, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (version_id, report_id, next_number, run_id, as_of, content_json,
             content_hash, agent_id, model, prompt_version, "generated", now),
        )
        for ref in input_refs:
            conn.execute(
                "INSERT INTO report_inputs (version_id, input_type, input_id, source_repo, role) VALUES (?, ?, ?, ?, ?)",
                (version_id, ref["input_type"], ref["input_id"], ref.get("source_repo"), ref["role"]),
            )
        conn.execute("UPDATE reports SET updated_at = ? WHERE report_id = ?", (now, report_id))
    return version_id, True


def set_version_status(db_path: str, *, version_id: str, status: str) -> None:
    with _connect_write(db_path) as conn:
        conn.execute("UPDATE report_versions SET status = ? WHERE version_id = ?", (status, version_id))


def publish_version(db_path: str, *, version_id: str) -> None:
    """Mark ``version_id`` published, point its report's ``current_version_id``
    at it, and mark the previously-current published version (if any)
    ``superseded``. Never mutates a published version's content -- only its
    status, and only forward (published -> superseded), never back.

    Refuses (raises) to publish ``version_id`` if the currently-published
    version is a LATER version (by ``version_number``) -- e.g. a
    delayed/out-of-order approval publishing v1 after v2 is already
    published must not supersede v2's content, since ``superseded`` is
    documented to mean "a newer version was published", not "some other
    version was published later in wall-clock time".
    """
    with _connect_write(db_path) as conn:
        row = conn.execute(
            "SELECT report_id, version_number FROM report_versions WHERE version_id = ?", (version_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no such version_id: {version_id!r}")
        report_id = row["report_id"]

        previous = conn.execute(
            """
            SELECT rv.version_id AS version_id, rv.version_number AS version_number
            FROM reports r JOIN report_versions rv ON rv.version_id = r.current_version_id
            WHERE r.report_id = ?
            """,
            (report_id,),
        ).fetchone()
        previous_version_id = previous["version_id"] if previous else None
        if previous is not None and previous["version_number"] > row["version_number"]:
            raise ValueError(
                f"cannot publish version {version_id!r} (version_number={row['version_number']}) -- "
                f"a newer version {previous_version_id!r} (version_number={previous['version_number']}) "
                "is already published"
            )

        now = _now_iso()
        conn.execute("UPDATE report_versions SET status = 'published' WHERE version_id = ?", (version_id,))
        if previous_version_id and previous_version_id != version_id:
            conn.execute(
                "UPDATE report_versions SET status = 'superseded' WHERE version_id = ? AND status = 'published'",
                (previous_version_id,),
            )
        conn.execute(
            "UPDATE reports SET current_version_id = ?, status = 'published', updated_at = ? WHERE report_id = ?",
            (version_id, now, report_id),
        )


def record_changes(db_path: str, *, current_version_id: str, previous_version_id: str | None, changes: list[dict]) -> None:
    with _connect_write(db_path) as conn:
        for change in changes:
            conn.execute(
                "INSERT INTO report_changes (current_version_id, previous_version_id, change_type, description, drivers) VALUES (?, ?, ?, ?, ?)",
                (current_version_id, previous_version_id, change["change_type"], change["description"], json.dumps(change.get("drivers", []))),
            )


def link_artifact(db_path: str, *, version_id: str, artifact_id: str, repo: str, role: str = "primary") -> None:
    with _connect_write(db_path) as conn:
        conn.execute(
            "INSERT INTO report_artifacts (version_id, artifact_id, repo, role) VALUES (?, ?, ?, ?)",
            (version_id, artifact_id, repo, role),
        )


def get_report(db_path: str, report_id: str) -> dict | None:
    conn = _connect_read(db_path)
    if conn is None:
        return None
    with conn:
        row = conn.execute("SELECT * FROM reports WHERE report_id = ?", (report_id,)).fetchone()
    return dict(row) if row is not None else None


def get_version(db_path: str, version_id: str) -> dict | None:
    conn = _connect_read(db_path)
    if conn is None:
        return None
    with conn:
        row = conn.execute("SELECT * FROM report_versions WHERE version_id = ?", (version_id,)).fetchone()
    return dict(row) if row is not None else None


def get_latest_version(db_path: str, *, report_id: str, status: str | None = "published") -> dict | None:
    """The newest version by version_number, optionally filtered by status
    (``status=None`` returns the newest version regardless of status)."""
    conn = _connect_read(db_path)
    if conn is None:
        return None
    with conn:
        if status is None:
            row = conn.execute(
                "SELECT * FROM report_versions WHERE report_id = ? ORDER BY version_number DESC LIMIT 1",
                (report_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM report_versions WHERE report_id = ? AND status = ? ORDER BY version_number DESC LIMIT 1",
                (report_id, status),
            ).fetchone()
    return dict(row) if row is not None else None


def get_previous_version(db_path: str, version_id: str) -> dict | None:
    """The version immediately before ``version_id`` in the same report's
    sequence (by version_number), regardless of status."""
    conn = _connect_read(db_path)
    if conn is None:
        return None
    with conn:
        current = conn.execute(
            "SELECT report_id, version_number FROM report_versions WHERE version_id = ?", (version_id,)
        ).fetchone()
        if current is None:
            return None
        row = conn.execute(
            "SELECT * FROM report_versions WHERE report_id = ? AND version_number < ? ORDER BY version_number DESC LIMIT 1",
            (current["report_id"], current["version_number"]),
        ).fetchone()
    return dict(row) if row is not None else None


def search_reports(
    db_path: str,
    *,
    report_type: str | None = None,
    scope_type: str | None = None,
    scope_key: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> list[dict]:
    if limit < 1:
        raise ValueError(f"limit must be a positive integer, got {limit!r}")
    clauses: list[str] = []
    params: list[object] = []
    if report_type is not None:
        clauses.append("report_type = ?")
        params.append(report_type)
    if scope_type is not None:
        clauses.append("scope_type = ?")
        params.append(scope_type)
    if scope_key is not None:
        clauses.append("scope_key = ?")
        params.append(scope_key)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = _connect_read(db_path)
    if conn is None:
        return []
    with conn:
        rows = conn.execute(
            f"SELECT * FROM reports {where} ORDER BY updated_at DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
    return [dict(r) for r in rows]


def list_inputs(db_path: str, version_id: str) -> list[dict]:
    conn = _connect_read(db_path)
    if conn is None:
        return []
    with conn:
        rows = conn.execute("SELECT * FROM report_inputs WHERE version_id = ?", (version_id,)).fetchall()
    return [dict(r) for r in rows]


def list_changes(db_path: str, current_version_id: str) -> list[dict]:
    conn = _connect_read(db_path)
    if conn is None:
        return []
    with conn:
        rows = conn.execute(
            "SELECT * FROM report_changes WHERE current_version_id = ?", (current_version_id,)
        ).fetchall()
    return [dict(r) for r in rows]
