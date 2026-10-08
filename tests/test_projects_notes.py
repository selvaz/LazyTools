from __future__ import annotations

import pytest
from lazybridge import Store

from lazytools.projects import notes


def test_checked_note_creation_reports_written_and_preserves_shape():
    store = Store()
    seen = []
    result = notes.write_project_note(store, "alpha", "hello", note_id="one", on_note=lambda *args: seen.append(args))
    assert result == notes.NoteWriteResult("one", True, ())
    assert len(seen) == 1
    raw = store.read("ceo:project-note:alpha:one")
    assert set(raw) == {"note_id", "project_id", "note", "at", "origin"}
    assert seen[0] == (store, raw)


def test_note_lost_race_reports_diagnostics_and_never_wakes(monkeypatch):
    store = Store()
    seen, messages = [], []
    cas = store.compare_and_swap

    def race(key, expected, value):
        store.write(key, {**value, "note": "winner"})
        return cas(key, expected, value)

    monkeypatch.setattr(store, "compare_and_swap", race)
    result = notes.write_project_note(store, "alpha", "loser", note_id="one", on_note=lambda *args: seen.append(args), diagnostic=messages.append)
    assert not result.written and tuple(messages) == result.diagnostics
    assert "lost CAS race" in messages[0] and seen == []
    assert notes.recent_project_notes(store, "alpha")[0]["note"] == "winner"


def test_note_hook_once_on_duplicate_and_legacy_api_returns_string():
    store = Store()
    seen = []
    kwargs = dict(note_id="one", on_note=lambda *args: seen.append(args))
    assert notes.add_project_note(store, "alpha", "hello", **kwargs) == "one"
    result = notes.write_project_note(store, "alpha", "again", **kwargs)
    assert not result.written and len(seen) == 1
    assert notes.recent_project_notes(store, "alpha")[0]["note"] == "hello"


@pytest.mark.parametrize("value,expected,warnings", [(1, 1.0, 0), ("1970-01-01T00:00:00", 0.0, 0), ("bad", 0.0, 1), (True, 0.0, 1), (None, 0.0, 1)])
def test_note_timestamp_diagnostics(value, expected, warnings):
    messages = []
    assert notes.note_timestamp(value, diagnostic=messages.append) == expected
    assert len(messages) == warnings


def test_note_reader_emits_malformed_timestamp_diagnostics_once_per_row():
    store = Store()
    store.write("ceo:project-note:alpha:one", {"note": "bad", "at": "bad"})
    store.write("ceo:project-note:alpha:two", {"note": "good", "at": 1})
    messages = []
    assert [r["note"] for r in notes.recent_project_notes(store, "alpha", diagnostic=messages.append)] == ["good", "bad"]
    assert len(messages) == 1
