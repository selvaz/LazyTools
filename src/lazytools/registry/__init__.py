"""Ecosystem DB registry + artifact catalog.

Two small, stdlib-only primitives for the multi-repo Lazy* ecosystem
(LazyBridge, LazyTools, LazyFin, LazyPulse, LazyCrawler, market-data-hub,
LazyStats), each of which owns its own domain DB:

* :mod:`lazytools.registry.db` — :data:`KNOWN_DBS` declares, in code
  (versioned, PR-reviewable), which env var names each repo's DB and who
  owns it. :func:`resolve_db` / :func:`status` / :func:`artifact_dbs` read
  the actual path from that env var — the value still comes from each
  deployment's own environment, exactly as today. Deliberately **not** a
  shared central DB or a shared config file (both were considered and
  rejected: each repo must stay isolated in its own domain DB, and a shared
  config file would drift across the ecosystem's independent Coolify/
  Railway deployments).
* :mod:`lazytools.registry.artifacts` — a small SQLite-backed catalog
  (``register_artifact`` / ``search_artifacts`` / ``get_artifact``) for
  saving and retrieving artifacts/analyses produced by agents or scheduled
  jobs, without shoving raw payloads into an LLM's context.
* :mod:`lazytools.registry.router` — fans searches/gets out across every
  repo's configured artifact DB (``search_everywhere`` / ``get_everywhere``).
* :mod:`lazytools.registry.tools` — :class:`RegistryTools`, the LazyBridge
  ``ToolProvider`` wrapping all of the above for agent use.

No extra required — this ships in the core package.
"""

from __future__ import annotations

from lazytools.registry.artifacts import get_artifact, register_artifact, search_artifacts
from lazytools.registry.db import KNOWN_DBS, DBEntry, artifact_dbs, resolve_db, status
from lazytools.registry.router import get_everywhere, search_everywhere
from lazytools.registry.tools import RegistryTools

__all__ = [
    "DBEntry",
    "KNOWN_DBS",
    "resolve_db",
    "status",
    "artifact_dbs",
    "register_artifact",
    "search_artifacts",
    "get_artifact",
    "search_everywhere",
    "get_everywhere",
    "RegistryTools",
]
