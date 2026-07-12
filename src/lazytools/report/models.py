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


class FigureBlock(BaseModel):
    """A figure named by artifact ref; bytes are resolved only at render time.

    ``ref`` is a canonical ``"scheme:key"`` string — the ecosystem's shared
    artifact identity (``lazydatacore.ArtifactRef``): ``regimes:<plot_key>``,
    ``crawler:<content_hash>``, ``chart:<spec>``, ``file:<path>``,
    ``bytes:<base64>``. See :mod:`lazytools.report.artifacts` for resolution.
    """

    ref: str = Field(min_length=3, pattern=r"^[a-z][a-z0-9_-]*:.+")
    caption: str = ""


class Section(BaseModel):
    """One memo section: a title, optional Markdown prose, optional tables and figures."""

    title: str
    body: str = ""
    tables: list[TableBlock] = Field(default_factory=list)
    figures: list[FigureBlock] = Field(default_factory=list)


class Memo(BaseModel):
    """A renderable memo/report: title, timestamp, sections, metadata."""

    title: str
    as_of: datetime | None = None
    sections: list[Section] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)
