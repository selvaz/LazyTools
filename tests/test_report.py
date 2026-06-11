"""LazyReport: deterministic Memo → Markdown/HTML rendering + ReportTools."""

from __future__ import annotations

from datetime import UTC, datetime

from lazytools.report import Memo, ReportTools, Section, TableBlock, render_html, render_markdown

MEMO = Memo(
    title="Daily Holdings Review",
    as_of=datetime(2026, 6, 9, 7, 0, tzinfo=UTC),
    sections=[
        Section(
            title="Prices",
            body="All holdings refreshed.\n\nNo stale quotes.",
            tables=[
                TableBlock(
                    columns=["Ticker", "Close"],
                    rows=[["AAPL", "203.92"], ["MSFT", "512.10"]],
                )
            ],
        ),
        Section(title="Risk", body="No hard-limit violations."),
    ],
    metadata={"portfolio": "us-core", "generator": "lazytools.report"},
)

EXPECTED_MARKDOWN = """\
# Daily Holdings Review

_as of 2026-06-09T07:00:00+00:00_

## Prices

All holdings refreshed.

No stale quotes.

| Ticker | Close |
| --- | --- |
| AAPL | 203.92 |
| MSFT | 512.10 |

## Risk

No hard-limit violations.

---

- generator: lazytools.report
- portfolio: us-core
"""

EXPECTED_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Daily Holdings Review</title>
</head>
<body>
<h1>Daily Holdings Review</h1>
<p class="as-of"><em>as of 2026-06-09T07:00:00+00:00</em></p>
<h2>Prices</h2>
<p>All holdings refreshed.</p>
<p>No stale quotes.</p>
<table>
<thead><tr><th>Ticker</th><th>Close</th></tr></thead>
<tbody>
<tr><td>AAPL</td><td>203.92</td></tr>
<tr><td>MSFT</td><td>512.10</td></tr>
</tbody>
</table>
<h2>Risk</h2>
<p>No hard-limit violations.</p>
<dl>
<dt>generator</dt><dd>lazytools.report</dd>
<dt>portfolio</dt><dd>us-core</dd>
</dl>
</body>
</html>
"""


# --- markdown -------------------------------------------------------------- #


def test_render_markdown_exact_output() -> None:
    assert render_markdown(MEMO) == EXPECTED_MARKDOWN


def test_render_markdown_is_deterministic() -> None:
    assert render_markdown(MEMO) == render_markdown(MEMO.model_copy(deep=True))


def test_render_markdown_minimal_memo() -> None:
    assert render_markdown(Memo(title="Empty")) == "# Empty\n"


def test_render_markdown_escapes_pipes_and_newlines_in_cells() -> None:
    memo = Memo(
        title="T",
        sections=[Section(title="S", tables=[TableBlock(columns=["a|b"], rows=[["x\ny"]])])],
    )
    out = render_markdown(memo)
    assert "| a\\|b |" in out
    assert "| x y |" in out


def test_render_markdown_metadata_keys_sorted() -> None:
    memo = Memo(title="T", metadata={"zeta": "1", "alpha": "2"})
    out = render_markdown(memo)
    assert out.index("alpha: 2") < out.index("zeta: 1")


# --- html ------------------------------------------------------------------ #


def test_render_html_exact_output() -> None:
    assert render_html(MEMO) == EXPECTED_HTML


def test_render_html_is_deterministic() -> None:
    assert render_html(MEMO) == render_html(MEMO.model_copy(deep=True))


def test_render_html_escapes_everything() -> None:
    memo = Memo(
        title='<script>alert("t")</script>',
        sections=[
            Section(
                title="<b>S</b>",
                body="a < b & c",
                tables=[TableBlock(columns=["<col>"], rows=[['<img src=x onerror="p()">']])],
            )
        ],
        metadata={"<k>": "<v>"},
    )
    out = render_html(memo)
    assert "<script>" not in out
    assert "<img" not in out
    assert "&lt;script&gt;" in out
    assert "a &lt; b &amp; c" in out
    assert "<th>&lt;col&gt;</th>" in out
    assert "<dt>&lt;k&gt;</dt><dd>&lt;v&gt;</dd>" in out


# --- ReportTools (ToolProvider) ---------------------------------------------- #


def test_provider_is_tool_provider() -> None:
    assert ReportTools()._is_lazy_tool_provider is True


def test_as_tools_exposes_expected_names() -> None:
    assert {t.name for t in ReportTools().as_tools()} == {"render_memo", "render_memo_html"}


def test_tools_run_sync_render_a_memo_dict() -> None:
    tools = {t.name: t for t in ReportTools().as_tools()}
    payload = MEMO.model_dump(mode="json")
    assert tools["render_memo"].run_sync(memo=payload) == EXPECTED_MARKDOWN
    assert tools["render_memo_html"].run_sync(memo=payload) == EXPECTED_HTML
