"""Reusable safety primitives for dangerous tools.

Two independent gates that ``GmailTools`` / ``TelegramTools`` (and any future
guarded tool) compose:

* :class:`Allowlist` — a case-insensitive target allow-list. ``None`` means
  "no allow-list configured" → permit everything; an empty iterable denies
  everything.
* :class:`ConfirmationGate` — one-shot, target-bound confirmation grants. Not a
  sticky boolean: each grant authorizes exactly one action and is consumed on
  use, so an approved single message can never silently authorize a flood.

A grant may additionally be bound to an opaque **scope** (the running task id,
in LazyPulse) read from :func:`current_scope`. Denials raise a typed
:class:`ActionBlocked` subclass.

A third, independent primitive lives in :mod:`lazytools.safety.urls`:
:func:`validate_public_url` — a small SSRF guard for connector-constructed
URLs (http(s)-only schemes, optional host pinning, non-global literal IPs
refused). Denials raise :class:`UrlBlocked`.
"""

from __future__ import annotations

from lazytools.safety.allowlist import Allowlist
from lazytools.safety.context import active_scope, current_scope
from lazytools.safety.gates import ConfirmationGate


class ActionBlocked(PermissionError):
    """Base for dangerous-action denials (allow-list / confirmation).

    Subclasses ``PermissionError`` so existing ``except PermissionError``
    handlers keep working. Carries an audit-friendly message that names the
    action and the reason and never leaks secrets.
    """


# Imported after ActionBlocked is defined: ``urls`` subclasses it, so this
# import must come last to avoid a circular-import failure.
from lazytools.safety.urls import UrlBlocked, validate_public_url

__all__ = [
    "Allowlist",
    "ConfirmationGate",
    "ActionBlocked",
    "UrlBlocked",
    "active_scope",
    "current_scope",
    "validate_public_url",
]
