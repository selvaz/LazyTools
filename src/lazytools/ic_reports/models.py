"""The Investment Committee report contract: a generic envelope every report
type shares, plus a per-``report_type`` content schema validated separately.

Deliberately narrow for its first version: only ``regional_report`` has a
fully-validated content model below. Every other declared report type
(``quantitative_report``, ``asset_class_view``, ``challenge_report``,
``ic_final_report``) is accepted at the envelope level but not yet
content-validated -- add its model to ``_CONTENT_MODELS`` when a real
producer for it exists, rather than guessing its shape now.

Claims/evidence are deliberately NOT separate models here: they live inside
each report type's own content payload (e.g. ``RegionalReportContent``).
Promote a field to its own table/model only when a real cross-report query
need shows up (see ``lazytools.ic_reports.db`` for why).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: Every report type the contract currently knows about. A report_type not in
#: this tuple is rejected by validate_envelope -- add here first, deliberately,
#: before any producer starts emitting it.
REPORT_TYPES = (
    "regional_report",
    "quantitative_report",
    "asset_class_view",
    "challenge_report",
    "ic_final_report",
)


class ReportStatus(StrEnum):
    DRAFT = "draft"
    GENERATED = "generated"
    VALIDATED = "validated"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    FAILED = "failed"


class ReportScope(BaseModel):
    """What this report is *about*. Every field optional -- a report can be
    scoped by region, asset class, both, or neither (a global report)."""

    region: str | None = None
    asset_class: str | None = None

    model_config = ConfigDict(extra="forbid")


class InputRef(BaseModel):
    """One input this report version was produced from."""

    input_type: str  # "artifact" | "report" | "external"
    input_id: str
    source_repo: str | None = None
    #: e.g. "primary_source", "supporting_source", "quantitative_input",
    #: "previous_report", "external_evidence" -- free text, not an enum, since
    #: producers will need new roles before the contract can be revised.
    role: str

    model_config = ConfigDict(extra="forbid")


class ArtifactRef(BaseModel):
    """A rendered/derived artifact (HTML, Markdown, a chart) this report
    version produced, registered separately in the Artifact Registry."""

    artifact_id: str
    repo: str
    role: str = "primary"

    model_config = ConfigDict(extra="forbid")


class ReportChange(BaseModel):
    """One difference between this version and its predecessor."""

    change_type: str
    description: str
    drivers: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class ReportEnvelope(BaseModel):
    """The generic envelope every report type shares. ``content`` holds the
    report_type-specific payload -- validated separately by
    :func:`validate_content`, not by this model, so the envelope itself never
    needs to change when a new report type's shape is added."""

    report_id: str
    report_type: str
    scope: ReportScope = Field(default_factory=ReportScope)
    as_of: datetime
    period_start: datetime | None = None
    period_end: datetime | None = None
    title: str
    summary: str
    status: ReportStatus = ReportStatus.DRAFT
    run_id: str
    agent_id: str
    model: str | None = None
    prompt_version: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    input_refs: list[InputRef] = Field(default_factory=list)
    changes: list[ReportChange] = Field(default_factory=list)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    content: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


class RegionalReportContent(BaseModel):
    """Content shape for ``report_type="regional_report"``."""

    key_events: list[str] = Field(default_factory=list)
    structural_themes: list[str] = Field(default_factory=list)
    new_events: list[str] = Field(default_factory=list)
    resolved_events: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    watch_items: list[str] = Field(default_factory=list)
    potential_impact: str | None = None

    model_config = ConfigDict(extra="forbid")


#: report_type -> its content model. A report_type absent here is accepted at
#: the envelope level (title/summary/status/... still validated) but its
#: `content` dict is passed through unchecked -- see the module docstring.
_CONTENT_MODELS: dict[str, type[BaseModel]] = {
    "regional_report": RegionalReportContent,
}


def content_model_for(report_type: str) -> type[BaseModel] | None:
    """The content model registered for ``report_type``, or ``None`` if this
    report type doesn't have one yet (content is then unvalidated)."""
    return _CONTENT_MODELS.get(report_type)


def validate_envelope(envelope: ReportEnvelope) -> ReportEnvelope:
    """Validate ``envelope`` and, if a content model is registered for its
    ``report_type``, its ``content`` payload too.

    Raises:
        ValueError: ``report_type`` isn't one of :data:`REPORT_TYPES`.
        pydantic.ValidationError: the content payload doesn't match its
            report_type's registered content model.
    """
    if envelope.report_type not in REPORT_TYPES:
        raise ValueError(f"unknown report_type {envelope.report_type!r}; known: {REPORT_TYPES}")
    content_model = content_model_for(envelope.report_type)
    if content_model is not None:
        content_model.model_validate(envelope.content)
    return envelope
