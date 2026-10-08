"""lazytools.projects.gantt -- the model-free graphical Gantt."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from lazybridge import Store
from lazybridge.ext.planners import DurableBlackboard

from lazytools.projects import gantt, records
from lazytools.projects import owner as owner_mod

NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)


def _project(store: Store, project_id: str, tasks: list[str]) -> DurableBlackboard:
    records.open_project(store, project_id=project_id, title=f"Title {project_id}", objective="x")
    records._apply(store, project_id, {"status": "open"}, allowed_from=("draft",))
    board = DurableBlackboard(store, plan_id=f"project:{project_id}")
    board.set_plan("reasoning", tasks)
    return board


def _schedule(board: DurableBlackboard, index: int, text: str, start: datetime, due: datetime) -> None:
    board.set_task_schedule(index, text, planned_start_at=start.timestamp(), due_at=due.timestamp(), reason="test")


def test_draws_a_bar_per_planned_task_and_marks_late_ones() -> None:
    store = Store()
    board = _project(store, "alpha", ["build <the> thing", "ship it", "no dates"])
    _schedule(board, 0, "build <the> thing", NOW - timedelta(days=6), NOW - timedelta(days=3))
    _schedule(board, 1, "ship it", NOW + timedelta(days=1), NOW + timedelta(days=4))

    page = gantt.render_gantt_html(store, now=NOW)

    assert page.startswith("<!doctype html>")
    assert "Title alpha" in page
    assert page.count('class="bar ') == 2  # the undated task gets no bar
    assert 'class="bar late' in page and 'class="bar open' in page
    assert "build &lt;the&gt; thing" in page  # task text is escaped, never raw markup
    assert "1 aperti senza date" in page
    assert ">oggi<" in page


def test_completed_tasks_without_a_plan_are_counted_not_drawn() -> None:
    store = Store()
    board = _project(store, "alpha", ["done without dates"])
    board.claim_task(0, "done without dates")
    board.mark_done(0, "ok")

    page = gantt.render_gantt_html(store, now=NOW)
    assert "1 chiusi senza pianificazione" in page
    assert 'class="done-mark"' not in page
    assert 'class="done-mark"' in gantt.render_gantt_html(store, now=NOW, unplanned_done=True)


def test_filters_by_status_and_owner() -> None:
    store = Store()
    _project(store, "mine", ["a"])
    _project(store, "theirs", ["b"])
    owner_mod.set_project_owner(store, "mine", "claude")

    only_claude = gantt.render_gantt_html(store, now=NOW, owners=["claude"])
    assert "Title mine" in only_claude and "Title theirs" not in only_claude
    assert "Nessun progetto da mostrare" in gantt.render_gantt_html(store, now=NOW, statuses=("done",))


def test_cli_writes_the_page(tmp_path) -> None:
    db = tmp_path / "store.sqlite"
    store = Store(db=str(db))
    _project(store, "alpha", ["a"])
    out = tmp_path / "out" / "g.html"
    assert gantt.main(["--store", str(db), "--out", str(out)]) == 0
    assert "Title alpha" in out.read_text(encoding="utf-8")


def test_mcp_tool_writes_the_page_and_returns_its_path(tmp_path) -> None:
    from lazytools.connectors.projects import ProjectsTools

    db = tmp_path / "store.sqlite"
    store = Store(db=str(db))
    _project(store, "alpha", ["a"])
    tools = ProjectsTools(store_db=str(db), allow_write=True)  # a caller-chosen out_path needs write access
    assert "projects_gantt" in {t.name for t in tools.as_tools()}  # in the default core profile
    result = tools.projects_gantt(out_path=str(tmp_path / "g.html"))
    from pathlib import Path

    assert "Title alpha" in Path(result["html_path"]).read_text(encoding="utf-8")
    assert "alpha" in result["text"]


def test_text_gantt_draws_one_character_per_day_around_today() -> None:
    store = Store()
    board = _project(store, "alpha", ["late one", "next one", "no dates"])
    _schedule(board, 0, "late one", NOW - timedelta(days=4), NOW - timedelta(days=2))
    _schedule(board, 1, "next one", NOW + timedelta(days=1), NOW + timedelta(days=3))

    text = gantt.render_gantt_text(store, now=NOW)
    lines = text.splitlines()
    late = next(line for line in lines if line.lstrip().startswith("late one"))
    upcoming = next(line for line in lines if line.lstrip().startswith("next one"))
    today_col = gantt.TEXT_LABEL + 2 + gantt.TEXT_DAYS_BACK
    assert late[today_col] == "|" and upcoming[today_col] == "|"
    assert late[today_col - 4 : today_col - 1] == "▓▓▓" and late.endswith("IN RITARDO")
    assert upcoming[today_col + 1 : today_col + 4] == "▒▒▒" and upcoming.endswith("da fare")
    assert not any(line.lstrip().startswith("no dates") for line in lines)
    assert "alpha  (0/3 fatti" in text
