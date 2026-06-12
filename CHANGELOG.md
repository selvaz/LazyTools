# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

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
