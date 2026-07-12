# Report (LazyReport)

Deterministic, domain-agnostic memo rendering. `lazytools.report` ships
pydantic models (`Memo` / `Section` / `TableBlock` / `FigureBlock`), two
pure-function renderers (`render_markdown`, `render_html`), a `ToolProvider`
(`ReportTools`) exposing `render_memo` and `render_memo_html`, and a
file-writing `ToolProvider` (`ReportFiles`) exposing `save_report` —
materialise a rendered report to disk so it can then be sent as an attachment
(e.g. with the Telegram connector's `telegram_send_document`).

The split of responsibilities is deliberate: **an LLM writes the prose** (the
section bodies); **the layout is deterministic** — the same memo always
produces byte-identical output, so reports are reproducible and auditable.

!!! info "Status & install"
    **Status: alpha.** The core (Markdown/HTML rendering, `file:`/`bytes:`
    figures) needs **no extra** — stdlib + the pydantic that ships with
    `lazybridge`:
    ```bash
    pip install "lazytoolkit @ git+https://github.com/selvaz/LazyTools.git"
    ```
    **Figure schemes add optional dependencies only when used**: `chart:`
    (on-demand charts from datahub series) needs the `[charts]` extra
    (matplotlib) plus `market-data-hub`; `crawler:` needs `[web]`; `regimes:`
    needs `lazystats[regimes]`. See [Figures](#figures-charts-and-images-in-a-memo).
    ```bash
    pip install "lazytoolkit[charts] @ git+https://github.com/selvaz/LazyTools.git"
    ```
    **PDF rendering is deliberately deferred**: it would pull a heavy
    dependency (weasyprint / reportlab). Render to HTML (self-contained, with
    embedded figures) and convert externally, or print-to-PDF, until a
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
| `FigureBlock` | `ref: str` (canonical `scheme:key` artifact ref), `caption: str = ""` |
| `Section` | `title: str`, `body: str = ""` (markdown prose), `tables: list[TableBlock] = []`, `figures: list[FigureBlock] = []` |
| `Memo` | `title: str`, `as_of: datetime \| None = None`, `sections: list[Section] = []`, `metadata: dict[str, str] = {}` |

## Figures: charts and images in a memo

A `FigureBlock` names its image with a canonical **artifact ref** — the
ecosystem's shared identity (`lazydatacore.ArtifactRef`, shape
`"scheme:key"`). `render_html` resolves each ref through an
`ArtifactResolvers` registry and embeds the image as a **base64 data URI**,
so the output stays a single self-contained file (Telegram/email/browser
ready); `render_markdown` stays text-only and degrades a figure to an italic
`Figure: caption (ref)` line.

| Scheme | Key | Source | Needs |
|---|---|---|---|
| `regimes:` | `plot_key` | PNG in the LazyStats regime depot (from `regime_generate_plots`) | `lazystats[regimes]` |
| `crawler:` | `content_hash` | blob in a LazyCrawler artifacts DB (crawl with `download_artifact_bytes=True`) | `lazycrawler` |
| `chart:` | querystring spec | rendered on demand from market-data-hub series | `lazytoolkit[charts]` + `market-data-hub` |
| `file:` | path | local file (optionally sandboxed via `file_base_dir`) | — |
| `bytes:` | base64 | inline payload | — |

The core registry (`ArtifactResolvers()`) resolves only `file:`/`bytes:` and
keeps the module stdlib-only; `ecosystem_resolvers()` registers the source
schemes, importing each producer lazily **at resolve time**:

```python
from lazytools.report import Memo, Section, FigureBlock, render_html, ecosystem_resolvers

memo = Memo(
    title="Weekly Regimes",
    sections=[Section(
        title="SPY",
        figures=[
            FigureBlock(ref="regimes:plotfit__series__SPY__20260710T070000", caption="SPY with regime bands"),
            FigureBlock(ref="chart:symbols=SPY,^VIX&start=2024-01-01&frequency=W", caption="SPY vs VIX"),
        ],
    )],
)
html = render_html(memo, artifacts=ecosystem_resolvers())  # one self-contained file
```

A `chart:` spec accepts `symbols` (comma-separated, required), `start`,
`end`, `domain` (`prices`/`macro`/`custom`/`crypto`/`factors`), `field`,
`transform` (`level`/`log_return`/`pct_change`/`diff`), `frequency`
(`D`/`W`/`M`/`Q`) and `title` — the same vocabulary as the hub's
`extract_series`. In `lazytools.report.charts`, `chart_series(...)` is the
plain-Python equivalent returning PNG bytes, `render_series_png(df, ...)` is
the pure DataFrame → PNG renderer under it (headless, deterministic), and
`parse_chart_spec(spec)` decodes a `chart:` querystring key into
`chart_series` kwargs. An unresolvable ref (unknown scheme, missing plot,
absent package, or a MIME that isn't a strict `image/*`) **raises** rather
than rendering a silently incomplete report.

## Tools it exposes

`ReportTools` (rendering):

| Tool | Gated? | Args | Returns |
|---|---|---|---|
| `render_memo` | No | `memo` (Memo-shaped JSON object) | Markdown string |
| `render_memo_html` | No | `memo` (Memo-shaped JSON object) | HTML string |
| `save_memo_html` | No¹ | `memo`, `filename` (basename) | absolute path |
| `save_memo_markdown` | No¹ | `memo`, `filename` (basename) | absolute path |

¹ `save_memo_html` / `save_memo_markdown` appear **only** when `ReportTools` is
built with a `files=ReportFiles(...)`. They render **and** write in a single
call, returning just the path.

!!! warning "For reports with figures, an agent must use `save_memo_html`"
    `render_memo_html` returns the HTML *string*. A self-contained HTML with
    embedded base64 images is hundreds of KB — far too large for a model to
    faithfully copy from one tool result into a `save_report(content=…)`
    argument (it truncates). `save_memo_html` keeps the bytes out of the LLM's
    token stream entirely: memo in, path out. Build `ReportTools` with a
    `files=` and point the agent at `save_memo_html`.

`ReportFiles(base_dir="reports")` (persistence):

| Tool | Gated? | Args | Returns |
|---|---|---|---|
| `save_report` | No | `filename` (basename — any directory part is ignored), `content` (full text) | absolute path of the written file |

`save_report` reduces `filename` to its basename and strips unsafe characters
(no path traversal); the extension must be one of
`md/markdown/html/htm/csv/txt/json` or `.md` is appended. Files are written
under `base_dir` (created on first write). Use it for text you already have;
for a memo (especially with figures) prefer the one-step `save_memo_html`.

```python
from lazybridge import Agent
from lazytools.report import ReportTools, ReportFiles, ecosystem_resolvers

files = ReportFiles(base_dir="/data/reports")
# figures resolve at render time; save_memo_html writes the full HTML to disk
report = ReportTools(artifacts=ecosystem_resolvers(datahub_db_path="…/hub.duckdb"), files=files)
agent = Agent("claude-opus-4-8", tools=[report, files])
```
