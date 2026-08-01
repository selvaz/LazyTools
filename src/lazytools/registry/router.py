"""Fan out artifact search/get across every configured repo's artifact DB.

Each repo owns its own artifact DB (see :mod:`lazytools.registry.db`); this
module is the only place that talks to more than one of them at once, and it
does so purely by resolving paths through :func:`lazytools.registry.db.artifact_dbs`
— no shared DB, no shared config file.
"""

from __future__ import annotations

from lazytools.registry import db
from lazytools.registry.artifacts import get_artifact, search_artifacts


def search_everywhere(
    *,
    query: str | None = None,
    kind: str | None = None,
    tags: list[str] | None = None,
    since: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Search every configured repo's artifact DB and merge the results.

    Args:
        query: Case-insensitive substring match against title/summary/tags.
        kind: Exact match on artifact kind.
        tags: Every tag in this list must be present on the artifact's tags.
        since: ISO8601 timestamp lower bound on ``created_at``.
        limit: Maximum merged rows to return.

    Returns:
        Records from every artifact DB whose env var is currently set (see
        :func:`lazytools.registry.db.artifact_dbs`), merged, each carrying a
        ``"repo"`` field, sorted by ``created_at`` descending and truncated
        to ``limit``.
    """
    merged: list[dict] = []
    for repo, path in db.artifact_dbs():
        records = search_artifacts(path, query=query, kind=kind, tags=tags, since=since, limit=limit)
        for record in records:
            record["repo"] = repo
        merged.extend(records)

    merged.sort(key=lambda r: r["created_at"], reverse=True)
    return merged[:limit]


def get_everywhere(repo: str, artifact_id: str) -> dict | None:
    """Fetch one artifact's full record from a specific repo's artifact DB.

    Args:
        repo: The owning repo, as it appears in
            :func:`lazytools.registry.db.artifact_dbs`' ``owner_repo`` (e.g.
            ``"market-data-hub"``).
        artifact_id: The artifact's id.

    Returns:
        The full record (see :func:`lazytools.registry.artifacts.get_artifact`),
        or ``None`` if the repo has no configured artifact DB, or the
        artifact is not found/expired.
    """
    for candidate_repo, path in db.artifact_dbs():
        if candidate_repo == repo:
            return get_artifact(path, artifact_id)
    return None
