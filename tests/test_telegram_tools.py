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
