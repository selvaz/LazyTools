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


class GmailService(Protocol):
    """The subset of a Gmail client that LazyPulse uses."""

    def list_message_ids(self, *, query: str | None = None, max_results: int = 25) -> list[str]: ...
    def get_message(self, message_id: str) -> dict[str, Any]: ...
    def create_draft(self, *, to: str, subject: str, body: str) -> dict[str, Any]: ...
    def send_message(self, *, to: str, subject: str, body: str) -> dict[str, Any]: ...


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
