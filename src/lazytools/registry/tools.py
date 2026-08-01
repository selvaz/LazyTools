"""The DB registry + artifact catalog as LazyBridge tools.

:class:`RegistryTools` is a lazybridge ``ToolProvider`` (the lazytools way:
one ``Tool.wrap`` per surfaced function) exposing:

* ``registry_status`` — which ecosystem DBs are configured in this
  environment (:func:`lazytools.registry.db.status`).
* ``artifact_register`` / ``artifact_search`` / ``artifact_get`` — the
  artifact catalog, fanned out across every repo's configured artifact DB
  (:mod:`lazytools.registry.router`).

Unlike the connector ``ToolProvider``\\ s, this one needs no external
credentials or optional dependency — it is stdlib-only (``sqlite3``) and
ships in the core package, so it needs no constructor arguments either.
"""

from __future__ import annotations

from lazybridge import Tool

from lazytools.registry import db
from lazytools.registry.artifacts import register_artifact
from lazytools.registry.router import get_everywhere, search_everywhere


class RegistryTools:
    """A ``ToolProvider`` exposing the DB registry and artifact catalog."""

    _is_lazy_tool_provider = True

    # ------------------------------------------------------------------ #
    # ToolProvider
    # ------------------------------------------------------------------ #
    def as_tools(self) -> list[Tool]:
        return [
            Tool.wrap(
                self._registry_status,
                name="registry_status",
                description=(
                    "List every known ecosystem DB (market_data, pulse_state, "
                    "crawler_raw, and each repo's optional artifact DB) with its "
                    "env var, owning repo, whether it's required, and whether it "
                    "is currently set in this environment. Returns JSON. No arguments."
                ),
            ),
            Tool.wrap(
                self._artifact_register,
                name="artifact_register",
                description=(
                    "Save an artifact (an analysis/report produced by an agent or "
                    "job) to its repo's artifact catalog, so it can be found later "
                    "without re-running the work or carrying its full payload "
                    "through an LLM's context. Returns the new artifact_id. Args: "
                    "repo (str, which repo owns the artifact DB to write into, "
                    "e.g. 'market-data-hub' — must have a configured artifact DB, "
                    "see registry_status); kind (str, free-text category e.g. "
                    "'backtest_report'); title (str); summary (str, cheap-to-read "
                    "description — do not put the full payload here); tags "
                    "(comma-separated string, optional); content (str, optional "
                    "full payload); ttl_days (int, optional expiry in days)."
                ),
            ),
            Tool.wrap(
                self._artifact_search,
                name="artifact_search",
                description=(
                    "Search artifact catalogs across every configured repo. Never "
                    "returns full content -- title/summary/tags only; follow up "
                    "with artifact_get for the full record. Returns JSON. Args: "
                    "query (str, optional substring match on title/summary/tags); "
                    "kind (str, optional exact match); tags (comma-separated "
                    "string, optional, ALL must be present); since (ISO8601 "
                    "string, optional lower bound on created_at); limit (int, "
                    "default 20)."
                ),
            ),
            Tool.wrap(
                self._artifact_get,
                name="artifact_get",
                description=(
                    "Fetch one artifact's full record (including content) from a "
                    "specific repo's artifact catalog. Returns JSON, or null if "
                    "not found/expired/that repo has no artifact DB configured. "
                    "Args: repo (str, owning repo, from artifact_search's 'repo' "
                    "field); artifact_id (str)."
                ),
            ),
        ]

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _artifact_db_for_repo(self, repo: str) -> str:
        """Resolve the artifact DB path owned by ``repo``.

        Raises:
            KeyError: no ``KNOWN_DBS`` artifact entry is owned by ``repo``.
            RuntimeError: the entry exists but its env var is unset.
        """
        for entry in db.KNOWN_DBS:
            if entry.name.endswith("_artifacts") and entry.owner_repo == repo:
                path = db.resolve_db(entry.name)
                if path is None:
                    raise RuntimeError(
                        f"No artifact DB configured for repo {repo!r}: env var {entry.env_var!r} is unset."
                    )
                return path
        known_repos = sorted({e.owner_repo for e in db.KNOWN_DBS if e.name.endswith("_artifacts")})
        raise KeyError(f"No known artifact DB for repo {repo!r}. Known artifact-DB repos: {known_repos}")

    # ------------------------------------------------------------------ #
    # Tool implementations
    # ------------------------------------------------------------------ #
    def _registry_status(self) -> list[dict]:
        return db.status()

    def _artifact_register(
        self,
        repo: str,
        kind: str,
        title: str,
        summary: str,
        tags: str = "",
        content: str = "",
        ttl_days: int = 0,
    ) -> str:
        db_path = self._artifact_db_for_repo(repo)
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        return register_artifact(
            db_path,
            repo=repo,
            kind=kind,
            title=title,
            summary=summary,
            tags=tag_list,
            content=content or None,
            ttl_days=ttl_days or None,
        )

    def _artifact_search(
        self,
        query: str = "",
        kind: str = "",
        tags: str = "",
        since: str = "",
        limit: int = 20,
    ) -> list[dict]:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        return search_everywhere(
            query=query or None,
            kind=kind or None,
            tags=tag_list,
            since=since or None,
            limit=limit,
        )

    def _artifact_get(self, repo: str, artifact_id: str) -> dict | None:
        return get_everywhere(repo, artifact_id)
