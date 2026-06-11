"""Thin wrapper around the Gmail REST API.

The Google client libraries are imported **lazily**, inside
:meth:`GmailClient.from_credentials`, so this module imports cleanly without
the ``gmail`` extra. :class:`~lazytools.connectors.gmail.tools.GmailTools`
depends only on the duck-typed :class:`GmailService` surface defined here,
which means tests inject a fake client and never touch Google.

Thread-safety note
------------------
``httplib2`` (used internally by ``googleapiclient``) is **not thread-safe**.
:class:`GmailClient` serialises every API call through a per-instance
``threading.Lock`` so that multiple worker threads (e.g. concurrent
:class:`~lazypulse.PulseAgent` tasks running via ``asyncio.to_thread`` /
``loop.run_in_executor``) can safely share a single client without corrupting
the underlying SSL/HTTP connection state.
"""

from __future__ import annotations

import base64
import threading
from email.mime.text import MIMEText
from typing import Any, Protocol

#: Headers we ask Gmail to return in ``format="metadata"`` reads. Keeping the
#: read at metadata scope (rather than full) is what lets a deployment stay
#: on the narrow ``gmail.metadata`` OAuth scope.
METADATA_HEADERS = ["From", "To", "Subject", "Date", "Authentication-Results"]


class GmailHistoryExpired(RuntimeError):
    """``startHistoryId`` is too old — Gmail expired the history window.

    Gmail keeps mailbox history for a limited period (typically about a
    week). Callers must treat this as "resynchronise": fetch a fresh
    cursor via :meth:`GmailService.get_history_id` and accept that changes
    inside the expired window are unknowable through the history API.
    """


class GmailService(Protocol):
    """The subset of a Gmail client that LazyPulse uses.

    The history/watch methods power event-driven intake (Gmail push
    notifications via Cloud Pub/Sub) and incremental sync; consumers that
    only poll (``GmailInbox``) never call them, so existing duck-typed
    fakes remain valid.
    """

    def list_message_ids(self, *, query: str | None = None, max_results: int = 25) -> list[str]: ...
    def get_message(self, message_id: str) -> dict[str, Any]: ...
    def create_draft(self, *, to: str, subject: str, body: str) -> dict[str, Any]: ...
    def send_message(self, *, to: str, subject: str, body: str) -> dict[str, Any]: ...
    def get_history_id(self) -> str: ...
    def list_history_message_ids(
        self, *, start_history_id: str, max_results: int = 100
    ) -> tuple[list[str], str]: ...
    def watch(self, *, topic_name: str, label_ids: list[str] | None = None) -> dict[str, Any]: ...
    def stop_watch(self) -> None: ...


class GmailClient:
    """Production :class:`GmailService` backed by ``googleapiclient``.

    All methods acquire a per-instance lock before touching the underlying
    ``googleapiclient`` resource so the client is safe to call from multiple
    threads concurrently (e.g. parallel PulseAgent task workers).
    """

    def __init__(self, service: Any) -> None:
        # ``service`` is a googleapiclient Resource (or any object exposing
        # the same ``users().messages()`` shape).
        self._service = service
        self._lock = threading.Lock()

    @classmethod
    def from_credentials(
        cls,
        *,
        credentials_path: str,
        token_path: str,
        scopes: list[str],
    ) -> GmailClient:
        """Build a client from an OAuth client-secret + cached token file.

        Imports the Google libraries lazily; raises a friendly
        ``ImportError`` if the ``gmail`` extra is not installed.
        """
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
        except ImportError as exc:  # pragma: no cover — exercised only without the extra
            raise ImportError(
                "GmailClient.from_credentials requires the 'gmail' extra. "
                "Install it with: pip install 'lazytoolkit[gmail]'"
            ) from exc

        import os

        creds = None
        if os.path.exists(token_path):
            creds = Credentials.from_authorized_user_file(token_path, scopes)
        if creds is None or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(credentials_path, scopes)
                creds = flow.run_local_server(port=0)
            with open(token_path, "w") as fh:
                fh.write(creds.to_json())
        # The cached token holds a long-lived OAuth refresh token; a
        # world-readable file (default umask often yields 0644) would let any
        # local user steal it.  Tighten to owner-only whenever the token file
        # exists — this also covers a still-valid token written by an older
        # version with loose permissions, where the rewrite branch above is
        # skipped entirely.
        if os.path.exists(token_path):
            try:
                os.chmod(token_path, 0o600)
            except OSError:  # pragma: no cover — e.g. unusual filesystems
                pass
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return cls(service)

    # ------------------------------------------------------------------ #
    # GmailService
    # ------------------------------------------------------------------ #
    def list_message_ids(self, *, query: str | None = None, max_results: int = 25) -> list[str]:
        with self._lock:
            resp = self._service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        return [m["id"] for m in resp.get("messages", [])]

    def get_message(self, message_id: str) -> dict[str, Any]:
        with self._lock:
            return (
                self._service.users()
                .messages()
                .get(userId="me", id=message_id, format="metadata", metadataHeaders=METADATA_HEADERS)
                .execute()
            )

    def create_draft(self, *, to: str, subject: str, body: str) -> dict[str, Any]:
        raw = _encode(to, subject, body)
        with self._lock:
            return self._service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()

    def send_message(self, *, to: str, subject: str, body: str) -> dict[str, Any]:
        raw = _encode(to, subject, body)
        with self._lock:
            return self._service.users().messages().send(userId="me", body={"raw": raw}).execute()

    # ------------------------------------------------------------------ #
    # History-based incremental sync + push notifications
    # ------------------------------------------------------------------ #
    def get_history_id(self) -> str:
        """Current mailbox history cursor (``users.getProfile``).

        One quota-cheap call that anchors incremental sync: changes after
        this point are retrievable via :meth:`list_history_message_ids`.
        """
        with self._lock:
            profile = self._service.users().getProfile(userId="me").execute()
        return str(profile["historyId"])

    def list_history_message_ids(
        self, *, start_history_id: str, max_results: int = 100
    ) -> tuple[list[str], str]:
        """Message ids added since ``start_history_id``, plus the new cursor.

        Uses ``users.history.list`` with ``historyTypes=messageAdded`` —
        the quota-cheap incremental alternative to re-listing the mailbox.
        Paginates internally (bounded), de-duplicates ids, and returns
        ``(message_ids, new_history_id)``; persist the returned cursor and
        pass it back next time.

        Raises :class:`GmailHistoryExpired` when Gmail reports the cursor
        is older than its retention window (HTTP 404) — resynchronise via
        :meth:`get_history_id`.
        """
        ids: list[str] = []
        seen: set[str] = set()
        new_cursor = str(start_history_id)
        page_token: str | None = None
        for _ in range(20):  # hard page cap — a tick should never walk an unbounded mailbox
            with self._lock:
                request = (
                    self._service.users()
                    .history()
                    .list(
                        userId="me",
                        startHistoryId=start_history_id,
                        historyTypes=["messageAdded"],
                        maxResults=min(max_results, 500),
                        pageToken=page_token,
                    )
                )
                try:
                    resp = request.execute()
                except Exception as exc:
                    if _http_status(exc) == 404:
                        raise GmailHistoryExpired(
                            f"Gmail history id {start_history_id!r} has expired; "
                            "resync with get_history_id()."
                        ) from exc
                    raise
            new_cursor = str(resp.get("historyId", new_cursor))
            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    message_id = added.get("message", {}).get("id")
                    if message_id and message_id not in seen:
                        seen.add(message_id)
                        ids.append(message_id)
            page_token = resp.get("nextPageToken")
            if not page_token or len(ids) >= max_results:
                break
        return ids[:max_results], new_cursor

    def watch(self, *, topic_name: str, label_ids: list[str] | None = None) -> dict[str, Any]:
        """Arm Gmail push notifications onto a Cloud Pub/Sub topic.

        Returns the API response: ``{"historyId": ..., "expiration": ...}``
        (``expiration`` is epoch **milliseconds** as a string; Gmail expires
        a watch after at most 7 days — re-arm before then).
        """
        body: dict[str, Any] = {
            "topicName": topic_name,
            "labelIds": label_ids or ["INBOX"],
            "labelFilterBehavior": "INCLUDE",
        }
        with self._lock:
            return self._service.users().watch(userId="me", body=body).execute()

    def stop_watch(self) -> None:
        """Disarm push notifications (``users.stop``)."""
        with self._lock:
            self._service.users().stop(userId="me").execute()


def _http_status(exc: Exception) -> int | None:
    """Best-effort HTTP status from a googleapiclient error, without
    importing google libraries (keeps the module import-clean sans extra)."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    return status if isinstance(status, int) else None


def _encode(to: str, subject: str, body: str) -> str:
    # Defence-in-depth against header injection: modern CPython raises on
    # embedded newlines in header values, but validate explicitly so the
    # failure is a clear, version-independent error rather than a stdlib
    # internals one.
    for field, value in (("to", to), ("subject", subject)):
        if "\r" in value or "\n" in value:
            raise ValueError(f"gmail {field} header must not contain newline characters")
    msg = MIMEText(body)
    msg["to"] = to
    msg["subject"] = subject
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()
