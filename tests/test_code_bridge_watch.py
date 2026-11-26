from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
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


def test_event_order_uses_real_time_not_text_across_offsets() -> None:
    # 09:59 at UTC-8 is 17:59 UTC, after 10:10 UTC; text order would put it first.
    later_local = "2026-11-14T09:59:00-08:00"
    earlier_utc = "2026-11-14T10:10:00Z"
    ordered = sorted([later_local, earlier_utc], key=_watch._time_key)
    assert ordered == [earlier_utc, later_local]


def _bot_payload(head: str, comments: list[str], checks: list[tuple[str, str]],
                 mergeable: bool | None = True) -> dict[str, Any]:
    return {
        "pr": {"head": {"sha": head}, "state": "open", "merged": False, "mergeable": mergeable,
               "mergeable_state": "clean" if mergeable else "dirty", "title": "t"},
        "issue_comments": [{"id": i, "body": body, "created_at": "2026-11-26T10:00:00Z",
                            "user": {"login": "chatgpt-codex-connector[bot]"}} for i, body in enumerate(comments)],
        "reviews": [], "inline_comments": [],
        "check_runs": {"check_runs": [{"id": i, "name": n, "status": "completed", "conclusion": c,
                                       "completed_at": "2026-11-26T10:00:00Z"} for i, (n, c) in enumerate(checks)]},
        "statuses": {"statuses": []},
    }


def test_watch_reports_bot_verdict_and_ready_for_the_current_head(monkeypatch: Any) -> None:
    head = "abcdef1234567890"
    state = ('<!-- codex-review-state:v1 {"code_review":{"commit_sha":"' + head + '","has_any_findings":false,'
             '"has_unresolved_findings":false,"status":"completed"}} -->')
    _install_api(monkeypatch, _bot_payload(head, [state], [("test", "success"), ("lint", "success")]))
    events, _ = _watch.poll_pull(_watch.parse_pull("o/r#1"), Store())
    kinds = {event.kind: event.detail for event in events}
    assert kinds["review"] == "abcdef1 clean"
    assert kinds["ready"] == "abcdef1 checks=2 review=clean"


def test_watch_red_checks_are_not_ready_and_receipt_is_parsed(monkeypatch: Any) -> None:
    head = "1234567abcdef"
    receipt = "Codex Review: Didn't find any major issues.\n\n**Reviewed commit:** `1234567abc`"
    _install_api(monkeypatch, _bot_payload(head, [receipt], [("test", "failure")]))
    events, _ = _watch.poll_pull(_watch.parse_pull("o/r#2"), Store())
    kinds = {event.kind: event.detail for event in events}
    assert kinds["review"] == "1234567 clean"
    assert "ready" not in kinds


@pytest.mark.parametrize(("body", "ready"), [
    ("P0: fix urgently", False),
    ("[P1] fix this", False),
    ("**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow)</sub></sub> Add a test**", True),
    ("[p3] simplify this", True),
    ("Unknown severity: this could be more urgent than P2", False),
    (None, False),
])
def test_watch_ready_uses_current_head_inline_severity(monkeypatch: Any, body: str | None, ready: bool) -> None:
    head = "1234567abcdef"
    state = ('<!-- codex-review-state:v1 {"code_review":{"commit_sha":"' + head + '","has_any_findings":true,'
             '"has_unresolved_findings":true,"status":"completed"}} -->')
    payload = _bot_payload(head, [state], [("test", "success")])
    bot = payload["issue_comments"][0]["user"]
    payload["inline_comments"] = [{"id": 1, "commit_id": "abcdef1234567", "body": "P0: old finding", "user": bot}]
    if body is not None:
        payload["inline_comments"].append({"id": 2, "commit_id": head, "body": body, "user": bot})
    _install_api(monkeypatch, payload)
    store = Store()
    pull = _watch.parse_pull("o/r#2")

    events, _ = _watch.poll_pull(pull, store)

    kinds = {event.kind: event.detail for event in events}
    assert kinds["review"] == "1234567 findings"
    assert kinds.get("ready") == ("1234567 checks=1 review=no-blocking" if ready else None)
    assert _watch.poll_pull(pull, store) == ([], False)


def test_watch_resolved_blocker_allows_ready(monkeypatch: Any) -> None:
    head = "abcdef1234567890"
    receipt = "Codex Review: Didn't find any major issues.\n\n**Reviewed commit:** `abcdef1234`"
    payload = _bot_payload(head, [receipt], [("test", "success")])
    bot = payload["issue_comments"][0]["user"]
    payload["inline_comments"] = [{"id": 1, "commit_id": head, "body": "P1: fix this", "user": bot}]
    _install_api(monkeypatch, payload)
    store = Store()
    pull = _watch.parse_pull("o/r#2")

    events, _ = _watch.poll_pull(pull, store)
    assert "ready" not in {event.kind for event in events}

    payload["issue_comments"][0]["body"] = (
        '<!-- codex-review-state:v1 {"code_review":{"commit_sha":"' + head + '","has_any_findings":true,'
        '"has_unresolved_findings":false,"status":"completed"}} -->'
    )
    events, _ = _watch.poll_pull(pull, store)
    assert [event.kind for event in events] == ["ready"]


def test_watch_reports_conflict_once(monkeypatch: Any) -> None:
    store = Store()
    _install_api(monkeypatch, _bot_payload("ffff000", [], [], mergeable=False))
    first, _ = _watch.poll_pull(_watch.parse_pull("o/r#3"), store)
    second, _ = _watch.poll_pull(_watch.parse_pull("o/r#3"), store)
    assert [e.kind for e in first].count("conflict") == 1
    assert [e.kind for e in second].count("conflict") == 0


def test_request_review_once_per_new_unreviewed_head(monkeypatch: Any) -> None:
    posted: list[tuple[str, int, str]] = []
    monkeypatch.setattr(_watch._gh, "pr_comment", lambda repo, number, body: posted.append((repo, number, body)) or "")
    pull = _watch.parse_pull("o/r#4")
    state: dict[str, Any] = {"head": "new", "first_head": "old", "unreviewed_polls": 2, "terminal": None}
    assert _watch._maybe_request_review(pull, state, "@codex review") is not None
    assert posted == [("o/r", 4, "@codex review")]
    assert _watch._maybe_request_review(pull, state, "@codex review") is None
    assert _watch._maybe_request_review(pull, {"head": "old", "first_head": "old", "unreviewed_polls": 5},
                                        "@codex review") is None
    assert _watch._maybe_request_review(pull, {"head": "x", "first_head": "old", "unreviewed_polls": 1},
                                        "@codex review") is None


def test_watch_ignores_review_text_not_posted_by_the_bot(monkeypatch: Any) -> None:
    head = "abcdef1234567890"
    payload = _bot_payload(head, ["Codex Review: Didn't find any major issues.\n\n**Reviewed commit:** `abcdef1234`"],
                           [("test", "success")])
    payload["issue_comments"][0]["user"]["login"] = "someone"
    _install_api(monkeypatch, payload)
    events, _ = _watch.poll_pull(_watch.parse_pull("o/r#5"), Store())
    assert not [event for event in events if event.kind in {"review", "ready"}]


def test_request_review_persists_intent_before_posting(monkeypatch: Any) -> None:
    order: list[str] = []
    monkeypatch.setattr(_watch._gh, "pr_comment", lambda repo, number, body: order.append("post") or "")
    state: dict[str, Any] = {"head": "new", "first_head": "old", "unreviewed_polls": 2, "terminal": None}
    _watch._maybe_request_review(_watch.parse_pull("o/r#6"), state, "@codex review",
                                 persist=lambda current: order.append("persist:" + ",".join(current["review_requested_heads"])))
    assert order == ["persist:new", "post"]


def test_later_findings_receipt_replaces_an_earlier_clean_one() -> None:
    head = "abcdef1234567890"
    bot = {"login": "chatgpt-codex-connector[bot]"}
    comments = [{"user": bot, "body": "Codex Review: Didn't find any major issues.\n\n**Reviewed commit:** `abcdef1234`"},
                {"user": bot, "body": "Codex Review: here are some suggestions.\n\n**Reviewed commit:** `abcdef1234`"}]
    review = _watch._bot_review(comments, head)
    assert review is not None and review.verdict == "findings"


def test_structured_state_wins_over_the_receipt_in_the_same_comment() -> None:
    head = "abcdef1234567890"
    body = ('<!-- codex-review-state:v1 {"code_review":{"commit_sha":"' + head + '","has_any_findings":true,'
            '"has_unresolved_findings":false,"status":"completed"}} -->\n'
            "Codex Review: here are some suggestions.\n\n**Reviewed commit:** `abcdef1234`")
    review = _watch._bot_review([{"user": {"login": "chatgpt-codex-connector[bot]"}, "body": body}], head)
    assert review is not None and review.verdict == "clean"


def test_ready_needs_confirmed_mergeability_and_accepts_status_only_ci(monkeypatch: Any) -> None:
    head = "abcdef1234567890"
    state = ('<!-- codex-review-state:v1 {"code_review":{"commit_sha":"' + head + '","has_any_findings":false,'
             '"has_unresolved_findings":false,"status":"completed"}} -->')
    unknown = _bot_payload(head, [state], [("test", "success")], mergeable=None)
    unknown["pr"]["mergeable_state"] = "unknown"
    _install_api(monkeypatch, unknown)
    events, _ = _watch.poll_pull(_watch.parse_pull("o/r#7"), Store())
    assert "ready" not in {event.kind for event in events}
    status_only = _bot_payload(head, [state], [])
    status_only["statuses"] = {"statuses": [{"id": 1, "context": "ci", "state": "success",
                                             "updated_at": "2026-11-26T10:00:00Z"}]}
    _install_api(monkeypatch, status_only)
    events, _ = _watch.poll_pull(_watch.parse_pull("o/r#8"), Store())
    assert "ready" in {event.kind for event in events}
