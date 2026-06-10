"""TelegramTools: the send tool is guarded (allow-list + one-shot confirmation)."""

from __future__ import annotations

import pytest

from lazytools.connectors.telegram import TelegramSendBlocked, TelegramTools
from lazytools.safety import ActionBlocked, active_scope
from lazytools.testing import FakeTelegramService


def test_provider_is_tool_provider() -> None:
    assert TelegramTools(FakeTelegramService())._is_lazy_tool_provider is True


def test_as_tools_exposes_send() -> None:
    by_name = {t.name for t in TelegramTools(FakeTelegramService()).as_tools()}
    assert by_name == {"telegram_send_message"}


def test_send_blocked_is_action_blocked() -> None:
    assert issubclass(TelegramSendBlocked, ActionBlocked)
    assert issubclass(TelegramSendBlocked, PermissionError)


async def test_send_blocked_without_confirmation() -> None:
    svc = FakeTelegramService()
    with pytest.raises(TelegramSendBlocked, match="no outstanding confirmation"):
        await TelegramTools(svc)._send_message(chat_id=42, text="hi")
    assert svc.sent == []


async def test_confirm_once_authorizes_exactly_one_send() -> None:
    svc = FakeTelegramService()
    tools = TelegramTools(svc)
    tools.confirm_once()
    await tools._send_message(chat_id=42, text="hi")
    assert len(svc.sent) == 1
    with pytest.raises(TelegramSendBlocked):
        await tools._send_message(chat_id=42, text="again")


async def test_confirm_send_bound_to_chat() -> None:
    svc = FakeTelegramService()
    tools = TelegramTools(svc)
    tools.confirm_send(chat_id=42)
    with pytest.raises(TelegramSendBlocked):
        await tools._send_message(chat_id=99, text="wrong chat")
    await tools._send_message(chat_id=42, text="ok")
    assert len(svc.sent) == 1


async def test_allow_list_enforced() -> None:
    svc = FakeTelegramService()
    tools = TelegramTools(svc, allowed_chat_ids=[42])
    tools.confirm_once()
    with pytest.raises(TelegramSendBlocked, match="allow-list"):
        await tools._send_message(chat_id=99, text="blocked")
    assert svc.sent == []


async def test_allow_list_is_case_insensitive_for_string_chats() -> None:
    # @username chat ids match case-insensitively (Telegram usernames are
    # case-insensitive); numeric ids are unaffected by normalization.
    svc = FakeTelegramService()
    tools = TelegramTools(svc, allowed_chat_ids=["@MyChannel"], require_confirmation=False)
    await tools._send_message(chat_id="@mychannel", text="ok")
    assert len(svc.sent) == 1


async def test_require_confirmation_false_allows_reply() -> None:
    svc = FakeTelegramService()
    tools = TelegramTools(svc, allowed_chat_ids=[42], require_confirmation=False)
    assert tools.require_confirmation is False
    await tools._send_message(chat_id=42, text="hi")
    assert len(svc.sent) == 1


async def test_scope_bound_grant_not_stolen_by_concurrent_scope() -> None:
    svc = FakeTelegramService()
    tools = TelegramTools(svc)
    tools.confirm_send(chat_id=42, task_id="TASK-A")

    token = active_scope.set("TASK-B")
    try:
        with pytest.raises(TelegramSendBlocked):
            await tools._send_message(chat_id=42, text="stolen?")
    finally:
        active_scope.reset(token)
    assert svc.sent == []

    token = active_scope.set("TASK-A")
    try:
        await tools._send_message(chat_id=42, text="ok")
    finally:
        active_scope.reset(token)
    assert len(svc.sent) == 1


# ------------------------------------------------------------------ #
# Client-level: bot-token redaction in error paths
# ------------------------------------------------------------------ #


def test_client_redacts_token_from_http_errors() -> None:
    """httpx error messages embed the request URL — which contains the bot
    token. The client must redact it before the error can reach a log."""
    from lazytools.connectors.telegram.client import TelegramClient

    class _Resp:
        def raise_for_status(self) -> None:
            raise RuntimeError("Client error '404' for url 'https://api.telegram.org/botSECRET-TOKEN/sendMessage'")

        def json(self) -> dict:  # pragma: no cover — raise_for_status fires first
            return {}

    class _Http:
        def post(self, url: str, json: dict | None = None) -> _Resp:
            return _Resp()

    client = TelegramClient("SECRET-TOKEN", http=_Http())
    with pytest.raises(RuntimeError) as excinfo:
        client.send_message(chat_id=1, text="hi")
    assert "SECRET-TOKEN" not in str(excinfo.value)
    assert "<bot-token>" in str(excinfo.value)
    # No chained exception — the original message contains the token.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True


def test_client_redacts_token_from_api_error_description() -> None:
    from lazytools.connectors.telegram.client import TelegramClient

    class _Resp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"ok": False, "description": "bad request for botSECRET-TOKEN"}

    class _Http:
        def post(self, url: str, json: dict | None = None) -> _Resp:
            return _Resp()

    client = TelegramClient("SECRET-TOKEN", http=_Http())
    with pytest.raises(RuntimeError) as excinfo:
        client.send_message(chat_id=1, text="hi")
    assert "SECRET-TOKEN" not in str(excinfo.value)


def test_client_close_closes_injected_http() -> None:
    from lazytools.connectors.telegram.client import TelegramClient

    class _Http:
        closed = False

        def close(self) -> None:
            self.closed = True

    http = _Http()
    with TelegramClient("tok", http=http):
        pass
    assert http.closed is True
