"""Persist rendered reports to files — the companion to the renderers.

:class:`~lazytools.report.tools.ReportTools` renders a :class:`Memo` to a
Markdown/HTML *string*; :class:`ReportFiles` writes report content to a file
under a **sandboxed base directory** and returns the path, so an agent can hand
that path to an outbound tool (e.g. ``telegram_send_document``). Kept separate
from ``ReportTools`` — which is pure, no I/O — on purpose.

The filename is reduced to its basename and stripped of unsafe characters
(no path traversal): only the file *name* is honoured, never a directory.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from lazybridge import Tool

#: Extensions a report may use; anything else gets ``.md`` appended.
_ALLOWED_EXT = {".md", ".markdown", ".html", ".htm", ".csv", ".txt", ".json"}
#: Collapse runs of unsafe characters in a filename to a single hyphen.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class ReportFiles:
    """A ``ToolProvider`` exposing ``save_report`` (write text → sandboxed file)."""

    _is_lazy_tool_provider = True

    def __init__(self, *, base_dir: str | os.PathLike[str] = "reports") -> None:
        #: All files are written under here; created on first write.
        self._base = Path(base_dir)

    # ------------------------------------------------------------------ #
    # ToolProvider
    # ------------------------------------------------------------------ #
    def as_tools(self) -> list[Tool]:
        return [
            Tool.wrap(
                self._save_report,
                name="save_report",
                description=(
                    "Write a report's text content to a file and return its absolute path "
                    "(hand the path to an outbound/attachment tool afterwards). "
                    "Args: filename (a basename like 'q3.md' — any directory part is ignored), "
                    "content (the full report text). Allowed extensions: "
                    "md/markdown/html/htm/csv/txt/json (defaults to .md)."
                ),
            ),
        ]

    # ------------------------------------------------------------------ #
    # Tool implementation
    # ------------------------------------------------------------------ #
    def _safe_name(self, filename: str) -> str:
        # Basename only — defeats ``../`` traversal and absolute paths.
        name = Path(str(filename)).name
        name = _UNSAFE.sub("-", name).strip("-. ") or "report"
        if Path(name).suffix.lower() not in _ALLOWED_EXT:
            name += ".md"
        return name

    def _save_report(self, filename: str, content: str) -> str:
        name = self._safe_name(filename)
        base = self._base.resolve()
        base.mkdir(parents=True, exist_ok=True)
        path = base / name
        # Defence in depth: ``name`` is already a basename, but the target may
        # be a pre-existing symlink pointing outside ``base`` — writing through
        # it would escape the sandbox. Refuse symlinks and any path that does
        # not resolve to a direct child of ``base``.
        if path.is_symlink() or path.resolve().parent != base:
            raise ValueError(f"save_report: refusing to write through a symlink or out-of-sandbox path: {name!r}")
        path.write_text(content, encoding="utf-8")
        return str(path)
