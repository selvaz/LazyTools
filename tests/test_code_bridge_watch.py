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


def test_watch_marks_verdict_for_old_marker_head_stale(monkeypatch: Any) -> None:
    payload = _fixture()
    payload["issue_comments"][1]["body"] = payload["issue_comments"][1]["body"].replace("bbbbbbbb", "aaaaaaaa")
    _install_api(monkeypatch, payload)

    events, _ = _watch.poll_pull(_watch.parse_pull("acme/widgets#7"), Store())

    verdict = next(event for event in events if event.kind == "verdict")
    assert verdict.detail.endswith("stale=true")


def test_watch_does_not_persist_events_when_emit_fails(monkeypatch: Any) -> None:
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_WATCH_MIN_INTERVAL", "1")
    payload = _fixture()
    _install_api(monkeypatch, payload)
    store = Store()
    pull = _watch.parse_pull("acme/widgets#7")

    def fail(_event: _watch.WatchEvent) -> None:
        raise RuntimeError("output closed")

    try:
        _watch.run_watch([pull], store, interval=1, until=None, emit=fail)
    except RuntimeError:
        pass
    else:
        raise AssertionError("emit failure was not propagated")

    events, _ = _watch.poll_pull(pull, store)
    assert events


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


def test_watch_skips_terminal_pull_while_another_remains_open(monkeypatch: Any) -> None:
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_WATCH_MIN_INTERVAL", "1")
    first = _watch.parse_pull("acme/widgets#7")
    second = _watch.parse_pull("acme/gadgets#8")
    calls: list[str] = []

    def poll(pull: _watch.PullRequest, _store: Store):
        calls.append(pull.label)
        done = pull == first or calls.count(second.label) == 2
        return [], done, {"head": "abc", "seen": [], "terminal": "closed" if done else None}

    monkeypatch.setattr(_watch, "_poll_pull", poll)
    result = _watch.run_watch([first, second], Store(), interval=1, until=None,
                              emit=lambda event: None, sleep=lambda delay: None)

    assert result == 2
    assert calls == [first.label, second.label, second.label]


def test_watch_retries_transient_poll_failures_with_backoff(monkeypatch: Any) -> None:
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_WATCH_MIN_INTERVAL", "7")
    attempts = 0
    sleeps: list[float] = []

    def poll(_pull: _watch.PullRequest, _store: Store):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("temporary gh failure")
        return [], True, {"head": "abc", "seen": [], "terminal": "closed"}

    monkeypatch.setattr(_watch, "_poll_pull", poll)
    result = _watch.run_watch([_watch.parse_pull("acme/widgets#7")], Store(), interval=1,
                              until=None, emit=lambda event: None, sleep=sleeps.append)

    assert result == 2
    assert sleeps == [7, 14]


def test_watch_raises_after_three_consecutive_poll_failures(monkeypatch: Any) -> None:
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_WATCH_MIN_INTERVAL", "1")
    monkeypatch.setattr(_watch, "_poll_pull", lambda pull, store: (_ for _ in ()).throw(RuntimeError("gh down")))

    try:
        _watch.run_watch([_watch.parse_pull("acme/widgets#7")], Store(), interval=1,
                         until=None, emit=lambda event: None, sleep=lambda delay: None)
    except RuntimeError as exc:
        assert str(exc) == "gh down"
    else:
        raise AssertionError("three consecutive failures did not stop the watch")
