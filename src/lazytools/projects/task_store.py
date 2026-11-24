"""Transactions against the projects Store's SQLite key/value table."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any


class TaskServiceError(Exception):
    """A typed refusal returned by the task and approval tools."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class TransactionStore:
    """Expose the Store operations used by projects and ApprovalQueue.

    The surrounding transaction owns the lock and commit. In particular,
    ApprovalQueue must use this adapter directly on the same connection.
    """

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def read(self, key: str, default: Any = None) -> Any:
        row = self.connection.execute("SELECT value FROM store WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def items(self, *, prefix: str) -> list[tuple[str, Any]]:
        # GLOB metacharacters in a prefix are literal Store key characters.
        pattern = "".join(f"[{char}]" if char in "[*?" else char for char in prefix)
        rows = self.connection.execute("SELECT key, value FROM store WHERE key GLOB ?", (pattern + "*",))
        return [(key, json.loads(value)) for key, value in rows]

    def write(self, key: str, value: Any, *, agent_id: str | None = None) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO store (key, value, written_at, agent_id) VALUES (?, ?, ?, ?)",
            (key, json.dumps(value), time.time(), agent_id),
        )

    def compare_and_swap(self, key: str, expected: Any, new: Any, *, agent_id: str | None = None) -> bool:
        if self.read(key) != expected:
            return False
        self.write(key, new, agent_id=agent_id)
        return True


@contextmanager
def transaction(path: Path | None, *, write: bool = False) -> Iterator[TransactionStore]:
    if path is None:
        raise TaskServiceError("unavailable", "A persistent projects store is required")
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=5)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield TransactionStore(connection)
    except (sqlite3.Error, json.JSONDecodeError) as exc:
        raise TaskServiceError("unavailable", "The projects store is unavailable") from exc
