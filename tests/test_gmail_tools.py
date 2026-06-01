"""GmailTools: draft is free, send is guarded."""

from __future__ import annotations

from typing import Any

import pytest

from lazytools.connectors.gmail import GmailSendBlocked, GmailTools
from lazytools.safety import ActionBlocked, active_scope
from lazytools.testing import FakeGmailService


def _tools(svc: FakeGmailService, **kw: Any) -> tuple[Any, Any]:
    provider = GmailTools(svc, **kw)
    by_name = {t.name: t for t in provider.as_tools()}
    return provider, by_name


def test_as_tools_exposes_all() -> None:
    _, by_name = _tools(FakeGmailService())
    assert set(by_name) == {"gmail_list_emails", "gmail_get_email", "gmail_create_draft", "gmail_send"}


def test_list_emails_no_messages() -> None:
    svc = FakeGmailService()
    provider, _ = _tools(svc)
    out = provider._list_emails()
    assert out == "No messages found."


def test_list_emails_returns_ids_and_subjects() -> None:
    svc = FakeGmailService(messages={
        "abc123": {"payload": {"headers": [
            {"name": "From", "value": "alice@x.com"},
            {"name": "Subject", "value": "Hello"},
        ]}},
    })
    provider, _ = _tools(svc)
    out = provider._list_emails()
    assert "abc123" in out
    assert "Hello" in out


def test_get_email_returns_headers_and_snippet() -> None:
    svc = FakeGmailService(messages={
        "msg1": {
            "snippet": "test snippet",
            "payload": {"headers": [
                {"name": "From", "value": "bob@x.com"},
                {"name": "Subject", "value": "Test"},
                {"name": "Date", "value": "Mon, 1 Jun 2026"},
            ]},
        }
    })
    provider, _ = _tools(svc)
    out = provider._get_email("msg1")
    assert "bob@x.com" in out
    assert "Test" in out
    assert "test snippet" in out


def test_provider_is_tool_provider() -> None:
    assert GmailTools(FakeGmailService())._is_lazy_tool_provider is True


def test_send_blocked_is_action_blocked() -> None:
    assert issubclass(GmailSendBlocked, ActionBlocked)
    assert issubclass(GmailSendBlocked, PermissionError)


def test_create_draft_is_not_blocked() -> None:
    svc = FakeGmailService()
    provider, _ = _tools(svc)
    out = provider._create_draft(to="a@x.com", subject="hi", body="b")
    assert "draft created" in out
    assert len(svc.drafts) == 1


async def test_send_without_confirmation_blocked() -> None:
    svc = FakeGmailService()
    provider, _ = _tools(svc)
    with pytest.raises(GmailSendBlocked, match="no outstanding confirmation"):
        await provider._send(to="a@x.com", subject="hi", body="b")
    assert svc.sent == []


async def test_confirm_once_authorizes_exactly_one_send() -> None:
    svc = FakeGmailService()
    provider, _ = _tools(svc)
    provider.confirm_once()
    await provider._send(to="a@x.com", subject="hi", body="b")
    assert len(svc.sent) == 1
    with pytest.raises(GmailSendBlocked):
        await provider._send(to="a@x.com", subject="hi", body="b")
    assert len(svc.sent) == 1


async def test_confirm_send_is_bound_to_recipient() -> None:
    svc = FakeGmailService()
    provider, _ = _tools(svc)
    provider.confirm_send(to="alice@x.com")
    with pytest.raises(GmailSendBlocked):
        await provider._send(to="bob@x.com", subject="hi", body="b")
    assert svc.sent == []
    await provider._send(to="alice@x.com", subject="hi", body="b")
    assert len(svc.sent) == 1
    with pytest.raises(GmailSendBlocked):
        await provider._send(to="alice@x.com", subject="hi", body="b")


async def test_send_respects_recipient_allowlist() -> None:
    svc = FakeGmailService()
    provider, _ = _tools(svc, allowed_recipients=["ok@x.com"])
    provider.confirm_once()
    with pytest.raises(GmailSendBlocked, match="allow-list"):
        await provider._send(to="evil@y.com", subject="hi", body="b")
    assert svc.sent == []


async def test_send_to_allowed_recipient_succeeds() -> None:
    svc = FakeGmailService()
    provider, _ = _tools(svc, allowed_recipients=["ok@x.com"])
    provider.confirm_send(to="ok@x.com")
    await provider._send(to="ok@x.com", subject="hi", body="b")
    assert len(svc.sent) == 1


async def test_require_confirmation_false_allows_send() -> None:
    svc = FakeGmailService()
    provider, _ = _tools(svc, require_confirmation=False)
    assert provider.require_confirmation is False
    await provider._send(to="a@x.com", subject="hi", body="b")
    assert len(svc.sent) == 1


# --- Scope-bound grants (the running task id, in LazyPulse) ------------- #


async def test_scope_bound_grant_only_consumed_in_that_scope() -> None:
    svc = FakeGmailService()
    provider, _ = _tools(svc)
    provider.confirm_send(to="a@x.com", task_id="TASK-A")

    token = active_scope.set("TASK-B")
    try:
        with pytest.raises(GmailSendBlocked):
            await provider._send(to="a@x.com", subject="hi", body="b")
    finally:
        active_scope.reset(token)
    assert svc.sent == []

    token = active_scope.set("TASK-A")
    try:
        await provider._send(to="a@x.com", subject="hi", body="b")
        with pytest.raises(GmailSendBlocked):
            await provider._send(to="a@x.com", subject="hi", body="b")
    finally:
        active_scope.reset(token)
    assert len(svc.sent) == 1


async def test_scope_bound_grant_not_consumed_outside_a_scope() -> None:
    svc = FakeGmailService()
    provider, _ = _tools(svc)
    provider.confirm_once(task_id="TASK-A")
    with pytest.raises(GmailSendBlocked):
        await provider._send(to="a@x.com", subject="hi", body="b")
    assert svc.sent == []


async def test_unbound_grant_works_in_any_scope() -> None:
    svc = FakeGmailService()
    provider, _ = _tools(svc)
    provider.confirm_once()  # no task_id → any scope
    token = active_scope.set("TASK-X")
    try:
        await provider._send(to="a@x.com", subject="hi", body="b")
    finally:
        active_scope.reset(token)
    assert len(svc.sent) == 1
