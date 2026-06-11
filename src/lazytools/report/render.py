"""Deterministic Memo renderers: Markdown and minimal HTML.

Both renderers are pure functions of their input — same :class:`Memo` in,
byte-identical string out. No LLM, no templates, stdlib only. Metadata keys
are emitted in sorted order so the output does not depend on insertion order.

* :func:`render_markdown` — H1 title, an ``_as of …_`` line, H2 sections,
  GFM tables, and a trailing ``key: value`` metadata list. Section bodies are
  Markdown prose and pass through verbatim.
* :func:`render_html` — minimal clean HTML with **everything escaped** via
  :func:`html.escape`. Section bodies are treated as plain text here (split
  into paragraphs on blank lines), not parsed as Markdown.
"""

from __future__ import annotations

import html

from lazytools.report.models import Memo


def render_markdown(memo: Memo) -> str:
    """Render a :class:`Memo` to GitHub-flavoured Markdown (deterministic)."""
    lines: list[str] = [f"# {memo.title}", ""]
    if memo.as_of is not None:
        lines += [f"_as of {memo.as_of.isoformat()}_", ""]
    for section in memo.sections:
        lines += [f"## {section.title}", ""]
        if section.body:
            lines += [section.body, ""]
        for table in section.tables:
            lines.append("| " + " | ".join(_md_cell(cell) for cell in table.columns) + " |")
            lines.append("| " + " | ".join("---" for _ in table.columns) + " |")
            for row in table.rows:
                lines.append("| " + " | ".join(_md_cell(cell) for cell in row) + " |")
            lines.append("")
    if memo.metadata:
        lines += ["---", ""]
        lines += [f"- {key}: {memo.metadata[key]}" for key in sorted(memo.metadata)]
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def render_html(memo: Memo) -> str:
    """Render a :class:`Memo` to minimal HTML; every value is escaped."""
    parts: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        f"<title>{html.escape(memo.title)}</title>",
        "</head>",
        "<body>",
        f"<h1>{html.escape(memo.title)}</h1>",
    ]
    if memo.as_of is not None:
        parts.append(f'<p class="as-of"><em>as of {html.escape(memo.as_of.isoformat())}</em></p>')
    for section in memo.sections:
        parts.append(f"<h2>{html.escape(section.title)}</h2>")
        for paragraph in section.body.split("\n\n"):
            if paragraph.strip():
                parts.append("<p>" + html.escape(paragraph).replace("\n", "<br>") + "</p>")
        for table in section.tables:
            parts.append("<table>")
            parts.append(
                "<thead><tr>" + "".join(f"<th>{html.escape(cell)}</th>" for cell in table.columns) + "</tr></thead>"
            )
            parts.append("<tbody>")
            for row in table.rows:
                parts.append("<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in row) + "</tr>")
            parts.append("</tbody>")
            parts.append("</table>")
    if memo.metadata:
        parts.append("<dl>")
        for key in sorted(memo.metadata):
            parts.append(f"<dt>{html.escape(key)}</dt><dd>{html.escape(memo.metadata[key])}</dd>")
        parts.append("</dl>")
    parts += ["</body>", "</html>"]
    return "\n".join(parts) + "\n"


def _md_cell(text: str) -> str:
    """Escape a Markdown table cell: pipes break columns, newlines break rows."""
    return text.replace("|", "\\|").replace("\n", " ")
