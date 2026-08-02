"""IC Report Registry: contract validation (models.py), SQLite persistence
(db.py), and the bridging API (api.py). Real SQLite throughout -- no mocks
for DB logic, matching lazytools.registry's own test conventions.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from lazytools.ic_reports import api, db
from lazytools.ic_reports.models import (
    REPORT_TYPES,
    ArtifactRef,
    InputRef,
    ReportChange,
    ReportEnvelope,
    ReportScope,
    validate_envelope,
)


def _envelope(**overrides) -> ReportEnvelope:
    defaults: dict = {
        "report_id": "r1",
        "report_type": "regional_report",
        "scope": ReportScope(region="europe"),
        "as_of": datetime(2026, 8, 2, tzinfo=UTC),
        "title": "Europe Regional Market Report",
        "summary": "Quiet week, no major dislocations.",
        "run_id": "run-1",
        "agent_id": "europe-specialist",
        "content": {},
    }
    defaults.update(overrides)
    return ReportEnvelope(**defaults)


# --------------------------------------------------------------------------- #
# models.py -- contract validation
# --------------------------------------------------------------------------- #
def test_validate_envelope_accepts_valid_regional_report() -> None:
    env = _envelope(content={"key_events": ["ECB held rates"], "risks": ["energy prices"]})
    validate_envelope(env)  # must not raise


def test_validate_envelope_rejects_unknown_report_type() -> None:
    env = _envelope(report_type="not_a_real_type")
    with pytest.raises(ValueError, match="unknown report_type"):
        validate_envelope(env)


def test_validate_envelope_rejects_invalid_regional_content() -> None:
    env = _envelope(content={"key_events": "should be a list, not a string"})
    with pytest.raises(ValidationError):
        validate_envelope(env)


def test_validate_envelope_rejects_unknown_content_field_for_regional_report() -> None:
    """RegionalReportContent forbids extra fields -- a typo'd key must fail
    loudly, not be silently ignored."""
    env = _envelope(content={"key_evnets": ["typo'd field name"]})
    with pytest.raises(ValidationError):
        validate_envelope(env)


def test_validate_envelope_passes_through_unvalidated_content_for_types_without_a_model() -> None:
    """quantitative_report has no registered content model yet -- any content
    dict is accepted at this layer (a real producer adds the model when it
    exists, per the module docstring)."""
    env = _envelope(report_type="quantitative_report", content={"anything": "goes", "for_now": True})
    validate_envelope(env)  # must not raise


def test_all_report_types_are_declared() -> None:
    assert "regional_report" in REPORT_TYPES
    assert "quantitative_report" in REPORT_TYPES
    assert "asset_class_view" in REPORT_TYPES
    assert "challenge_report" in REPORT_TYPES
    assert "ic_final_report" in REPORT_TYPES


def test_report_scope_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ReportScope(region="europe", sector="tech")  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# db.py -- SQLite persistence
# --------------------------------------------------------------------------- #
def test_get_or_create_report_is_idempotent_on_type_and_scope(tmp_path) -> None:
    db_path = str(tmp_path / "ic.db")
    first = db.get_or_create_report(db_path, report_type="regional_report", scope_type="region", scope_key="europe", title="Europe")
    second = db.get_or_create_report(db_path, report_type="regional_report", scope_type="region", scope_key="europe", title="Europe (again)")
    assert first == second


def test_get_or_create_report_distinguishes_different_scopes(tmp_path) -> None:
    db_path = str(tmp_path / "ic.db")
    europe = db.get_or_create_report(db_path, report_type="regional_report", scope_type="region", scope_key="europe", title="Europe")
    asia = db.get_or_create_report(db_path, report_type="regional_report", scope_type="region", scope_key="asia", title="Asia")
    assert europe != asia


def test_create_version_is_idempotent_on_run_id(tmp_path) -> None:
    db_path = str(tmp_path / "ic.db")
    report_id = db.get_or_create_report(db_path, report_type="regional_report", scope_type="region", scope_key="europe", title="Europe")
    first = db.create_version(
        db_path, report_id=report_id, run_id="run-1", as_of="2026-08-02T00:00:00+00:00",
        content_json="{}", agent_id="a", model=None, prompt_version=None, input_refs=[],
    )
    second = db.create_version(
        db_path, report_id=report_id, run_id="run-1", as_of="2026-08-02T00:00:00+00:00",
        content_json='{"different": "content"}', agent_id="a", model=None, prompt_version=None, input_refs=[],
    )
    assert first == second
    # The second call's different content must NOT have overwritten the first.
    stored = db.get_version(db_path, first)
    assert stored["content_json"] == "{}"


def test_create_version_increments_version_number(tmp_path) -> None:
    db_path = str(tmp_path / "ic.db")
    report_id = db.get_or_create_report(db_path, report_type="regional_report", scope_type="region", scope_key="europe", title="Europe")
    v1 = db.create_version(db_path, report_id=report_id, run_id="run-1", as_of="2026-08-01T00:00:00+00:00", content_json="{}", agent_id="a", model=None, prompt_version=None, input_refs=[])
    v2 = db.create_version(db_path, report_id=report_id, run_id="run-2", as_of="2026-08-02T00:00:00+00:00", content_json="{}", agent_id="a", model=None, prompt_version=None, input_refs=[])
    assert db.get_version(db_path, v1)["version_number"] == 1
    assert db.get_version(db_path, v2)["version_number"] == 2


def test_publish_version_supersedes_the_previous_published_version(tmp_path) -> None:
    db_path = str(tmp_path / "ic.db")
    report_id = db.get_or_create_report(db_path, report_type="regional_report", scope_type="region", scope_key="europe", title="Europe")
    v1 = db.create_version(db_path, report_id=report_id, run_id="run-1", as_of="2026-08-01T00:00:00+00:00", content_json="{}", agent_id="a", model=None, prompt_version=None, input_refs=[])
    v2 = db.create_version(db_path, report_id=report_id, run_id="run-2", as_of="2026-08-02T00:00:00+00:00", content_json="{}", agent_id="a", model=None, prompt_version=None, input_refs=[])

    db.publish_version(db_path, version_id=v1)
    assert db.get_version(db_path, v1)["status"] == "published"
    assert db.get_report(db_path, report_id)["current_version_id"] == v1

    db.publish_version(db_path, version_id=v2)
    assert db.get_version(db_path, v1)["status"] == "superseded"
    assert db.get_version(db_path, v2)["status"] == "published"
    assert db.get_report(db_path, report_id)["current_version_id"] == v2


def test_publish_version_raises_for_unknown_version(tmp_path) -> None:
    db_path = str(tmp_path / "ic.db")
    with pytest.raises(KeyError):
        db.publish_version(db_path, version_id="not-a-real-version")


def test_get_latest_version_filters_by_status(tmp_path) -> None:
    db_path = str(tmp_path / "ic.db")
    report_id = db.get_or_create_report(db_path, report_type="regional_report", scope_type="region", scope_key="europe", title="Europe")
    v1 = db.create_version(db_path, report_id=report_id, run_id="run-1", as_of="2026-08-01T00:00:00+00:00", content_json="{}", agent_id="a", model=None, prompt_version=None, input_refs=[])
    db.create_version(db_path, report_id=report_id, run_id="run-2", as_of="2026-08-02T00:00:00+00:00", content_json="{}", agent_id="a", model=None, prompt_version=None, input_refs=[])

    db.publish_version(db_path, version_id=v1)
    latest_published = db.get_latest_version(db_path, report_id=report_id, status="published")
    assert latest_published["version_id"] == v1

    latest_any = db.get_latest_version(db_path, report_id=report_id, status=None)
    assert latest_any["version_number"] == 2  # v2 exists but isn't published


def test_get_previous_version(tmp_path) -> None:
    db_path = str(tmp_path / "ic.db")
    report_id = db.get_or_create_report(db_path, report_type="regional_report", scope_type="region", scope_key="europe", title="Europe")
    v1 = db.create_version(db_path, report_id=report_id, run_id="run-1", as_of="2026-08-01T00:00:00+00:00", content_json="{}", agent_id="a", model=None, prompt_version=None, input_refs=[])
    v2 = db.create_version(db_path, report_id=report_id, run_id="run-2", as_of="2026-08-02T00:00:00+00:00", content_json="{}", agent_id="a", model=None, prompt_version=None, input_refs=[])

    assert db.get_previous_version(db_path, v2)["version_id"] == v1
    assert db.get_previous_version(db_path, v1) is None


def test_search_reports_filters(tmp_path) -> None:
    db_path = str(tmp_path / "ic.db")
    db.get_or_create_report(db_path, report_type="regional_report", scope_type="region", scope_key="europe", title="Europe")
    db.get_or_create_report(db_path, report_type="regional_report", scope_type="region", scope_key="asia", title="Asia")
    db.get_or_create_report(db_path, report_type="quantitative_report", scope_type=None, scope_key=None, title="Quant")

    regional = db.search_reports(db_path, report_type="regional_report")
    assert {r["scope_key"] for r in regional} == {"europe", "asia"}

    europe_only = db.search_reports(db_path, report_type="regional_report", scope_key="europe")
    assert len(europe_only) == 1


def test_search_reports_rejects_non_positive_limit(tmp_path) -> None:
    db_path = str(tmp_path / "ic.db")
    with pytest.raises(ValueError):
        db.search_reports(db_path, limit=0)


def test_record_changes_and_list_changes(tmp_path) -> None:
    db_path = str(tmp_path / "ic.db")
    report_id = db.get_or_create_report(db_path, report_type="regional_report", scope_type="region", scope_key="europe", title="Europe")
    v1 = db.create_version(db_path, report_id=report_id, run_id="run-1", as_of="2026-08-01T00:00:00+00:00", content_json="{}", agent_id="a", model=None, prompt_version=None, input_refs=[])
    v2 = db.create_version(db_path, report_id=report_id, run_id="run-2", as_of="2026-08-02T00:00:00+00:00", content_json="{}", agent_id="a", model=None, prompt_version=None, input_refs=[])

    db.record_changes(db_path, current_version_id=v2, previous_version_id=v1, changes=[
        {"change_type": "new_risk", "description": "energy prices spiked", "drivers": ["oil supply"]},
    ])
    changes = db.list_changes(db_path, v2)
    assert len(changes) == 1
    assert changes[0]["change_type"] == "new_risk"


def test_link_artifact_and_list_inputs(tmp_path) -> None:
    db_path = str(tmp_path / "ic.db")
    report_id = db.get_or_create_report(db_path, report_type="regional_report", scope_type="region", scope_key="europe", title="Europe")
    v1 = db.create_version(
        db_path, report_id=report_id, run_id="run-1", as_of="2026-08-01T00:00:00+00:00", content_json="{}",
        agent_id="a", model=None, prompt_version=None,
        input_refs=[{"input_type": "artifact", "input_id": "abc123", "source_repo": "lazycrawler", "role": "primary_source"}],
    )
    db.link_artifact(db_path, version_id=v1, artifact_id="html-abc", repo="lazytools", role="html_render")

    inputs = db.list_inputs(db_path, v1)
    assert len(inputs) == 1
    assert inputs[0]["role"] == "primary_source"


# --------------------------------------------------------------------------- #
# Read paths on a missing/uninitialized DB file -- must return empty/None,
# never create the file. Mirrors registry.artifacts' own read-path tests.
# --------------------------------------------------------------------------- #
def test_reads_on_missing_db_file_return_empty_and_create_nothing(tmp_path) -> None:
    db_path = str(tmp_path / "never_written.db")
    assert db.get_report(db_path, "anything") is None
    assert db.get_version(db_path, "anything") is None
    assert db.get_latest_version(db_path, report_id="anything") is None
    assert db.get_previous_version(db_path, "anything") is None
    assert db.search_reports(db_path) == []
    assert db.list_inputs(db_path, "anything") == []
    assert db.list_changes(db_path, "anything") == []
    assert not (tmp_path / "never_written.db").exists()


def test_reads_on_uninitialized_existing_file_return_empty(tmp_path) -> None:
    db_path = tmp_path / "uninitialized.db"
    db_path.touch()
    assert db.get_report(str(db_path), "anything") is None
    assert db.search_reports(str(db_path)) == []


# --------------------------------------------------------------------------- #
# api.py -- the full lifecycle
# --------------------------------------------------------------------------- #
def test_full_lifecycle_resolve_submit_validate_publish(tmp_path) -> None:
    db_path = str(tmp_path / "ic.db")
    report_id = api.resolve_report_id(db_path, report_type="regional_report", scope=ReportScope(region="europe"), title="Europe")

    env = _envelope(report_id=report_id, content={"key_events": ["ECB held rates"]})
    version_id = api.submit_report_version(db_path, env)

    assert api.get_report_version(db_path, version_id).report_id == report_id

    with pytest.raises(ValueError, match="not 'validated'"):
        api.publish_report_version(db_path, version_id)

    api.validate_report_version(db_path, version_id)
    api.publish_report_version(db_path, version_id)

    published = api.get_latest_report_version(db_path, report_id)
    assert published.run_id == env.run_id


def test_submit_report_version_requires_resolve_report_id_first(tmp_path) -> None:
    db_path = str(tmp_path / "ic.db")
    env = _envelope(report_id="never-resolved")
    with pytest.raises(KeyError, match="resolve_report_id"):
        api.submit_report_version(db_path, env)


def test_submit_report_version_rejects_invalid_content(tmp_path) -> None:
    db_path = str(tmp_path / "ic.db")
    report_id = api.resolve_report_id(db_path, report_type="regional_report", scope=ReportScope(region="europe"), title="Europe")
    env = _envelope(report_id=report_id, content={"key_events": "not a list"})
    with pytest.raises(ValidationError):
        api.submit_report_version(db_path, env)


def test_validate_report_version_succeeds_for_valid_content(tmp_path) -> None:
    db_path = str(tmp_path / "ic.db")
    report_id = api.resolve_report_id(db_path, report_type="regional_report", scope=ReportScope(region="europe"), title="Europe")
    version_id = api.submit_report_version(db_path, _envelope(report_id=report_id))
    assert db.get_version(db_path, version_id)["status"] == "generated"
    api.validate_report_version(db_path, version_id)
    assert db.get_version(db_path, version_id)["status"] == "validated"


def test_validate_report_version_raises_on_bad_content_without_changing_status(tmp_path) -> None:
    """A version can end up with content that fails re-validation (e.g. it
    was written directly at the db layer, bypassing submit_report_version's
    own check, or the report_type's schema changed since it was submitted).
    validate_report_version must raise, not silently mark it validated, and
    must not corrupt its stored status."""
    db_path = str(tmp_path / "ic.db")
    report_id = db.get_or_create_report(db_path, report_type="regional_report", scope_type="region", scope_key="europe", title="Europe")
    # Bypass submit_report_version's own validation entirely, to simulate
    # content that's invalid for its report_type already being in the DB
    # (e.g. written directly, or valid when submitted but the schema since
    # tightened).
    bad_envelope_dict = _envelope(report_id=report_id).model_dump(mode="json")
    bad_envelope_dict["content"] = {"key_events": "not a list"}
    version_id = db.create_version(
        db_path, report_id=report_id, run_id="bad-run", as_of="2026-08-02T00:00:00+00:00",
        content_json=json.dumps(bad_envelope_dict), agent_id="a", model=None, prompt_version=None, input_refs=[],
    )
    with pytest.raises(ValidationError):
        api.validate_report_version(db_path, version_id)
    assert db.get_version(db_path, version_id)["status"] == "generated"


def test_reject_report_version(tmp_path) -> None:
    db_path = str(tmp_path / "ic.db")
    report_id = api.resolve_report_id(db_path, report_type="regional_report", scope=ReportScope(region="europe"), title="Europe")
    version_id = api.submit_report_version(db_path, _envelope(report_id=report_id))
    api.reject_report_version(db_path, version_id)
    assert db.get_version(db_path, version_id)["status"] == "rejected"


def test_compare_report_versions_reports_scalar_and_content_diffs(tmp_path) -> None:
    db_path = str(tmp_path / "ic.db")
    report_id = api.resolve_report_id(db_path, report_type="regional_report", scope=ReportScope(region="europe"), title="Europe")
    v1 = api.submit_report_version(db_path, _envelope(report_id=report_id, run_id="run-1", confidence=0.5, content={"key_events": ["A"]}))
    v2 = api.submit_report_version(db_path, _envelope(report_id=report_id, run_id="run-2", confidence=0.8, content={"key_events": ["A", "B"], "risks": ["C"]}))

    diff = api.compare_report_versions(db_path, v1, v2)
    assert diff["scalar_diff"]["confidence"] == (0.5, 0.8)
    assert diff["content_diff"]["added"] == ["risks"]
    assert diff["content_diff"]["changed"] == ["key_events"]


def test_compare_report_versions_raises_for_unknown_version(tmp_path) -> None:
    db_path = str(tmp_path / "ic.db")
    report_id = api.resolve_report_id(db_path, report_type="regional_report", scope=ReportScope(region="europe"), title="Europe")
    v1 = api.submit_report_version(db_path, _envelope(report_id=report_id))
    with pytest.raises(KeyError):
        api.compare_report_versions(db_path, v1, "not-a-real-version")


def test_submit_report_version_persists_input_refs_and_artifact_links(tmp_path) -> None:
    db_path = str(tmp_path / "ic.db")
    report_id = api.resolve_report_id(db_path, report_type="regional_report", scope=ReportScope(region="europe"), title="Europe")
    env = _envelope(
        report_id=report_id,
        input_refs=[InputRef(input_type="artifact", input_id="crawl-1", source_repo="lazycrawler", role="primary_source")],
        artifact_refs=[ArtifactRef(artifact_id="html-1", repo="lazytools", role="html_render")],
    )
    version_id = api.submit_report_version(db_path, env)

    inputs = api.list_report_inputs(db_path, version_id)
    assert inputs[0]["input_id"] == "crawl-1"


def test_submit_report_version_records_declared_changes(tmp_path) -> None:
    db_path = str(tmp_path / "ic.db")
    report_id = api.resolve_report_id(db_path, report_type="regional_report", scope=ReportScope(region="europe"), title="Europe")
    api.submit_report_version(db_path, _envelope(report_id=report_id, run_id="run-1"))
    v2 = api.submit_report_version(db_path, _envelope(
        report_id=report_id, run_id="run-2",
        changes=[ReportChange(change_type="new_risk", description="energy spike", drivers=["oil"])],
    ))
    changes = api.list_report_changes(db_path, v2)
    assert len(changes) == 1
    assert changes[0]["change_type"] == "new_risk"


def test_search_reports_is_metadata_only(tmp_path) -> None:
    db_path = str(tmp_path / "ic.db")
    report_id = api.resolve_report_id(db_path, report_type="regional_report", scope=ReportScope(region="europe"), title="Europe")
    api.submit_report_version(db_path, _envelope(report_id=report_id))
    results = api.search_reports(db_path, report_type="regional_report")
    assert len(results) == 1
    assert "content_json" not in results[0]  # metadata table row, not version content
