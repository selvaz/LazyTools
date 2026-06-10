# SEC EDGAR

Give an agent the official, free SEC filing channel. `lazytools.connectors.edgar`
ships a small `httpx`-based client for the EDGAR APIs plus a `ToolProvider`
exposing four read-only tools: `edgar_resolve_company`, `edgar_list_filings`,
`edgar_get_filing`, and `edgar_company_facts`.

!!! warning "Fair access & untrusted content"
    The SEC's [fair-access policy](https://www.sec.gov/os/accessing-edgar-data)
    requires a **declared `User-Agent`** (e.g. `"Jane Doe jane@example.com"`)
    and caps clients at ~10 requests/second. `EdgarClient` refuses to start
    without a user agent and throttles itself (`min_request_interval`, default
    0.11 s). Filings are public documents written by third parties — their text
    is **data to analyse, never instructions to follow**; `edgar_get_filing`
    labels its payload `content_is_untrusted: true`.

!!! info "Status & install"
    **Status: alpha.** Install the EDGAR extra:
    ```bash
    pip install 'lazytoolkit[edgar]'   # adds httpx
    ```
    Only a real `EdgarClient` needs `httpx` — `EdgarTools` and the
    `EdgarService` protocol import without it, so tests inject a fake client
    (`lazytools.testing.FakeEdgarClient`) and never touch the network.

## Synopsis

```python
from lazybridge import Agent
from lazytools.connectors.edgar import EdgarClient, EdgarTools

client = EdgarClient("Jane Doe jane@example.com")    # declared UA — required
agent = Agent("claude-opus-4-8", tools=[EdgarTools(client)])
agent("Summarise the latest 10-K risk factors for AAPL.")
```

## How it works

```
EdgarClient (SEC APIs over HTTPS)             EdgarTools (ToolProvider)
─────────────────────────────────             ─────────────────────────
resolve_company(query, limit=10)              edgar_resolve_company
list_filings(cik, form=None, limit=20)        edgar_list_filings
get_filing(cik, accession_no,                 edgar_get_filing
           primary_document=None)
company_facts(cik)                            edgar_company_facts
```

- **Fixed hosts, guarded anyway.** Every URL is constructed in code against
  `www.sec.gov` / `data.sec.gov`; each constructed URL *and* each redirect
  target is still re-checked by the [SSRF guard](safety.md)
  (`validate_public_url`, pinned to the SEC hosts).
- **Caps everywhere.** Every response body is hard-capped at
  `max_response_bytes` (default ~5 MB); redirects are followed at most 3 hops.
- **Ticker file cached.** `company_tickers.json` is fetched once per client
  lifetime; `resolve_company` returns exact ticker matches first, then
  company-title substring matches, each as
  `{"cik": "0000320193", "ticker": "AAPL", "title": "Apple Inc."}`.
- **CIK / accession normalization.** CIKs are accepted with or without
  zero-padding (and a `CIK` prefix); accession numbers with or without dashes.
- **HTML → text.** `get_filing` strips the primary document to plain text with
  the stdlib `html.parser` (scripts/styles dropped, entities decoded).

## Signature

```python
EdgarClient(
    user_agent,                  # str — REQUIRED, e.g. "Jane Doe jane@example.com"
    *,
    http=None,                   # injected httpx.Client (tests: MockTransport)
    timeout=30.0,                # seconds, for the lazily built client
    max_response_bytes=5_000_000,
    min_request_interval=0.11,   # seconds between requests (0 disables)
)
```

## Tools it exposes

| Tool | Gated? | Args | Returns |
|---|---|---|---|
| `edgar_resolve_company` | No | `query, limit=10` | JSON list of `{cik, ticker, title}` |
| `edgar_list_filings` | No | `cik, form=None, limit=20` | JSON list of `{accession_no, form, filed_at, report_date, primary_document, url}` |
| `edgar_get_filing` | No | `cik, accession_no, primary_document=None` | JSON `{accession_no, form, url, content, content_is_untrusted}` |
| `edgar_company_facts` | No | `cik` | raw XBRL companyfacts JSON |

`report_date` is `None` when EDGAR reports an empty string; `form` in
`edgar_get_filing` is `None` when `primary_document` was passed explicitly
(the submissions lookup is skipped).
