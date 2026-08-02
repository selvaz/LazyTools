"""The Investment Committee Report Registry -- versioned, traceable storage
for structured analytical reports (regional, quantitative, per-asset-class,
challenge, and final IC reports), separate from and complementary to
:mod:`lazytools.registry` (the generic cross-repo artifact catalog).

Where the Artifact Registry answers "where can I find this document again,
cheaply, from another repo," this module answers "what did this report
actually conclude, how did that conclusion evolve across versions, and what
was it based on." A report version's rendered HTML/Markdown still lives in
the Artifact Registry -- linked from here via :class:`~lazytools.ic_reports.models.ArtifactRef`,
never duplicated.

See :mod:`lazytools.ic_reports.models` for the contract (what a report looks
like), :mod:`lazytools.ic_reports.db` for the SQLite schema, and
:mod:`lazytools.ic_reports.api` for the public functions producers/agents
call. Configured via the ``IC_REPORTS_DB`` env var (see
``lazytools.registry.db.KNOWN_DBS``); optional, like every other artifact-
style DB in this ecosystem -- absent, every read here returns empty rather
than erroring.
"""

from __future__ import annotations

from lazytools.ic_reports.api import (
    compare_report_versions,
    get_latest_report_version,
    get_previous_report_version,
    get_report,
    get_report_version,
    list_report_changes,
    list_report_inputs,
    publish_report_version,
    reject_report_version,
    resolve_report_id,
    search_reports,
    submit_report_version,
    validate_report_version,
)
from lazytools.ic_reports.models import (
    REPORT_TYPES,
    ArtifactRef,
    InputRef,
    ReportChange,
    ReportEnvelope,
    ReportScope,
    ReportStatus,
    validate_envelope,
)

__all__ = [
    "REPORT_TYPES",
    "ArtifactRef",
    "InputRef",
    "ReportChange",
    "ReportEnvelope",
    "ReportScope",
    "ReportStatus",
    "validate_envelope",
    "resolve_report_id",
    "submit_report_version",
    "validate_report_version",
    "reject_report_version",
    "publish_report_version",
    "get_report",
    "get_report_version",
    "get_latest_report_version",
    "get_previous_report_version",
    "compare_report_versions",
    "search_reports",
    "list_report_inputs",
    "list_report_changes",
]
