"""Transactions against the projects Store's SQLite key/value table."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from collections.abc import Iterable, Iterator, Mapping
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Protocol

_log = logging.getLogger(__name__)


def _env_seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default

PUBLICATION_RECEIPTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS publication_receipts (
    receipt_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    pr_number INTEGER,
    status TEXT NOT NULL CHECK (status IN ('published', 'refused')),
    receipt_json TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


class TaskServiceError(Exception):
    """A typed refusal returned by the task and approval tools."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ProjectsStore(Protocol):
    """The Store operations shared by normal and transactional project code."""

    def read(self, key: str, default: Any = None) -> Any: ...
    def items(self, *, prefix: str) -> list[tuple[str, Any]]: ...
    def compare_and_swap(self, key: str, expected: Any, new: Any, *, agent_id: str | None = None) -> bool: ...


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

    def compare_and_swap_many(
        self,
        expected: Mapping[str, Any],
        updates: Mapping[str, Any],
        *,
        delete: Iterable[str] = (),
        agent_id: str | None = None,
    ) -> bool:
        """LazyBridge Store semantics inside the surrounding transaction.

        All expectations must match (an expected ``None`` matches a missing key); then every update applies, then
        every deletion. LazyBridge's per-card approval registry uses it.
        """
        if any(self.read(key) != value for key, value in expected.items()):
            return False
        for key, value in updates.items():
            self.write(key, value, agent_id=agent_id)
        for key in delete:
            self.connection.execute("DELETE FROM store WHERE key = ?", (key,))
        return True


@contextmanager
def transaction(path: Path | None, *, write: bool = False) -> Iterator[TransactionStore]:
    if path is None:
        raise TaskServiceError("unavailable", "A persistent projects store is required")
    try:
        timeout = _env_seconds("LAZYBRIDGE_STORE_BUSY_TIMEOUT", 30.0)
        with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=timeout)) as connection, connection:
            started = time.monotonic()
            try:
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            finally:
                elapsed = time.monotonic() - started
                if write and elapsed >= _env_seconds("LAZYBRIDGE_STORE_SLOW_WRITE", 1.0):
                    _log.warning("slow projects Store lock wait: %.2fs in %s", elapsed, path)
            yield TransactionStore(connection)
    except sqlite3.Error as exc:
        code = getattr(exc, "sqlite_errorcode", None)
        if code is not None and code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise TaskServiceError("busy", "The projects store is busy") from exc
        raise TaskServiceError("unavailable", "The projects store is unavailable") from exc
    except json.JSONDecodeError as exc:
        raise TaskServiceError("unavailable", "The projects store is unavailable") from exc


def migrate_publication_receipts(path: Path | None) -> None:
    """Add publication receipts to an existing projects store."""
    with transaction(path, write=True) as store:
        store.connection.execute(PUBLICATION_RECEIPTS_SCHEMA)


def publication_receipt(store: TransactionStore, receipt_id: str) -> str | None:
    exists = store.connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'publication_receipts'"
    ).fetchone()
    if not exists:
        return None
    row = store.connection.execute(
        "SELECT receipt_json FROM publication_receipts WHERE receipt_id = ?", (receipt_id,)
    ).fetchone()
    return str(row[0]) if row else None


def publication_for_ticket(store: TransactionStore, ticket_id: str) -> tuple[bool, str | None]:
    receipt_id = store.read(f"publication:receipt:{ticket_id}")
    return receipt_id is not None, publication_receipt(store, receipt_id) if receipt_id else None


def claim_ticket_publication(store: TransactionStore, ticket_id: str) -> None:
    # An empty receipt ID reserves the effect until its outcome is recorded.
    if not store.compare_and_swap(f"publication:receipt:{ticket_id}", None, ""):
        raise TaskServiceError("conflict", "This approval already has a publication claim")


def save_publication_receipt(
    path: Path | None,
    *,
    receipt_id: str,
    task_id: str,
    attempt: int,
    head_sha: str,
    pr_number: int | None,
    status: str,
    receipt_json: str,
    created_at: str,
    consumed_ticket_id: str | None,
) -> None:
    with transaction(path, write=True) as store:
        store.connection.execute(
            "INSERT INTO publication_receipts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (receipt_id, task_id, attempt, head_sha, pr_number, status, receipt_json, created_at),
        )
        if consumed_ticket_id and not store.compare_and_swap(
            f"publication:receipt:{consumed_ticket_id}", "", receipt_id
        ):
            raise TaskServiceError("conflict", "This approval already has a publication outcome")
