"""LazyBridge tools for read-only code mapping and issue-text search."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from lazytools.codemap._backend import RepoMapCLI
from lazytools.connectors.code_support._review import _resolve_repo


class RepoMapTools:
    """Expose RepoMap while confining every requested repository to ``root``."""

    _is_lazy_tool_provider = True

    def __init__(
        self,
        *,
        root: str | Path | None = None,
        cache_dir: str | Path | None = None,
        backend: RepoMapCLI | None = None,
    ) -> None:
        configured = root or os.environ.get("LAZYTOOLS_CODE_ROOT") or Path.cwd()
        self._root = Path(configured).expanduser().resolve()
        self._backend = backend or RepoMapCLI(cache_dir=cache_dir)

    def codemap_search(self, query: str, repo_path: str, limit: int = 10) -> Any:
        """Rank candidate files for issue text and return RepoMap's short matching lines and reasons.

        Args:
            query: issue title, description, or natural-language code question.
            repo_path: repository directory, absolute or relative to the configured code root.
            limit: maximum ranked candidates to return, from 1 through 50.
        """
        if not query.strip():
            raise ValueError("query is required")
        bounded = max(1, min(int(limit), 50))
        repo = _resolve_repo(repo_path, self._root)
        return self._backend.search(query.strip(), repo, bounded)

    def codemap_map(self, repo_path: str, tokens: int = 1200) -> Any:
        """Return a compact repository map within the requested token budget.

        Args:
            repo_path: repository directory, absolute or relative to the configured code root.
            tokens: approximate output budget, from 100 through 10000 tokens.
        """
        bounded = max(100, min(int(tokens), 10_000))
        repo = _resolve_repo(repo_path, self._root)
        return self._backend.map(repo, bounded)

    def as_tools(self) -> list[Any]:
        from lazybridge import Tool

        return [
            Tool.wrap(self.codemap_search, name="codemap_search"),
            Tool.wrap(self.codemap_map, name="codemap_map"),
        ]
