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

When a :class:`~lazytools.report.files.ReportFiles` is passed as ``files``,
two extra tools render **and** persist in a single call (``save_memo_html`` /
``save_memo_markdown``), returning only the file path. This matters for reports
with embedded figures: a self-contained HTML with base64 images is far too
large to route back through the model as a ``save_report`` argument — an agent
that renders then re-passes it truncates the document. Rendering and writing in
one tool keeps the bytes out of the LLM's token stream entirely.
"""

from __future__ import annotations

from typing import Any

from lazybridge import Tool

from lazytools.report.artifacts import ArtifactResolvers
from lazytools.report.files import ReportFiles
from lazytools.report.models import Memo
from lazytools.report.render import render_html, render_markdown

_MEMO_SHAPE = (
    "Args: memo (object) — {title: str, as_of: ISO datetime (optional), "
    "sections: [{title: str, body: markdown str, tables: "
    "[{columns: [str], rows: [[str]]}], figures: "
    "[{ref: 'scheme:key' artifact ref, caption: str}]}], metadata: {str: str}}."
)


class ReportTools:
    """A ``ToolProvider`` exposing the deterministic memo renderers.

    ``artifacts`` configures how figure refs are resolved when rendering
    HTML; the default registry handles only ``file:`` and ``bytes:``.
    Pass a registry with source resolvers (``regimes:``, ``crawler:``,
    ``chart:``) registered — and, since the memo comes from an agent,
    prefer one constructed with a ``file_base_dir`` sandbox.
    """

    _is_lazy_tool_provider = True

    def __init__(
        self,
        *,
        artifacts: ArtifactResolvers | None = None,
        files: ReportFiles | None = None,
    ) -> None:
        self._artifacts = artifacts
        self._files = files

    # ------------------------------------------------------------------ #
    # ToolProvider
    # ------------------------------------------------------------------ #
    def as_tools(self) -> list[Tool]:
        tools = [
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
                    "escaped (deterministic, no LLM). Returns the HTML string. "
                    "For a report WITH figures/images, prefer save_memo_html "
                    "(if available) — an embedded-image HTML is too large to "
                    "pass on to another tool. " + _MEMO_SHAPE
                ),
            ),
        ]
        if self._files is not None:
            tools += [
                Tool.wrap(
                    self._save_memo_html,
                    name="save_memo_html",
                    description=(
                        "Render a memo to self-contained HTML (figures embedded "
                        "as base64) AND write it to a file in one step, returning "
                        "the absolute path. Use this for any report with figures: "
                        "the HTML never passes back through you, so embedded "
                        "images are never truncated. Args: memo (object, same "
                        "shape as render_memo_html), filename (basename like "
                        "'report.html'). " + _MEMO_SHAPE
                    ),
                ),
                Tool.wrap(
                    self._save_memo_markdown,
                    name="save_memo_markdown",
                    description=(
                        "Render a memo to Markdown AND write it to a file in one "
                        "step, returning the absolute path. Args: memo (object), "
                        "filename (basename like 'report.md')."
                    ),
                ),
            ]
        return tools

    # ------------------------------------------------------------------ #
    # Tool implementations
    # ------------------------------------------------------------------ #
    def _render_memo(self, memo: dict[str, Any]) -> str:
        return render_markdown(Memo.model_validate(memo))

    def _render_memo_html(self, memo: dict[str, Any]) -> str:
        return render_html(Memo.model_validate(memo), artifacts=self._artifacts)

    def _save_memo_html(self, memo: dict[str, Any], filename: str = "report.html") -> str:
        html = render_html(Memo.model_validate(memo), artifacts=self._artifacts)
        return self._files.save(filename, html)

    def _save_memo_markdown(self, memo: dict[str, Any], filename: str = "report.md") -> str:
        md = render_markdown(Memo.model_validate(memo))
        return self._files.save(filename, md)
