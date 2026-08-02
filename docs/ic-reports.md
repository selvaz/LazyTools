# IC Report Registry

`lazytools.ic_reports` gives producers of structured analytical reports
(regional, quantitative, per-asset-class, challenge, and final Investment
Committee reports) a place to persist **versioned, traceable** report
content — separate from, and complementary to, the
[DB registry & artifacts](registry.md) module.

!!! info "Ships in the core package"
    No extra needed beyond pydantic (already a `lazybridge` dependency).
    ```python
    from lazytools.ic_reports import ReportEnvelope, ReportScope, resolve_report_id, submit_report_version
    ```

## Why a second registry

The [Artifact Registry](registry.md) answers *"where can I find this
document again, cheaply, from another repo"* — a lightweight, generic
catalog for anything any repo wants to stash (a news digest, a backtest
report, a Tree Studio export). It doesn't, and shouldn't, know what's
*inside* those documents.

The IC Report Registry answers a narrower, different question: *"what did
this specific report conclude, how did that conclusion evolve across
versions, and what was it based on."* A report version's rendered
HTML/Markdown still lives in the Artifact Registry — linked from here via
`ArtifactRef`, never duplicated.

```
Artifact Registry                    IC Report Registry
    HTML, Markdown, JSON,                report identity, versions,
    charts, attachments, files           inputs, changes  ──▶ ArtifactRef
```

## The contract

Every report is a `ReportEnvelope` — a generic envelope every report type
shares, plus a `content` payload validated against a per-`report_type`
model:

```python
from datetime import datetime, timezone
from lazytools.ic_reports import ReportEnvelope, ReportScope, resolve_report_id, submit_report_version

report_id = resolve_report_id(
    IC_REPORTS_DB, report_type="regional_report",
    scope=ReportScope(region="europe"), title="Europe Regional Market Report",
)

envelope = ReportEnvelope(
    report_id=report_id, report_type="regional_report",
    scope=ReportScope(region="europe"), as_of=datetime.now(timezone.utc),
    title="Europe Regional Market Report", summary="Quiet week, no major dislocations.",
    run_id="run-2026-08-02", agent_id="europe-specialist",
    content={"key_events": ["ECB held rates"], "risks": ["energy prices"]},
)
version_id = submit_report_version(IC_REPORTS_DB, envelope)
```

`resolve_report_id` is idempotent on `(report_type, scope)` — call it once
per logical report series, embed the id it returns in every version's
envelope. `submit_report_version` is idempotent on `run_id` — resubmitting
the same run returns the existing `version_id` rather than creating a
duplicate, and never overwrites a version's stored content.

Only `regional_report` has a fully-validated `content` model today
(`RegionalReportContent`). Every other declared type in `REPORT_TYPES`
(`quantitative_report`, `asset_class_view`, `challenge_report`,
`ic_final_report`) is accepted at the envelope level but not yet
content-validated — add its model to `lazytools.ic_reports.models` when a
real producer for it exists, rather than guessing its shape ahead of time.

Claims and evidence are deliberately **not** separate models/tables: they
live inside each report type's own `content` payload. Promote a field to
its own table only when a real cross-report query need shows up (e.g.
"every claim with confidence < 0.5 across all reports") — until then it's
schema-migration overhead with no payoff.

## Lifecycle

```python
from lazytools.ic_reports import validate_report_version, publish_report_version

validate_report_version(IC_REPORTS_DB, version_id)   # raises on invalid content; else -> "validated"
publish_report_version(IC_REPORTS_DB, version_id)    # requires "validated"; supersedes the prior published version
```

Statuses: `draft → generated → validated → published`, with `superseded`
(a newer version was published), `rejected`, and `failed` as terminal/side
states. A version only ever moves forward — nothing here mutates a
published version's content, and `publish_report_version` refuses anything
that isn't already `validated`.

## Retrieval and comparison

```python
from lazytools.ic_reports import get_latest_report_version, get_previous_report_version, compare_report_versions, search_reports

latest = get_latest_report_version(IC_REPORTS_DB, report_id)         # the current published version
previous = get_previous_report_version(IC_REPORTS_DB, version_id)    # the one before a given version
diff = compare_report_versions(IC_REPORTS_DB, version_a, version_b)  # scalar + top-level content diff

results = search_reports(IC_REPORTS_DB, report_type="regional_report", scope_key="europe")
```

`search_reports` returns metadata rows (the `reports` table), never full
version content — same convention as the Artifact Registry's own
`search_artifacts`. `compare_report_versions` is deliberately a shallow,
generic diff (changed scalar fields + added/removed/changed top-level
`content` keys) — producers build richer, domain-aware comparisons on top
of this primitive using their own knowledge of what a "material change"
means for their report type.

## Configuration

Set via `IC_REPORTS_DB` (see `KNOWN_DBS`) — optional, like every
`*_artifacts` entry: absent, every read here returns empty/`None` rather
than erroring, and the database file is never created by a read alone.

## See also

- [DB registry & artifacts](registry.md) — the generic cross-repo catalog
  this module links to, never duplicates.
