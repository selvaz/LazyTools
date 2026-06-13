"""Thin wrapper around a **locally running** Outlook desktop (Windows).

Where :mod:`lazytools.connectors.gmail.client` talks to Gmail's cloud REST API
(OAuth, quota, Pub/Sub for push), this client talks to the copy of Outlook
that is *already open and signed in* on the user's Windows machine, over COM
(MAPI). That trade is the whole point: **no cloud credentials, no API quota,
no Pub/Sub** — the connector reuses the desktop app's existing session and
reads the local message store. The price is that it only works where Outlook
desktop runs (Windows), and the LazyPulse daemon must run on that same
machine.

``pywin32`` is imported **lazily**, inside :meth:`OutlookClient.connect`, so
this module imports cleanly on any platform (Linux/macOS CI, tests) without
the ``outlook`` extra. :class:`~lazytools.connectors.outlook.tools.OutlookTools`
and the LazyPulse ``OutlookInbox`` depend only on the duck-typed
:class:`OutlookService` surface defined here, so tests inject a fake client and
never touch COM.

Message shape
-------------
:meth:`get_message` returns a dict in the **same shape** as the Gmail message
resource — ``{"payload": {"headers": [{"name", "value"}, ...]}, "snippet": ...}``
— so the authentication-aware ``InboundMessage`` conversion in LazyPulse is
shared verbatim between the two connectors. The genuine, top-most
``Authentication-Results`` header is lifted out of the message's transport
headers (``PR_TRANSPORT_MESSAGE_HEADERS``) and placed first, preserving the
first-wins anti-spoofing property the Gmail path relies on.

Thread-safety / COM affinity
----------------------------
COM objects have apartment affinity: they are happiest used from the thread
that created them. Like :class:`~lazytools.connectors.gmail.client.GmailClient`,
this client serialises every call through a per-instance ``threading.Lock``.
When driving it from worker threads (e.g. PulseAgent tasks offloaded via
``asyncio.to_thread``) call :func:`pythoncom.CoInitialize` once on each such
thread, or confine the client to a single-threaded executor.
"""

from __future__ import annotations

import re
import threading
from typing import Any, Protocol

#: ``olFolderInbox`` — the default Inbox folder index for ``GetDefaultFolder``.
_OL_FOLDER_INBOX = 6
#: ``olMailItem`` — the item type passed to ``CreateItem`` for a new email.
_OL_MAIL_ITEM = 0
#: ``PR_TRANSPORT_MESSAGE_HEADERS`` (PT_UNICODE) — the raw RFC 822 header block
#: the receiving server stamped on the message. This is where the genuine
#: ``Authentication-Results`` header lives.
_PR_TRANSPORT_HEADERS = "http://schemas.microsoft.com/mapi/proptag/0x007D001F"
#: ``PR_SMTP_ADDRESS`` (PT_UNICODE) — the sender's SMTP address, used to turn an
#: Exchange ``/o=.../cn=...`` sender into a real ``user@host`` address.
_PR_SMTP_ADDRESS = "http://schemas.microsoft.com/mapi/proptag/0x39FE001F"


class OutlookService(Protocol):
    """The subset of an Outlook client that LazyPulse / OutlookTools use.

    Deliberately mirrors the read/draft/send slice of
    :class:`~lazytools.connectors.gmail.client.GmailService` so the inbox
    adapter and tool provider are near-identical between the two connectors.
    There is no history/watch surface: local polling replaces push, with no
    cloud quota to conserve.
    """

    def list_message_ids(self, *, query: str | None = None, max_results: int = 25) -> list[str]: ...
    def get_message(self, message_id: str) -> dict[str, Any]: ...
    def create_draft(self, *, to: str, subject: str, body: str) -> dict[str, Any]: ...
    def send_message(self, *, to: str, subject: str, body: str) -> dict[str, Any]: ...


class OutlookClient:
    """Production :class:`OutlookService` backed by a local Outlook via COM.

    Build one with :meth:`connect` (attaches to the running/registered
    Outlook application). All methods acquire a per-instance lock before
    touching COM so the client is safe to share across threads, subject to the
    COM affinity note in the module docstring.
    """

    def __init__(self, namespace: Any, application: Any, *, folder_index: int = _OL_FOLDER_INBOX) -> None:
        # ``namespace`` is a MAPI namespace; ``application`` is the Outlook
        # Application object (kept for CreateItem on the send path).
        self._namespace = namespace
        self._application = application
        self._folder_index = folder_index
        self._lock = threading.Lock()

    @classmethod
    def connect(cls, *, folder_index: int = _OL_FOLDER_INBOX) -> OutlookClient:
        """Attach to the local Outlook desktop application via COM.

        Imports ``pywin32`` lazily; raises a friendly ``ImportError`` if the
        ``outlook`` extra (or a non-Windows platform) makes it unavailable.
        Outlook must be installed and signed in; the call reuses that session,
        so there are no separate credentials to manage.
        """
        try:
            import win32com.client  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover — exercised only without the extra
            raise ImportError(
                "OutlookClient.connect requires the 'outlook' extra on Windows "
                "(local Outlook desktop + pywin32). Install it with: "
                "pip install 'lazytoolkit[outlook]'"
            ) from exc
        application = win32com.client.Dispatch("Outlook.Application")
        namespace = application.GetNamespace("MAPI")
        return cls(namespace, application, folder_index=folder_index)

    # ------------------------------------------------------------------ #
    # OutlookService
    # ------------------------------------------------------------------ #
    def list_message_ids(self, *, query: str | None = None, max_results: int = 25) -> list[str]:
        """Entry IDs of messages in the watched folder, newest first.

        ``query`` is an Outlook **Restrict** filter (DASL or the
        ``"[Field] = 'value'"`` macro syntax), e.g. ``"[Unread] = true"``;
        ``None`` returns the whole folder (capped at ``max_results``).
        """
        with self._lock:
            folder = self._namespace.GetDefaultFolder(self._folder_index)
            items = folder.Items
            items.Sort("[ReceivedTime]", True)  # descending → newest first
            if query:
                items = items.Restrict(query)
            out: list[str] = []
            item = items.GetFirst()
            while item is not None and len(out) < max_results:
                entry_id = getattr(item, "EntryID", None)
                if entry_id:
                    out.append(entry_id)
                item = items.GetNext()
            return out

    def get_message(self, message_id: str) -> dict[str, Any]:
        with self._lock:
            item = self._namespace.GetItemFromID(message_id)
            return self._to_resource(message_id, item)

    def create_draft(self, *, to: str, subject: str, body: str) -> dict[str, Any]:
        _reject_header_injection(to=to, subject=subject)
        with self._lock:
            mail = self._application.CreateItem(_OL_MAIL_ITEM)
            mail.To = to
            mail.Subject = subject
            mail.Body = body
            mail.Save()  # lands in Drafts, not sent
            return {"id": getattr(mail, "EntryID", "<unknown>")}

    def send_message(self, *, to: str, subject: str, body: str) -> dict[str, Any]:
        _reject_header_injection(to=to, subject=subject)
        with self._lock:
            mail = self._application.CreateItem(_OL_MAIL_ITEM)
            mail.To = to
            mail.Subject = subject
            mail.Body = body
            entry_id = getattr(mail, "EntryID", "<unknown>")
            mail.Send()
            return {"id": entry_id}

    # ------------------------------------------------------------------ #
    # COM item → Gmail-shaped resource
    # ------------------------------------------------------------------ #
    def _to_resource(self, message_id: str, item: Any) -> dict[str, Any]:
        raw_headers = _read_property(item, _PR_TRANSPORT_HEADERS) or ""
        auth = _first_header(raw_headers, "Authentication-Results")
        headers = [
            {"name": "From", "value": _sender_address(item)},
            {"name": "To", "value": str(getattr(item, "To", "") or "")},
            {"name": "Subject", "value": str(getattr(item, "Subject", "") or "")},
            {"name": "Date", "value": str(getattr(item, "ReceivedTime", "") or "")},
        ]
        if auth is not None:
            headers.append({"name": "Authentication-Results", "value": auth})
        body = str(getattr(item, "Body", "") or "")
        return {"id": message_id, "snippet": _snippet(body), "payload": {"headers": headers}}


def _sender_address(item: Any) -> str:
    """``"Display Name <smtp@host>"`` for an Outlook item, robust to Exchange.

    ``SenderEmailAddress`` is an opaque ``/o=.../cn=...`` X.500 string for
    Exchange senders; prefer the resolved SMTP address from
    ``PR_SMTP_ADDRESS`` when present so owner-matching in the policy works.
    """
    name = str(getattr(item, "SenderName", "") or "")
    smtp = _read_property(item, _PR_SMTP_ADDRESS)
    if not smtp:
        smtp = str(getattr(item, "SenderEmailAddress", "") or "")
    if name and smtp:
        return f"{name} <{smtp}>"
    return smtp or name


def _read_property(item: Any, schema: str) -> str | None:
    """Best-effort read of a MAPI property via ``PropertyAccessor``.

    Returns ``None`` when the property is absent or the accessor raises (COM
    surfaces "property not found" as an exception, not an empty value).
    """
    try:
        accessor = item.PropertyAccessor
        value = accessor.GetProperty(schema)
    except Exception:
        return None
    return str(value) if value else None


_WS_RE = re.compile(r"\s+")


def _snippet(body: str, *, limit: int = 200) -> str:
    """A one-line preview: collapse whitespace and truncate, like Gmail's."""
    collapsed = _WS_RE.sub(" ", body).strip()
    return collapsed[:limit]


def _first_header(raw_headers: str, name: str) -> str | None:
    """Return the value of the **first** occurrence of ``name`` in an RFC 822
    header block, unfolding continuation lines.

    First-wins is deliberate: the receiving server prepends its genuine
    ``Authentication-Results`` at the top, so a forged copy carried lower in
    the message is ignored — the same defence the Gmail path gets from
    Gmail stripping inbound copies of its own authserv-id.
    """
    if not raw_headers:
        return None
    target = name.lower()
    lines = raw_headers.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        head, sep, value = line.partition(":")
        if sep and head.strip().lower() == target:
            collected = [value.strip()]
            i += 1
            # Unfold: continuation lines begin with whitespace.
            while i < len(lines) and lines[i][:1] in (" ", "\t"):
                collected.append(lines[i].strip())
                i += 1
            return " ".join(p for p in collected if p)
        i += 1
    return None


def _reject_header_injection(*, to: str, subject: str) -> None:
    # Defence-in-depth against header injection on the send path, mirroring the
    # Gmail client: a clear, explicit error rather than relying on Outlook's
    # internals to reject embedded newlines.
    for field, value in (("to", to), ("subject", subject)):
        if "\r" in value or "\n" in value:
            raise ValueError(f"outlook {field} header must not contain newline characters")
