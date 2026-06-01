# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

### Security
- **Gmail OAuth token written world-readable.** `GmailClient.from_credentials`
  persisted the cached token with the process umask, so the long-lived OAuth
  refresh token could land at mode `0644` and be read by any local user. The
  token file is now `chmod`-ed to `0600` after every write (which also tightens
  a pre-existing token file).

### Fixed
- **DOCX table reader dropped its row filter.** `documents.read_docs` had a
  dead `or True` guard that made non-row table children (`<w:tblPr>`,
  `<w:tblGrid>`) be treated as rows; the reader now iterates only genuine
  `<w:tr>` rows.
- **MCP tool-cache race.** `MCPServer.alist_tools` read and refilled the
  discovered-tools cache outside any lock, so concurrent callers could issue
  duplicate `list_tools()` round-trips and clobber each other's cache. The
  check + refill is now guarded by the server's lock.

### Changed
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
