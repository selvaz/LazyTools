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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from lazytools.projects.keys import PROJECT_NOTE_PREFIX

if TYPE_CHECKING:
    from lazybridge import Store

Diagnostic = Callable[[str], None]
NoteHook = Callable[["Store", dict], None]


@dataclass(frozen=True)
class NoteWriteResult:
    note_id: str
    written: bool
    diagnostics: tuple[str, ...] = ()


def write_project_note(
    store: Store, project_id: str, text: str, *, origin: str = "note",
    note_id: str | None = None, prefix: str = PROJECT_NOTE_PREFIX,
    on_note: NoteHook | None = None, diagnostic: Diagnostic | None = None,
) -> NoteWriteResult:
    """Checked create-only CAS. A duplicate/lost race never invokes on_note.

    Hooks are synchronous and exceptions propagate, without retrying a
    successful write. The existing note record shape stays unchanged.
    """
    identity = note_id or str(uuid.uuid4())
    record = {"note_id": identity, "project_id": project_id, "note": text, "at": time.time(), "origin": origin}
    written = bool(store.compare_and_swap(f"{prefix}{project_id}:{identity}", None, record))
    messages = () if written else (f"note {identity!r} was not written: already exists or lost CAS race",)
    if diagnostic is not None:
        for message in messages:
            diagnostic(message)
    if written and on_note is not None:
        on_note(store, record)
    return NoteWriteResult(identity, written, messages)


def add_project_note(
    store: Store,
    project_id: str,
    text: str,
    *,
    origin: str = "note",
    note_id: str | None = None,
    prefix: str = PROJECT_NOTE_PREFIX,
    on_note: NoteHook | None = None,
    diagnostic: Diagnostic | None = None,
) -> str:
    """Record a note against a project. Returns the note_id.

    ``origin`` is free text identifying who/what wrote it (e.g. ``"ceo"``,
    ``"claude"``) -- this module does not interpret it, unlike LazyCEO's own
    ``add_operator_note``, which stamps a fixed ``"supervisor"`` and runs the
    text through a Telegram-specific prefix. A note is create-only: no
    edit/delete, same as LazyCEO's.
    """
    return write_project_note(store, project_id, text, origin=origin, note_id=note_id, prefix=prefix, on_note=on_note, diagnostic=diagnostic).note_id


def note_timestamp(value: object, *, diagnostic: Diagnostic | None = None) -> float:
    """Coerce a note's ``at`` field to an epoch float, however it was actually written."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            if diagnostic is not None:
                diagnostic(f"note timestamp {value!r} is not a valid ISO datetime; treating as 0.0")
            return 0.0
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        try:
            return parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            if diagnostic is not None:
                diagnostic(f"note timestamp {value!r} is out of range; treating as 0.0")
            return 0.0
    if diagnostic is not None:
        diagnostic(f"note timestamp {value!r} has unexpected type {type(value).__name__}; treating as 0.0")
    return 0.0


_note_timestamp = note_timestamp  # compatibility for existing parser consumers


def recent_project_notes(
    store: Store, project_id: str, *, limit: int = 5, prefix: str = PROJECT_NOTE_PREFIX,
    diagnostic: Diagnostic | None = None,
) -> list[dict]:
    """The newest ``limit`` notes recorded against a project, most recent first."""
    records = [raw for _key, raw in store.items(prefix=f"{prefix}{project_id}:") if isinstance(raw, dict)]
    records.sort(key=lambda r: note_timestamp(r.get("at"), diagnostic=diagnostic), reverse=True)
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


__all__ = ["Diagnostic", "NoteHook", "NoteWriteResult", "add_project_note", "note_timestamp", "project_board_summary", "recent_project_notes", "write_project_note"]
