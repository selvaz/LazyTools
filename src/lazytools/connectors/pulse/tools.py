"""Read-only visibility into a LazyCEO deployment's shared state -- the MCP
surface for the "PulseAgent as a tool for Claude Code" control channel from
the architecture blueprint.

Deliberately does NOT read PulseAgent task records directly: ``lazytools``
must never import ``lazypulse`` (enforced by
``tests/test_no_lazypulse_import.py`` -- lazypulse already depends on
lazytools for its telegram/gmail/outlook extras, so the reverse import would
make the two packages circular). Task-level visibility into the productive/
observer PulseAgent schedules therefore has to be exposed by ``lazyceo``
itself (which is allowed to depend on lazypulse) if it's ever needed here --
this provider is scoped to what's genuinely lazyceo-native: backlog items and
approval tickets, both owned by ``lazyceo.state``/``lazyceo.approvals``.

v1.0 read-only only, deliberately: mutating tools (approve/reject/pause) need
a dedicated, isolated MCP process and an audited actor/channel before they're
safe to expose here -- see the blueprint's phased rollout. This provider
never emits a writer regardless of ``allow_write``, same convention as
``econ_calendar``/``earnings_calendar``: the surface is inherently read-only,
not merely read-only-by-default.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lazybridge import Store
from lazyceo.approvals import list_pending_tickets
from lazyceo.state import list_backlog as ceo_list_backlog


def _open_store(db_path: str | None) -> Store:
    if not db_path:
        return Store()
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return Store(db=db_path)


class PulseTools:
    """Read-only tools over the shared CEO state store (backlog, approval
    tickets) a LazyCEO deployment writes to."""

    _is_lazy_tool_provider = True

    def __init__(self, *, ceo_state_db: str | None = None) -> None:
        self._ceo_store = _open_store(ceo_state_db)

    def pulse_list_backlog(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Backlog items filed by observer-cycle detectors (job health, idea-lab
        triage, LLM-provider watch), optionally filtered by status
        (open/promoted/dismissed)."""
        return [i.model_dump(mode="json") for i in ceo_list_backlog(self._ceo_store, status=status, limit=limit)]

    def pulse_list_pending_approvals(self, limit: int = 100) -> list[dict[str, Any]]:
        """Approval tickets currently awaiting a human decision (the queue
        TieredGate's "ask"/"session" tiers file into during a dispatch)."""
        return [t.model_dump(mode="json") for t in list_pending_tickets(self._ceo_store, limit=limit)]

    def pulse_state_snapshot(self) -> dict[str, int]:
        """A point-in-time count of open backlog items and pending approvals.

        NOT proof either PulseAgent process is alive -- only that the shared
        Store holds these records. Task-level state (is the productive cycle
        actually ticking) isn't in this snapshot at all: exposing it would
        require reading lazypulse task records, which this connector cannot
        do (see module docstring).
        """
        return {
            "open_backlog": len(ceo_list_backlog(self._ceo_store, status="open", limit=10_000)),
            "pending_approvals": len(list_pending_tickets(self._ceo_store, limit=10_000)),
        }

    def as_tools(self) -> list[Any]:
        from lazybridge import Tool

        return [
            Tool.wrap(self.pulse_list_backlog, name="pulse_list_backlog"),
            Tool.wrap(self.pulse_list_pending_approvals, name="pulse_list_pending_approvals"),
            Tool.wrap(self.pulse_state_snapshot, name="pulse_state_snapshot"),
        ]
