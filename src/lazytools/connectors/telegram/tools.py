"""Telegram outbound tool for the worker.

Exposes ``telegram_send_message`` via the lazybridge ``ToolProvider``
protocol. Sending is an outbound action, so — exactly like ``GmailTools`` — it
consumes a **single, explicit confirmation** and the chat must pass the
optional allow-list. A blocked send raises :class:`TelegramSendBlocked`.

Two common setups:

* **Reply freely to a known chat** (typical chat-bot): pass
  ``allowed_chat_ids=[your_chat]`` and ``require_confirmation=False``. Sends
  are still bounded to the allow-list.
* **May message arbitrary chats**: keep ``require_confirmation=True`` and grant
  one send per approved task via ``confirm_send(chat_id=...)`` /
  ``confirm_once()`` — each grant authorizes exactly one send.
"""

from __future__ import annotations

import asyncio
import os

from lazybridge import Tool

from lazytools.connectors.telegram.client import TelegramService
from lazytools.safety import ActionBlocked, Allowlist, ConfirmationGate, current_scope

#: Telegram Bot API ``sendDocument`` hard limit for uploads.
_MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
#: Telegram caption hard limit.
_MAX_CAPTION_CHARS = 1024


def _read_file(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


class TelegramSendBlocked(ActionBlocked):
    """Raised when ``telegram_send_message`` is invoked without authorization."""


class TelegramTools:
    """A ``ToolProvider`` wrapping a :class:`TelegramService` for the worker."""

    _is_lazy_tool_provider = True

    def __init__(
        self,
        client: TelegramService,
        *,
        allowed_chat_ids: list[int | str] | None = None,
        require_confirmation: bool = True,
        attachments_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self._client = client
        self._allowlist = Allowlist(allowed_chat_ids)
        self._gate = ConfirmationGate(enabled=require_confirmation)
        # When set, ``telegram_send_document`` may only upload files resolving
        # *under* this directory. ``file_path`` is typically model-controlled,
        # so confining it stops an agent from exfiltrating arbitrary host files.
        # ``None`` permits any path (trusted-caller mode) — mirrors how
        # ``allowed_chat_ids=None`` permits any chat. Set it whenever the tool
        # is exposed to an LLM (e.g. to the directory ``save_report`` writes to).
        self._attachments_dir = os.path.realpath(os.fspath(attachments_dir)) if attachments_dir is not None else None

    @property
    def require_confirmation(self) -> bool:
        """Whether a send needs an outstanding confirmation (public attribute)."""
        return self._gate.enabled

    def confirm_once(self, *, task_id: str | None = None) -> None:
        """Authorize exactly one send to any chat (subject to the allow-list).
        Pass ``task_id=`` to bind the grant to a single task so a concurrent
        task cannot consume it."""
        self._gate.grant_any(scope=task_id)

    def confirm_send(self, *, chat_id: int | str, task_id: str | None = None) -> None:
        """Authorize exactly one send to a specific chat — the tighter grant.
        Pass ``task_id=`` to also bind it to a single task."""
        self._gate.grant(chat_id, scope=task_id)

    # ------------------------------------------------------------------ #
    # ToolProvider
    # ------------------------------------------------------------------ #
    def as_tools(self) -> list[Tool]:
        return [
            Tool.wrap(
                self._send_message,
                name="telegram_send_message",
                description="Send a Telegram message. Requires a one-shot confirmation. Args: chat_id, text.",
            ),
            Tool.wrap(
                self._send_document,
                name="telegram_send_document",
                description=(
                    "Send a Telegram document/file attachment (e.g. a rendered report). "
                    "Requires a one-shot confirmation. Args: chat_id, file_path, caption (optional)."
                ),
            ),
        ]

    # ------------------------------------------------------------------ #
    # Tool implementation
    # ------------------------------------------------------------------ #
    async def _send_message(self, chat_id: int | str, text: str) -> str:
        # Async so the task-bound grant check can read the worker's task
        # context (lazybridge runs async tools in-context). The blocking Bot
        # API call is offloaded to a thread so it never stalls the tick loop.
        key = str(chat_id)
        if not self._allowlist.permits(chat_id):
            raise TelegramSendBlocked(f"telegram_send_message blocked: chat {key!r} is not in the allow-list")
        if not self._gate.consume(chat_id, scope=current_scope()):
            raise TelegramSendBlocked("telegram_send_message blocked: no outstanding confirmation for this send")
        result = await asyncio.to_thread(self._client.send_message, chat_id=chat_id, text=text)
        return f"sent: message_id={result.get('message_id', '<unknown>')}"

    async def _send_document(self, chat_id: int | str, file_path: str, caption: str = "") -> str:
        # Same two guards as _send_message: allow-list then one-shot grant. The
        # file read and the blocking Bot API upload are offloaded to threads so
        # they never stall the tick loop. Path/sandbox/size are validated BEFORE
        # consuming the grant, so a rejected send never burns an approval.
        key = str(chat_id)
        if not self._allowlist.permits(chat_id):
            raise TelegramSendBlocked(f"telegram_send_document blocked: chat {key!r} is not in the allow-list")
        # Resolve symlinks so the sandbox check can't be bypassed via a link.
        path = os.path.realpath(os.fspath(file_path))
        if self._attachments_dir is not None and os.path.commonpath([self._attachments_dir, path]) != self._attachments_dir:
            raise TelegramSendBlocked(
                f"telegram_send_document blocked: {file_path!r} is outside the allowed attachments directory"
            )
        if not os.path.isfile(path):
            raise FileNotFoundError(f"telegram_send_document: file not found: {file_path!r}")
        size = os.path.getsize(path)
        if size > _MAX_DOCUMENT_BYTES:
            raise ValueError(
                f"telegram_send_document: {file_path!r} is {size} bytes, over Telegram's {_MAX_DOCUMENT_BYTES}-byte limit"
            )
        if not self._gate.consume(chat_id, scope=current_scope()):
            raise TelegramSendBlocked("telegram_send_document blocked: no outstanding confirmation for this send")
        blob = await asyncio.to_thread(_read_file, path)
        cap = caption[:_MAX_CAPTION_CHARS] if caption else None
        result = await asyncio.to_thread(
            self._client.send_document,
            chat_id=chat_id,
            document=blob,
            filename=os.path.basename(path),
            caption=cap,
        )
        return f"sent: message_id={result.get('message_id', '<unknown>')}"
