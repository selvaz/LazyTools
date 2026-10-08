"""lazytools.projects.records -- the durable project registry."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from lazybridge import Store

from lazytools.projects import owner as owner_mod
from lazytools.projects import records


def _store() -> Store:
    return Store()  # in-memory


def test_open_project_starts_as_draft() -> None:
    store = _store()
    record = records.open_project(store, project_id="alpha", title="Alpha", objective="do a thing")
    assert record.status == "draft"
    assert records.get_project(store, "alpha").status == "draft"


def test_open_project_rejects_duplicate_id() -> None:
    store = _store()
    records.open_project(store, project_id="alpha", title="Alpha", objective="x")
    with pytest.raises(ValueError, match="already registered"):
        records.open_project(store, project_id="alpha", title="Again", objective="y")


def test_open_project_rejects_invalid_id() -> None:
    store = _store()
    with pytest.raises(ValueError):
        records.open_project(store, project_id="Not-Valid", title="Alpha", objective="x")


def test_open_project_requires_full_classification_tuple() -> None:
    store = _store()
    with pytest.raises(ValueError, match="must all be provided together"):
        records.open_project(store, project_id="alpha", title="A", objective="x", size="small")


def test_open_project_rejects_process_below_required_rank() -> None:
    store = _store()
    with pytest.raises(ValueError, match="requires"):
        records.open_project(
            store, project_id="alpha", title="A", objective="x", size="large", risk="low", process="inline"
        )


def test_list_projects_filters_by_status_oldest_first() -> None:
    store = _store()
    records.open_project(store, project_id="proj-a", title="A", objective="x")
    records.open_project(store, project_id="proj-b", title="B", objective="y")
    records._apply(store, "proj-a", {"status": "open"}, allowed_from=("draft",))
    listed = records.list_projects(store, status="open")
    assert [r.project_id for r in listed] == ["proj-a"]
    assert [r.project_id for r in records.list_projects(store)] == ["proj-a", "proj-b"]


def test_list_projects_owner_filter_defaults_legacy_to_ceo() -> None:
    store = _store()
    records.open_project(store, project_id="legacy", title="L", objective="x")
    records.open_project(store, project_id="claude-one", title="C", objective="y")
    owner_mod.set_project_owner(store, "claude-one", "claude")

    assert [r.project_id for r in records.list_projects(store, owner="ceo")] == ["legacy"]
    assert [r.project_id for r in records.list_projects(store, owner="claude")] == ["claude-one"]
    assert {r.project_id for r in records.list_projects(store)} == {"legacy", "claude-one"}


def test_pause_resume_close_lifecycle() -> None:
    store = _store()
    records.open_project(store, project_id="alpha", title="A", objective="x")
    records._apply(store, "alpha", {"status": "open"}, allowed_from=("draft",))

    assert records.pause_project(store, "alpha") is True
    assert records.get_project(store, "alpha").status == "paused"
    # pausing a draft is refused
    assert records.pause_project(store, "nope") is False

    assert records.resume_project(store, "alpha") is True
    assert records.get_project(store, "alpha").status == "open"

    assert records.close_project(store, "alpha") is True
    assert records.get_project(store, "alpha").status == "done"
    # closing twice is successful
    assert records.close_project(store, "alpha") is True


def test_set_project_deadline_requires_timezone_aware() -> None:
    store = _store()
    records.open_project(store, project_id="alpha", title="A", objective="x")
    records._apply(store, "alpha", {"status": "open"}, allowed_from=("draft",))
    with pytest.raises(ValueError, match="timezone-aware"):
        records.set_project_deadline(store, "alpha", target_completion_at=datetime(2026, 1, 1))

    ok = records.set_project_deadline(
        store, "alpha", target_completion_at=datetime(2026, 1, 1, tzinfo=UTC), schedule_timezone="Europe/Rome"
    )
    assert ok is True
    record = records.get_project(store, "alpha")
    assert record.target_completion_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert record.schedule_timezone == "Europe/Rome"


def test_set_project_deadline_rejects_bad_timezone() -> None:
    store = _store()
    records.open_project(store, project_id="alpha", title="A", objective="x")
    records._apply(store, "alpha", {"status": "open"}, allowed_from=("draft",))
    with pytest.raises(ValueError, match="IANA timezone"):
        records.set_project_deadline(
            store, "alpha", target_completion_at=datetime(2026, 1, 1, tzinfo=UTC), schedule_timezone="Not/AZone"
        )


def test_touch_project_progress_only_applies_when_open() -> None:
    store = _store()
    records.open_project(store, project_id="alpha", title="A", objective="x")
    # draft: silently does nothing (best-effort, never raises)
    records.touch_project_progress(store, "alpha")
    assert records.get_project(store, "alpha").last_progress_at is None

    records._apply(store, "alpha", {"status": "open"}, allowed_from=("draft",))
    records.touch_project_progress(store, "alpha")
    progressed = records.get_project(store, "alpha").last_progress_at
    assert progressed is not None
    assert abs((datetime.now(UTC) - progressed).total_seconds()) < 10


def test_adopt_existing_project_grandfathers_into_open() -> None:
    store = _store()
    adopted = records.adopt_existing_project(store, adoption_reason="migrating legacy work", project_id="legacy", title="L", objective="x")
    assert adopted.status == "open"
    assert adopted.classification_rationale == "migrating legacy work"


def test_apply_round_trips_unknown_fields_extra_allow() -> None:
    """The concurrency-safety property docs/projects.md relies on: a record
    written by a model that knows MORE fields than ``ProjectRecord`` (e.g. an
    existing LazyCEO install's own autonomy_level/paused_specialists) must
    come back out of ``_apply`` with those fields intact."""
    store = _store()
    key = records._key("ceo-legacy")
    now = datetime.now(UTC).isoformat()
    store.write(
        key,
        {
            "project_id": "ceo-legacy",
            "title": "Legacy",
            "objective": "x",
            "status": "open",
            "created_at": now,
            "autonomy_level": "supervised",
            "paused_specialists": ["worker-a"],
        },
    )
    assert records.touch_project_progress(store, "ceo-legacy") is None  # best-effort, no return value
    raw = store.read(key)
    assert raw["autonomy_level"] == "supervised"
    assert raw["paused_specialists"] == ["worker-a"]
    assert raw["last_progress_at"] is not None


def test_project_classification_suffix() -> None:
    store = _store()
    record = records.open_project(
        store, project_id="alpha", title="A", objective="x", size="small", risk="low", process="inline",
        classification_rationale="simple one-off",
    )
    suffix = records.project_classification_suffix(record)
    assert "size=small" in suffix and "risk=low" in suffix and "process=inline" in suffix
    assert "simple one-off" in suffix
