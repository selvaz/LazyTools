"""Remembering which line was which, because a filed document never changes.

A mapping is a property of the document, not of the day it was read. Once a
filing is accepted by EDGAR its statements are fixed for good, so asking a model
twice which line is revenue is paying twice for the same answer — and, worse,
risking two different ones. Two runs over Cisco's FY2024 filing produced
different coverage before this existed: one placed the dividends, the other did
not.

So the mapping is stored, keyed by the filing and by the version of the element
registry it was made against. That second key is the one people forget. A
mapping is an answer to "which of THESE elements is which line"; add an element
to the registry and every stored mapping becomes an answer to a different
question, silently missing the new one. Bumping ``SCHEMA_VERSION`` retires them
all, which is the correct and cheap behaviour.

What is deliberately NOT stored is any figure. The cache holds references —
statement and label — exactly as the mapping interface does, so a stale cache
can cost a re-read but can never supply a wrong number.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from lazytools.connectors.edgar.mapping import Absence, LineRef, Mapping
from lazytools.financials.normalised import SCHEMA_VERSION

_SCHEMA = """
CREATE TABLE IF NOT EXISTS statement_mapping (
    accession       TEXT    NOT NULL,
    column_index    INTEGER NOT NULL,
    schema_version  INTEGER NOT NULL,
    model           TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    payload         TEXT    NOT NULL,
    PRIMARY KEY (accession, column_index, schema_version)
);
"""


@dataclass(frozen=True)
class CachedMapping:
    """A stored mapping and what produced it."""

    mapping: Mapping
    model: str
    created_at: str


class MappingStore:
    """A SQLite cache of statement mappings, keyed by filing and registry version.

    Args:
        path: the database file. ``:memory:`` for a cache that lives as long as
            the process, which is what tests use.

    Not a general-purpose store: it holds one kind of row and knows why. A
    filing's mapping is written once and read many times, so there is no update
    path — a re-mapping under the same keys replaces the row, and the model that
    produced it travels with it so a bad one can be found and cleared.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._shared = sqlite3.connect(self.path) if self.path == ":memory:" else None
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        """A connection, shared for an in-memory store and per-call otherwise.

        An in-memory database belongs to its connection, so opening a new one
        each call would be a new, empty database every time. A file-backed store
        opens and closes per call, which is what makes concurrent readers safe;
        WAL is set once at creation and persists in the file.
        """
        if self._shared is not None:
            yield self._shared
            return
        connection = sqlite3.connect(self.path)
        try:
            yield connection
        finally:
            connection.close()

    def get(self, accession: str, column: int) -> CachedMapping | None:
        """The stored mapping for this filing, or ``None``.

        Returns nothing for a mapping made against a different element registry:
        it answered a different question, and reusing it would silently omit
        whatever the registry gained since.
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT model, created_at, payload FROM statement_mapping "
                "WHERE accession = ? AND column_index = ? AND schema_version = ?",
                (accession, column, SCHEMA_VERSION),
            ).fetchone()
        if row is None:
            return None
        model, created_at, payload = row
        return CachedMapping(mapping=_from_payload(json.loads(payload)),
                             model=model, created_at=created_at)

    def put(self, accession: str, column: int, mapping: Mapping, *, model: str) -> None:
        """Store a mapping. Replaces any earlier one for the same keys."""
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO statement_mapping "
                "(accession, column_index, schema_version, model, created_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (accession, column, SCHEMA_VERSION, model,
                 datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 json.dumps(_to_payload(mapping))),
            )
            connection.commit()

    def forget(self) -> int:
        """Drop every stored mapping.

        A model that mapped badly leaves rows that look exactly like good ones,
        and the only honest remedy is to clear them and let them be recomputed.
        The model that produced each row travels with it, so which rows to
        distrust is answerable; a filtered delete is not worth its own code path
        until something needs one.
        """
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM statement_mapping")
            connection.commit()
            return cursor.rowcount

    def __len__(self) -> int:
        with self._connect() as connection:
            return connection.execute("SELECT COUNT(*) FROM statement_mapping").fetchone()[0]


def _to_payload(mapping: Mapping) -> dict:
    return {
        "refs": [{"element_id": r.element_id, "statement": r.statement,
                  "label": r.label} for r in mapping.refs],
        "absences": [{"element_id": a.element_id, "reason": a.reason}
                     for a in mapping.absences],
    }


def _from_payload(payload: dict) -> Mapping:
    return Mapping(
        refs=tuple(LineRef(**r) for r in payload.get("refs", [])),
        absences=tuple(Absence(**a) for a in payload.get("absences", [])),
    )


__all__ = ["CachedMapping", "MappingStore"]
