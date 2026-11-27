"""MCP tools over the lessons store that ``lazytools-code-bridge dispatch --knowledge-db`` uses."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from lazybridge.ext.knowledge import DEFAULT_PREFIX, DurableKnowledgeBase, slugify

from lazytools.code_bridge import _store
from lazytools.code_bridge._cloud import redact

DB_ENV_VAR = "LAZYTOOLS_KNOWLEDGE_DB"
MAX_TOPIC_CHARS = 200
MAX_TEXT_CHARS = 4000


class KnowledgeTools:
    """``lessons_search``, plus ``lessons_record`` when ``allow_write`` is set."""

    _is_lazy_tool_provider = True

    def __init__(self, db_path: str | None = None, *, allow_write: bool = False) -> None:
        path = db_path or os.environ.get(DB_ENV_VAR) or os.path.join("~", ".lazytools", "knowledge.sqlite")
        self._db_path = Path(path).expanduser()
        self._allow_write = allow_write

    @contextmanager
    def _open(self) -> Iterator[tuple[Any, DurableKnowledgeBase]]:
        store = _store.build_store(self._db_path)
        try:
            yield store, DurableKnowledgeBase(store, prefix=DEFAULT_PREFIX)
        finally:
            store.close()

    def lessons_search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Search recorded lessons by keyword, best match first."""
        if limit < 1:
            raise ValueError("limit must be positive")
        if not self._db_path.exists():
            return []  # a read never creates the database
        with self._open() as (_, knowledge):
            return knowledge.find_lessons(query, limit=limit)

    def lessons_record(self, topic: str, what_worked: str, gotchas: str = "") -> str:
        """Save a lesson, or revise the one already recorded under the same topic.

        Use the dispatcher's topic form, "<repository>: <subject>". A new topic is
        rejected when its slug exceeds the knowledge base's limit. Empty ``gotchas``
        keeps the ones an existing lesson already has.
        """
        for name, value, maximum in (
            ("topic", topic, MAX_TOPIC_CHARS),
            ("what_worked", what_worked, MAX_TEXT_CHARS),
            ("gotchas", gotchas, MAX_TEXT_CHARS),
        ):
            if len(value) > maximum:
                raise ValueError(f"{name} must be at most {maximum} characters")
        topic, what_worked, gotchas = (redact(value).strip() for value in (topic, what_worked, gotchas))
        slug = slugify(topic)
        key = DEFAULT_PREFIX + slug
        with self._open() as (store, knowledge):
            for _ in range(20):
                previous = store.read(key)
                if isinstance(previous, dict):
                    result = knowledge.revise_lesson(
                        slug=slug,
                        expected_revision=previous.get("revision", 1),
                        what_worked=what_worked,
                        gotchas=gotchas or None,
                    )
                else:
                    result = knowledge.save_lesson(topic=topic, what_worked=what_worked, gotchas=gotchas)
                # Retry only a rejection caused by another writer changing the record.
                if not result.startswith("REJECTED:") or store.read(key) == previous:
                    break
            return result

    def as_tools(self) -> list[Any]:
        from lazybridge import Tool

        methods = [self.lessons_search, *([self.lessons_record] if self._allow_write else [])]
        return [Tool.wrap(method, name=method.__name__) for method in methods]


__all__ = ["KnowledgeTools"]
