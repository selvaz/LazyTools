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
import os
import sqlite3
import urllib.request
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


def _connect_write(db_path: str) -> sqlite3.Connection:
    """Open (creating the file/schema if needed) for a mutating call."""
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    return conn


def _connect_read(db_path: str) -> sqlite3.Connection | None:
    """Open ``db_path`` read-only for a search/get call, without ever
    creating the file or its schema.

    A search/get against a repo whose artifact DB hasn't been written to
    yet (or whose deployment mounts the filesystem read-only) must return
    an empty result, not silently create a database file nor raise on a
    read-only mount -- so a missing file short-circuits to ``None`` before
    ``sqlite3.connect`` ever touches disk. Likewise a file that exists but
    predates the first ``register_artifact`` call (so it has no
    ``artifacts`` table yet) is an empty catalog, not an error.

    ``?``/``#`` are structurally significant in a ``file:`` URI (query
    string / fragment delimiters) -- both are valid filename characters on
    Linux, where this ships (see the Coolify/VPS deploy docs), so a literal
    ``db_path`` can't be interpolated into the URI directly without
    truncating or misrouting it. ``pathname2url`` percent-encodes it correctly.
    """
    if not os.path.exists(db_path):
        return None
    uri = "file:" + urllib.request.pathname2url(os.path.abspath(db_path)) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifacts'"
    ).fetchone()
    if has_table is None:
        conn.close()
        return None
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

    with _connect_write(db_path) as conn:
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
        limit: Maximum rows to return. Must be a positive integer.

    Returns:
        Matching records (no ``content``/``content_uri``/``content_hash``),
        ordered by ``created_at`` descending, excluding expired artifacts.

    Raises:
        ValueError: ``limit`` is not a positive integer.
    """
    limit = int(limit)
    if limit < 1:
        raise ValueError(f"limit must be a positive integer, got {limit!r}")

    conn = _connect_read(db_path)
    if conn is None:
        return []

    with conn:
        clauses = ["(expires_at IS NULL OR expires_at > ?)"]
        params: list[object] = [_now_iso()]

        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if since:
            # created_at is always stored as a UTC isoformat string; a valid
            # non-UTC offset in `since` (e.g. "...+02:00") must compare as
            # the same instant, not as mismatched string spellings.
            since_dt = datetime.fromisoformat(since)
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=UTC)
            clauses.append("created_at >= ?")
            params.append(since_dt.astimezone(UTC).isoformat())

        where = " AND ".join(clauses)
        query_lower = query.lower() if query else None
        wanted = set(tags) if tags else None

        def _matches(record: dict) -> bool:
            # Matched against the *decoded* tag values, not the raw
            # json.dumps() bytes -- a tag like "café" is escaped to
            # "café" (and one containing quotes gains backslashes) in
            # the serialized column, so a literal-text query would never
            # find it there even though it's an exact tag match.
            if query_lower is not None and not (
                query_lower in record["title"].lower()
                or query_lower in record["summary"].lower()
                or any(query_lower in t.lower() for t in record["tags"])
            ):
                return False
            return wanted is None or wanted.issubset(set(record["tags"]))

        if query_lower is None and wanted is None:
            # No Python-side filter needed -- bound the fetch in SQL itself
            # instead of pulling the whole catalog and discarding excess.
            sql = (
                f"SELECT {', '.join(_METADATA_COLUMNS)} FROM artifacts "
                f"WHERE {where} ORDER BY created_at DESC LIMIT ?"
            )
            rows = conn.execute(sql, [*params, limit]).fetchall()
            return [_row_to_dict(row, _METADATA_COLUMNS) for row in rows]

        # `query`/`tags` need a Python-side check (substring match against
        # decoded tags, or a tag-set subset check), so SQL can't bound the
        # result directly. Fetch in growing batches until `limit` matches
        # are found or the table is exhausted, rather than unconditionally
        # decoding every row.
        matched: list[dict] = []
        batch_size = max(limit * 5, 100)
        offset = 0
        while len(matched) < limit:
            sql = (
                f"SELECT {', '.join(_METADATA_COLUMNS)} FROM artifacts "
                f"WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?"
            )
            rows = conn.execute(sql, [*params, batch_size, offset]).fetchall()
            if not rows:
                break
            for row in rows:
                record = _row_to_dict(row, _METADATA_COLUMNS)
                if _matches(record):
                    matched.append(record)
                    if len(matched) >= limit:
                        break
            if len(rows) < batch_size:
                break  # exhausted the table
            offset += batch_size
        return matched[:limit]


def get_artifact(db_path: str, artifact_id: str) -> dict | None:
    """Fetch one artifact's full record, including ``content``.

    Args:
        db_path: Path to the SQLite file.
        artifact_id: The artifact's id.

    Returns:
        The full record, or ``None`` if not found or if its ``expires_at``
        has already passed.
    """
    conn = _connect_read(db_path)
    if conn is None:
        return None

    with conn:
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
