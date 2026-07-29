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
    "[{ref: 'scheme:key' artifact ref, caption: str}]}], metadata: {str: str}}. "
    "To include an image, put it in a section's figures list as "
    "{ref: 'file:my_chart.png', caption: '...'} -- 'file:' is the built-in "
    "scheme for a PNG/JPEG/SVG already saved to disk (e.g. by a chart-export "
    "tool); the filename is looked up relative to whatever sandbox directory "
    "this ReportTools instance was configured with, so pass the bare "
    "filename, never a path like 'reports/my_chart.png'. Do NOT write "
    '<img src="..."> by hand inside a section\'s body text -- a hand-written '
    "path only resolves on the machine that wrote it and breaks the moment "
    "the rendered file is opened anywhere else (e.g. downloaded as a "
    "standalone attachment); figures in the figures list get embedded as "
    "base64 straight into the HTML by save_memo_html, so the file is "
    "self-contained and portable."
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
                    "Render a structured memo to GitHub-flavoured Markdown (deterministic, no LLM). "
                    "WARNING: any figure in the memo degrades to a plain italic text caption here -- "
                    "no image is embedded. For a memo with figures the user should actually see, use "
                    "render_memo_html or save_memo_html instead. " + _MEMO_SHAPE
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
            if self._artifacts is not None:
                schemes_note = f"Figure refs resolvable in this deployment: {', '.join(self._artifacts.schemes())}. "
            else:
                schemes_note = (
                    "NOTE: no artifact resolver is configured here, so any "
                    "figures list is silently unable to embed images -- omit "
                    "figures entirely (tables and body text still work fine). "
                )
            tools += [
                Tool.wrap(
                    self._save_memo_html,
                    name="save_memo_html",
                    description=(
                        "Render a memo to self-contained HTML (figures embedded "
                        "as base64) AND write it to a file in one step, returning "
                        "the absolute path. Use this for any report with figures: "
                        "the HTML never passes back through you, so embedded "
                        "images are never truncated. " + schemes_note + "Args: memo (object, same "
                        "shape as render_memo_html), filename (basename like "
                        "'report.html'). " + _MEMO_SHAPE
                    ),
                ),
                Tool.wrap(
                    self._save_memo_markdown,
                    name="save_memo_markdown",
                    description=(
                        "Render a memo to Markdown AND write it to a file in one "
                        "step, returning the absolute path. WARNING: Markdown "
                        "CANNOT embed images -- any figure in the memo degrades to "
                        "a plain italic text caption, no picture. If the memo has "
                        "ANY figures and the user should actually see them, use "
                        "save_memo_html instead; reach for this tool only for "
                        "reports with no figures at all. Args: memo (object), "
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
        assert self._files is not None  # registered only when files was provided
        html = render_html(Memo.model_validate(memo), artifacts=self._artifacts)
        return self._files.save(filename, html)

    def _save_memo_markdown(self, memo: dict[str, Any], filename: str = "report.md") -> str:
        assert self._files is not None  # registered only when files was provided
        md = render_markdown(Memo.model_validate(memo))
        return self._files.save(filename, md)
