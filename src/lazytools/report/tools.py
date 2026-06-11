"""Report tools ("LazyReport") for the worker.

Exposes two deterministic rendering tools via the lazybridge ``ToolProvider``
protocol:

* ``render_memo``      — Memo (JSON object) → GitHub-flavoured Markdown.
* ``render_memo_html`` — Memo (JSON object) → minimal, fully-escaped HTML.

The split of responsibilities is deliberate: an LLM writes the *prose* (the
section bodies); the *layout* is rendered deterministically here, so the same
memo always produces the same document. **PDF rendering is deliberately
deferred** — it would pull a heavy dependency (weasyprint / reportlab) into an
otherwise stdlib-only module; render to HTML and convert externally (or
print-to-PDF) until a ``heavy_render``-style extra lands.
"""

from __future__ import annotations

from typing import Any

from lazybridge import Tool

from lazytools.report.models import Memo
from lazytools.report.render import render_html, render_markdown

_MEMO_SHAPE = (
    "Args: memo (object) — {title: str, as_of: ISO datetime (optional), "
    "sections: [{title: str, body: markdown str, tables: "
    "[{columns: [str], rows: [[str]]}]}], metadata: {str: str}}."
)


class ReportTools:
    """A ``ToolProvider`` exposing the deterministic memo renderers."""

    _is_lazy_tool_provider = True

    # ------------------------------------------------------------------ #
    # ToolProvider
    # ------------------------------------------------------------------ #
    def as_tools(self) -> list[Tool]:
        return [
            Tool.wrap(
                self._render_memo,
                name="render_memo",
                description=(
                    "Render a structured memo to GitHub-flavoured Markdown "
                    "(deterministic, no LLM). " + _MEMO_SHAPE
                ),
            ),
            Tool.wrap(
                self._render_memo_html,
                name="render_memo_html",
                description=(
                    "Render a structured memo to minimal HTML with all values "
                    "escaped (deterministic, no LLM). " + _MEMO_SHAPE
                ),
            ),
        ]

    # ------------------------------------------------------------------ #
    # Tool implementations
    # ------------------------------------------------------------------ #
    def _render_memo(self, memo: dict[str, Any]) -> str:
        return render_markdown(Memo.model_validate(memo))

    def _render_memo_html(self, memo: dict[str, Any]) -> str:
        return render_html(Memo.model_validate(memo))
