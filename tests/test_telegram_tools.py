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
    assert by_name == {"telegram_send_message", "telegram_send_document"}


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
# telegram_send_document (attachment) — same guards as the text send
# ------------------------------------------------------------------ #


async def test_send_document_blocked_without_confirmation(tmp_path) -> None:
    f = tmp_path / "r.md"
    f.write_text("# report", encoding="utf-8")
    svc = FakeTelegramService()
    with pytest.raises(TelegramSendBlocked, match="no outstanding confirmation"):
        await TelegramTools(svc)._send_document(chat_id=42, file_path=str(f))
    assert svc.sent == []


async def test_send_document_uploads_after_confirm(tmp_path) -> None:
    f = tmp_path / "report.md"
    f.write_text("# hello", encoding="utf-8")
    svc = FakeTelegramService()
    tools = TelegramTools(svc, allowed_chat_ids=[42], require_confirmation=False)
    out = await tools._send_document(chat_id=42, file_path=str(f), caption="ciao")
    assert "message_id=" in out
    assert len(svc.sent) == 1
    sent = svc.sent[0]
    assert sent["chat_id"] == 42
    assert sent["filename"] == "report.md"
    assert sent["document"] == b"# hello"
    assert sent["caption"] == "ciao"


async def test_send_document_allow_list_enforced(tmp_path) -> None:
    f = tmp_path / "r.md"
    f.write_text("x", encoding="utf-8")
    svc = FakeTelegramService()
    tools = TelegramTools(svc, allowed_chat_ids=[42], require_confirmation=False)
    with pytest.raises(TelegramSendBlocked, match="allow-list"):
        await tools._send_document(chat_id=99, file_path=str(f))
    assert svc.sent == []


async def test_send_document_sandbox_allows_inside(tmp_path) -> None:
    d = tmp_path / "reports"
    d.mkdir()
    f = d / "r.md"
    f.write_text("x", encoding="utf-8")
    svc = FakeTelegramService()
    tools = TelegramTools(svc, allowed_chat_ids=[42], require_confirmation=False, attachments_dir=str(d))
    await tools._send_document(chat_id=42, file_path=str(f))
    assert len(svc.sent) == 1


async def test_send_document_sandbox_blocks_outside(tmp_path) -> None:
    d = tmp_path / "reports"
    d.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("s", encoding="utf-8")
    svc = FakeTelegramService()
    tools = TelegramTools(svc, allowed_chat_ids=[42], require_confirmation=False, attachments_dir=str(d))
    with pytest.raises(TelegramSendBlocked, match="attachments directory"):
        await tools._send_document(chat_id=42, file_path=str(outside))
    assert svc.sent == []


async def test_send_document_missing_file_raises(tmp_path) -> None:
    svc = FakeTelegramService()
    tools = TelegramTools(svc, require_confirmation=False)
    with pytest.raises(FileNotFoundError):
        await tools._send_document(chat_id=42, file_path=str(tmp_path / "nope.md"))
    assert svc.sent == []


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


# --- split_message (4096-char Bot API limit) ---------------------------- #


def test_split_message_short_text_single_chunk() -> None:
    from lazytools.connectors.telegram import split_message

    assert split_message("hello") == ["hello"]
    assert split_message("x" * 4096) == ["x" * 4096]
    assert split_message("   ") == []


def test_split_message_prefers_natural_breaks() -> None:
    from lazytools.connectors.telegram import split_message

    para_a = "a" * 3000
    para_b = "b" * 3000
    chunks = split_message(f"{para_a}\n\n{para_b}")
    assert chunks == [para_a, para_b]


def test_split_message_hard_cuts_unbreakable_text() -> None:
    from lazytools.connectors.telegram import split_message

    chunks = split_message("y" * 9000)
    assert [len(c) for c in chunks] == [4096, 4096, 808]
    assert "".join(chunks) == "y" * 9000


def test_split_message_never_exceeds_limit_and_loses_nothing() -> None:
    from lazytools.connectors.telegram import split_message

    text = "\n".join(f"line {i} " + "z" * (i % 200) for i in range(300))
    chunks = split_message(text)
    assert all(len(c) <= 4096 for c in chunks)
    # Nothing but whitespace at the split points is lost.
    assert "".join(c.replace("\n", "").replace(" ", "") for c in chunks) == text.replace("\n", "").replace(" ", "")


async def test_send_message_chunks_long_text() -> None:
    svc = FakeTelegramService()
    tools = TelegramTools(svc, require_confirmation=False)
    out = await tools._send_message(chat_id=42, text="a" * 3000 + "\n\n" + "b" * 3000)
    assert [len(s["text"]) for s in svc.sent] == [3000, 3000]
    assert all(s["chat_id"] == 42 for s in svc.sent)
    assert out == "sent: message_id=1,2"
