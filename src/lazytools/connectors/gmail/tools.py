"""Gmail tools for the worker.

Exposes four tools via the lazybridge ``ToolProvider`` protocol:

Read tools (always allowed):
* ``gmail_list_emails`` — list inbox messages matching a Gmail query.
* ``gmail_get_email``   — fetch headers + snippet of a single message.

Write tools:
* ``gmail_create_draft`` — create a draft (not sent; always allowed).
* ``gmail_send``         — send an email. Guarded by an optional allow-list
  and a one-shot confirmation gate so an agent can never send to arbitrary
  recipients or flood the inbox without explicit approval.

Confirmation is deliberately *not* a sticky boolean. A human approval (via the
review queue) authorizes **one** send — either any recipient (``confirm_once``)
or a specific one (``confirm_send(to=...)``). Each send consumes one matching
grant, so an approved single message can't silently authorize a flood.

A grant may additionally be **bound to a task** with ``task_id=`` (the
``task_id`` returned by ``approve_task`` / ``schedule``). A task-bound grant is
only consumable by *that* task's worker, so under
``max_concurrent_inbound > 1`` an approval for one task can never be spent by a
different task running at the same time. The binding works because the gated
send is an ``async`` tool: lazybridge runs it in the worker's own context,
where ``PulseAgent`` has published the active task id.
"""

from __future__ import annotations

import asyncio
from typing import Any

from lazybridge import Tool

from lazytools.connectors.gmail.client import GmailService
from lazytools.safety import ActionBlocked, Allowlist, ConfirmationGate, current_scope


class GmailSendBlocked(ActionBlocked):
    """Raised when ``gmail_send`` is invoked without authorization."""


class GmailTools:
    """A ``ToolProvider`` wrapping a :class:`GmailService` for the worker.

    Exposes four tools: ``gmail_list_emails``, ``gmail_get_email``,
    ``gmail_create_draft``, and ``gmail_send``.

    The underlying :class:`~lazytools.connectors.gmail.client.GmailClient`
    is thread-safe (serialises calls through an internal lock), so all four
    tools are safe to invoke from concurrent PulseAgent task workers.
    """

    _is_lazy_tool_provider = True

    def __init__(
        self,
        client: GmailService,
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
        allow-list). Call once per approved message. Pass ``task_id=`` to bind
        the grant to a single task so a concurrent task cannot consume it."""
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
                name="gmail_list_emails",
                description=(
                    "Search and list emails from the Gmail inbox. "
                    "All parameters are optional and combinable. "
                    "Args: "
                    "sender (str) — filter by sender address or name, e.g. 'alice@example.com'; "
                    "subject (str) — filter by subject keywords, e.g. 'invoice'; "
                    "contains (str) — filter by text anywhere in the email; "
                    "unread (bool, default False) — if True only return unread emails; "
                    "query (str) — raw Gmail search string for advanced filters "
                    "(combined with the other args if both provided); "
                    "max_results (int, default 10)."
                ),
            ),
            Tool.wrap(
                self._get_email,
                name="gmail_get_email",
                description=(
                    "Get headers and snippet of a single email by its message ID. "
                    "Args: message_id (str, from gmail_list_emails)."
                ),
            ),
            Tool.wrap(
                self._create_draft,
                name="gmail_create_draft",
                description="Create a Gmail draft (not sent). Args: to, subject, body.",
            ),
            Tool.wrap(
                self._send,
                name="gmail_send",
                description="Send an email via Gmail. Args: to, subject, body.",
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
        """Search Gmail inbox with optional filters.

        All parameters are combined with AND logic into a single Gmail query.
        ``query`` can be used alongside the structured filters for advanced
        operators (e.g. ``query="after:2026/01/01"``).
        """
        parts: list[str] = []
        if sender:
            parts.append(f"from:{sender}")
        if subject:
            # Quote multi-word subjects so Gmail treats them as a phrase
            parts.append(f'subject:("{subject}")' if " " in subject else f"subject:{subject}")
        if contains:
            parts.append(f'"{contains}"' if " " in contains else contains)
        if unread:
            parts.append("is:unread")
        if query:
            parts.append(query)
        # Default to inbox if no filters given
        final_query = " ".join(parts) if parts else "in:inbox"

        ids = self._client.list_message_ids(query=final_query, max_results=max_results)
        if not ids:
            return f"No messages found. (query: {final_query!r})"
        lines = [f"query: {final_query!r}"]
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
        # context (lazybridge runs async tools in-context). The blocking Gmail
        # API call is offloaded to a thread so it never stalls the tick loop.
        if not self._allowlist.permits(to):
            raise GmailSendBlocked(f"gmail_send blocked: recipient {to!r} is not in the allow-list")
        if not self._gate.consume(to, scope=current_scope()):
            raise GmailSendBlocked("gmail_send blocked: no outstanding confirmation for this send")
        result = await asyncio.to_thread(self._client.send_message, to=to, subject=subject, body=body)
        return f"sent: {result.get('id', '<unknown>')}"


def _headers(raw: dict[str, Any]) -> dict[str, str]:
    """Flatten a Gmail message resource's header list into a lowercase dict."""
    payload = raw.get("payload", {})
    result: dict[str, str] = {}
    for header in payload.get("headers", []):
        name = header.get("name", "").lower()
        if name and name not in result:
            result[name] = header.get("value", "")
    return result
