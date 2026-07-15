# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

### Added — MCP server (`lazytools.mcp_server`, `lazytools-mcp`)
- New `lazytools.mcp_server` package and `lazytools-mcp` console script that
  **expose** LazyTools' tool providers over the Model Context Protocol — the
  mirror of the `connectors/mcp` client. Any MCP host (Claude Desktop, Claude
  Code, Codex) can now call `datahub_*`, `statistical_*`, `regime_*` and web
  search/crawl as native tools. The bridge is thin: each `lazybridge.Tool`
  already carries the JSON Schema (`tool.definition()`) MCP wants for
  `inputSchema` and an async dispatch (`tool.run()`) for `call_tool`.
- `build_server(providers, *, read_only=True, ...)` wires `list_tools` /
  `call_tool` on a low-level MCP `Server`; `serve_stdio(server)` runs it over
  the stdio transport MCP hosts launch; `default_providers(ids=None)` is the
  read-only provider menu (`datahub`, `statistical`, `regimes`, `web`).
- **Read-only by default.** Providers are built in their read-only shape and a
  secondary name guard (`UNSAFE_TOOL_PATTERNS`) drops obvious writers; mutating
  tools require explicit `--allow-unsafe` / `read_only=False`. Providers whose
  optional extra is absent are skipped at expansion time, so a bare `[mcp]`
  install serves datahub + statistical and regimes/web light up with their
  extras. Reuses the existing `[mcp]` extra (`mcp>=1.0,<2.0`).

### Added — series transformation layer + OLS/Ridge/Lasso regression tools
- `statistical_analysis` gains a generic `load_series` data path: any
  instrument argument now accepts `'<id>[|level|log_return|pct_change|diff]'`
  specs across ticker/factor/macro domains (default transform per domain:
  ticker `log_return`, factor `level`, macro `diff`), so the existing
  volatility/correlation/outlier tools can analyse prices in level, Fama-
  French factors or FRED macro series, not just ticker returns. Mixed-domain
  panels are outer-joined on dates; `load_returns` keeps its original
  behaviour for backward compatibility.
- Three new tools — `statistical_regression_ols`, `statistical_regression_ridge`,
  `statistical_regression_lasso` — fit univariate/multivariate OLS
  (statsmodels, robust/HAC standard errors) and Ridge/Lasso (scikit-learn,
  cross-validated alpha by default) between any hub series read only through
  market-data-hub. Max 10 regressors; results are coefficients and
  diagnostics only, never the raw series. Requires `lazystats[regression]`.

### Added — one-step render-and-save report tools
- `ReportTools(files=ReportFiles(...))` now also exposes `save_memo_html` and
  `save_memo_markdown`: they render a memo AND write it to the sandboxed
  `base_dir` in a single tool call, returning just the path. This is required
  for an autonomous agent to produce a report **with figures** — a
  self-contained HTML embeds base64 images and is far too large to route back
  through the model from `render_memo_html` into `save_report(content=…)`
  (the model truncates it). The bytes now never enter the LLM's token stream.
- `ReportFiles.save(filename, content)` is the public write method (the
  `save_report` tool and the new render-and-save tools share it).

## [0.3.3] — 2026-07-12

### Added — report figures (charts and images in memos)
- `FigureBlock` on `Section`: a figure named by canonical `scheme:key`
  artifact ref (the `lazydatacore.ArtifactRef` string shape, parsed with
  stdlib only). `render_html` embeds each figure as a base64 data URI —
  one self-contained HTML file; `render_markdown` degrades to an italic
  `Figure: caption (ref)` line.
- `ArtifactResolvers` (`report/artifacts.py`): per-scheme registry →
  `(bytes, mime)`. Core schemes `file:` (optional `file_base_dir` sandbox)
  and `bytes:` (inline base64, MIME sniffed); unregistered schemes fail
  loudly at resolve time.
- `ecosystem_resolvers()` (`report/resolvers.py`): registers `regimes:`
  (LazyStats depot plots, needs `RegimeDB.get_plot` — LazyStats#7),
  `crawler:` (LazyCrawler artifact blobs by `content_hash` —
  LazyCrawler#36) and `chart:`; every producer imported lazily at resolve
  time, duck-typed stores injectable.
- `report/charts.py` + new `charts` extra (matplotlib): on-demand PNG
  charts from any market-data-hub series. `chart_series()` (fetch via
  `extract_series` + headless render), `render_series_png()` (pure
  DataFrame → PNG), `parse_chart_spec()` for `chart:` querystring refs.
- `ReportTools(artifacts=...)` threads a resolver registry into
  `render_memo_html`; memo shape in tool descriptions documents `figures`.

## [0.3.2] — 2026-07-12

First tagged public release. Version `0.3.1` was a source-only bump: it was
never published anywhere (no Git tag, no PyPI artifact) and is superseded by
this release without a changelog entry of its own.

### Distribution
- lazytoolkit is distributed **exclusively via GitHub**: install with
  `pip install "lazytoolkit @ git+https://github.com/selvaz/LazyTools.git@v0.3.2"`
  or from the wheel attached to the GitHub Release (SHA-256 checksums
  published alongside). Only LazyBridge lives on PyPI; `lazytoolkit` is not
  and will not be a PyPI package.
- The release workflow now builds wheel + sdist, verifies the built wheel's
  `Requires-Dist` metadata, smoke-installs the wheel in a clean venv, and
  attaches the artifacts with checksums to a GitHub Release (it no longer
  targets PyPI).

### Fixed — distribution metadata
- The `web` extra declared a bare `lazycrawler>=0.14` requirement: LazyCrawler
  is not on PyPI, so the name could never resolve and was a
  dependency-confusion exposure (the PyPI name is unregistered). It is now an
  immutable direct reference to the LazyCrawler repository.
- Dependencies confirmed on the stable core line: `lazybridge>=1.0.1,<2.0`.

### Fixed — raw series, file loading and unbounded output out of the default agent surface (audit CA-02/CA-11/CA-12)
Architectural invariant restated by the user and applied across the board:
an agent never carries a time series through its own context — it passes
symbols/ids in and gets bounded results out; tools run inside the process
and read from market-data-hub, never the other way around.

- **CA-02** — `datahub_get_series`/`datahub_get_returns` (raw matrices) are
  no longer in `DataHubTools`'s default profile. New `allow_raw_series=True`
  opt-in for explicit verification/spot-checking, still capped at 500 rows.
  Mirrored in market-data-hub itself: `agent_tools.TOOL_FUNCTIONS` no longer
  includes `tool_get_series`/`tool_get_returns`; they moved to
  `RAW_SERIES_TOOL_FUNCTIONS` (opt-in, same cap).
- **CA-11** — `RegimeTools` no longer exposes `lazystats.regimes.tools.
  load_time_series` (arbitrary `file_path` read from disk) at all, gated or
  not. Replaced with `regime_load_from_datahub` — symbols/dates/frequency/
  data_key in, a bounded summary out, market-data-hub as the sole source.
- **CA-12** — `regime_get_changes` and `regime_db_get_state_sequence` now
  hard-cap their output (200 changes / 500 timesteps) at the bridge even
  when the caller passes `last_n=0` ("return everything"); both report a
  `truncated` flag and the applied `hard_cap`.

### Fixed — `ResolveTools` deprecated: it bypassed market-data-hub (audit CA-03)
- `connectors.fin.ResolveTools.get_financial_facts` fetched RAW EDGAR company
  facts directly through its injected client — no DB, no coverage, no run
  ledger, no provenance, exactly the direct-fetch pattern `EdgarTools`/
  `MarketDataTools` were already deprecated for. Now warns at construction
  with the same one-release-compatibility discipline, pointing to
  `datahub_resolve_instrument` + `datahub_get_financial_facts` /
  `datahub_ensure_financials`.
- New cross-repo boundary test (`test_no_direct_finance_clients.py`):
  asserts `connectors/fin/agents.py` never imports `connectors.edgar` /
  `connectors.marketdata`, and that `pm_supervisor`'s default tool list never
  silently grows to include a direct-fetch provider.

### Added — `connectors/regimes`: closes the last LLM-callability gap (plan v3.1 Fase 6)
- New `RegimeTools` provider wraps `lazystats.regimes`'s ~27 HMM/MS
  regime-detection functions (`fit_regimes`, `get_current_regime`,
  `get_regime_changes`, `regime_store_*`, `regime_params_*`, `db_*` depot
  inspection/mutation, ...) as LazyBridge tools — before this connector,
  none of them were reachable by an agent through LazyTools. Uses
  `Tool.wrap`'s native `Annotated[type, "description"]` support directly on
  the migrated functions, no reimplementation.
- 18 read tools always exposed; 9 write tools (fit, persist, delete) gated
  by `RegimeTools(allow_write=True)`.

### Fixed — statistical_analysis no longer duplicates lazystats' math
- `StatisticalAnalysisTools` now delegates volatility/correlation/outlier
  computation to `lazystats.core.returns` instead of carrying a second,
  independent implementation of the same formulas (drift risk: a future fix
  in one repo would silently not propagate to the other). Tool signatures,
  output shape and golden numbers are unchanged.

### Added — `connectors/fin`: LazyFin's agentic surface moves here (plan v3.1 Fase 5)
- **`lazytools.connectors.fin`**: the five LazyFin tool providers
  (`PortfolioTools`, `RiskTools`, `OptimizerTools`, `ScoringTools`,
  `ResolveTools`) and the five agent factories (`pm_supervisor`,
  `filing_analyst`, `value_selection`, `macro_analyst`, `geo_risk_analyst`)
  now live in LazyTools — the single LLM bridge. LazyFin keeps the pure
  kernel; its own copies are one-release deprecation shims. Requires
  `lazyfin` (git-installed, no PyPI extra by design).

### Added — datahub connector: hub identity/financials/health tools
- New read tools mirroring market-data-hub's plan-v3.1 surface:
  `datahub_resolve_instrument`, `datahub_get_price_summary`,
  `datahub_get_financials_coverage`, `datahub_get_financial_facts`,
  `datahub_get_statement`, `datahub_get_job_status`,
  `datahub_get_ingestion_health`.
- New gated write tools (only with `allow_refresh=True`):
  `datahub_ensure_price_history`, `datahub_ensure_financials`. The backend
  passes the hub-side `allow_write=True` itself — gating stays in the
  provider construction.

### Deprecated — direct-fetch financial tools
- **`MarketDataTools`** and **`EdgarTools`** now emit a `DeprecationWarning`
  at construction: market-data-hub is the sole data owner; use the hub-backed
  `DataHubTools` instead. Kept for one compatibility release.

### Added — datahub connector: opt-in `datahub_refresh_prices` write tool
- **`DataHubTools(allow_refresh=True)`** now exposes market-data-hub's
  `tool_refresh_prices` as the write tool `datahub_refresh_prices` (download
  prices from Yahoo, persist into the hub DB, rebuild coverage). The surface
  stays read-only by default. This restores full parity with the (now removed)
  ToolProvider that market-data-hub used to ship — this connector is the one
  LazyBridge binding for the hub.
- **Contract tests against the real hub** (skip when it is not installed): a
  signature-parity guard between the `DataHubBackend` Protocol and
  `market_data_hub.agent_tools.tool_*` (the drift the hand-mirrored Protocol
  could not detect), plus an end-to-end call through `MarketDataHubBackend`.

### Removed — `datahub` PyPI extra (dependency-confusion hardening)
- Dropped `lazytoolkit[datahub]`: market-data-hub is a private, git-installed
  package, so the bare PyPI name could never resolve — and could be squatted.
  Install the hub directly:
  `pip install 'market-data-hub @ git+https://github.com/selvaz/market-data-hub.git'`.
  The connector still imports lazily and raises a clear `ImportError` hint.

### Added — Telegram 4096-char message chunking (0.3.1)
- **`split_message` / `MAX_MESSAGE_CHARS`** in
  `lazytools.connectors.telegram`: splits text into Bot-API-acceptable
  chunks (≤4096 chars), preferring paragraph → line → space breaks and
  hard-cutting only as a last resort.
- **`telegram_send_message` now chunks long text** instead of failing: the
  Bot API rejects `sendMessage` payloads over 4096 characters outright, so
  a long model answer previously errored the whole send. One confirmation
  grant still covers the one logical message (all its chunks). The tool's
  return value lists every sent `message_id`, comma-separated.

### Added — Telegram document attachments + report file output
- **`telegram_send_document`** — the Telegram connector can now send file
  attachments, not just text. `TelegramClient.send_document(chat_id, document,
  filename, caption)` uploads via the Bot API `sendDocument` (multipart, with
  the same bot-token redaction on error as the other calls), and `TelegramTools`
  exposes `telegram_send_document(chat_id, file_path, caption)` behind the
  **identical Allowlist + one-shot `ConfirmationGate`** as `telegram_send_message`.
  Enforces the 50 MB Bot API limit, truncates captions to 1024 chars, and reads
  the file + uploads off the event loop (`asyncio.to_thread`). Because
  `file_path` is typically model-controlled, `TelegramTools(attachments_dir=…)`
  confines uploads to a directory (symlinks resolved) — set it whenever the tool
  is exposed to an LLM so an agent can't attach arbitrary host files.
- **`lazytools.report.ReportFiles`** — a `ToolProvider` exposing `save_report`
  (filename, content) → writes the report to a file under a sandboxed `base_dir`
  and returns the absolute path. Filenames are reduced to their basename and
  hardened (no path traversal); the extension must be one of
  `md/markdown/html/htm/csv/txt/json` (else `.md` is appended). Refuses to write
  through a pre-existing symlink or any path that resolves outside `base_dir`.
  Pairs with `render_memo` and `telegram_send_document` to render → persist → attach.

### Added — local Outlook desktop connector
- **`lazytools.connectors.outlook`** — a `GmailClient`/`GmailTools` mirror
  pointed at the copy of Outlook **already running and signed in** on the
  local Windows machine, over COM (MAPI), instead of a cloud API. No OAuth,
  no API quota, no Pub/Sub — at the cost of running where Outlook desktop
  lives. New `outlook` extra pulls `pywin32` (Windows-only marker, so the
  install is a no-op elsewhere; the connector imports without it and only
  `OutlookClient.connect()` needs it).
  - `OutlookClient` / `OutlookService` — duck-typed `list_message_ids` /
    `get_message` / `create_draft` / `send_message`. `get_message` returns the
    **same Gmail-shaped resource** (`payload.headers` + `snippet`), lifting the
    genuine top-most `Authentication-Results` out of `PR_TRANSPORT_MESSAGE_HEADERS`
    (first-wins, so a body-forged copy is ignored) and resolving Exchange
    senders to their SMTP address. Header parsing reuses the existing
    provider-agnostic `parse_authentication_results`.
  - `OutlookTools` / `OutlookSendBlocked` — `outlook_list_emails` /
    `outlook_get_email` / `outlook_create_draft` / `outlook_send`, with the
    identical Allowlist + one-shot (task-scopable) `ConfirmationGate` guarding
    the send path as `GmailTools`. Structured filters compile to an Outlook
    Restrict (`@SQL=` DASL) expression.
  - `testing.FakeOutlookService` for tests that never touch COM.

## [0.3.0] — 2026-06-11

### Changed (breaking) — code_support write access is now a gated capability
- **`claude_code` / `codex` are read-only by construction.** `mode="write"`
  is no longer an argument (`claude_code` keeps `read`/`plan`; `codex` drops
  `mode` entirely): the LLM controls tool *arguments*, so write access must
  not be one. Writes live behind the new **`CodeWriteTools`** provider —
  mandatory existing `base_dir` sandbox (every call's `cwd` must resolve
  inside it; escapes raise `CodeWriteBlocked` without burning a grant),
  one-shot `confirm_write()` gate per write call by default (task-scopable,
  mirroring the Gmail send tools), Codex writes keep the git rail
  (`codex_skip_git_check=False`). Exposes `claude_code_write` (and
  `codex_write` with `codex=True`). The write tools are async so task-scoped
  grants bind correctly.
- **CLI results are labelled untrusted.** `claude_code` / `codex` /
  `CodeWriteTools` successes now return
  `{"result": ..., "content_is_untrusted": true}` (repo-derived text is a
  prompt-injection surface); connector-level failures stay plain
  `"[claude_code] ..."` / `"[codex] ..."` strings.
- **`build_cli_collaboration` defaults to the read-only three-session
  pipeline** (Claude Code analyst + Codex critic, both read-only, plus a
  synthesizer that writes the *plan*). `execute=True` now requires
  `base_dir=` (internal ungated writer, sandbox + git as rails) **or**
  `writer=` (bring your own `CodeWriteTools` — the only way to run a
  gate-enabled executor, since the caller must hold the instance to call
  `confirm_write()` while the pipeline runs; post-review fix from #32).

### Fixed
- **`build_cli_collaboration` crashed on released lazybridge builds.** It
  imported `lazybridge.dedup_guard`, which does not exist in lazybridge
  0.9.0 (PyPI) — the pipeline could not even be constructed there, and no
  test built it. The `DeduplicateGuard` import now degrades gracefully on
  older lazybridge, and the new tests construct the pipeline.

### Added
- **Gmail history & push surface** (`lazytools.connectors.gmail`) — the
  client plumbing for event-driven mail intake (consumed by LazyPulse's
  `GmailPushInbox`): `GmailClient.get_history_id()` (cursor anchor via
  `users.getProfile`), `GmailClient.list_history_message_ids()`
  (`users.history.list` with `historyTypes=messageAdded`, bounded
  pagination, de-duplication, and an HTTP 404 ->
  `GmailHistoryExpired` mapping so callers know to resync), and
  `GmailClient.watch()` / `stop_watch()` to arm/disarm Gmail push
  notifications onto a Cloud Pub/Sub topic. `GmailHistoryExpired` is
  exported from `lazytools.connectors.gmail`. `FakeGmailService` grows
  the same surface (`add_message()` advances a fake history cursor;
  `history_expired` simulates retention expiry) so downstream adapter
  tests stay network-free. See *Gmail -> History & push notifications*.
  Cursor safety (post-review hardening): the returned cursor never
  advances past a message that was not returned — a capped walk resumes
  from the last fully consumed history record, and a single oversized
  record is consumed whole so the caller always makes progress.

### Fixed
- **SSRF URL guard: legacy numeric IP literals.** `validate_public_url` now
  recognizes the legacy numeric host forms that resolvers normalize to an IP
  (decimal `2130706433`, hex `0x7f000001`, octal `0177.0.0.1`, short dotted
  `127.1`) as IP literals and applies the same non-global block, instead of
  treating them as DNS names. Previously these could bypass the loopback/
  private-IP refusal for callers not pinning `allowed_hosts`. The check stays
  purely syntactic (no DNS, no I/O). (#27)

## [0.2.0] — 2026-06-10

### Added
- **SEC EDGAR connector** (`lazytools.connectors.edgar`, extra `[edgar]`) —
  `EdgarClient` against the official, free SEC APIs (company_tickers,
  submissions, XBRL companyfacts, Archives documents) and an `EdgarTools`
  provider with `edgar_resolve_company`, `edgar_list_filings`,
  `edgar_get_filing`, `edgar_company_facts`. The client requires a declared
  `user_agent` (SEC fair-access policy), throttles requests (~10 req/s),
  hard-caps every response body, re-validates redirects against the pinned
  SEC hosts, and labels filing text `content_is_untrusted`.
- **Market-data connector** (`lazytools.connectors.marketdata`, extra
  `[marketdata]`) — `MarketDataClient` over a swappable `MarketDataAdapter`
  protocol, with the free, key-less `StooqAdapter` (stooq.com CSV) first;
  `MarketDataTools` exposes `prices_get` / `prices_history` (`1m`/`3m`/`6m`/
  `1y`/`5y`). All prices are strings, so downstream `Decimal` parsing never
  loses precision.
- **LazyReport** (`lazytools.report`, no extra) — deterministic, generic
  memo rendering: `Memo`/`Section`/`TableBlock` pydantic models plus
  `render_markdown` / `render_html` (pure functions, fully HTML-escaped) and
  a `ReportTools` provider (`render_memo`, `render_memo_html`). PDF rendering
  is deliberately deferred (heavy dependency).
- **SSRF URL guard** (`lazytools.safety.urls`) — `validate_public_url` /
  `UrlBlocked`: http(s)-only schemes, optional host pinning, non-global
  literal IPs refused; applied by the new connectors to every constructed URL
  and every redirect target.
- **Testing fakes** — `FakeEdgarClient` and `FakeMarketDataAdapter` in
  `lazytools.testing` with small Apple-ish canned data.

### Added
- **`TelegramClient.close()` + context-manager support.** The HTTP connection
  pool created by `from_token()` can now be released explicitly
  (`with TelegramClient.from_token(...) as client: ...`).
- **Real-SDK MCP integration tests.** A toy FastMCP stdio server now exercises
  the sync-discovery → cross-loop-call path and the async context-manager
  round-trip; the `mcp` extra is part of `[test]` so CI runs them instead of
  silently skipping.
- **Gmail read tools — `gmail_list_emails` and `gmail_get_email`.** `GmailTools`
  now exposes four tools instead of two: a structured inbox search
  (`gmail_list_emails`, filtering by `sender` / `subject` / `contains` /
  `unread` / raw `query`, AND-combined, with `max_results`) and a single-message
  reader (`gmail_get_email`, headers + snippet). Both are ungated and work on
  the narrow `gmail.metadata` scope (metadata-format reads). The `GmailService`
  protocol seam (`list_message_ids` / `get_message`) is unchanged, so existing
  fakes keep working. `as_tools()` returns the read tools ahead of the existing
  `gmail_create_draft` / `gmail_send`.
- **Thread-safe `GmailClient`.** The googleapiclient-backed client guards its
  service calls so it can be shared across an agent's concurrent tool calls.

### Security
- **`skill_builder_tools` is now sandboxed (breaking).** The builder tool used
  to expose `build_skill` to the LLM unsandboxed: LLM-chosen `source_dirs`
  (arbitrary file read via later queries), LLM-chosen `output_root`, and an
  `overwrite=True` default that `rmtree`-d whatever already existed at the
  target path. `skill_builder_tools(base_dir=...)` is now **required** — source
  dirs must resolve inside the sandbox and bundles are always written to
  `<base_dir>/generated_skills`. Additionally `build_skill(overwrite=True)`
  refuses to delete a directory that does not look like a skill bundle (no
  `manifest.json`), for direct callers too.
- **`claude_code(mode="read")` no longer allows Bash.** `--allowedTools`
  pre-approves tools rather than sandboxing them, so the previous
  `Read,Bash,Grep,Glob` set gave "read" mode arbitrary command execution. Read
  mode is now `Read,Grep,Glob`; use `mode="write"` when commands are needed.
- **Telegram bot token no longer leaks into error messages.** The Bot API
  embeds the token in the request URL and `httpx` error text includes the URL;
  `TelegramClient` now redacts the token from all re-raised errors (and
  suppresses exception chaining, which would have carried the original
  message into logged tracebacks).
- **Gateway refuses to send `api_key` over plain HTTP.**
  `JsonHttpExternalToolClient` now raises at construction when a bearer key is
  combined with a non-HTTPS `base_url` (loopback addresses excepted, for local
  development gateways).
- **Gmail header-injection guard.** `_encode` rejects CR/LF in `to` / `subject`
  explicitly instead of relying on the Python version's stdlib behaviour.
- **`ConfirmationGate` is now thread-safe.** `grant`/`consume` are guarded by a
  lock so a grant issued from a review-queue/UI thread can never be double-spent
  by concurrently consuming workers.
- **Skill indexing skips hidden directories.** `build_skill` no longer descends
  into `.git` / `.venv` / other dot-directories (previously only dot-*files*
  were skipped), keeping vendored and internal files out of bundles.
- **Gmail OAuth token written world-readable.** `GmailClient.from_credentials`
  persisted the cached token with the process umask, so the long-lived OAuth
  refresh token could land at mode `0644` and be read by any local user. The
  token file is now `chmod`-ed to `0600` after every write (which also tightens
  a pre-existing token file).

### Fixed
- **MCP connector now works with the real SDK in every usage pattern.** The
  official SDK's sessions are loop- and task-affine. Previously the sync
  `as_tools()` facade (what `Agent(tools=[MCP.stdio(...)])` triggers) connected
  on a throwaway `asyncio.run` loop, so discovery succeeded but **every
  subsequent tool call failed** with `ClosedResourceError`; closing from a
  different task also tripped anyio's "exit cancel scope in a different task"
  error. Each `MCPServer` now owns a dedicated background event loop that all
  transport operations are dispatched to, and each SDK transport runs its
  session inside a single long-lived lifecycle task that both enters and exits
  the SDK context. Sync discovery, cross-loop tool calls, and the async
  context-manager pattern all work; covered by real-SDK integration tests.
- **MCP `deny=` can now skip a malformed tool.** Allow/deny filtering is
  applied to (namespaced) tool names *before* schema validation, so a denied
  tool with a non-`object` `inputSchema` no longer raises — which is exactly
  the escape hatch the error message recommends.
- **`MCPServer.aclose()` is terminal even when never connected**, matching the
  documented "closure is terminal" contract.
- **`alist_tools()` / `as_tools()` return a defensive copy** of the cached tool
  list; callers mutating the returned list can no longer corrupt the cache.
- **`read_folder_docs` docstring drift.** The docstring claimed a nonexistent
  path is reported as a plain string; it raises `FileNotFoundError` and is now
  documented as such (docs site updated to match).
- **DOCX table reader dropped its row filter.** `documents.read_docs` had a
  dead `or True` guard that made non-row table children (`<w:tblPr>`,
  `<w:tblGrid>`) be treated as rows; the reader now iterates only genuine
  `<w:tr>` rows.
- **MCP tool-cache race.** `MCPServer.alist_tools` read and refilled the
  discovered-tools cache outside any lock, so concurrent callers could issue
  duplicate `list_tools()` round-trips and clobber each other's cache. The
  check + refill is now guarded by the server's lock.

### Changed
- **Custom transports (`MCP.from_transport`) now run on the server's dedicated
  loop.** As part of the loop-affinity fix, *all* transport methods —
  including those of caller-supplied transports — execute on the
  `MCPServer`'s background loop, never the caller's loop. Transports must
  create loop-affine resources inside `connect()` rather than pre-binding
  them to the caller's loop (a pattern that already failed under the old
  sync `as_tools()` facade, which ran on a throwaway loop). In exchange,
  custom transports get the same guarantee as the SDK transports: one
  consistent loop for the whole connect → call → close lifecycle.
- The `[test]` extra now includes the `mcp` SDK so the real-server integration
  tests run in CI; `test.yml` / `release.yml` action versions aligned with
  `docs.yml` (`checkout@v6`, `setup-python@v6`).
- Corrected the external-gateway documentation: `lazytools.connectors.gateway`
  passes each tool's JSON response through to the agent **unmodified** and does
  not sanitise results. Sanitisation is the remote gateway's responsibility
  (previously the docstring overstated this as "sanitized tool results").
- Rebased ported-from-LazyBridge version narrative in the MCP connector and
  docs onto lazytoolkit's own `0.1.x` line (removed "since 0.7.9",
  "pre-1.0.x", "audit H-E", "0.7-era" references); behaviour is unchanged.
- Freshened model identifiers in docs/code examples to `claude-opus-4-8`.
- CI now enforces a coverage floor (`--cov-fail-under=70`).

### Added
- **Code Support Agent connector** (`lazytools.connectors.code_support`) —
  delegate coding work to the Claude Code and Codex CLIs, each in **CLI mode**
  (the CLI is the agent) or **MCP mode** (the CLI exposes its tools and your
  agent orchestrates them), and let them collaborate. Stdlib only
  (`subprocess`, `json`, `shutil`) for the CLI-mode tools; MCP mode needs the
  `mcp` extra. See the
  [Code Support Agent guide](https://tools.lazybridge.com/code-support/).
  - **Claude Code** — `claude_code(task, *, mode, cwd, session_id, timeout)`
    (CLI mode) wraps `claude -p ... --output-format json` (`read` / `write` /
    `plan` modes, `--resume`); `claude_code_mcp(...)` (MCP mode) runs
    `claude mcp serve` and exposes Claude Code's own tools (View/Edit/LS/Bash/…).
  - **Codex** — `codex(task, *, mode, cwd, resume_last, timeout, skip_git_check)`
    (CLI mode) wraps `codex exec ...` (`read` = `-s read-only`, `write` =
    `-s workspace-write --full-auto`); `codex_mcp(...)` (MCP mode) runs
    `codex mcp-server` and exposes its `codex` / `codex-reply` tools
    (experimental, per OpenAI).
  - `build_cli_collaboration(*, name, description, claude_model, codex_model,
    synthesizer_model, executor_model, execute)` — the multi-agent pipeline
    (Claude Code analyses → Codex critiques → synthesizer plans → executor
    implements) packaged as a **single named `Agent`** that drops into
    `Agent(tools=[...])` like the function tools. `execute=False` yields a
    read-only analyse-and-plan pipeline.
  - `check_clis_available()` — `shutil.which` check for both CLIs at startup.
  - The CLI-mode tools use `subprocess.run` (sync) and are dispatched to a
    thread pool by `Tool.run`, so the event loop stays free. Set
    `tool_timeout=None` on `LLMEngine` so the engine never cancels a running
    subprocess (which would orphan it). The MCP-mode factories are deny-by-
    default (`allow=`/`deny=` required), built on `MCP.stdio`.
- `CHANGELOG.md` and `SECURITY.md`.
- Expanded test coverage for `skills.doc_skills` (BM25 scoring, heading-aware
  chunking, `query_skill` modes, `build_skill` options) and a DOCX
  paragraph+table reader test.

---

## [0.1.0] — 2026-05-28 — initial extraction

First standalone release of **`lazytoolkit`** (import name `lazytools`), the
reusable tool-providers / connector-clients / safety-wrappers package extracted
from LazyBridge. `lazytools` depends only on `lazybridge`; it never imports
`lazypulse`, and `lazybridge` never imports `lazytools`.

> **Publishing note.** The first `0.1.0` upload to PyPI is performed **manually**
> with `twine` (the project must exist before OIDC trusted publishing can run);
> every subsequent tagged release is published by the `release.yml` workflow via
> PyPI Trusted Publishing.

### Added
- **Gmail connector** (`lazytools.connectors.gmail`, extra `[gmail]`) —
  `GmailClient` (thin Gmail REST wrapper, lazy Google imports) and `GmailTools`
  tool provider. `gmail_create_draft` is always available; `gmail_send` is
  gated by the safety layer. Includes DKIM/SPF/DMARC `Authentication-Results`
  parsing with authserv-id pinning.
- **Telegram connector** (`lazytools.connectors.telegram`, extra `[telegram]`) —
  `TelegramClient` (minimal Bot API wrapper over `httpx`) and `TelegramTools`
  tool provider with a gated `telegram_send_message`.
- **MCP connector** (`lazytools.connectors.mcp`, extra `[mcp]`) — `MCP` /
  `MCPServer` tool providers over stdio and Streamable HTTP transports,
  deny-by-default tool filtering (`allow=` / `deny=` required), namespacing,
  and a TTL'd tool-discovery cache.
- **External tool gateway** (`lazytools.connectors.gateway`) — adapt remote
  JSON-HTTP tool catalogues (Pipedream/Composio/Arcade/internal) into
  LazyBridge tools, with a same-origin redirect handler that refuses
  host/scheme changes so the gateway `Authorization` header can't leak.
- **Document reader** (`lazytools.documents`, extra `[docs]`) — read
  txt/md/pdf/docx/html from a folder or file for LLM consumption, sandboxed to
  a `base_dir` with file-count and per-file size caps when exposed as a tool.
- **Doc skills** (`lazytools.skills`) — index documentation folders into a
  portable BM25 (Robertson IDF) skill bundle and query it
  (locate/extract/summarize/answer modes); symlinks are skipped at index time.
- **Safety layer** (`lazytools.safety`) — `Allowlist` (case-insensitive target
  allow-list) and `ConfirmationGate` (one-shot, scope-bound grants) for gating
  dangerous tools.
- **Testing helpers** (`lazytools.testing`) — in-memory fakes for the connector
  Protocols.

### Security
- `read_docs` exposes a sandboxed tool surface: `base_dir` is mandatory when
  building a tool, paths that escape it are refused, and oversized files are
  skipped rather than read.
- `ConfirmationGate` issues one-shot, target- and scope-bound grants so a
  concurrent task can never spend another task's approval (no sticky global
  approval).
- Gmail OAuth token files are persisted with owner-only (`0600`) permissions.
