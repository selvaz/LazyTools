from __future__ import annotations

import pytest
from lazybridge import Store
from pydantic import Field, ValidationError

from lazytools.projects import records


class CallerProject(records.ProjectRecord):
    autonomy_level: str | None = None
    paused_specialists: list[str] = Field(default_factory=list)


def test_creation_factory_sets_typed_defaults_in_initial_cas(monkeypatch):
    store = Store()
    cas = store.compare_and_swap
    writes = []

    def capture(key, expected, value):
        writes.append((key, expected, value))
        return cas(key, expected, value)

    monkeypatch.setattr(store, "compare_and_swap", capture)
    project = records.open_project(store, project_id="alpha", title="Alpha", objective="files", record_factory=CallerProject.model_validate)
    assert isinstance(project, CallerProject)
    assert len(writes) == 1 and writes[0][1] is None
    assert writes[0][2]["autonomy_level"] is None and writes[0][2]["paused_specialists"] == []
    assert records.get_project(store, "alpha").model_dump()["paused_specialists"] == []


def test_creation_extra_fields_survive_shared_mutations_and_adoption():
    store = Store()
    project = records.adopt_existing_project(store, project_id="alpha", title="Alpha", objective="files", adoption_reason="existing",
        extra_fields={"autonomy_level": "supervised", "custom": {"a": 1}}, record_factory=CallerProject.model_validate)
    assert project.status == "open" and project.model_dump()["autonomy_level"] == "supervised"
    records.pause_project(store, "alpha")
    raw = store.read("ceo:project:alpha")
    assert raw["autonomy_level"] == "supervised" and raw["custom"] == {"a": 1}


def test_creation_default_shape_has_no_caller_fields():
    store = Store()
    records.open_project(store, project_id="alpha", title="Alpha", objective="files")
    assert set(store.read("ceo:project:alpha")) == set(records.ProjectRecord.model_fields)


def test_factory_validation_failure_does_not_consume_id():
    store = Store()
    with pytest.raises(ValidationError):
        records.open_project(store, project_id="alpha", title="Alpha", objective="files", extra_fields={"paused_specialists": 3}, record_factory=CallerProject.model_validate)
    assert records.get_project(store, "alpha") is None
    assert records.open_project(store, project_id="alpha", title="Alpha", objective="files").status == "draft"


@pytest.mark.parametrize("field,value", [("project_id", "beta"), ("status", "open"), ("risk", "invalid")])
def test_extra_fields_cannot_override_validated_fields(field, value):
    store = Store()
    with pytest.raises(ValueError, match="cannot override"):
        records.open_project(store, project_id="alpha", title="Alpha", objective="files", extra_fields={field: value})
    assert records.get_project(store, "alpha") is None


def test_factory_cannot_bypass_draft_or_identity():
    store = Store()

    def bad_factory(raw):
        return CallerProject.model_validate({**raw, "status": "open"})

    with pytest.raises(ValueError, match="cannot change"):
        records.open_project(store, project_id="alpha", title="Alpha", objective="files", record_factory=bad_factory)
    assert records.get_project(store, "alpha") is None
