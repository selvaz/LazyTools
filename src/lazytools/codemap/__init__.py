"""Optional Python-native code intelligence backed by SylphxAI/repomap.

The third-party engine is MIT licensed. Neither it nor MCP is required to
import LazyTools' top-level package or install its core dependencies.
"""
from __future__ import annotations

from ._backend import RepoMapCLI, RepoMapError
from .tools import RepoMapTools

__all__ = ["RepoMapCLI", "RepoMapError", "RepoMapTools"]
