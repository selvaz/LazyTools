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
choose another location.

For an already installed package:

```text
lazytools-operations init
lazytools-operations list-runs --limit 20
```

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
description and complete node configuration. This includes inline/free nodes;
they do not need to be saved in Tree Studio first.

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

The database stores metadata and relationships. Contents are stored once in a
SHA-256 content-addressed artifact directory, so repeated reports do not
create duplicate payloads. `artifacts_for_run(run_id)` returns the complete
run bundle.

## Integration rules

* Keep domain data in the owning repository's database.
* Use one `run_id` per scheduled invocation, including failures.
* Record parameters, source repository, source database and relevant config or
  Git revision in `metadata`.
* Register reports, models, logs and result manifests as artifacts.
* Do not store secrets in parameters, metadata or reports.
* Do not use cross-database SQL transactions; the catalog contains references,
  not copies of specialist data.

SQLite WAL mode and a 30-second busy timeout support concurrent Windows Task
Scheduler processes.
