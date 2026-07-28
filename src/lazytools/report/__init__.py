"""LazyReport — generic, deterministic memo/report rendering.

The renderers themselves call no LLM and need no new dependency: pydantic
models (:class:`Memo`, :class:`Section`, :class:`TableBlock`), pure-function
renderers (:func:`render_markdown`, :func:`render_html`), a rendering
``ToolProvider`` (:class:`ReportTools`), and a file-writing ``ToolProvider``
(:class:`ReportFiles`, exposing ``save_report``). PDF rendering is deliberately
deferred (heavy dependency) — see :mod:`lazytools.report.tools`.

:func:`report_specialist` (:mod:`lazytools.report.agents`) is the one LLM-using
addition: a LazyBridge agent factory pairing those same tools with a system
prompt expert at Memo structure — still no data-gathering tools of its own.
"""

from __future__ import annotations

from lazytools.report.agents import REPORT_SPECIALIST_SYSTEM, report_specialist
from lazytools.report.artifacts import ArtifactResolvers
from lazytools.report.files import ReportFiles
from lazytools.report.models import FigureBlock, Memo, Section, TableBlock
from lazytools.report.render import render_html, render_markdown
from lazytools.report.resolvers import ecosystem_resolvers
from lazytools.report.tools import ReportTools

__all__ = [
    "ArtifactResolvers",
    "ecosystem_resolvers",
    "FigureBlock",
    "Memo",
    "REPORT_SPECIALIST_SYSTEM",
    "ReportFiles",
    "ReportTools",
    "Section",
    "TableBlock",
    "render_html",
    "render_markdown",
    "report_specialist",
]
