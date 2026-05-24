"""Ambient scope for binding one-shot confirmation grants.

A guarded tool can bind a grant to an opaque **scope** so that, under
concurrency, an approval issued for one unit of work can never be spent by
another. The orchestrator sets the scope for the duration of a run; the tool
reads it when consuming a grant.

In LazyPulse the scope is the running task id: the orchestrator (e.g.
``PulseAgent``) sets :data:`active_scope` for the duration of
``Agent.run``, and ``GmailTools`` / ``TelegramTools`` read
:func:`current_scope` when consuming a send grant. The value propagates into
**async** tools (lazybridge awaits them in the same context) but not into sync
tools (run in a fresh thread context) — which is why the gated send tools are
async.

This module is dependency-free; it is the single contextvar both sides share.
"""

from __future__ import annotations

import contextvars

#: The opaque scope of the work currently running (e.g. a task id), or ``None``
#: outside a tracked run (e.g. a direct tool call in a test).
active_scope: contextvars.ContextVar[str | None] = contextvars.ContextVar("lazytools_active_scope", default=None)


def current_scope() -> str | None:
    """Return the ambient scope of the current run, if any."""
    return active_scope.get()
