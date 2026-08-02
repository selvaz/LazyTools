"""The IC Report Registry's public API: bridges the Pydantic contract
(:mod:`lazytools.ic_reports.models`) to SQLite persistence
(:mod:`lazytools.ic_reports.db`).

Typical flow for a producer::

    report_id = resolve_report_id(db_path, report_type="regional_report",
                                   scope=ReportScope(region="europe"),
                                   title="Europe Regional Market Report")
    envelope = ReportEnvelope(report_id=report_id, report_type="regional_report",
                               scope=ReportScope(region="europe"), as_of=..., title=...,
                               summary=..., run_id=..., agent_id=..., content={...})
    version_id = submit_report_version(db_path, envelope)
    validate_report_version(db_path, version_id)   # raises on a bad content payload
    publish_report_version(db_path, version_id)     # requires 'validated'
"""

from __future__ import annotations

from typing import Any

from lazytools.ic_reports import db
from lazytools.ic_reports.models import ReportEnvelope, ReportScope, validate_envelope


def _flatten_scope(scope: ReportScope) -> tuple[str | None, str | None]:
    """Reduce a (possibly two-dimensional) scope to the single
    ``(scope_type, scope_key)`` pair the DB indexes on. Deterministic and
    reversible -- a scope with both dimensions set gets a composite key
    rather than silently dropping one."""
    if scope.region and scope.asset_class:
        return "region+asset_class", f"{scope.region}:{scope.asset_class}"
    if scope.region:
        return "region", scope.region
    if scope.asset_class:
        return "asset_class", scope.asset_class
    return None, None


def resolve_report_id(db_path: str, *, report_type: str, scope: ReportScope, title: str) -> str:
    """The stable logical identity for a (report_type, scope) series --
    idempotent, call this before constructing a :class:`ReportEnvelope` so
    its ``report_id`` field is populated correctly."""
    scope_type, scope_key = _flatten_scope(scope)
    return db.get_or_create_report(db_path, report_type=report_type, scope_type=scope_type, scope_key=scope_key, title=title)


def submit_report_version(db_path: str, envelope: ReportEnvelope) -> str:
    """Validate ``envelope`` and persist it as a new version. Idempotent on
    ``envelope.run_id``: resubmitting the same run returns the existing
    version_id instead of creating a duplicate.

    Raises:
        ValueError / pydantic.ValidationError: the envelope or its
            report_type-specific content is invalid.
        KeyError: ``envelope.report_id`` doesn't exist -- call
            :func:`resolve_report_id` first.
    """
    validate_envelope(envelope)
    if db.get_report(db_path, envelope.report_id) is None:
        raise KeyError(
            f"no such report_id {envelope.report_id!r} -- call resolve_report_id() first "
            "to allocate the logical report identity before submitting a version"
        )
    version_id = db.create_version(
        db_path,
        report_id=envelope.report_id,
        run_id=envelope.run_id,
        as_of=envelope.as_of.isoformat(),
        content_json=envelope.model_dump_json(),
        agent_id=envelope.agent_id,
        model=envelope.model,
        prompt_version=envelope.prompt_version,
        input_refs=[ref.model_dump() for ref in envelope.input_refs],
    )
    if envelope.changes:
        previous = db.get_previous_version(db_path, version_id)
        db.record_changes(
            db_path,
            current_version_id=version_id,
            previous_version_id=previous["version_id"] if previous else None,
            changes=[c.model_dump() for c in envelope.changes],
        )
    for ref in envelope.artifact_refs:
        db.link_artifact(db_path, version_id=version_id, artifact_id=ref.artifact_id, repo=ref.repo, role=ref.role)
    return version_id


def validate_report_version(db_path: str, version_id: str) -> None:
    """Re-validate a persisted version's content against its report_type's
    schema. Sets status to ``'validated'`` on success. On failure, RAISES
    without changing status -- the caller decides whether to mark it
    ``'rejected'`` (via :func:`reject_report_version`) or leave it as-is
    for a retry.
    """
    row = db.get_version(db_path, version_id)
    if row is None:
        raise KeyError(f"no such version_id: {version_id!r}")
    envelope = ReportEnvelope.model_validate_json(row["content_json"])
    validate_envelope(envelope)  # raises on invalid content
    db.set_version_status(db_path, version_id=version_id, status="validated")


def reject_report_version(db_path: str, version_id: str) -> None:
    db.set_version_status(db_path, version_id=version_id, status="rejected")


def publish_report_version(db_path: str, version_id: str) -> None:
    """Requires the version to already be ``'validated'`` -- publishing a
    draft/generated/rejected version is refused, matching the plan's own
    rule that a final report may only consume validated/published inputs.
    """
    row = db.get_version(db_path, version_id)
    if row is None:
        raise KeyError(f"no such version_id: {version_id!r}")
    if row["status"] != "validated":
        raise ValueError(
            f"version {version_id!r} has status {row['status']!r}, not 'validated' -- "
            "call validate_report_version() first"
        )
    db.publish_version(db_path, version_id=version_id)


def get_report(db_path: str, report_id: str) -> dict | None:
    """The logical report's own metadata row (not its content) -- see
    :func:`get_report_version` for a specific version's full content."""
    return db.get_report(db_path, report_id)


def get_report_version(db_path: str, version_id: str) -> ReportEnvelope | None:
    row = db.get_version(db_path, version_id)
    if row is None:
        return None
    return ReportEnvelope.model_validate_json(row["content_json"])


def get_latest_report_version(db_path: str, report_id: str, *, status: str | None = "published") -> ReportEnvelope | None:
    row = db.get_latest_version(db_path, report_id=report_id, status=status)
    if row is None:
        return None
    return ReportEnvelope.model_validate_json(row["content_json"])


def get_previous_report_version(db_path: str, version_id: str) -> ReportEnvelope | None:
    row = db.get_previous_version(db_path, version_id)
    if row is None:
        return None
    return ReportEnvelope.model_validate_json(row["content_json"])


def compare_report_versions(db_path: str, version_a: str, version_b: str) -> dict[str, Any]:
    """A shallow, deterministic diff between two versions of the same
    report: scalar envelope fields (title/summary/status/confidence) plus
    added/removed/changed top-level keys in ``content``. Intentionally not
    a semantic diff -- producers/agents build richer comparisons on top of
    this primitive using the actual domain knowledge of their report type.
    """
    row_a = db.get_version(db_path, version_a)
    row_b = db.get_version(db_path, version_b)
    if row_a is None or row_b is None:
        missing = version_a if row_a is None else version_b
        raise KeyError(f"no such version_id: {missing!r}")

    env_a = ReportEnvelope.model_validate_json(row_a["content_json"])
    env_b = ReportEnvelope.model_validate_json(row_b["content_json"])

    scalar_diff: dict[str, tuple[Any, Any]] = {}
    for field in ("title", "summary", "status", "confidence"):
        val_a, val_b = getattr(env_a, field), getattr(env_b, field)
        if val_a != val_b:
            scalar_diff[field] = (val_a, val_b)

    keys_a, keys_b = set(env_a.content), set(env_b.content)
    content_diff = {
        "added": sorted(keys_b - keys_a),
        "removed": sorted(keys_a - keys_b),
        "changed": sorted(k for k in keys_a & keys_b if env_a.content[k] != env_b.content[k]),
    }

    return {
        "version_a": version_a,
        "version_b": version_b,
        "scalar_diff": scalar_diff,
        "content_diff": content_diff,
    }


def search_reports(
    db_path: str,
    *,
    report_type: str | None = None,
    scope_type: str | None = None,
    scope_key: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Metadata-only (report identity rows, not full content) -- call
    :func:`get_report_version` for a specific version's full content, same
    convention as the Artifact Registry's own ``search_artifacts``."""
    return db.search_reports(
        db_path, report_type=report_type, scope_type=scope_type, scope_key=scope_key, status=status, limit=limit
    )


def list_report_inputs(db_path: str, version_id: str) -> list[dict]:
    return db.list_inputs(db_path, version_id)


def list_report_changes(db_path: str, version_id: str) -> list[dict]:
    return db.list_changes(db_path, version_id)


__all__ = [
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
