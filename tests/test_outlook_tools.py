"""OutlookTools: draft is free, send is guarded (mirror of test_gmail_tools)."""

from __future__ import annotations

from typing import Any

import pytest

from lazytools.connectors.outlook import OutlookSendBlocked, OutlookTools
from lazytools.safety import ActionBlocked, active_scope
from lazytools.testing import FakeOutlookService


def _tools(svc: FakeOutlookService, **kw: Any) -> tuple[Any, Any]:
    provider = OutlookTools(svc, **kw)
    by_name = {t.name: t for t in provider.as_tools()}
    return provider, by_name


def test_as_tools_exposes_all() -> None:
    _, by_name = _tools(FakeOutlookService())
    assert set(by_name) == {
        "outlook_list_emails",
        "outlook_get_email",
        "outlook_create_draft",
        "outlook_send",
    }


def test_list_emails_no_messages() -> None:
    provider, _ = _tools(FakeOutlookService())
    assert "No messages found." in provider._list_emails()


def test_list_emails_returns_ids_and_subjects() -> None:
    svc = FakeOutlookService(messages={
        "AAA": {"payload": {"headers": [
            {"name": "From", "value": "alice@x.com"},
            {"name": "Subject", "value": "Hello"},
        ]}},
    })
    provider, _ = _tools(svc)
    out = provider._list_emails()
    assert "AAA" in out
    assert "Hello" in out


def test_list_emails_filters_build_dasl_restriction() -> None:
    svc = FakeOutlookService()
    provider, _ = _tools(svc)
    provider._list_emails(sender="bob@x.com", subject="invoice", unread=True)
    query = svc.queries[-1]
    assert query is not None and query.startswith("@SQL=")
    assert "bob@x.com" in query
    assert "invoice" in query
    assert "= 0" in query  # unread clause


def test_list_emails_escapes_single_quotes() -> None:
    svc = FakeOutlookService()
    provider, _ = _tools(svc)
    provider._list_emails(subject="o'brien")
    assert "o''brien" in svc.queries[-1]


def test_list_emails_no_filters_passes_none() -> None:
    svc = FakeOutlookService()
    provider, _ = _tools(svc)
    provider._list_emails()
    assert svc.queries[-1] is None


def test_list_emails_raw_macro_query_passes_through_unchanged() -> None:
    svc = FakeOutlookService()
    provider, _ = _tools(svc)
    provider._list_emails(query="[Unread] = true")
    assert svc.queries[-1] == "[Unread] = true"  # not re-wrapped in @SQL=


def test_list_emails_raw_sql_query_not_double_prefixed() -> None:
    svc = FakeOutlookService()
    provider, _ = _tools(svc)
    provider._list_emails(query='@SQL="urn:schemas:httpmail:read" = 0')
    assert svc.queries[-1] == '@SQL="urn:schemas:httpmail:read" = 0'


def test_list_emails_structured_plus_raw_sql_dedups_prefix() -> None:
    svc = FakeOutlookService()
    provider, _ = _tools(svc)
    provider._list_emails(subject="hi", query='@SQL="urn:schemas:httpmail:read" = 0')
    query = svc.queries[-1]
    assert query.startswith("@SQL=")
    assert query.count("@SQL=") == 1  # raw clause's @SQL= stripped before folding
    assert "AND" in query and "read" in query


def test_get_email_renders_headers() -> None:
    svc = FakeOutlookService(messages={
        "AAA": {"snippet": "hi there", "payload": {"headers": [
            {"name": "From", "value": "alice@x.com"},
            {"name": "Subject", "value": "Hello"},
        ]}},
    })
    provider, _ = _tools(svc)
    out = provider._get_email("AAA")
    assert "alice@x.com" in out
    assert "Hello" in out
    assert "hi there" in out


def test_create_draft_is_free() -> None:
    svc = FakeOutlookService()
    provider, _ = _tools(svc)
    out = provider._create_draft(to="x@y.com", subject="s", body="b")
    assert "draft created" in out
    assert svc.drafts == [{"to": "x@y.com", "subject": "s", "body": "b"}]


async def test_send_blocked_without_confirmation() -> None:
    svc = FakeOutlookService()
    provider, _ = _tools(svc)  # require_confirmation defaults True
    with pytest.raises(OutlookSendBlocked):
        await provider._send(to="x@y.com", subject="s", body="b")
    assert svc.sent == []


async def test_send_allowed_after_confirm_once() -> None:
    svc = FakeOutlookService()
    provider, _ = _tools(svc)
    provider.confirm_once()
    out = await provider._send(to="x@y.com", subject="s", body="b")
    assert "sent:" in out
    assert svc.sent == [{"to": "x@y.com", "subject": "s", "body": "b"}]


async def test_confirmation_is_one_shot() -> None:
    svc = FakeOutlookService()
    provider, _ = _tools(svc)
    provider.confirm_once()
    await provider._send(to="x@y.com", subject="s", body="b")
    with pytest.raises(OutlookSendBlocked):
        await provider._send(to="x@y.com", subject="s", body="b")
    assert len(svc.sent) == 1


async def test_send_blocked_by_allowlist() -> None:
    svc = FakeOutlookService()
    provider, _ = _tools(svc, allowed_recipients=["ok@y.com"])
    provider.confirm_once()
    with pytest.raises(OutlookSendBlocked):
        await provider._send(to="evil@y.com", subject="s", body="b")
    assert svc.sent == []


async def test_send_without_confirmation_required() -> None:
    svc = FakeOutlookService()
    provider, _ = _tools(svc, require_confirmation=False)
    out = await provider._send(to="x@y.com", subject="s", body="b")
    assert "sent:" in out


def test_send_blocked_is_action_blocked() -> None:
    assert issubclass(OutlookSendBlocked, ActionBlocked)


async def test_task_bound_grant_consumed_in_scope() -> None:
    svc = FakeOutlookService()
    provider, _ = _tools(svc)
    provider.confirm_send(to="x@y.com", task_id="task-1")
    token = active_scope.set("task-1")
    try:
        out = await provider._send(to="x@y.com", subject="s", body="b")
    finally:
        active_scope.reset(token)
    assert "sent:" in out
