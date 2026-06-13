"""Outlook tools for the worker.

The mirror image of :mod:`lazytools.connectors.gmail.tools`, pointed at a
local Outlook desktop instead of the Gmail cloud API. Exposes four tools via
the lazybridge ``ToolProvider`` protocol:

Read tools (always allowed):
* ``outlook_list_emails`` — list inbox messages matching simple filters.
* ``outlook_get_email``   — fetch headers + snippet of a single message.

Write tools:
* ``outlook_create_draft`` — create a draft in Outlook's Drafts (not sent).
* ``outlook_send``         — send an email. Guarded by the same optional
  allow-list + one-shot confirmation gate as ``gmail_send`` (see that module
  for the rationale: a human approval authorizes exactly **one** send, and a
  grant may be bound to a task id so concurrent tasks can't spend each
  other's approvals).

The structured filters are translated to an Outlook **Restrict** DASL query
(``@SQL=...``) rather than a Gmail search string, but the tool surface and the
safety model are identical, so an agent written against ``GmailTools`` behaves
the same way here.
"""

from __future__ import annotations

import asyncio
from typing import Any

from lazybridge import Tool

from lazytools.connectors.outlook.client import OutlookService
from lazytools.safety import ActionBlocked, Allowlist, ConfirmationGate, current_scope

#: DASL property URNs used to build Outlook Restrict filters.
_DASL_SUBJECT = "urn:schemas:httpmail:subject"
_DASL_FROM_EMAIL = "urn:schemas:httpmail:fromemail"
_DASL_TEXT = "urn:schemas:httpmail:textdescription"
_DASL_READ = "urn:schemas:httpmail:read"


class OutlookSendBlocked(ActionBlocked):
    """Raised when ``outlook_send`` is invoked without authorization."""


class OutlookTools:
    """A ``ToolProvider`` wrapping an :class:`OutlookService` for the worker.

    Exposes ``outlook_list_emails``, ``outlook_get_email``,
    ``outlook_create_draft``, and ``outlook_send`` with the same Allowlist +
    ConfirmationGate guarding the send path as :class:`GmailTools`.
    """

    _is_lazy_tool_provider = True

    def __init__(
        self,
        client: OutlookService,
        *,
        allowed_recipients: list[str] | None = None,
        require_confirmation: bool = True,
    ) -> None:
        self._client = client
        self._allowlist = Allowlist(allowed_recipients)
        self._gate = ConfirmationGate(enabled=require_confirmation)

    @property
    def require_confirmation(self) -> bool:
        """Whether a send needs an outstanding confirmation (public attribute)."""
        return self._gate.enabled

    def confirm_once(self, *, task_id: str | None = None) -> None:
        """Authorize exactly one send to any recipient (subject to the
        allow-list). Pass ``task_id=`` to bind the grant to a single task."""
        self._gate.grant_any(scope=task_id)

    def confirm_send(self, *, to: str, task_id: str | None = None) -> None:
        """Authorize exactly one send to a specific recipient — the tighter,
        preferred grant. Pass ``task_id=`` to also bind it to a single task."""
        self._gate.grant(to, scope=task_id)

    # ------------------------------------------------------------------ #
    # ToolProvider
    # ------------------------------------------------------------------ #
    def as_tools(self) -> list[Tool]:
        return [
            Tool.wrap(
                self._list_emails,
                name="outlook_list_emails",
                description=(
                    "Search and list emails from the local Outlook inbox. "
                    "All parameters are optional and combinable. "
                    "Args: "
                    "sender (str) — filter by sender email address; "
                    "subject (str) — filter by subject keywords; "
                    "contains (str) — filter by text anywhere in the email; "
                    "unread (bool, default False) — if True only return unread emails; "
                    "query (str) — raw Outlook Restrict/DASL filter for advanced use "
                    "(combined with the other args if both provided); "
                    "max_results (int, default 10)."
                ),
            ),
            Tool.wrap(
                self._get_email,
                name="outlook_get_email",
                description=(
                    "Get headers and snippet of a single email by its Outlook entry ID. "
                    "Args: message_id (str, from outlook_list_emails)."
                ),
            ),
            Tool.wrap(
                self._create_draft,
                name="outlook_create_draft",
                description="Create an Outlook draft (not sent). Args: to, subject, body.",
            ),
            Tool.wrap(
                self._send,
                name="outlook_send",
                description="Send an email via local Outlook. Args: to, subject, body.",
            ),
        ]

    # ------------------------------------------------------------------ #
    # Tool implementations
    # ------------------------------------------------------------------ #
    def _list_emails(
        self,
        sender: str | None = None,
        subject: str | None = None,
        contains: str | None = None,
        unread: bool = False,
        query: str | None = None,
        max_results: int = 10,
    ) -> str:
        """Search the Outlook inbox with optional filters.

        Structured filters are combined with AND into a single Outlook
        Restrict (DASL ``@SQL=``) expression. A raw ``query`` is folded in as
        an extra DASL clause when structured filters are present, or passed
        through **unchanged** when it is the only filter (so a complete
        macro/DASL filter is never re-wrapped into an invalid one).
        """
        clauses: list[str] = []
        if sender:
            clauses.append(f'"{_DASL_FROM_EMAIL}" LIKE \'%{_escape(sender)}%\'')
        if subject:
            clauses.append(f'"{_DASL_SUBJECT}" LIKE \'%{_escape(subject)}%\'')
        if contains:
            clauses.append(f'"{_DASL_TEXT}" LIKE \'%{_escape(contains)}%\'')
        if unread:
            clauses.append(f'"{_DASL_READ}" = 0')

        final_query: str | None
        if clauses:
            # Structured filters compile to a DASL @SQL= restriction. A raw
            # query is folded in as an extra clause; strip a leading "@SQL="
            # the caller may have prefixed so the combined filter isn't
            # doubled to "@SQL=...@SQL=...".
            if query:
                clauses.append(query[len("@SQL="):] if query.startswith("@SQL=") else query)
            final_query = "@SQL=" + " AND ".join(clauses)
        else:
            # No structured filters: pass the raw Outlook Restrict filter
            # through untouched. It may be macro syntax (e.g. "[Unread] = true")
            # or its own "@SQL=..." DASL — wrapping either in another "@SQL="
            # makes Outlook reject it as an invalid filter.
            final_query = query or None

        ids = self._client.list_message_ids(query=final_query, max_results=max_results)
        if not ids:
            return f"No messages found. (filter: {final_query!r})"
        lines = [f"filter: {final_query!r}"]
        for msg_id in ids:
            raw = self._client.get_message(msg_id)
            hdrs = _headers(raw)
            lines.append(
                f"- id={msg_id}  from={hdrs.get('from', 'unknown')}  "
                f"subject={hdrs.get('subject', '(no subject)')}"
            )
        return "\n".join(lines)

    def _get_email(self, message_id: str) -> str:
        raw = self._client.get_message(message_id)
        headers = _headers(raw)
        return (
            f"From: {headers.get('from', 'unknown')}\n"
            f"Date: {headers.get('date', 'unknown')}\n"
            f"Subject: {headers.get('subject', '(no subject)')}\n\n"
            f"Snippet: {raw.get('snippet', '')}"
        )

    def _create_draft(self, to: str, subject: str, body: str) -> str:
        result = self._client.create_draft(to=to, subject=subject, body=body)
        return f"draft created: {result.get('id', '<unknown>')}"

    async def _send(self, to: str, subject: str, body: str) -> str:
        # Async so the task-bound grant check can read the worker's task
        # context; the blocking COM call is offloaded to a thread so it never
        # stalls the tick loop.
        if not self._allowlist.permits(to):
            raise OutlookSendBlocked(f"outlook_send blocked: recipient {to!r} is not in the allow-list")
        if not self._gate.consume(to, scope=current_scope()):
            raise OutlookSendBlocked("outlook_send blocked: no outstanding confirmation for this send")
        result = await asyncio.to_thread(self._client.send_message, to=to, subject=subject, body=body)
        return f"sent: {result.get('id', '<unknown>')}"


def _escape(value: str) -> str:
    """Escape a value for inclusion in a DASL string literal.

    Single quotes delimit DASL literals; double them to embed one, and drop
    newlines so a filter value can't break out of its clause.
    """
    return value.replace("'", "''").replace("\r", " ").replace("\n", " ")


def _headers(raw: dict[str, Any]) -> dict[str, str]:
    """Flatten an Outlook message resource's header list into a lowercase dict."""
    payload = raw.get("payload", {})
    result: dict[str, str] = {}
    for header in payload.get("headers", []):
        name = header.get("name", "").lower()
        if name and name not in result:
            result[name] = header.get("value", "")
    return result
