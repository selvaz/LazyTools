"""Pydantic models for the deterministic report renderer ("LazyReport").

A :class:`Memo` is the generic, domain-agnostic shape any agent can fill:
a title, an optional ``as_of`` timestamp, prose-plus-tables sections, and a
flat string metadata map. The renderers in
:mod:`lazytools.report.render` turn it into Markdown or HTML with zero LLM
involvement — same input, identical output.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class TableBlock(BaseModel):
    """A simple rectangular table: header columns plus string-cell rows."""

    columns: list[str]
    rows: list[list[str]]


class Section(BaseModel):
    """One memo section: a title, optional Markdown prose, optional tables."""

    title: str
    body: str = ""
    tables: list[TableBlock] = Field(default_factory=list)


class Memo(BaseModel):
    """A renderable memo/report: title, timestamp, sections, metadata."""

    title: str
    as_of: datetime | None = None
    sections: list[Section] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)
