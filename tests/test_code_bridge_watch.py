from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from lazybridge import Store

from lazytools.code_bridge import _watch


def _fixture() -> dict[str, Any]:
    return json.loads((Path(__file__).parent / "fixtures" / "code_bridge_watch.json").read_text(encoding="utf-8"))


def _install_api(monkeypatch: Any, payload: dict[str, Any]) -> None:
    def api(path: str) -> Any:
        if "/issues/" in path:
            return payload["issue_comments"]
        if path.endswith("/reviews"):
            return payload["reviews"]
        if path.endswith("/comments"):
            return payload["inline_comments"]
        if path.endswith("/check-runs"):
            return payload["check_runs"]
        if path.endswith("/status"):
            return payload["statuses"]
        return payload["pr"]

    monkeypatch.setattr(_watch, "_api", api)

    def paged(path: str, key: str | None = None) -> list[dict[str, Any]]:
        value = api(path)
        if key:
            value = value[key]
        return value

    monkeypatch.setattr(_watch, "_paged_items", paged)


def test_watch_detects_recorded_rest_events_and_current_head_only(monkeypatch: Any) -> None:
    payload = _fixture()
    _install_api(monkeypatch, payload)
    store = Store()

    events, terminal = _watch.poll_pull(_watch.parse_pull("acme/widgets#7"), store)

    assert not terminal
    assert [event.kind for event in events] == ["comment", "verdict", "comment", "inline", "check", "check"]
    assert events[0].detail == "codex-review-bot P1"
    assert events[1].detail == "verdict=pass head=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    assert all(event.detail != "old" for event in events)


def test_watch_restart_uses_persisted_ids_without_duplicates(monkeypatch: Any, tmp_path: Path) -> None:
    payload = _fixture()
    _install_api(monkeypatch, payload)
    db = tmp_path / "bridge.sqlite"
    pull = _watch.parse_pull("acme/widgets#7")
    with Store(db=str(db)) as store:
        first, _ = _watch.poll_pull(pull, store)
    with Store(db=str(db)) as restarted:
        second, _ = _watch.poll_pull(pull, restarted)

    assert first
    assert second == []


def test_watch_emits_head_and_terminal_transitions(monkeypatch: Any) -> None:
    payload = _fixture()
    _install_api(monkeypatch, payload)
    store = Store()
    pull = _watch.parse_pull("acme/widgets#7")
    _watch.poll_pull(pull, store)
    payload["pr"]["head"]["sha"] = "cccccccccccccccccccccccccccccccccccccccc"
    payload["pr"].update({"state": "closed", "merged": True, "merged_at": "2026-11-14T10:10:00Z"})

    events, terminal = _watch.poll_pull(pull, store)

    assert terminal
    assert [event.kind for event in events] == ["merged", "head"]


def test_watch_until_exits_zero_without_polling(monkeypatch: Any) -> None:
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_WATCH_MIN_INTERVAL", "5")
    now = datetime(2026, 11, 14, 12, tzinfo=UTC)
    called = False

    def emit(_event: _watch.WatchEvent) -> None:
        nonlocal called
        called = True

    result = _watch.run_watch([_watch.parse_pull("acme/widgets#7")], Store(), interval=1,
                              until=now, emit=emit, now=lambda: now)
    assert result == 0
    assert not called


def test_watch_never_sleeps_faster_than_configured_minimum(monkeypatch: Any) -> None:
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_WATCH_MIN_INTERVAL", "7")
    payload = _fixture()
    _install_api(monkeypatch, payload)
    sleeps: list[float] = []
    start = datetime(2026, 11, 14, 12, tzinfo=UTC)
    current = start

    def sleep(delay: float) -> None:
        nonlocal current
        sleeps.append(delay)
        current += timedelta(seconds=delay)

    result = _watch.run_watch([_watch.parse_pull("acme/widgets#7")], Store(), interval=1,
                              until=start + timedelta(seconds=14), emit=lambda event: None,
                              sleep=sleep, now=lambda: current)
    assert result == 0
    assert sleeps == [7, 7]


def test_watch_all_terminal_exits_two(monkeypatch: Any) -> None:
    payload = _fixture()
    payload["pr"].update({"state": "closed", "closed_at": "2026-11-14T10:10:00Z"})
    _install_api(monkeypatch, payload)
    result = _watch.run_watch([_watch.parse_pull("acme/widgets#7")], Store(), interval=60,
                              until=None, emit=lambda event: None)
    assert result == 2
