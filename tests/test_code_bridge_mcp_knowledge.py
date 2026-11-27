"""MCP provider ``knowledge`` (SPEC R7): shares the dispatcher's lessons store."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lazytools.code_bridge import _store
from lazytools.code_bridge._dispatch import Dispatcher
from lazytools.mcp_server.catalog import tool_metadata
from lazytools.mcp_server.providers import PROVIDER_FACTORIES
from lazytools.mcp_server.server import expand_tools


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "lessons" / "knowledge.sqlite"


def _tools(db: Path, *, allow_write: bool = True) -> dict[str, Any]:
    provider = PROVIDER_FACTORIES["knowledge"](allow_write, data_source={"knowledge_db": str(db)})
    return expand_tools([provider], read_only=not allow_write)


def _search(db: Path, query: str) -> list[dict[str, Any]]:
    return _tools(db)["lessons_search"].run_sync(query=query)


def _record(db: Path, **arguments: str) -> str:
    return _tools(db)["lessons_record"].run_sync(**arguments)


def _dispatcher(store: Any) -> Dispatcher:
    return Dispatcher(None, None, None, "project", knowledge_store=store)  # type: ignore[arg-type]


def test_read_only_serves_only_search(db: Path) -> None:
    for allow_write in (False, True):
        provider = PROVIDER_FACTORIES["knowledge"](allow_write, data_source={"knowledge_db": str(db)})
        assert set(expand_tools([provider], read_only=True)) == {"lessons_search"}
    assert set(_tools(db, allow_write=False)) == {"lessons_search"}
    assert set(_tools(db)) == {"lessons_search", "lessons_record"}
    # A provider built without write access never emits the writer, whatever the server allows.
    provider = PROVIDER_FACTORIES["knowledge"](False, data_source={"knowledge_db": str(db)})
    assert set(expand_tools([provider], read_only=False)) == {"lessons_search"}
    assert tool_metadata("knowledge", "lessons_search")["access"] == "read"
    assert tool_metadata("knowledge", "lessons_record")["access"] == "write"


def test_search_does_not_create_the_database(db: Path) -> None:
    assert _search(db, "anything") == []
    assert not db.parent.exists()


def test_db_path_from_config_then_env_then_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(tmp_path / "home"))

    def record(data_source: dict[str, str] | None) -> None:
        provider = PROVIDER_FACTORIES["knowledge"](True, data_source=data_source)
        expand_tools([provider], read_only=False)["lessons_record"].run_sync(topic="paths", what_worked="ok")

    monkeypatch.delenv("LAZYTOOLS_KNOWLEDGE_DB", raising=False)
    record(None)
    assert (tmp_path / "home" / ".lazytools" / "knowledge.sqlite").exists()
    monkeypatch.setenv("LAZYTOOLS_KNOWLEDGE_DB", str(tmp_path / "env.sqlite"))
    record(None)
    assert (tmp_path / "env.sqlite").exists()
    record({"knowledge_db": str(tmp_path / "config.sqlite")})
    assert (tmp_path / "config.sqlite").exists()


def test_recording_the_same_topic_revises_the_lesson(db: Path) -> None:
    assert "revision 1" in _record(db, topic="acme/repo: flaky tests", what_worked="rerun", gotchas="slow disk")
    assert "revision 2" in _record(db, topic="Acme/Repo: Flaky Tests", what_worked="isolate")

    [lesson] = _search(db, "flaky")
    assert (lesson["what_worked"], lesson["gotchas"], lesson["revision"]) == ("isolate", "slow disk", 2)


def test_tool_lesson_is_found_by_the_dispatcher(db: Path) -> None:
    _record(db, topic="acme/repo: windows paths", what_worked="use forward slashes")

    store = _store.build_store(db)
    try:
        task = _dispatcher(store)._with_lessons({"task_id": "project/t1", "text": "fix the windows paths"})
    finally:
        store.close()

    assert "acme/repo: windows paths" in task["text"]
    assert "use forward slashes" in task["text"]


def test_dispatcher_lesson_is_found_and_revised_by_the_tools(db: Path) -> None:
    task = SimpleNamespace(task_id="project/t1", data={"repo": "acme/repo"})
    store = _store.build_store(db)
    try:
        _dispatcher(store)._record_lesson(task, "engine crash", "heartbeat went stale")  # type: ignore[arg-type]
    finally:
        store.close()

    [lesson] = _search(db, "heartbeat")
    assert lesson["topic"] == "acme/repo: engine crash"

    assert "revision 2" in _record(db, topic="acme/repo: engine crash", what_worked="restart the engine")
    [lesson] = _search(db, "heartbeat")
    assert (lesson["what_worked"], lesson["gotchas"]) == ("restart the engine", "heartbeat went stale")


def test_secrets_are_redacted(db: Path) -> None:
    secret = "ghp_" + "a1B2c3D4e5F6g7H8"
    _record(db, topic=f"auth {secret}", what_worked=f"export token={secret}", gotchas=f"Bearer {secret}")

    [lesson] = _search(db, "auth")
    assert secret not in str(lesson)
    assert "[redacted]" in lesson["what_worked"]


@pytest.mark.parametrize(
    ("field", "length"), [("topic", 201), ("what_worked", 4001), ("gotchas", 4001)]
)
def test_overlong_input_is_refused(db: Path, field: str, length: int) -> None:
    arguments = {"topic": "limits", "what_worked": "ok", "gotchas": "", field: "x" * length}
    provider = PROVIDER_FACTORIES["knowledge"](True, data_source={"knowledge_db": str(db)})
    with pytest.raises(ValueError, match=field):
        provider.lessons_record(**arguments)
    assert not db.exists()


def test_rejections_of_the_knowledge_base_are_returned(db: Path) -> None:
    assert _record(db, topic="empty", what_worked="  ").startswith("REJECTED:")
    assert _record(db, topic="long " * 20, what_worked="ok").startswith("REJECTED:")
    assert _search(db, "empty long") == []
