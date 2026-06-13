"""OutlookClient: COM-item → Gmail-shaped resource, listing, draft/send.

Exercises the pure conversion + iteration logic against fake COM objects, so
it runs on any platform without pywin32 or a real Outlook.
"""

from __future__ import annotations

from typing import Any

import pytest

from lazytools.connectors.outlook.client import (
    _PR_SMTP_ADDRESS,
    _PR_TRANSPORT_HEADERS,
    OutlookClient,
    _first_header,
    _snippet,
)

_TRANSPORT = (
    "Authentication-Results: mx.example.com;\r\n"
    "\tdkim=pass header.d=x.com; spf=pass; dmarc=pass\r\n"
    "Received: from somewhere\r\n"
    "Subject: ignored-here\r\n"
)


class FakeAccessor:
    def __init__(self, props: dict[str, Any]) -> None:
        self._props = props

    def GetProperty(self, schema: str) -> Any:
        if schema in self._props:
            return self._props[schema]
        raise Exception("property not found")  # COM surfaces absence as a raise


class FakeItem:
    def __init__(self, **attrs: Any) -> None:
        self.EntryID = attrs.get("EntryID", "")
        self.SenderName = attrs.get("SenderName", "")
        self.SenderEmailAddress = attrs.get("SenderEmailAddress", "")
        self.To = attrs.get("To", "")
        self.Subject = attrs.get("Subject", "")
        self.ReceivedTime = attrs.get("ReceivedTime", "")
        self.Body = attrs.get("Body", "")
        self.PropertyAccessor = FakeAccessor(attrs.get("props", {}))


class FakeItems:
    def __init__(self, items: list[FakeItem]) -> None:
        self._items = items
        self._i = 0
        self.sorted: tuple[str, bool] | None = None
        self.restricted: str | None = None

    def Sort(self, field: str, descending: bool) -> None:
        self.sorted = (field, descending)

    def Restrict(self, query: str) -> FakeItems:
        self.restricted = query
        return self

    def GetFirst(self) -> FakeItem | None:
        self._i = 0
        return self._items[0] if self._items else None

    def GetNext(self) -> FakeItem | None:
        self._i += 1
        return self._items[self._i] if self._i < len(self._items) else None


class FakeFolder:
    def __init__(self, items: list[FakeItem]) -> None:
        self.Items = FakeItems(items)


class FakeNamespace:
    def __init__(self, items: list[FakeItem]) -> None:
        self._folder = FakeFolder(items)
        self._by_id = {it.EntryID: it for it in items}

    def GetDefaultFolder(self, index: int) -> FakeFolder:
        return self._folder

    def GetItemFromID(self, entry_id: str) -> FakeItem:
        return self._by_id[entry_id]


class FakeMail:
    def __init__(self) -> None:
        self.To = ""
        self.Subject = ""
        self.Body = ""
        self.EntryID = "new-entry"
        self.saved = False
        self.sent = False

    def Save(self) -> None:
        self.saved = True

    def Send(self) -> None:
        self.sent = True


class FakeApplication:
    def __init__(self) -> None:
        self.created: list[FakeMail] = []

    def CreateItem(self, item_type: int) -> FakeMail:
        mail = FakeMail()
        self.created.append(mail)
        return mail


def _client(items: list[FakeItem]) -> tuple[OutlookClient, FakeApplication]:
    app = FakeApplication()
    return OutlookClient(FakeNamespace(items), app), app


# -- pure helpers ------------------------------------------------------- #


def test_first_header_unfolds_continuation() -> None:
    value = _first_header(_TRANSPORT, "Authentication-Results")
    assert value is not None
    assert "mx.example.com" in value
    assert "dkim=pass" in value and "dmarc=pass" in value


def test_first_header_first_wins() -> None:
    raw = (
        "Authentication-Results: mx.example.com; dkim=fail\r\n"
        "Authentication-Results: attacker.test; dkim=pass\r\n"
    )
    assert _first_header(raw, "Authentication-Results") == "mx.example.com; dkim=fail"


def test_first_header_absent_returns_none() -> None:
    assert _first_header("Subject: hi\r\n", "Authentication-Results") is None
    assert _first_header("", "Authentication-Results") is None


def test_snippet_collapses_and_truncates() -> None:
    assert _snippet("  hello\n\tworld  ") == "hello world"
    assert len(_snippet("x" * 500)) == 200


# -- resource conversion ------------------------------------------------ #


def test_get_message_builds_gmail_shaped_resource() -> None:
    item = FakeItem(
        EntryID="E1",
        SenderName="Alice",
        Subject="Hello",
        Body="Body text here",
        props={
            _PR_TRANSPORT_HEADERS: _TRANSPORT,
            _PR_SMTP_ADDRESS: "alice@x.com",
        },
    )
    client, _ = _client([item])
    raw = client.get_message("E1")
    headers = {h["name"]: h["value"] for h in raw["payload"]["headers"]}
    assert headers["From"] == "Alice <alice@x.com>"
    assert headers["Subject"] == "Hello"
    assert "dkim=pass" in headers["Authentication-Results"]
    assert raw["snippet"] == "Body text here"


def test_get_message_falls_back_to_sender_email_without_smtp() -> None:
    item = FakeItem(EntryID="E1", SenderName="Bob", SenderEmailAddress="bob@y.com")
    client, _ = _client([item])
    headers = {h["name"]: h["value"] for h in client.get_message("E1")["payload"]["headers"]}
    assert headers["From"] == "Bob <bob@y.com>"


def test_get_message_omits_auth_header_when_no_transport_headers() -> None:
    item = FakeItem(EntryID="E1", SenderName="Bob", SenderEmailAddress="bob@y.com")
    raw = _client([item])[0].get_message("E1")
    names = [h["name"] for h in raw["payload"]["headers"]]
    assert "Authentication-Results" not in names


# -- listing ------------------------------------------------------------ #


def test_list_message_ids_returns_entry_ids_capped() -> None:
    items = [FakeItem(EntryID=f"E{i}") for i in range(5)]
    client, _ = _client(items)
    assert client.list_message_ids(max_results=3) == ["E0", "E1", "E2"]


def test_list_message_ids_applies_restrict_query() -> None:
    items = [FakeItem(EntryID="E0")]
    client, _ = _client(items)
    client.list_message_ids(query="[Unread] = true")
    assert client._namespace._folder.Items.restricted == "[Unread] = true"


# -- draft / send ------------------------------------------------------- #


def test_create_draft_saves_not_sends() -> None:
    client, app = _client([])
    out = client.create_draft(to="x@y.com", subject="s", body="b")
    mail = app.created[-1]
    assert mail.saved and not mail.sent
    assert (mail.To, mail.Subject, mail.Body) == ("x@y.com", "s", "b")
    assert out["id"] == "new-entry"


def test_send_message_sends() -> None:
    client, app = _client([])
    client.send_message(to="x@y.com", subject="s", body="b")
    assert app.created[-1].sent


def test_send_rejects_header_injection() -> None:
    client, _ = _client([])
    with pytest.raises(ValueError, match="newline"):
        client.send_message(to="x@y.com\r\nBcc: evil@z.com", subject="s", body="b")
