"""Report-writing specialist agent factory.

Wraps LazyReport's deterministic renderers (``ReportTools``/``ReportFiles``,
see ``report/tools.py``) with a LazyBridge agent whose expertise is Memo
*structure*: sections, tables and correctly-referenced figures, not data
gathering. Deliberately narrow — this agent has no direct access to
``datahub_*``/``statistical_*``/``regime_*``; whoever calls it supplies the
content (numbers, findings, prose) to structure and render, in the task
itself or via prior tool results earlier in the same conversation. That
keeps its behavior predictable and its tool-call cost low, and means the
same specialist works for any domain a caller hands it, not just finance.
"""

from __future__ import annotations

from typing import Any

from lazybridge import Agent

__all__ = ["REPORT_SPECIALIST_SYSTEM", "report_specialist"]

REPORT_SPECIALIST_SYSTEM = (
    "You are a report-writing specialist over LazyReport's deterministic memo "
    "renderer. Your job is structure, not research: the content you report on "
    "-- numbers, findings, prose -- must come only from what you were given in "
    "the task or from your own tool results; never fabricate a figure, claim or "
    "table row. Before rendering anything, build a Memo deliberately: a clear "
    "title, sections in a sensible order, tables for anything comparative or "
    "tabular (never restate a table as prose), and prose reserved for "
    "narrative, method and caveats. Reference figures only through a canonical "
    "'scheme:key' ref in a section's figures list (file:/bytes:/chart:/"
    "regimes:) -- never write '<img src=...>' by hand in body text, since a "
    "hand-written path only resolves on the machine that wrote it. The moment "
    "a memo has any figures, use save_memo_html instead of render_memo_html: "
    "the embedded-image HTML is too large to safely pass back through you as "
    "an argument again, so render-and-save in one call. NEVER use render_memo "
    "or save_memo_markdown for a memo that has any figures -- Markdown cannot "
    "embed images, each figure silently degrades to a plain text caption and "
    "the user sees no chart; reserve Markdown output only for memos with no "
    "figures at all. State assumptions, "
    "data limitations or a thin sample size explicitly in their own section "
    "rather than burying them in a caption."
)


def report_specialist(
    engine: Any,
    *,
    tools: list[Any],
    name: str = "report-specialist",
) -> Agent:
    """An agent expert at composing and rendering a structured LazyReport memo.

    ``tools`` is typically ``[ReportTools(artifacts=ecosystem_resolvers(...),
    files=ReportFiles(...))]`` — render/save only, no data-gathering tools.
    Pair ``engine`` with :data:`REPORT_SPECIALIST_SYSTEM` (e.g.
    ``LLMEngine(model, system=REPORT_SPECIALIST_SYSTEM)``).
    """

    return Agent(
        engine=engine,
        tools=tools,
        name=name,
        description=(
            "Specialist agent for structured reports: turns content you supply "
            "(numbers, findings, prose, figure references) into a well-"
            "organized Memo -- title, sections, tables, figures -- and renders "
            "or saves it. Give it a task describing the report you want and "
            "the material to include; it does not fetch data itself."
        ),
    )
