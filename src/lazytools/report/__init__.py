"""LazyReport — generic, deterministic memo/report rendering.

No LLM, no new dependencies: pydantic models (:class:`Memo`,
:class:`Section`, :class:`TableBlock`), pure-function renderers
(:func:`render_markdown`, :func:`render_html`), a rendering ``ToolProvider``
(:class:`ReportTools`), and a file-writing ``ToolProvider``
(:class:`ReportFiles`, exposing ``save_report``). PDF rendering is deliberately
deferred (heavy dependency) — see :mod:`lazytools.report.tools`.
"""

from __future__ import annotations

from lazytools.report.files import ReportFiles
from lazytools.report.models import Memo, Section, TableBlock
from lazytools.report.render import render_html, render_markdown
from lazytools.report.tools import ReportTools

__all__ = [
    "Memo",
    "ReportFiles",
    "ReportTools",
    "Section",
    "TableBlock",
    "render_html",
    "render_markdown",
]
