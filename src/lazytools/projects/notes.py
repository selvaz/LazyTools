"""Freeform notes against a project, and a one-line board summary.

Ported from ``lazyceo.projects`` (``add_project_note``, ``recent_project_notes``,
``project_board_summary``). Left behind, as CEO/Telegram policy:
``lazyceo.projects.add_operator_note`` -- the supervisor-note path that also
wakes the CEO's tick loop over Telegram. Nothing here wakes anything; a note
written through this module just sits on the record for the next reader.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from lazytools.projects.keys import PROJECT_NOTE_PREFIX

if TYPE_CHECKING:
    from lazybridge import Store


def add_project_note(
    store: Store,
    project_id: str,
    text: str,
    *,
    origin: str = "note",
    note_id: str | None = None,
    prefix: str = PROJECT_NOTE_PREFIX,
) -> str:
    """Record a note against a project. Returns the note_id.

    ``origin`` is free text identifying who/what wrote it (e.g. ``"ceo"``,
    ``"claude"``) -- this module does not interpret it, unlike LazyCEO's own
    ``add_operator_note``, which stamps a fixed ``"supervisor"`` and runs the
    text through a Telegram-specific prefix. A note is create-only: no
    edit/delete, same as LazyCEO's.
    """
    note_id = note_id or str(uuid.uuid4())
    note_key = f"{prefix}{project_id}:{note_id}"
    record = {"note_id": note_id, "project_id": project_id, "note": text, "at": time.time(), "origin": origin}
    store.compare_and_swap(note_key, None, record)
    return note_id


def _note_timestamp(value: object) -> float:
    """Coerce a note's ``at`` field to an epoch float, however it was actually written."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return 0.0
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()
    return 0.0


def recent_project_notes(
    store: Store, project_id: str, *, limit: int = 5, prefix: str = PROJECT_NOTE_PREFIX
) -> list[dict]:
    """The newest ``limit`` notes recorded against a project, most recent first."""
    records = [raw for _key, raw in store.items(prefix=f"{prefix}{project_id}:") if isinstance(raw, dict)]
    records.sort(key=lambda r: _note_timestamp(r.get("at")), reverse=True)
    return records[:limit]


def project_board_summary(store: Store, project_id: str) -> str:
    """A one-line "N todo, M done, ..." summary of a project's task board, or "no tasks"."""
    from lazybridge.ext.planners import DurableBlackboard

    board = DurableBlackboard(store, f"project:{project_id}").snapshot()
    counts: dict[str, int] = {}
    for task in board.tasks:
        status = str(task.get("status")) if isinstance(task, dict) else "?"
        counts[status] = counts.get(status, 0) + 1
    return ", ".join(f"{n} {status}" for status, n in sorted(counts.items())) or "no tasks"


__all__ = ["add_project_note", "project_board_summary", "recent_project_notes"]
