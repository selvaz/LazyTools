"""LazyReport — generic, deterministic memo/report rendering.

No LLM, no new dependencies: pydantic models (:class:`Memo`,
:class:`Section`, :class:`TableBlock`) plus pure-function renderers
(:func:`render_markdown`, :func:`render_html`) and a ``ToolProvider``
(:class:`ReportTools`). PDF rendering is deliberately deferred (heavy
dependency) — see :mod:`lazytools.report.tools`.
"""

from __future__ import annotations

from lazytools.report.models import Memo, Section, TableBlock
from lazytools.report.render import render_html, render_markdown
from lazytools.report.tools import ReportTools

__all__ = [
    "Memo",
    "ReportTools",
    "Section",
    "TableBlock",
    "render_html",
    "render_markdown",
]
