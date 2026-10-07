"""lazytools.connectors.projects.ProjectsTools -- the MCP-facing wrapper.

test_mcp_surface_contract.py pins the exact tool-name sets; this file
exercises BEHAVIOR: the owner-default view, the brake switch reaching
through to the read-only brake_status tool, write-tool invariants, and the
one safe-by-default property that matters most for this provider --
constructing it with no configuration at all must never touch a real file.
"""

from __future__ import annotations

from lazybridge import Store

from lazytools.connectors.projects import ProjectsTools
from lazytools.projects import brake, owner


def _rw(tmp_path) -> ProjectsTools:
    return ProjectsTools(store_db=str(tmp_path / "store.sqlite"), allow_write=True)


def test_constructing_with_no_config_is_in_memory(monkeypatch) -> None:
    monkeypatch.delenv("LAZYTOOLS_PROJECTS_STORE_DB", raising=False)
    monkeypatch.delenv("LAZYCEO_CEO_STORE_DB", raising=False)
    tools = ProjectsTools()
    assert isinstance(tools._store, Store)
    assert tools._store._db is None


def test_env_var_resolution_order(tmp_path, monkeypatch) -> None:
    explicit = str(tmp_path / "explicit.sqlite")
    env_path = str(tmp_path / "env.sqlite")
    ceo_env_path = str(tmp_path / "ceo-env.sqlite")
    monkeypatch.setenv("LAZYTOOLS_PROJECTS_STORE_DB", env_path)
    monkeypatch.setenv("LAZYCEO_CEO_STORE_DB", ceo_env_path)

    # explicit wins over both env vars
    assert ProjectsTools(store_db=explicit)._store._db == explicit
    # LAZYTOOLS_PROJECTS_STORE_DB wins over LAZYCEO_CEO_STORE_DB
    assert ProjectsTools()._store._db == env_path

    monkeypatch.delenv("LAZYTOOLS_PROJECTS_STORE_DB", raising=False)
    assert ProjectsTools()._store._db == ceo_env_path


def test_read_only_default_omits_write_tools(tmp_path) -> None:
    ro = ProjectsTools(store_db=str(tmp_path / "s.sqlite"), allow_write=False)
    names = {t.name for t in ro.as_tools()}
    assert "projects_create" not in names
    assert "projects_accept_verification" not in names
    assert "projects_list" in names


def test_projects_create_defaults_owner_to_claude(tmp_path) -> None:
    pt = _rw(tmp_path)
    pt.projects_create("alpha", "Alpha", "an objective")
    record = pt.projects_get("alpha")
    assert record["owner"] == "claude"
    assert record["brake_enabled"] is True


def test_projects_list_default_view_excludes_ceo(tmp_path) -> None:
    pt = _rw(tmp_path)
    pt.projects_create("mine", "Mine", "x")
    owner.set_project_owner(pt._store, "legacy-ceo", "ceo")
    from lazytools.projects import records

    records.open_project(pt._store, project_id="legacy-ceo", title="Legacy", objective="y")

    default_view = {p["project_id"] for p in pt.projects_list()}
    assert default_view == {"mine"}
    all_view = {p["project_id"] for p in pt.projects_list(owner="all")}
    assert all_view == {"mine", "legacy-ceo"}
    ceo_view = {p["project_id"] for p in pt.projects_list(owner="ceo")}
    assert ceo_view == {"legacy-ceo"}


def test_projects_set_brake_enabled_reaches_brake_status(tmp_path) -> None:
    pt = _rw(tmp_path)
    pt.projects_create("alpha", "Alpha", "x")
    assert brake.get_project_brake_enabled(pt._store, "alpha") is True

    msg = pt.projects_set_brake_enabled("alpha", False)
    assert "OFF" in msg
    status = pt.projects_brake_status("alpha", "codex")
    assert status["brake_enabled"] is False
    assert status["would_admit"] is True


def test_projects_pause_resume_close_lifecycle(tmp_path) -> None:
    pt = _rw(tmp_path)
    pt.projects_create("alpha", "Alpha", "x")
    review = pt.projects_review_plan(
        "alpha", "obs", "2026-12-01",
        [{"text": "t0", "acceptance_criteria": ["c0 passes"]}, {"text": "t1", "acceptance_criteria": ["c1 passes"]}],
    )
    assert review["ok"] is True
    pt.projects_promote(
        "alpha", "obs", "2026-12-01",
        [{"text": "t0", "acceptance_criteria": ["c0 passes"]}, {"text": "t1", "acceptance_criteria": ["c1 passes"]}],
    )

    assert "paused project" in pt.projects_pause("alpha")
    assert "resumed project" in pt.projects_resume("alpha")

    closed_while_open_tasks = pt.projects_close("alpha")
    assert closed_while_open_tasks.startswith("REJECTED") and "unfinished task" in closed_while_open_tasks

    pt.projects_retire_task("alpha", 0, "t0", "not needed", "obsolete")
    pt.projects_retire_task("alpha", 1, "t1", "not needed", "obsolete")
    assert "closed project" in pt.projects_close("alpha")


def test_projects_set_owner_and_set_deadline(tmp_path) -> None:
    pt = _rw(tmp_path)
    pt.projects_create("alpha", "Alpha", "x")
    assert "now owned by 'shared'" in pt.projects_set_owner("alpha", "shared")
    assert pt.projects_get("alpha")["owner"] == "shared"

    bad = pt.projects_set_deadline("alpha", "not-a-date")
    assert bad.startswith("REJECTED")

    # set_project_deadline's own mechanism only allows open/paused/done, not draft
    msg = pt.projects_set_deadline("alpha", "2026-12-31T00:00:00+00:00")
    assert msg.startswith("REJECTED")


def test_projects_add_note_and_read_back(tmp_path) -> None:
    pt = _rw(tmp_path)
    pt.projects_create("alpha", "Alpha", "x")
    pt.projects_add_note("alpha", "checked in with the operator", origin="claude")
    notes = pt.projects_notes("alpha")
    assert len(notes) == 1
    assert notes[0]["note"] == "checked in with the operator"
    assert notes[0]["origin"] == "claude"


def test_projects_quota_tool_never_raises_on_unreachable_provider(tmp_path) -> None:
    """No Codex/Claude Code CLI is necessarily available in this test
    environment -- the tool must report an error field, never throw."""
    pt = _rw(tmp_path)
    result = pt.projects_quota("codex")
    assert result["engine"] == "codex"
    assert "windows" in result


def test_projects_cost_report_and_jobs_empty_for_new_project(tmp_path) -> None:
    pt = _rw(tmp_path)
    pt.projects_create("alpha", "Alpha", "x")
    report = pt.projects_cost_report("alpha")
    assert report["job_count"] == 0
    assert pt.projects_jobs("alpha") == []
