"""GmailClient history/watch surface — incremental sync + push arming.

Drives ``GmailClient`` against a stub of the googleapiclient resource
chain (``users().history().list(...).execute()``), covering pagination,
de-duplication, the 404 → ``GmailHistoryExpired`` mapping, and the
watch/stop calls. Also pins the ``FakeGmailService`` test double to the
same contract so LazyPulse's adapter tests can rely on it.
"""

from __future__ import annotations

from typing import Any

import pytest

from lazytools.connectors.gmail import GmailClient, GmailHistoryExpired
from lazytools.testing import FakeGmailService


class _Request:
    def __init__(self, response: Any):
        self._response = response

    def execute(self) -> Any:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _Http404(Exception):
    status_code = 404


class _StubUsers:
    """Mimics the googleapiclient ``users()`` resource shape."""

    def __init__(self, history_pages: list[Any], profile: dict[str, Any] | None = None):
        self._pages = history_pages
        self._profile = profile or {"historyId": "42", "emailAddress": "me@x"}
        self.watch_bodies: list[dict[str, Any]] = []
        self.stopped = False
        self.history_calls: list[dict[str, Any]] = []

    # users().getProfile(...)
    def getProfile(self, userId: str) -> _Request:
        return _Request(self._profile)

    # users().history().list(...)
    def history(self) -> _StubUsers:
        return self

    def list(self, **kwargs: Any) -> _Request:
        self.history_calls.append(kwargs)
        page_index = len(self.history_calls) - 1
        page = self._pages[min(page_index, len(self._pages) - 1)]
        return _Request(page)

    # users().watch(...) / users().stop(...)
    def watch(self, userId: str, body: dict[str, Any]) -> _Request:
        self.watch_bodies.append(body)
        return _Request({"historyId": "42", "expiration": "1700000000000"})

    def stop(self, userId: str) -> _Request:
        self.stopped = True
        return _Request({})


class _StubService:
    def __init__(self, users: _StubUsers):
        self._users = users

    def users(self) -> _StubUsers:
        return self._users


def test_get_history_id_reads_profile():
    client = GmailClient(_StubService(_StubUsers([])))
    assert client.get_history_id() == "42"


def test_list_history_paginates_and_dedupes():
    pages = [
        {
            "historyId": "50",
            "nextPageToken": "p2",
            "history": [
                {"messagesAdded": [{"message": {"id": "m1"}}, {"message": {"id": "m2"}}]},
                {"messagesAdded": [{"message": {"id": "m1"}}]},  # duplicate across records
            ],
        },
        {
            "historyId": "51",
            "history": [{"messagesAdded": [{"message": {"id": "m3"}}]}],
        },
    ]
    users = _StubUsers(pages)
    client = GmailClient(_StubService(users))

    ids, cursor = client.list_history_message_ids(start_history_id="42")

    assert ids == ["m1", "m2", "m3"]
    assert cursor == "51"
    assert len(users.history_calls) == 2
    assert users.history_calls[0]["startHistoryId"] == "42"
    assert users.history_calls[0]["historyTypes"] == ["messageAdded"]


def test_list_history_404_maps_to_expired():
    users = _StubUsers([_Http404("expired")])
    client = GmailClient(_StubService(users))

    with pytest.raises(GmailHistoryExpired, match="resync"):
        client.list_history_message_ids(start_history_id="1")


def test_list_history_other_errors_propagate():
    class _Boom(Exception):
        status_code = 500

    users = _StubUsers([_Boom("server error")])
    client = GmailClient(_StubService(users))

    with pytest.raises(_Boom):
        client.list_history_message_ids(start_history_id="1")


def test_watch_and_stop():
    users = _StubUsers([])
    client = GmailClient(_StubService(users))

    resp = client.watch(topic_name="projects/p/topics/gmail")
    assert resp["expiration"] == "1700000000000"
    assert users.watch_bodies == [
        {
            "topicName": "projects/p/topics/gmail",
            "labelIds": ["INBOX"],
            "labelFilterBehavior": "INCLUDE",
        }
    ]

    client.stop_watch()
    assert users.stopped


# --------------------------------------------------------------------------- #
# FakeGmailService contract — what LazyPulse adapter tests build on
# --------------------------------------------------------------------------- #


def test_fake_history_surface_matches_contract():
    fake = FakeGmailService()
    start = fake.get_history_id()

    fake.add_message("m1")
    fake.add_message("m2")
    ids, cursor = fake.list_history_message_ids(start_history_id=start)
    assert ids == ["m1", "m2"]

    # Nothing new after the returned cursor.
    ids2, cursor2 = fake.list_history_message_ids(start_history_id=cursor)
    assert ids2 == [] and cursor2 == cursor

    fake.history_expired = True
    with pytest.raises(GmailHistoryExpired):
        fake.list_history_message_ids(start_history_id=cursor)

    fake.watch(topic_name="t")
    assert fake.watches and not fake.watch_stopped
    fake.stop_watch()
    assert fake.watch_stopped


# --------------------------------------------------------------------------- #
# Cursor safety under max_results capping (Codex P1: must not skip mail)
# --------------------------------------------------------------------------- #


def _one_page_per_record(n: int, start: int = 100) -> dict[str, Any]:
    """A single Gmail page with n records, one messageAdded each."""
    return {
        "historyId": str(start + n + 1000),  # Gmail's "now" — far past the records
        "history": [{"id": str(start + i), "messagesAdded": [{"message": {"id": f"m{i}"}}]} for i in range(1, n + 1)],
    }


def test_capped_run_does_not_advance_cursor_past_returned_mail():
    users = _StubUsers([_one_page_per_record(5)])
    client = GmailClient(_StubService(users))

    ids, cursor = client.list_history_message_ids(start_history_id="100", max_results=3)

    assert ids == ["m1", "m2", "m3"]
    # NOT the response-level "1105": the cursor stops at the last record
    # actually consumed, so m4/m5 are picked up by the next call.
    assert cursor == "103"

    ids2, cursor2 = client.list_history_message_ids(start_history_id=cursor, max_results=100)
    assert ids2 == ["m1", "m2", "m3", "m4", "m5"]  # stub replays the page; real Gmail resumes
    assert cursor2 == "1105"  # fully drained -> response-level cursor


def test_oversized_single_record_is_consumed_whole():
    """max_results is a soft cap: one giant record must not stall the cursor."""
    page = {
        "historyId": "9000",
        "history": [
            {
                "id": "200",
                "messagesAdded": [{"message": {"id": f"big{i}"}} for i in range(10)],
            }
        ],
    }
    users = _StubUsers([page])
    client = GmailClient(_StubService(users))

    ids, cursor = client.list_history_message_ids(start_history_id="100", max_results=3)

    assert len(ids) == 10  # record consumed whole despite the cap
    assert cursor == "9000"  # drained -> safe to jump to "now"


def test_fake_capped_cursor_matches_contract():
    fake = FakeGmailService()
    start = fake.get_history_id()
    for i in range(7):
        fake.add_message(f"m{i}")

    ids, cursor = fake.list_history_message_ids(start_history_id=start, max_results=5)
    assert ids == [f"m{i}" for i in range(5)]

    ids2, cursor2 = fake.list_history_message_ids(start_history_id=cursor, max_results=5)
    assert ids2 == ["m5", "m6"]  # nothing skipped
    assert cursor2 == fake.get_history_id()
