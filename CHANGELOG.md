# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

### Changed
- **The reviewers may now read the web too.** `claude_code_review` runs with
  `web=True` (the engine's own WebSearch/WebFetch) instead of the previous
  offline-by-design default, and both reviewer prompts (Codex's and
  Claude's) now permit web lookups — a review can legitimately need to check
  a CVE, whether an API is really deprecated, or a library's current
  documented behavior, and the offline default was costing real findings.
  Both prompts require web-sourced claims to be marked as such and kept out
  of the repo-verified findings list. `claude_reviewer(web=False)` restores
  the old behavior. Codex's reviewers were never gated in code here: its
  native web tool (`web__run`) is an account-level capability governed by
  `~/.codex/config.toml`, independent of role.

### Added
- The two `*_ask` consultants now carry the server's own **read-only
  LazyTools toolset** (`web`, `datahub`, `statistical`, `fin`,
  `econ_calendar` providers) as dynamic tools, built from the same factories
  and configuration the MCP server serves — replacing the web-only toolset.
  Handed over the engine channels (Codex `dynamicTools` / Claude in-process
  MCP), so no participant-side MCP registration or approval policy is
  involved: a Codex thread calling this server through its own `config.toml`
  MCP entry is rejected by `approval_policy="never"` before execution
  (observed live 2026-08-18), which is the path this bypasses.

### Fixed
- The consultant toolset built every provider write-enabled, which made
  `DataHubTools` decorate its READ tools' descriptions with a recovery path
  through `datahub_register_listing` / `datahub_ensure_*` — tools the
  consultant filter then strips, leaving the agents advertised tools they
  cannot call. Only `fin` (the one provider whose compute tools need it) is
  built write-enabled now. Found by Codex reviewing PR #122.
- The Claude review/consult agents ran with `CodingAgentConfig.reviewer()`,
  whose `preapprove_application_tools=False` plus the absent approval gate
  made the SDK fail-close **every** application tool — the read-only
  `git_diff`/`git_status` the reviewer is documented to have, and the entire
  toolset handed to `claude_ask`. They now run the default (pre-approving)
  profile; confinement is unchanged (`file_roots` is enforced by a
  `PreToolUse` hook regardless, and the engine grants no shell).
- The two `*_ask` consultants grew consulting-grade options the reviewers
  deliberately don't have. `codex_ask` and `claude_ask` now take per-call
  `model` and `effort` / `thinking` overrides (the env-var settings remain the
  defaults), and both consultant agents receive the LazyCrawler web tools
  (`web_search`, `web_crawl`, `get_page`, …) — built against the same news db
  and smart-mode model as the `web` provider, degrading to no web tools when
  `lazycrawler` is missing. `claude_ask` additionally runs with the engine's
  own `web=True` (WebSearch/WebFetch). The reviewers (`codex_code_review`,
  `claude_code_review`, `codex_review_changes`) are unchanged and stay
  offline: reading the web is not what a code review is for.
  `codex_consultant` / `claude_consultant` expose the same via `tools=` /
  `web=` factory parameters.

## [0.6.0] — 2026-08-18

### Added
- `lazytools.connectors.code_support.codex_reviewer` and the MCP provider
  `code_review`: Codex as the **engine of a LazyBridge agent**
  (`lazybridge.engines.codex.CodexEngine` over `codex app-server`, reusing the
  CLI's own login — no API key), pinned to a reviewer system prompt and served
  as a single `codex_code_review(task, repo_path, diff_ref, paths)` tool. An
  MCP host can now hand a code review of a local repository to Codex, which
  reads the files and runs `git` itself. Read-only by construction
  (`CodexPolicy`'s `sandbox="read-only"` / `approval_policy="never"`, so it
  reports but never patches and no approval prompt can block the
  non-interactive transport) and confined: `repo_path` must resolve inside
  `code_root` / `LAZYTOOLS_CODE_ROOT` (default: the server's cwd). Opt-in like
  the other agent providers (`--allow-unsafe`) because each call spends a real
  model turn, and skipped entirely when the `codex` CLI can't be located.
  Model / effort / per-review timeout via `LAZYTOOLS_CODE_REVIEW_MODEL`,
  `LAZYTOOLS_CODE_REVIEW_EFFORT`, `LAZYTOOLS_CODE_REVIEW_TIMEOUT` (900 s).
  See `docs/code-support/codex.md` and `docs/mcp-server.md`.
  Every entry of `paths` is confined to `repo_path` too — read-only stops
  writes, not reads elsewhere on the host — and the reviewer prompt forbids
  reading outside the working directory (an instruction, not a sandbox: the
  free-text `task` cannot be checked structurally).
- Both tools run on a **durable Codex thread** and report `thread_id=<repo>#<id>`
  in their reply header; passing it back continues that conversation instead of
  starting cold, so a follow-up does not re-read the repository (measured live:
  141 s for the review, 10.6 s for the follow-up on its thread). Requires
  LazyBridge's `CodexEngine(persist_thread=...)`. The handle names its
  repository and is refused against a different one — path confinement says
  nothing about a thread, so a mis-pasted id would otherwise splice another
  repository's transcript into the answer.
- **The same surface on Claude Code**: `claude_reviewer` / `claude_consultant`
  (`lazytools.connectors.code_support`) and the MCP provider `claude_review`,
  serving `claude_code_review` and `claude_ask` — identical arguments and the
  identical durable-handle protocol (`session_id=<repo>#<id>`) as their Codex
  twins, on `ClaudeCodeEngine`, so one diff can be given to both and the
  answers compared. Verified live: a session resumed from a *different process*
  still knew what it had read (21 s first call, 9 s follow-up). Two differences
  are forced by the runtime — the engine grants `Read`/`Glob`/`Grep` and **no
  shell**, so read-only `git_diff`/`git_status` are supplied as ordinary tools;
  and the Agent SDK has no `review/start`, so there is no counterpart to
  `codex_review_changes`. Registered as its own provider so a missing CLI on
  one side cannot take the other's tools down.
- `lazytools.connectors.code_support.codex_native_reviewer` — the third tool,
  `codex_review_changes(repo_path, scope, ref)`: Codex' **own** review harness
  through the App Server's `review/start`, with a typed target
  (`uncommitted` / `branch` + ref / `commit` + ref) instead of a prompt. No
  instructions are sent — the target is the instruction — so it cannot be
  steered; that is the trade for its findings being Codex' own, severity-tagged
  with file:line. Verified against codex-cli 0.148.0 on a repo with a planted
  defect: all three targets find and rank it. Runs inline on a durable thread,
  so the `thread_id` it returns can be handed to `codex_ask` to interrogate the
  findings without re-running the review. Detached delivery is deliberately not
  wired: it completes on a different thread and raises an approval request the
  parent never sees.
- `lazytools.connectors.code_support.codex_consultant` — the second tool,
  `codex_ask(question, repo_path, thread_id)`: same engine and confinement,
  but instructed as a design partner rather than a reviewer (separate verified
  from inferred; "I don't know, here is the experiment" beats a guess). The
  reviewer prompt answers a design question with a findings list, which is why
  this is a separate surface rather than a phrasing convention.
- `lazytools.skills.council`: `WizengAImot`, a spontaneous multi-agent
  council built on LazyBridge `AgentPool`. Members research and open in
  parallel, then debate freely — routing to whoever should speak next,
  revising positions, and casting structured votes — until the moderator
  confirms quorum and closes. Includes `standard_council()` (four-member,
  multi-provider) and `deepseek_claude_news_council()` (DeepSeek + Claude
  Code subscription preset sharing a LazyCrawler news database), plus
  optional `knowledge()` grounding (static text or BM25 skill mode). A
  `reasoning` parameter controls extended-thinking effort throughout:
  a bool on `WizengAImot`'s default moderator/synthesiser and on
  `standard_council()`'s members; a graduated `"low"`–`"max"` level on
  `deepseek_claude_news_council()`'s debater/moderator/synthesiser (its
  two fast analysts stay reasoning-disabled by design). Debater and
  moderator memory now runs `Memory(strategy="summary", ...)`, compressing
  the free-form debate as it runs so a long discussion can't push the
  final synthesis call past the model's context window; the summarizer
  defaults to a cheap, non-reasoning DeepSeek agent and is overridable
  via `memory_summarizer=`. `route()` now records every exchange once to
  a shared debate history (a `Memory` in LazyBridge's "shared use" mode,
  attached to every participant's `sources=[...]`), so members can see
  what others said instead of having to recap it themselves — reducing
  duplicated content across private memories — and the final transcript
  reflects the debate's real chronological order instead of each agent's
  own turn order. Fixed a gap found via live-testing: closing the debate
  raises `ConcludeSignal` (a `BaseException`, by design, so any nested
  agent can end the whole discussion instantly), which was unwinding
  straight past the transcript-recording code at every nesting level
  between the close and the top, leaving the recorded transcript with
  only its first entry. `route()` now records the propagated closing
  message before re-raising the signal, instead of losing it.

## [0.5.0] — 2026-08-02

### Added
- `lazytools.registry`: core, always-installed module for the ecosystem's
  DB registry + artifact catalog. `KNOWN_DBS`/`resolve_db()`/`status()` map
  a logical DB name to the env var that names it, per repo, declared in
  code (PR-reviewable, no shared config file). `register_artifact()`/
  `search_artifacts()`/`get_artifact()` give every repo an optional,
  stdlib-sqlite3-only per-repo artifact catalog (`*_ARTIFACTS_DB`);
  `search_everywhere()`/`get_everywhere()` fan out across all of them.
  `RegistryTools` exposes it as a LazyBridge tool provider. See
  `docs/registry.md`.
- `KNOWN_DBS` entries: `lazystats_depot` (`LAZYSTATS_RESULT_DEPOT_DB`) and
  `lazyportfolio_artifacts` (`LAZYPORTFOLIO_ARTIFACTS_DB`).
- `KNOWN_DBS`'s `regime_tools_db` entry (`LAZYTOOLS_REGIME_DB`) — LazyStats'
  own `RegimeDB` depot (fitted HMM params/figures/state sequences) backing
  the `regime_*` MCP tools. A separate store from `lazystats_depot`, which
  holds market-data-hub's persisted regime *run results*.
- `mcp_server`: the `registry` provider, exposing `RegistryTools` over MCP
  — read-only by default (`registry_status`/`artifact_search`/
  `artifact_get`); `artifact_register` only under `allow_write=True`/
  `--allow-unsafe`. Already in the default provider set (core, no extra).
- `setup_first_run.ps1`: prompts for `MARKET_DATA_DB` and all four
  artifact-registry DBs (`MARKET_DATA_ARTIFACTS_DB`, `PULSE_ARTIFACTS_DB`,
  `CRAWLER_ARTIFACTS_DB`, `LAZYPORTFOLIO_ARTIFACTS_DB`), previously
  invisible to this installer.

### Fixed
- `KNOWN_DBS`'s `crawler_raw` entry pointed at `CRAWLER_DB`, which
  LazyCrawler itself never reads (a LazyPulse-local convention for a
  different file) — corrected to `LAZYCRAWLER_NEWS_DB`.
- `registry.artifacts`: `search_artifacts`/`get_artifact` shared a
  connection helper with `register_artifact` that ran `CREATE TABLE`/
  `CREATE INDEX` schema DDL on every call — a read against an
  unconfigured/empty catalog silently created the DB file, and could raise
  on a read-only-mounted filesystem. Reads now use a genuinely read-only
  connection that never touches disk when the file is absent, correctly
  treats an existing-but-uninitialized file as an empty catalog, and
  percent-encodes the path (`?`/`#` are valid Linux filename characters
  but structurally significant in a `file:` URI).
- `docs/registry.md`'s quickstart example still showed `RegistryTools()`
  promising `artifact_register`, despite the new `allow_write` gate.

## [0.4.0] — 2026-07-30

### Added — two specialist agents, exposed as single MCP tools
- `optimizer_agent` (`portfolio-optimizer-specialist`) and `report_agent`
  (`report-specialist`): LLM-driven experts wrapping the `fin`/`report` tool
  surfaces, each exposed over MCP as **one tool** taking a single `task:
  string` argument — `lazybridge.Agent`'s existing `_is_lazy_agent` → `Tool`
  mechanism (`mcp_server/server.py::expand_tools`) already did this
  automatically; no server changes needed to wire it up.
- `optimizer_specialist` (`connectors/fin/optimizer_agent.py`, new — no
  `lazyfin` dependency, unlike the LazyFin-domain `connectors/fin/agents.py`)
  drives `DataHubTools` + `portfolio_optimizer_*` + `portfolio_tree_*`: picks
  flat vs. tree, validates before persisting, never overrides the tree's own
  mode derivation, only states figures that came from a tool result.
- `report_specialist` (`report/agents.py`, new) drives `ReportTools`/
  `ReportFiles` only — deliberately **not** wired to `datahub_*`/
  `statistical_*`/`regime_*`, so it structures/renders what a caller supplies
  rather than gathering data itself.
- **Both are opt-in only**, unlike every other provider here: the factory
  raises unless `allow_write=True` *and* the configured model's API key is
  present (`LAZYTOOLS_OPTIMIZER_AGENT_MODEL`/`LAZYTOOLS_REPORT_AGENT_MODEL`,
  default `deepseek-v4-flash`, needs `DEEPSEEK_API_KEY`) — so the tool is
  entirely absent from the surface, not present-but-limited, matching the
  `telegram`/`gmail`/`outlook` precedent. A real LLM call is a different
  risk profile than this server's other deterministic tools (cost,
  non-determinism, its own multi-step tool sequencing a per-name guard can't
  inspect from outside), hence the stricter default. Added a `-specialist`
  `UNSAFE_TOOL_PATTERNS` entry as a second-layer guard regardless.

### Added — `portfolio_tree_*` MCP tools (hierarchical tree over MCP, interoperable with Tree Studio)
- `PortfolioOptimizationTools` (`portfolio_optimizer_*`) only ever wrapped a
  single flat node — its own docstring said the full node-tree (parent/child
  hierarchies, per-node proxies) was "only exposed through Tree Studio /
  `V2Model.from_config` directly, not through this LLM-facing surface." Added
  a sibling provider, `PortfolioTreeTools` (`lazytools/connectors/fin/tree_tools.py`),
  closing that gap: `portfolio_tree_validate`/`_list`/`_load` (always on) and,
  in write mode, `_save`/`_delete`/`_estimate`/`_backtest`.
- **Real interop with Tree Studio, not an export/import translation.** A tree
  saved via `portfolio_tree_save` is a LazyPortfolio V2 config JSON file
  written to the exact directory and through the exact validate-before-write
  logic Tree Studio's own save endpoint uses (`lazyportfolio.v2.store`, new
  this release, extracted out of `tree_studio.py` so both processes call
  identical code) — it appears in Tree Studio's saved-model list immediately,
  and a tree built/edited in the GUI loads via `portfolio_tree_load`/`_list`.
  Shared via the `LAZYPORTFOLIO_TREE_MODELS_DIR` env var (both sides must
  point at the same directory for interop; each `list`/`save` response
  reports the resolved directory so a mismatch is visible, not silent).
- `portfolio_tree_estimate`/`_backtest` derive `flat`/`forward`/
  `forward_backward` from the tree's own `backtest.forward_enabled`/
  `hierarchy_mode` via `lazyportfolio.v2.mode.mode_from_config` (also new,
  promoted out of Tree Studio's `_v2_mode` so the two processes can't
  silently disagree on a saved tree's mode) — never chosen ad hoc. Both
  accept either an inline `config` or a saved `name`; optional
  `estimation_frequency`/`train_size`/`rebalance_frequency`/
  `transaction_cost_bps` override the tree's own values for that call only,
  without touching the saved file. Neither tool's response ever includes
  `synthetic_returns` or backtest curves — only weights, aggregate metrics,
  and provenance, matching this connector's existing no-raw-observations rule.
- `PortfolioTreeTools` gates its own write tools at construction
  (`allow_write`), unlike its sibling `PortfolioOptimizationTools` (which
  relies solely on the server's read-only name guard) — a deliberate,
  acknowledged inconsistency within the `fin` provider, not an oversight.
- Fixed a read-only guard gap: `portfolio_tree_estimate`/`_backtest` matched
  none of the existing `UNSAFE_TOOL_PATTERNS` (`optimizer_run`/
  `optimizer_backtest` require that literal substring); added `tree_estimate`/
  `tree_backtest` patterns.
- Fixed a stale contract test: `test_fin_optimizer_contract` asserted a
  7-tool set and an `OptimizationStore`-based constructor from
  `lazyfin.optimization`, both removed by the earlier V1→V2 port — silently
  asserting nothing every run since `lazyfin.optimization` doesn't exist and
  `pytest.importorskip` always skipped it. Replaced with
  `test_fin_provider_contract`, built through `default_providers(["fin"], ...)`
  against the real, live tool set.

### Added — `report` MCP provider (mount LazyReport on the server)
- LazyReport (`lazytools.report`) existed as a fully-built, tested module
  (`Memo`/`Section`/`TableBlock`/`FigureBlock`, `render_markdown`/`render_html`,
  `ArtifactResolvers`/`ecosystem_resolvers`, `ReportTools`, `ReportFiles`) but
  was never registered in `PROVIDER_FACTORIES` — it was unreachable over MCP.
  Added the `report` provider: always emits `render_memo` / `render_memo_html`;
  in write mode (`--allow-unsafe`) also emits `save_memo_html` /
  `save_memo_markdown` / `save_report` against a sandboxed `reports/`
  directory under the data home. Figures resolve from the regime depot
  (`regimes:`, same path `RegimeTools` writes to), on-demand market-data-hub
  charts (`chart:`), or a local file (`file:`) — no extra credentials needed,
  so `report` is always constructed like `datahub`/`statistical`.
- `default_providers()` now flattens a factory that returns a `list`/`tuple`
  of providers, not just a single one — `report` needs two
  (`ReportTools` + `ReportFiles`, the same pairing `lazytools.skills` already
  wires for agents) so `save_report` is reachable without colliding with
  `ReportTools`' own `save_memo_*` tool names.
- Fixed a read-only guard gap: `UNSAFE_TOOL_PATTERNS` matched `_save` (a
  writer *ending* in `save`, e.g. `regime_params_save`) but not a tool
  *starting* with `save_` — the shape of the new `save_memo_*` / `save_report`
  tools. Added a `save_` pattern so these stay off the read-only surface.

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

### Added — Black-Litterman views wired into the tree optimizer
- `portfolio_tree_estimate` accepts `constraints.views`/`view_tau`/
  `view_covariance_policy` per node — an LLM can now express macro views
  (e.g. "SPY: +8% expected return, 60% confidence") and have them blended
  into the tree's expected-return estimates via Idzorek confidence-scaled
  Black-Litterman, not just consume a fixed benchmark allocation.
- Fixed a LazyBridge MCP schema bug that rejected real tree configs: a
  `dict[K, Any]`-typed nested value (e.g. `constraints.views`) was forced
  to `{"type": "string"}` by the `Any` fallback instead of `{"type":
  "object"}`, silently corrupting any config containing one.
- Fixed the `web` (LazyCrawler) provider defaulting to an always-empty
  `:memory:` cache with no indication when no explicit `db=`/env var was
  set — now resolves through `LAZYCRAWLER_NEWS_DB` and warns instead of
  staying silent.

### Changed — centralized DB path resolution, `--config` for the MCP server
- The regime depot path (used by both the `regimes` provider and the
  `report` provider's `regimes:` figure resolver) and the news cache path
  now resolve through one canonical chain each (`lazystats.regimes.
  resolve_depot_path()`, `lazycrawler.config.resolve_news_db_path()`)
  instead of each caller computing its own default independently — the
  exact class of bug that let one caller's explicit path get silently
  overridden by another's default elsewhere in the same process.
- `lazytools-mcp --config <path.json>` (or `LAZYTOOLS_MCP_CONFIG`)
  populates every provider factory's `data_source` dict from one file,
  instead of setting each env var separately.
- **Fixed after external audit**: `stats_agents` constructed real
  `lazybridge.Agent` instances unconditionally, with no `allow_write`/
  credential gate — unlike `optimizer_agent`/`report_agent`, which already
  required both. `AnalystConfig.hub_db` was only threaded into the report
  specialist's resolver; every other tool builder (`DataHubTools`,
  `StatisticalAnalysisTools`, `RegimeTools.market_data_path`) silently fell
  back to market-data-hub's own default, risking a single run reading two
  different databases. `RegimeTools`' write/read tools only re-pointed the
  shared regime depot at construction time, not before each call — building
  a second `RegimeTools` instance for a different depot elsewhere in the
  same process could silently redirect an earlier instance's writes; every
  tool call now re-asserts its own depot immediately before delegating.

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
