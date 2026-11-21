"""Native LazyBridge ToolProvider backed by the optional local repomap CLI."""
from __future__ import annotations

from pathlib import Path
from typing import Mapping

from lazybridge import Tool

from ._backend import RepoMapCLI


class RepoMapTools:
    """Code intelligence tools for configured repository roots.

    Supply this provider directly to Agent(tools=[provider]). MCP is not
    required. The third-party engine runs only when a tool is invoked.
    """

    _is_lazy_tool_provider = True

    def __init__(
        self,
        roots: Mapping[str, str | Path],
        *,
        executable: str = "repomap",
        timeout_seconds: float = 45.0,
        max_result_chars: int = 16_000,
        use_embeddings: bool = True,
    ) -> None:
        self.backend = RepoMapCLI(
            roots, executable=executable, timeout_seconds=timeout_seconds,
            max_result_chars=max_result_chars, use_embeddings=use_embeddings,
        )

    def as_tools(self) -> list[Tool]:
        return [
            Tool.wrap(
                self.backend.overview,
                name="codemap_overview",
                description=(
                    "Summarize code modules, central files, key symbols and repository "
                    "relationships. repo is an optional configured alias (empty = all). "
                    "tokens is an approximate map budget from 256 to 4000 (default 1200)."
                ),
            ),
            Tool.wrap(
                self.backend.search,
                name="codemap_search",
                description=(
                    "Find relevant symbols/files by identifier or natural language. "
                    "query is required; repo is an optional configured alias (empty = all). "
                    "limit is 1-20. Prefer this before reading entire files."
                ),
            ),
            Tool.wrap(
                self.backend.context,
                name="codemap_context",
                description=(
                    "Inspect a code symbol or repo-relative file with callers, imports and "
                    "related tests. target is required; repo is optional; code_lines is 0-80. "
                    "Static analysis is not proof of runtime behavior."
                ),
            ),
            Tool.wrap(
                self.backend.impact,
                name="codemap_impact",
                description=(
                    "Estimate change impact: callers, dependencies, modules and candidate "
                    "tests. target is a symbol or relative file; repo is an optional alias; "
                    "depth is 1-4. Validate estimated impacts against tests."
                ),
            ),
        ]
