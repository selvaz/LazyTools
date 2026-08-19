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
    DBEntry(
        "pulse_state",
        "STORE_DB",
        "lazypulse",
        False,
        "LazyPulse's always-on PulseAgent/Telegram bot state store. "
        "Optional, not required: a deployment that schedules its jobs "
        "externally (e.g. the Windows Task Scheduler) runs no PulseAgent "
        "and so has nothing to persist here. It was declared required=True, "
        "which made status() report a missing required DB in every such "
        "deployment -- a permanent false alarm that trains the reader to "
        "ignore the one signal the registry exists to give. No caller "
        "resolves this entry; set STORE_DB only when actually running the "
        "always-on agent.",
    ),
    DBEntry("crawler_raw", "LAZYCRAWLER_NEWS_DB", "lazycrawler", True, "News crawl page cache"),
    DBEntry(
        "lazystats_depot",
        "LAZYSTATS_RESULT_DEPOT_DB",
        "lazystats",
        True,
        "LazyStats analysis result depot (regime, regression, ...)",
    ),
    DBEntry(
        "regime_tools_db",
        "LAZYTOOLS_REGIME_DB",
        "lazystats",
        False,
        "LazyHMM regime-fitting tool depot (fitted params, figures, state "
        "sequences) backing LazyTools' regime_* MCP tools -- a separate "
        "store from lazystats_depot, which holds market-data-hub's "
        "persisted regime *run results*, not the fitting tools' own state",
    ),
    DBEntry(
        "market_data_artifacts",
        "MARKET_DATA_ARTIFACTS_DB",
        "market-data-hub",
        False,
        "Artifacts produced by market-data-hub",
    ),
    DBEntry("pulse_artifacts", "PULSE_ARTIFACTS_DB", "lazypulse", False, "Artifacts produced by LazyPulse"),
    DBEntry("crawler_artifacts", "CRAWLER_ARTIFACTS_DB", "lazycrawler", False, "Artifacts produced by LazyCrawler"),
    DBEntry(
        "crawler_econ_state",
        "ECON_STATE_DB",
        "lazycrawler",
        False,
        "Economic-release monitor cursor state (which releases have already "
        "been reported). NOT append-only history: two copies each advance "
        "their own cursor, so never union them -- take the one the live "
        "producer most recently advanced.",
    ),
    DBEntry(
        "crawler_digests",
        "DIGESTS_DB",
        "lazycrawler",
        False,
        "Full text of every executive news digest (make_news_report.py), "
        "keyed UNIQUE(session_id, engine) -- crawler_artifacts holds the "
        "catalogue entry and file pointer, this holds the prose itself. "
        "make_news_report.py reads this variable and only falls back to a "
        "checkout-relative reports/news/digests.db when it is unset, which "
        "is how a pinned runtime worktree once split the history.",
    ),
    DBEntry(
        "lazyray_db",
        "LAZYRAY_DB",
        "lazyray",
        False,
        "LazyRay's own DuckDB output (Dalio-style scores, regimes, "
        "classifications). LazyRay resolves its own settings-based default "
        "when this is unset -- that silent fallback split a deployment's "
        "history once, so deployments should wire it explicitly.",
    ),
    DBEntry(
        "lazyportfolio_artifacts",
        "LAZYPORTFOLIO_ARTIFACTS_DB",
        "lazyportfolio",
        False,
        "Artifacts (reports) produced by LazyPortfolio",
    ),
    DBEntry(
        "lazyportfolio_store",
        "LAZYPORTFOLIO_TREE_DB",
        "lazyportfolio",
        False,
        "Tree Studio's primary store (lazyportfolio.v2.db): saved tree "
        "configs, and structured run history/artifacts (weights, metrics, "
        "data-as-of, config hash) for every estimate/backtest/report run. "
        "A separate store from lazyportfolio_artifacts above: that one is "
        "the opt-in cross-repo artifact catalog entry for the rendered "
        "HTML report; this one is LazyPortfolio's own primary data. Like "
        "every optional entry here, resolve_db() only returns a path when "
        "LAZYPORTFOLIO_TREE_DB is actually set -- LazyPortfolio itself "
        "still works with it unset (falls back to a repo-relative default "
        "path), but that fallback path is NOT visible through resolve_db()/"
        "status(); call lazyportfolio.v2.store.resolve_store_path() "
        "directly to see the path actually in use in that case.",
    ),
    DBEntry(
        "anomaly_explanations",
        "ANOMALY_EXPLANATIONS_DB",
        "lazystats",
        False,
        "LLM-generated causal explanations for statistical anomalies "
        "(return outliers, volatility/correlation shifts) flagged in "
        "lazystats_depot's etf_daily_stats series, plus the Saturday "
        "weekly review that verifies them and looks for emerging trends -- "
        "narrative/evidence content, kept in its own store separate from "
        "lazystats_depot's deterministic quantitative results.",
    ),
    # ``ic_reports`` used to be declared here, pointing at code that lived in
    # ``lazytools.ic_reports``. Both moved to the private investmentcommittee
    # repository, which now owns the database and resolves IC_REPORTS_DB
    # itself. A general-purpose package should not carry another project's
    # domain model, nor declare a database it does not own.
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
