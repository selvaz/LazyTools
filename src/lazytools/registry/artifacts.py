"""Artifact catalog: save/retrieve agent- or job-produced analyses without
shoving raw payloads into an LLM's context.

Stdlib ``sqlite3`` only — no new dependency. Every function takes an
explicit ``db_path: str`` (dependency injection; nothing here reaches into
:mod:`lazytools.registry.db` or any global state), so callers resolve the
path once (typically via :func:`lazytools.registry.db.resolve_db` or
:func:`lazytools.registry.db.artifact_dbs`) and pass it in.

An "artifact" is a small, self-describing record: a title/summary an agent
can read cheaply, optional full ``content`` (or a ``content_uri`` pointing
at it) for when the caller actually needs the payload, tags for filtering,
and an optional TTL. :func:`search_artifacts` deliberately never returns
``content`` — that's what :func:`get_artifact` is for — so browsing a
catalog of artifacts stays cheap even when individual artifacts are large.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    expires_at TEXT,
    content TEXT,
    content_uri TEXT,
    content_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_artifacts_kind ON artifacts(kind);
CREATE INDEX IF NOT EXISTS idx_artifacts_created_at ON artifacts(created_at);
"""

_ROW_COLUMNS = (
    "artifact_id",
    "repo",
    "kind",
    "title",
    "summary",
    "tags",
    "created_at",
    "expires_at",
    "content",
    "content_uri",
    "content_hash",
)

_METADATA_COLUMNS = (
    "artifact_id",
    "repo",
    "kind",
    "title",
    "summary",
    "tags",
    "created_at",
    "expires_at",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    return conn


def _row_to_dict(row: tuple, columns: tuple[str, ...]) -> dict:
    record = dict(zip(columns, row, strict=True))
    if "tags" in record:
        record["tags"] = json.loads(record["tags"])
    return record


def register_artifact(
    db_path: str,
    *,
    repo: str,
    kind: str,
    title: str,
    summary: str,
    tags: list[str] | None = None,
    content: str | None = None,
    content_uri: str | None = None,
    ttl_days: int | None = None,
) -> str:
    """Create (if missing) the ``artifacts`` table and insert one record.

    Args:
        db_path: Path to the SQLite file (the caller's artifact DB).
        repo: Which repo produced this artifact (e.g. ``"lazytools"``).
        kind: Free-text artifact category (e.g. ``"backtest_report"``).
        title: Short human-readable title.
        summary: Cheap-to-read summary — what :func:`search_artifacts`
            returns; keep the full payload out of this field.
        tags: Optional list of string tags for filtering.
        content: Optional full payload, stored inline.
        content_uri: Optional pointer to the payload when it lives
            elsewhere (e.g. an object-storage URL) instead of inline.
        ttl_days: Optional time-to-live in days from now; sets
            ``expires_at``. ``None`` means the artifact never expires.

    Returns:
        The new artifact's id (``str(uuid.uuid4())``).
    """
    artifact_id = str(uuid.uuid4())
    created_at = _now_iso()
    expires_at = None
    if ttl_days is not None:
        expires_at = (datetime.now(UTC) + timedelta(days=ttl_days)).isoformat()
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest() if content is not None else None

    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO artifacts (
                artifact_id, repo, kind, title, summary, tags,
                created_at, expires_at, content, content_uri, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                repo,
                kind,
                title,
                summary,
                json.dumps(tags or []),
                created_at,
                expires_at,
                content,
                content_uri,
                content_hash,
            ),
        )
    return artifact_id


def search_artifacts(
    db_path: str,
    *,
    query: str | None = None,
    kind: str | None = None,
    tags: list[str] | None = None,
    since: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Search the artifact catalog. Never returns ``content`` — metadata +
    summary only; call :func:`get_artifact` for the full record.

    Args:
        db_path: Path to the SQLite file.
        query: Case-insensitive substring match against ``title``,
            ``summary``, or ``tags``.
        kind: Exact match on ``kind``.
        tags: Every tag in this list must be present on the artifact's tags.
        since: ISO8601 timestamp; only artifacts with ``created_at >= since``.
        limit: Maximum rows to return.

    Returns:
        Matching records (no ``content``/``content_uri``/``content_hash``),
        ordered by ``created_at`` descending, excluding expired artifacts.
    """
    with _connect(db_path) as conn:
        clauses = ["(expires_at IS NULL OR expires_at > ?)"]
        params: list[object] = [_now_iso()]

        if query:
            clauses.append("(title LIKE ? OR summary LIKE ? OR tags LIKE ?)")
            like = f"%{query}%"
            params.extend([like, like, like])
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if since:
            clauses.append("created_at >= ?")
            params.append(since)

        sql = (
            f"SELECT {', '.join(_METADATA_COLUMNS)} FROM artifacts "
            f"WHERE {' AND '.join(clauses)} ORDER BY created_at DESC"
        )
        rows = conn.execute(sql, params).fetchall()

    records = [_row_to_dict(row, _METADATA_COLUMNS) for row in rows]

    if tags:
        wanted = set(tags)
        records = [r for r in records if wanted.issubset(set(r["tags"]))]

    return records[:limit]


def get_artifact(db_path: str, artifact_id: str) -> dict | None:
    """Fetch one artifact's full record, including ``content``.

    Args:
        db_path: Path to the SQLite file.
        artifact_id: The artifact's id.

    Returns:
        The full record, or ``None`` if not found or if its ``expires_at``
        has already passed.
    """
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT {', '.join(_ROW_COLUMNS)} FROM artifacts WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchone()

    if row is None:
        return None

    record = _row_to_dict(row, _ROW_COLUMNS)
    if record["expires_at"] and record["expires_at"] <= _now_iso():
        return None
    return record
