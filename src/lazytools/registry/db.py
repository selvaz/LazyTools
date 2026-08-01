"""Ecosystem DB registry: which env var to check before opening another
repo's SQLite/DuckDB file.

The Lazy* ecosystem is deliberately **not** backed by a shared central DB or
a shared config file — each repo (LazyBridge, LazyTools, LazyFin, LazyPulse,
LazyCrawler, market-data-hub, LazyStats) owns its own domain DB, and the
ecosystem runs across independent Coolify/Railway deployments each with
their own env vars. A shared config file would drift out of sync across
those deployments.

Instead, :data:`KNOWN_DBS` declares — in code, versioned, PR-reviewable —
which env var names which DB and which repo owns it. The **value** of that
env var (the actual path/DSN) still comes from each deployment's own
environment, exactly as it does today; this module only centralizes the
*name* of the env var so callers don't have to memorize or re-guess it.

Add a new DB by adding a :class:`DBEntry` to :data:`KNOWN_DBS` in a PR —
that's the whole mechanism; there is no runtime registration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class DBEntry:
    """One known ecosystem DB: which env var holds its path, who owns it.

    Attributes:
        name: Stable logical identifier (e.g. ``"market_data"``).
        env_var: Name of the environment variable holding the DB path/DSN.
        owner_repo: The repo that owns and writes this DB.
        required: If ``True``, a caller expects this DB to always be
            configured in a fully set-up deployment; :func:`resolve_db`
            raises ``RuntimeError`` rather than returning ``None`` when it
            is unset. Artifact DBs (opt-in, per-repo) are ``required=False``.
        description: Short human-readable note on what the DB holds.
    """

    name: str
    env_var: str
    owner_repo: str
    required: bool = True
    description: str = ""


KNOWN_DBS: tuple[DBEntry, ...] = (
    DBEntry("market_data", "MARKET_DATA_DB", "market-data-hub", True, "Prices and historical series"),
    DBEntry("pulse_state", "STORE_DB", "lazypulse", True, "Telegram bot state"),
    DBEntry("crawler_raw", "CRAWLER_DB", "lazycrawler", True, "Crawling cache"),
    DBEntry(
        "market_data_artifacts",
        "MARKET_DATA_ARTIFACTS_DB",
        "market-data-hub",
        False,
        "Artifacts produced by market-data-hub",
    ),
    DBEntry("pulse_artifacts", "PULSE_ARTIFACTS_DB", "lazypulse", False, "Artifacts produced by LazyPulse"),
    DBEntry("crawler_artifacts", "CRAWLER_ARTIFACTS_DB", "lazycrawler", False, "Artifacts produced by LazyCrawler"),
)

_BY_NAME: dict[str, DBEntry] = {entry.name: entry for entry in KNOWN_DBS}


def resolve_db(name: str) -> str | None:
    """Resolve a known DB's path from its declared environment variable.

    Args:
        name: Logical DB name — must be one of ``KNOWN_DBS``' ``name`` values.

    Returns:
        The env var's value (the DB path/DSN) if set. ``None`` if the entry
        is optional (``required=False``) and its env var is unset.

    Raises:
        KeyError: ``name`` is not a known DB (not in ``KNOWN_DBS``).
        RuntimeError: The entry is required (``required=True``) and its env
            var is unset. The message names the env var so the operator
            knows exactly what to set.
    """
    try:
        entry = _BY_NAME[name]
    except KeyError:
        raise KeyError(f"Unknown DB {name!r}. Known DBs: {sorted(_BY_NAME)}") from None

    value = os.environ.get(entry.env_var)
    if value:
        return value
    if entry.required:
        raise RuntimeError(
            f"DB {entry.name!r} (owned by {entry.owner_repo}) requires env var "
            f"{entry.env_var!r} to be set, but it is unset."
        )
    return None


def status() -> list[dict]:
    """Report, for every known DB, whether its env var is currently set.

    Returns:
        One dict per :data:`KNOWN_DBS` entry:
        ``{name, env_var, owner_repo, required, set}``.
    """
    return [
        {
            "name": entry.name,
            "env_var": entry.env_var,
            "owner_repo": entry.owner_repo,
            "required": entry.required,
            "set": bool(os.environ.get(entry.env_var)),
        }
        for entry in KNOWN_DBS
    ]


def artifact_dbs() -> list[tuple[str, str]]:
    """List the artifact DBs that are actually configured in this environment.

    Returns:
        ``[(owner_repo, path), ...]`` for every ``KNOWN_DBS`` entry whose
        ``name`` ends in ``"_artifacts"`` and whose env var is set. Entries
        whose env var is unset are silently skipped (artifact DBs are
        opt-in per repo).
    """
    result: list[tuple[str, str]] = []
    for entry in KNOWN_DBS:
        if not entry.name.endswith("_artifacts"):
            continue
        value = os.environ.get(entry.env_var)
        if value:
            result.append((entry.owner_repo, value))
    return result
