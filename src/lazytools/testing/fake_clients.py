"""In-memory fake service clients for testing guarded tools.

These satisfy the duck-typed ``GmailService`` / ``TelegramService`` Protocols
without touching any network, consolidating the per-test fakes that previously
lived in each suite.
"""

from __future__ import annotations

from typing import Any


class FakeGmailService:
    """In-memory :class:`~lazytools.connectors.gmail.client.GmailService`."""

    def __init__(self, messages: dict[str, dict[str, Any]] | None = None) -> None:
        self._messages = messages or {}
        self.drafts: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []

    def list_message_ids(self, *, query: str | None = None, max_results: int = 25) -> list[str]:
        return list(self._messages)[:max_results]

    def get_message(self, message_id: str) -> dict[str, Any]:
        return self._messages.get(message_id, {"id": message_id})

    def create_draft(self, *, to: str, subject: str, body: str) -> dict[str, Any]:
        self.drafts.append({"to": to, "subject": subject, "body": body})
        return {"id": f"draft-{len(self.drafts)}"}

    def send_message(self, *, to: str, subject: str, body: str) -> dict[str, Any]:
        self.sent.append({"to": to, "subject": subject, "body": body})
        return {"id": f"sent-{len(self.sent)}"}


class FakeTelegramService:
    """In-memory :class:`~lazytools.connectors.telegram.client.TelegramService`."""

    def __init__(self, updates: list[dict[str, Any]] | None = None) -> None:
        self._updates = updates or []
        self.sent: list[dict[str, Any]] = []
        self.offsets: list[int] = []

    def get_updates(self, *, offset: int, timeout: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        self.offsets.append(offset)
        return [u for u in self._updates if u["update_id"] >= offset][:limit]

    def send_message(self, *, chat_id: int | str, text: str) -> dict[str, Any]:
        self.sent.append({"chat_id": chat_id, "text": text})
        return {"message_id": len(self.sent)}
