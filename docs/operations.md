# Operations catalog

The operations catalog is the shared index for scheduled work across the
LazyTools ecosystem. It does not replace specialist databases: market-data,
crawler and regime databases remain owned by their repositories.

## Install

On Windows, from the LazyTools checkout:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_operations.ps1
```

The installer creates `%USERPROFILE%\.lazytools\operations.sqlite` and
`%USERPROFILE%\.lazytools\artifacts\`, and persists
`LAZYTOOLS_OPERATIONS_DB` and `LAZYTOOLS_ARTIFACTS_DIR`. Pass `-DataRoot` to
choose another location. A relative `db_path`/`artifact_dir` passed directly
to `OperationsCatalog(...)` is resolved to an absolute path once, at
construction time, so it stays correct across a later working-directory
change.

For an already installed package:

```text
lazytools-operations init
lazytools-operations list-runs --limit 20
```

Set `LAZYTOOLS_OPERATIONS_DISABLED=1` to skip catalog writes entirely.
Scheduled jobs are expected to publish by default; this is for interactive/
exploratory callers (e.g. an agent driving the MCP server) that want to keep
ad-hoc or test runs out of the shared catalog and artifact store, which has
no retention/pruning policy of its own.

## Run and artifact contract

Each scheduled task creates one run and attaches all outputs to it:

```python
from lazytools.operations import OperationsCatalog

catalog = OperationsCatalog()
run_id = catalog.start_run(
    "crawler_3x_daily",
    parameters={"preset": "news_scan"},
    source_repo="LazyCrawler",
    source_db="C:/work/LazyCrawler/news.db",
)
try:
    catalog.register_report(run_id, "Crawler report", markdown, name="report.md")
    catalog.register_json(run_id, "crawl-manifest.json", manifest, kind="result")
    catalog.finish_run(run_id)
except Exception as exc:
    catalog.fail_run(run_id, str(exc))
    raise
```

Models and existing files use the same catalog:

```python
catalog.register_model(run_id, "daily-hmm", model_bytes, version="2026-07-31")
catalog.register_file(run_id, "reports/latest.html", kind="report", role="html")
```

Portfolio optimizers publish the result and weights automatically. Tree
estimates/backtests also register every constructed node, including its name,
description and complete node configuration, plus the full resolved tree
config (its `data`/`root_id`/`backtest` fields, not just the `backtest`
block) so two runs over different inputs stay distinguishable in the
catalog. This includes inline/free nodes; they do not need to be saved in
Tree Studio first.

```python
from lazytools.operations import publish_portfolio_run

publish_portfolio_run(
    "portfolio_tree_estimate",
    parameters={"name": "free-node-demo"},
    result={"terminal_weights": {"ticker:SPY": 0.6, "ticker:TLT": 0.4}},
    config={"nodes": [{
        "id": "free_1", "name": "Free node",
        "description": "Unrestricted sleeve",
    }]},
)
```

`publish_portfolio_run` (and the lower-level `publish()` it wraps) can also
attach to a run started earlier via
`lazytools.operations.integration.start()`, instead of always starting its
own -- this lets a caller register the run *before* the fallible work that
produces `result` (data loading, estimation, config parsing), so a failure
there still lands in the catalog as `status="failed"` instead of leaving no
record at all. This is how `portfolio_optimizer_run`/`_backtest` and
`portfolio_tree_estimate`/`_backtest` are wired internally; see
`src/lazytools/connectors/fin/tools.py` and `tree_tools.py` for the pattern.

The database stores metadata and relationships, including run lineage
(`RunRecord.parent_run_id`, when `start_run(parent_run_id=...)` was used).
Contents are stored once in a SHA-256 content-addressed artifact directory
-- keyed by digest alone, so identical bytes registered under different
names/extensions still dedupe to one physical file -- so repeated reports do
not create duplicate payloads. `artifacts_for_run(run_id)` returns the
complete run bundle.

## Integration rules

* Keep domain data in the owning repository's database.
* Use one `run_id` per scheduled invocation, including failures. Register the
  run *before* any fallible pre-work (universe/config resolution, DB lookups
  needed to build `parameters`), not just before the main operation -- a
  script that fails resolving its own inputs should still produce a `failed`
  record, not silence.
* Wrap the whole post-`start_run()` body in `try`/`except`, and `fail_run()`
  (or `finish_operations(..., ok=False, error=...)`) in the `except` before
  re-raising -- otherwise an exception on any path that doesn't already call
  `finish_run()`/`finish_operations()` leaves the record `running` forever.
* Record parameters, source repository, source database and relevant config or
  Git revision in `metadata`.
* Register reports, models, logs and result manifests as artifacts.
* Do not store secrets in parameters, metadata or reports.
* Do not use cross-database SQL transactions; the catalog contains references,
  not copies of specialist data.

### Domain repos (market-data-hub, LazyCrawler, ...)

The `start`/`finish`/`register_file`/`register_json` bridge logic lives once
in `lazytools.operations.integration`, not copy-pasted per repo. A domain
repo still needs its own tiny local `operations_integration.py`, though,
because the `lazytools` import itself must stay optional -- a scheduled job
has to keep running even where LazyTools isn't installed:

```python
"""Best-effort bridge from <repo> jobs to LazyTools' operations catalog."""
import sys
from typing import Any

_SOURCE_REPO = "<repo-name>"

try:
    from lazytools.operations.integration import finish, register_file, register_json
    from lazytools.operations.integration import start as _start
except ImportError:
    def start(task_name: str, *, parameters: dict[str, Any], source_db: str | None = None):
        print("Operations catalog unavailable; continuing without central run registration.",
              file=sys.stderr)
        return None, None
    def finish(catalog, run_id, *, ok, error=None): ...          # no-op
    def register_file(catalog, run_id, path, *, kind="artifact", role=None): ...  # no-op
    def register_json(catalog, run_id, name, value, *, kind="result"): ...        # no-op
else:
    def start(task_name: str, *, parameters: dict[str, Any], source_db: str | None = None):
        return _start(task_name, source_repo=_SOURCE_REPO, parameters=parameters, source_db=source_db)
```

See `market-data-hub/operations_integration.py` and
`LazyCrawler/operations_integration.py` for the real, complete versions.

SQLite WAL mode and a 30-second busy timeout support concurrent Windows Task
Scheduler processes.
