# Report (LazyReport)

Deterministic, domain-agnostic memo rendering. `lazytools.report` ships
pydantic models (`Memo` / `Section` / `TableBlock`), two pure-function
renderers (`render_markdown`, `render_html`), a `ToolProvider` (`ReportTools`)
exposing `render_memo` and `render_memo_html`, and a file-writing
`ToolProvider` (`ReportFiles`) exposing `save_report` — materialise a rendered
report to disk so it can then be sent as an attachment (e.g. with the Telegram
connector's `telegram_send_document`).

The split of responsibilities is deliberate: **an LLM writes the prose** (the
section bodies); **the layout is deterministic** — the same memo always
produces byte-identical output, so reports are reproducible and auditable.

!!! info "Status & install"
    **Status: alpha. No extra needed** — stdlib + the pydantic that ships with
    `lazybridge`:
    ```bash
    pip install "lazytoolkit @ git+https://github.com/selvaz/LazyTools.git"
    ```
    **PDF rendering is deliberately deferred**: it would pull a heavy
    dependency (weasyprint / reportlab) into an otherwise stdlib-only module.
    Render to HTML and convert externally (or print-to-PDF) until a
    `heavy_render`-style extra lands.

## Synopsis

```python
from datetime import datetime, UTC
from lazytools.report import Memo, Section, TableBlock, render_markdown, render_html

memo = Memo(
    title="Daily Holdings Review",
    as_of=datetime(2026, 6, 9, 7, 0, tzinfo=UTC),
    sections=[
        Section(
            title="Prices",
            body="All holdings refreshed.",
            tables=[TableBlock(columns=["Ticker", "Close"], rows=[["AAPL", "203.92"]])],
        ),
    ],
    metadata={"portfolio": "us-core"},
)

print(render_markdown(memo))   # H1, _as of …_, H2 sections, GFM tables, metadata list
print(render_html(memo))       # minimal HTML, html.escape on every value
```

As a tool provider:

```python
from lazybridge import Agent
from lazytools.report import ReportTools

agent = Agent("claude-opus-4-8", tools=[ReportTools()])
```

## How it works

- **`render_markdown`** — H1 title, an `_as of …_` line (ISO timestamp), H2
  sections, GitHub-flavoured tables (cell pipes escaped, newlines collapsed),
  and a trailing `key: value` metadata list. Section bodies are Markdown prose
  and pass through verbatim.
- **`render_html`** — minimal clean HTML; **everything** goes through
  `html.escape`, so untrusted strings (filing excerpts, tickers, LLM prose)
  can never inject markup. Bodies are treated as plain text here (paragraphs
  split on blank lines), not parsed as Markdown.
- **Deterministic.** Both renderers are pure functions; metadata keys are
  emitted in sorted order so output never depends on insertion order.

## Models

| Model | Fields |
|---|---|
| `TableBlock` | `columns: list[str]`, `rows: list[list[str]]` |
| `Section` | `title: str`, `body: str = ""` (markdown prose), `tables: list[TableBlock] = []` |
| `Memo` | `title: str`, `as_of: datetime \| None = None`, `sections: list[Section] = []`, `metadata: dict[str, str] = {}` |

## Tools it exposes

`ReportTools` (rendering):

| Tool | Gated? | Args | Returns |
|---|---|---|---|
| `render_memo` | No | `memo` (Memo-shaped JSON object) | Markdown string |
| `render_memo_html` | No | `memo` (Memo-shaped JSON object) | HTML string |

`ReportFiles(base_dir="reports")` (persistence):

| Tool | Gated? | Args | Returns |
|---|---|---|---|
| `save_report` | No | `filename` (basename — any directory part is ignored), `content` (full text) | absolute path of the written file |

`save_report` reduces `filename` to its basename and strips unsafe characters
(no path traversal); the extension must be one of
`md/markdown/html/htm/csv/txt/json` or `.md` is appended. Files are written
under `base_dir` (created on first write). Typical flow: `render_memo` →
`save_report` → `telegram_send_document`.

```python
from lazybridge import Agent
from lazytools.report import ReportTools, ReportFiles

agent = Agent("claude-opus-4-8", tools=[ReportTools(), ReportFiles(base_dir="/data/reports")])
```
