# LazyTools Repository Audit

Date: 2026-06-10 · Commit audited: `946d40f` · Auditor: automated deep audit (Claude Code session)

## Scope & method

Full read of all 27 source modules (~3.6k LOC), test suite, CI workflows, packaging,
and docs. Findings below were **verified by execution** where marked — including an
integration test against a real MCP server (toy FastMCP over stdio), which the CI
suite does not currently run.

Baseline health:

- `pytest`: **187 passed, 3 skipped** (mcp/docx extras absent), 77% coverage (CI floor 70%).
- `ruff check src tests`: clean. `mypy src/lazytools`: clean.
- Installs and runs against sibling LazyBridge 0.9.1 (pin `>=0.7.9,<0.10` is satisfied).

---

## High-severity findings

### H1. MCP connector is broken with the real SDK in its primary documented usage pattern *(verified)*

The quick-start in `lazytools/connectors/mcp/__init__.py` (and the
`claude_code_mcp` / `codex_mcp` docstrings — "Drop it straight into
`Agent(tools=[...])`") leads to `as_tools()` being called from a sync context.
`_run_sync` (`transports.py:250`) then executes connect + discovery via
`asyncio.run(...)` on a **temporary event loop that is closed when discovery
returns**. The official `mcp` SDK's `ClientSession`/streams are loop-affine
(anyio task groups), so every subsequent tool call awaits a session bound to a
dead loop.

Reproduced with a real stdio server (`mcp` SDK 1.x, FastMCP toy server):

```
discovered: ['toy.add']                      # discovery works
CALL FAILED: ClosedResourceError             # every tool call fails
RuntimeError: Attempted to exit cancel scope in a different task ...
```

The fully-async path (`async with MCP.stdio(...)` + `await alist_tools()` +
tool calls on the same loop) **works correctly**. The in-process fake
transports used by the test suite have no loop affinity, which is why
`tests/test_mcp.py` passes while real usage breaks.

**Recommendation.** Either (a) give each transport a dedicated background
event-loop thread that owns the session, dispatching all operations via
`asyncio.run_coroutine_threadsafe` (makes the sync facade genuinely safe), or
(b) make `as_tools()` raise when the transport is not yet connected and
document that real-SDK servers must be connected via `async with` on the loop
that will run the agent. Also add an opt-in integration test with a toy
FastMCP server — the one used for this audit is ~10 lines and would have
caught this.

### H2. `skill_builder_tools()` hands the LLM unsandboxed read + recursive-delete *(verified)*

`skills/doc_skills.py:skill_builder_tools` wraps `build_skill` directly as an
LLM tool. All arguments are LLM-controlled and unsandboxed:

- `source_dirs` — arbitrary filesystem **read** (indexed content is returned to
  the model via `query_skill`).
- `output_root` + `skill_name` — arbitrary **write** location.
- `overwrite=True` (default) — `shutil.rmtree(skill_dir)` on whatever already
  exists at `output_root/<slug>`. Verified: a pre-existing, non-bundle
  directory with user data is silently deleted.

This is the exact threat `read_docs_tools` defends against — that function
*requires* `base_dir` and refuses to run without a sandbox, with a docstring
explaining why. The two tools in the same package have opposite postures.

**Recommendation.** Require a `base_dir`-style sandbox for both
`source_dirs` and `output_root` in `skill_builder_tools`; default
`overwrite=False` for the LLM-facing tool; and before any `rmtree`, verify the
target looks like a bundle (e.g. contains `manifest.json`) so the tool can
never delete arbitrary directories.

---

## Medium-severity findings

### M1. MCP `deny=` cannot skip a bad-schema tool, contradicting the error's own advice *(verified)*

`MCPServer.alist_tools` wraps **all** advertised tools before applying
allow/deny filters (`server.py:140-142`), but `_wrap_tool` raises `ValueError`
for non-object input schemas — and that error message explicitly recommends
``deny=[...]`` as the remediation. Verified: a denied tool with
`inputSchema: {"type": "array"}` still raises, so one malformed tool on a
server bricks the whole connector with no workaround except `allow=`-listing
around it. **Fix:** apply name-based allow/deny filtering to the raw specs
*before* wrapping.

### M2. `claude_code(mode="read")` is not read-only despite docs saying so

`_claude_code.py:27` puts `Bash` in the `"read"` mode's `--allowedTools`
(`Read,Bash,Grep,Glob`), and `docs/code-support/claude-code.md` describes this
as "analysis only, safe default". An unrestricted `Bash` tool can write,
delete, and exfiltrate. Contrast with the Codex connector, whose read mode
uses an *enforced* sandbox (`-s read-only`). The collaboration pipeline's
"claude_analyst (mode='read')" step inherits the same gap.
**Fix:** drop `Bash` from read mode (keep `Grep`/`Glob` for search), or pair it
with an enforced sandbox, or rewrite the docs to state plainly that read mode
can execute arbitrary shell commands.

### M3. Telegram bot token leaks into exception text *(verified)*

`TelegramClient` embeds the token in the request URL (Bot API requirement),
but `_call()` lets `httpx`'s `raise_for_status()` propagate — and httpx
includes the full URL in the error message. Any logged HTTP failure therefore
prints the bot token. **Fix:** catch `HTTPStatusError`/transport errors in
`_call()` and re-raise with the token redacted from the URL.

---

## Low-severity findings & nits

- **L1 — Docstring drift in `read_folder_docs`:** the docstring (twice) claims a
  nonexistent path is "reported as a plain string", but the code raises
  `FileNotFoundError` (`read_docs.py:198`). Verified. Make them agree.
- **L2 — `ConfirmationGate` is not thread-safe:** `consume()` does a
  read-modify-write on a plain dict. Safe on a single event loop, but
  `confirm_*` is designed to be called from a review-queue/UI thread while a
  worker consumes. A `threading.Lock` is cheap; at minimum document the
  constraint.
- **L3 — Gmail header injection is only stopped by the stdlib:** `_encode` puts
  raw `to`/`subject` into MIME headers. Current CPython raises
  `HeaderParseError` on embedded newlines (verified), so this is mitigated on
  supported runtimes, but explicit `\r\n` validation would be cheap
  defense-in-depth and give a friendlier `ActionBlocked`-style error.
- **L4 — Gateway accepts plain-HTTP base URLs with an API key:**
  `JsonHttpExternalToolClient` will send `Authorization: Bearer <key>` over
  `http://`. Consider refusing (or warning) for non-HTTPS, non-localhost URLs.
  (The same-origin/no-downgrade redirect handler is a genuinely good guard.)
- **L5 — `build_skill`/`_iter_docs` descends into dot-directories:** only dot
  *files* are skipped, so `.venv/**/*.py`, `.github/**/*.yml` etc. get indexed —
  noise and surprise reads. Skip any path with a dot-component.
- **L6 — `TelegramClient.from_token` leaks the `httpx.Client`:** no `close()`
  or context-manager support; long-running processes accumulate the connection
  pool until GC.
- **L7 — `alist_tools` returns the cached list object itself:** a caller that
  mutates the returned list corrupts the cache for everyone. Return a copy.
- **L8 — "Closure is terminal" not upheld for never-connected servers:**
  `aclose()` on an unconnected `MCPServer` is a no-op that does not set
  `_closed`, so the server remains usable after "closing" in that edge case.
- **L9 — CI nits:** the `mcp` extra is not part of the `test` extra, so the two
  real-SDK tests in `tests/test_mcp.py` are *always skipped in CI* (this is one
  reason H1 went unnoticed); `docs.yml` uses `checkout@v6`/`setup-python@v6`
  while `test.yml`/`release.yml` use v4/v5.

---

## What is in good shape

- **Safety layer** (`safety/`): one-shot, target-bound, scope-bound grants with
  no global mutable state; clear allowlist semantics (`None` = allow all, empty
  = deny all); typed `ActionBlocked` denials; contextvar scope propagation with
  the async-tool rationale documented. The "dry-run-first" pattern (ungated
  `gmail_create_draft` beside gated `gmail_send`) is consistently applied.
- **Gmail `Authentication-Results` parser**: authserv-id exact-match pinning,
  comment stripping before parsing, token anchoring against `x-dkim=pass`-style
  spoofs — carefully hardened and well-tested.
- **`read_docs_tools`**: mandatory sandbox, symlink refusal, per-file and
  per-scan caps; the LLM-facing posture H2 should be brought in line with.
- **Credential hygiene**: OAuth token file `chmod 0600` (covering legacy loose
  files too); MCP tool exposure is deny-by-default with loud, actionable errors.
- **Engineering hygiene**: protocol seams + in-memory fakes everywhere (the
  suite runs in ~1.5 s with zero network); a boundary test enforcing the
  `lazytools → lazybridge`-only dependency rule; ruff + mypy clean; coverage
  gate in CI; OIDC trusted publishing with tag/version verification; a
  disciplined changelog.

## Priority of fixes

1. H1 (MCP loop affinity) — the connector's flagship pattern fails in real use.
2. H2 (`skill_builder_tools` sandbox/rmtree) — destructive capability exposed to the LLM.
3. M1–M3 — small, contained patches.
4. L-items opportunistically; L9's `mcp`-in-test-extra change is the cheapest
   way to keep H1 fixed once it is fixed.
