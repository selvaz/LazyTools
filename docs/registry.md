# DB registry & artifact catalog

`lazytools.registry` gives the multi-repo Lazy* ecosystem (LazyBridge,
LazyTools, LazyFin, LazyPulse, LazyCrawler, market-data-hub, LazyStats) two
small, cross-cutting things every repo currently has to reinvent:

1. **Which env var to check** before connecting to another repo's domain DB.
2. **A place to save/retrieve artifacts** (analyses, reports, backtests)
   produced by agents or scheduled jobs, without shoving the raw payload
   through an LLM's context just to hand it off.

!!! info "Ships in the core package"
    No extra needed, no new dependency — stdlib `sqlite3` only.
    `pip install "lazytoolkit @ git+https://github.com/selvaz/LazyTools.git"`.
    ```python
    from lazytools.registry import RegistryTools, resolve_db, search_everywhere
    ```

This is **not** a connector — it doesn't bridge to an external service, it's
a cross-cutting feature (like [Safety](safety.md)), which is why it lives at
`lazytools/registry/`, not under `lazytools/connectors/`.

## The design rule

A shared central DB, and a shared config file mapping repo → DB path, were
both considered and explicitly **rejected**:

- Each repo must stay isolated in its own domain DB — a shared DB would
  couple deployments that are supposed to fail independently.
- The ecosystem runs across independent Coolify/Railway deployments, each
  with its own environment variables. A shared config file listing paths
  would drift out of sync the moment one deployment's path changed without
  the others being redeployed.

Instead: **DB names are declared explicitly in code** (`KNOWN_DBS`,
versioned, PR-reviewable) — and the *value* of each is resolved from that
repo's own env var, exactly as it works today. Nothing here reaches across a
network or a shared filesystem; `resolve_db` only reads `os.environ`.

## `KNOWN_DBS`

```python
from lazytools.registry import KNOWN_DBS, DBEntry

for entry in KNOWN_DBS:
    print(entry.name, entry.env_var, entry.owner_repo, entry.required)
```

| name | env_var | owner_repo | required | description |
|---|---|---|---|---|
| `market_data` | `MARKET_DATA_DB` | market-data-hub | yes | Prices and historical series |
| `pulse_state` | `STORE_DB` | lazypulse | yes | Telegram bot state |
| `crawler_raw` | `LAZYCRAWLER_NEWS_DB` | lazycrawler | yes | News crawl page cache |
| `market_data_artifacts` | `MARKET_DATA_ARTIFACTS_DB` | market-data-hub | no | Artifacts produced by market-data-hub |
| `pulse_artifacts` | `PULSE_ARTIFACTS_DB` | lazypulse | no | Artifacts produced by LazyPulse |
| `crawler_artifacts` | `CRAWLER_ARTIFACTS_DB` | lazycrawler | no | Artifacts produced by LazyCrawler |

**Adding a new DB is a PR**, not a runtime registration: append a `DBEntry`
to `KNOWN_DBS` in `lazytools/registry/db.py`. That's the entire mechanism —
there is no dynamic/self-registering path, by design, so every DB the
ecosystem depends on is visible in one reviewable diff.

```python
from lazytools.registry import resolve_db, status

resolve_db("market_data")             # -> the path from MARKET_DATA_DB, or
                                       #    raises RuntimeError if unset (required=True)
resolve_db("market_data_artifacts")   # -> the path, or None if unset (required=False)
resolve_db("not_a_real_db")           # -> raises KeyError

status()  # -> [{"name": ..., "env_var": ..., "owner_repo": ..., "required": ..., "set": bool}, ...]
```

## Artifact catalog

`lazytools.registry.artifacts` is a small SQLite-backed catalog for saving
and retrieving artifacts — analyses, reports, backtests — produced by agents
or scheduled jobs. Every function takes an explicit `db_path: str`
(dependency injection: no monkeypatching, no hidden global state), so a
caller resolves the path once — typically via `resolve_db` or
`artifact_dbs()` — and passes it through.

```python
from lazytools.registry import register_artifact, search_artifacts, get_artifact

artifact_id = register_artifact(
    db_path, repo="market-data-hub", kind="backtest_report",
    title="SPY momentum backtest", summary="Sharpe 1.2 over 2020-2025",
    tags=["equities", "momentum"], content=full_report_markdown, ttl_days=90,
)

# Cheap to browse: title/summary/tags only, never the full content.
hits = search_artifacts(db_path, query="momentum", limit=10)

# Full record, including content, only when you actually need it.
record = get_artifact(db_path, artifact_id)
```

`search_artifacts` never returns `content` — that keeps browsing a catalog
of potentially large artifacts cheap; call `get_artifact` for the full
record. Expired artifacts (`expires_at` in the past) are excluded from both.

## Fan-out across repos

`lazytools.registry.router` searches/fetches across every repo's *configured*
artifact DB in one call — it only ever talks to DBs whose env var is
actually set (`KNOWN_DBS` entries ending in `_artifacts`), so a deployment
missing one repo's artifact DB is silently skipped, not an error.

```python
from lazytools.registry import search_everywhere, get_everywhere

results = search_everywhere(query="momentum", limit=20)   # merged, sorted, each row carries "repo"
record = get_everywhere("market-data-hub", results[0]["artifact_id"])
```

## At a glance

```python
from lazybridge import Agent
from lazytools.registry import RegistryTools

# registry_status, artifact_register, artifact_search, artifact_get —
# no constructor arguments, no credentials, no optional dependency.
agent = Agent("claude-opus-4-8", tools=[RegistryTools()])
```

## See also

- [Tools overview](connectors.md) — every connector at a glance (this module
  is deliberately not in that table — it isn't a connector).
- [Safety](safety.md) — the other cross-cutting, stdlib-only feature module.
