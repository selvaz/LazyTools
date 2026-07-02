"""Thin wrapper around the Telegram Bot API.

``httpx`` is imported lazily inside :meth:`TelegramClient.from_token`, so this
module imports cleanly without the ``telegram`` extra.
:class:`~lazytools.connectors.telegram.tools.TelegramTools` depends only on the
duck-typed :class:`TelegramService` surface defined here, which means tests
inject a fake client and never touch the network.

Why not aiogram / python-telegram-bot? Those ship a *dispatcher* that runs its
own polling loop — a second loop competing with the PulseAgent's tick loop.
LazyPulse only needs two Bot API methods (``getUpdates`` + ``sendMessage``),
which map cleanly onto the adapter's pull-based ``drain()``, so a small HTTP
wrapper keeps the dependency surface minimal. Swap in your own
``TelegramService`` (e.g. aiogram-backed) if you prefer.
"""

from __future__ import annotations

from typing import Any, Protocol

#: Telegram Bot API hard limit on ``sendMessage`` text length.
MAX_MESSAGE_CHARS = 4096


def split_message(text: str, *, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split ``text`` into chunks the Bot API will accept (each ≤ ``limit``).

    Telegram rejects ``sendMessage`` payloads over 4096 characters outright,
    so any caller relaying model output must chunk. Splits prefer paragraph
    breaks, then line breaks, then spaces, and hard-cut only as a last resort,
    so chunks stay readable. Returns ``[]`` for empty text.
    """
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    while len(text) > limit:
        window = text[:limit]
        cut = -1
        for sep in ("\n\n", "\n", " "):
            cut = window.rfind(sep)
            if cut > 0:
                break
        if cut <= 0:
            cut = limit  # no natural break — hard cut
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


class TelegramService(Protocol):
    """The subset of a Telegram Bot API client that LazyPulse uses."""

    def get_updates(self, *, offset: int, timeout: int = 0, limit: int = 100) -> list[dict[str, Any]]: ...
    def send_message(self, *, chat_id: int | str, text: str) -> dict[str, Any]: ...
    def send_document(
        self, *, chat_id: int | str, document: bytes, filename: str = "document", caption: str | None = None
    ) -> dict[str, Any]: ...


class TelegramClient:
    """Production :class:`TelegramService` backed by the Bot API over HTTPS."""

    def __init__(self, token: str, *, http: Any | None = None, base_url: str = "https://api.telegram.org") -> None:
        # ``http`` is an ``httpx.Client`` (or any object exposing
        # ``post(url, json=...) -> response`` with ``raise_for_status`` + ``json``).
        self._token = token
        self._base = f"{base_url}/bot{token}"
        self._http = http

    @classmethod
    def from_token(cls, token: str, *, timeout: float = 30.0) -> TelegramClient:
        """Build a client from a bot token (obtained from @BotFather).

        Imports ``httpx`` lazily; raises a friendly ``ImportError`` if the
        ``telegram`` extra is not installed.
        """
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover — exercised only without the extra
            raise ImportError(
                "TelegramClient.from_token requires the 'telegram' extra. "
                "Install it with: pip install 'lazytoolkit[telegram]'"
            ) from exc
        return cls(token, http=httpx.Client(timeout=timeout))

    def close(self) -> None:
        """Close the underlying HTTP client (its connection pool), if it has one."""
        close = getattr(self._http, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> TelegramClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "<bot-token>") if self._token else text

    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        if self._http is None:
            raise RuntimeError("TelegramClient has no HTTP client; use from_token() or inject http=")
        try:
            resp = self._http.post(f"{self._base}/{method}", json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            # The Bot API embeds the token in the URL, and httpx error
            # messages include the URL — re-raise with the token redacted
            # and without chaining (the original message would leak it into
            # logged tracebacks).
            raise RuntimeError(f"Telegram API call {method!r} failed: {self._redact(str(exc))}") from None
        if not data.get("ok", False):
            raise RuntimeError(f"Telegram API error on {method}: {self._redact(str(data.get('description', data)))}")
        return data.get("result")

    def _call_multipart(self, method: str, data: dict[str, Any], files: dict[str, Any]) -> Any:
        """Like :meth:`_call` but for a ``multipart/form-data`` upload
        (``sendDocument``). Same token-redaction-on-error contract."""
        if self._http is None:
            raise RuntimeError("TelegramClient has no HTTP client; use from_token() or inject http=")
        try:
            resp = self._http.post(f"{self._base}/{method}", data=data, files=files)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            raise RuntimeError(f"Telegram API call {method!r} failed: {self._redact(str(exc))}") from None
        if not payload.get("ok", False):
            raise RuntimeError(f"Telegram API error on {method}: {self._redact(str(payload.get('description', payload)))}")
        return payload.get("result")

    # ------------------------------------------------------------------ #
    # TelegramService
    # ------------------------------------------------------------------ #
    def get_updates(self, *, offset: int, timeout: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        result = self._call("getUpdates", {"offset": offset, "timeout": timeout, "limit": limit})
        return list(result or [])

    def send_message(self, *, chat_id: int | str, text: str) -> dict[str, Any]:
        return dict(self._call("sendMessage", {"chat_id": chat_id, "text": text}) or {})

    def send_document(
        self,
        *,
        chat_id: int | str,
        document: bytes,
        filename: str = "document",
        caption: str | None = None,
    ) -> dict[str, Any]:
        """Upload ``document`` (raw bytes) to ``chat_id`` via ``sendDocument``.

        ``filename`` is the name shown in Telegram; ``caption`` is optional
        (≤1024 chars, enforced by the caller). Returns the Bot API ``result``.
        """
        data: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        files = {"document": (filename, document)}
        return dict(self._call_multipart("sendDocument", data, files) or {})
