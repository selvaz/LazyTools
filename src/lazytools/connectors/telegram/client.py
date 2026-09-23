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


class TelegramAPIError(RuntimeError):
    """A Telegram Bot API call failed with a machine-readable reason.

    Raised instead of a bare ``RuntimeError`` whenever the Bot API responds
    with ``ok: false`` (whether or not the HTTP status was also an error) or
    the error body couldn't be parsed as JSON. ``str(exc)`` never contains
    the request URL or bot token — only ``method`` and the redacted
    ``description`` — so it is safe to log directly, unlike the ``httpx``
    exception it replaces (whose message embeds the token in the URL).

    Kept a subclass of ``RuntimeError`` with the same
    ``"Telegram API call '<method>' failed: ..."`` message prefix so
    existing callers that pattern-match on that text keep working.
    """

    def __init__(
        self,
        method: str,
        *,
        http_status: int | None = None,
        error_code: int | None = None,
        description: str,
        retry_after: int | None = None,
        migrate_to_chat_id: int | None = None,
    ) -> None:
        self.method = method
        self.http_status = http_status
        self.error_code = error_code
        self.description = description
        self.retry_after = retry_after
        self.migrate_to_chat_id = migrate_to_chat_id
        detail = f"HTTP {http_status}: {description}" if http_status is not None else description
        super().__init__(f"Telegram API call {method!r} failed: {detail}")


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
        sep_len = 0
        for sep in ("\n\n", "\n", " "):
            cut = window.rfind(sep)
            if cut > 0:
                sep_len = len(sep)
                break
        if cut <= 0:
            cut, sep_len = limit, 0  # no natural break — hard cut
        # Drop exactly the separator, nothing more: stripping further would
        # eat significant whitespace (e.g. the indentation of a code block
        # that happens to start the next chunk) and alter the relayed text.
        chunks.append(text[:cut])
        text = text[cut + sep_len :]
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
                'Install it with: pip install "lazytoolkit[telegram] @ git+https://github.com/selvaz/LazyTools.git"'
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

    def _api_error(self, method: str, resp: Any, data: dict[str, Any]) -> TelegramAPIError:
        """Build a :class:`TelegramAPIError` from a parsed ``ok: false`` body.

        Applies whether the HTTP status was itself an error (4xx/5xx, the
        common case) or the call returned ``200`` with ``ok: false`` in the
        body (Telegram does this for some soft failures) — the Bot API's own
        ``description`` is what actually explains the failure either way.
        """
        parameters = data.get("parameters") or {}
        description = self._redact(str(data.get("description") or data))
        return TelegramAPIError(
            method,
            http_status=getattr(resp, "status_code", None),
            error_code=data.get("error_code"),
            description=description,
            retry_after=parameters.get("retry_after"),
            migrate_to_chat_id=parameters.get("migrate_to_chat_id"),
        )

    def _fallback_error(self, method: str, resp: Any) -> TelegramAPIError:
        """Build a :class:`TelegramAPIError` when the error body wasn't JSON
        (or wasn't a Telegram-shaped object), from HTTP status alone."""
        status = getattr(resp, "status_code", None)
        reason = getattr(resp, "reason_phrase", None) or getattr(resp, "reason", None)
        if status is None:
            # No status attribute to build a description from (e.g. a bare
            # test double) — fall back to whatever raise_for_status says,
            # redacted; it may embed the URL, so never chain it (`from None`
            # at the call site) and never surface it unredacted.
            try:
                resp.raise_for_status()
            except Exception as exc:
                return TelegramAPIError(method, description=self._redact(str(exc)))
            return TelegramAPIError(method, description="response body was not valid JSON")
        description = f"response body was not valid JSON (status {status}"
        description += f" {reason})" if reason else ")"
        return TelegramAPIError(method, http_status=status, description=self._redact(description))

    def _parse_response(self, method: str, resp: Any) -> Any:
        try:
            data = resp.json()
        except Exception:
            data = None
        if isinstance(data, dict) and "ok" in data:
            if data.get("ok", False):
                return data.get("result")
            raise self._api_error(method, resp, data) from None
        raise self._fallback_error(method, resp) from None

    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        if self._http is None:
            raise RuntimeError("TelegramClient has no HTTP client; use from_token() or inject http=")
        try:
            resp = self._http.post(f"{self._base}/{method}", json=payload)
        except Exception as exc:
            # The Bot API embeds the token in the URL, and httpx error
            # messages include the URL — re-raise with the token redacted
            # and without chaining (the original message would leak it into
            # logged tracebacks). This is the network/timeout path; HTTP
            # error *responses* are handled below, after we've had a chance
            # to read the JSON body Telegram sends with them.
            raise RuntimeError(f"Telegram API call {method!r} failed: {self._redact(str(exc))}") from None
        return self._parse_response(method, resp)

    def _call_multipart(self, method: str, data: dict[str, Any], files: dict[str, Any]) -> Any:
        """Like :meth:`_call` but for a ``multipart/form-data`` upload
        (``sendDocument``). Same token-redaction-on-error contract."""
        if self._http is None:
            raise RuntimeError("TelegramClient has no HTTP client; use from_token() or inject http=")
        try:
            resp = self._http.post(f"{self._base}/{method}", data=data, files=files)
        except Exception as exc:
            raise RuntimeError(f"Telegram API call {method!r} failed: {self._redact(str(exc))}") from None
        return self._parse_response(method, resp)

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
