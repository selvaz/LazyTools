# lazytools-orchestration-mcp: parity inventory

Status: inventory (step 1 of the project), 2026-11-26. Source: `docs/CAPABILITIES.md` at main `8cd88c8`, `src/lazytools/mcp_server/providers.py`, `src/lazytools/code_bridge/_mcp.py`, `src/lazytools/connectors/projects/task_tools.py`.

## Goal (owner, 26/11)

A dedicated MCP server, `lazytools-orchestration-mcp`, separate from the data server `lazytools-mcp`, through which a Claude Code or Codex agent (a CEO agent) can do every orchestration action the lead does today from the CLI: run and follow coding jobs, decide approval tickets, drive a project graph, watch PRs to merge-ready, review, route by quota, read lessons and capabilities. Same permissions and gates as the CLI. Must work with a single provider installed.

## How the existing MCP tools work (pattern to keep)

`CodeBridgeTools` (provider `code_bridge`) runs the `lazytools-code-bridge` CLI in a child process and returns its JSON, so validation, redaction, placement, approvals and landing are identical to the CLI. It adds two MCP-only guards: `cwd` confined to the code root, and no `@file` task text. New tools follow the same pattern: thin wrappers over the CLI (or over the service the CLI uses), never a second implementation.

## Parity table

Legend: **have** = an MCP tool exists; **gap** = CLI only; **lib** = library only, no CLI or MCP. Write = needs `--allow-unsafe`.

| Lead capability | CLI today | MCP today | Gap / proposed tool | Write |
|---|---|---|---|---|
| Start a local or cloud job | `run [--detach] [--where cloud] [--tier T]` | `code_bridge_submit` | have | yes |
| Job status / result | `status`, `result` | `code_bridge_status`, `code_bridge_result` | have | no |
| List jobs | `jobs [--all] --json` | none | gap: `bridge_jobs` | no |
| Event stream (ticket channel) | `wait-any --all-active --loop --follow` | none | gap: `bridge_events(since, timeout)`: one bounded call returning new events (done, failed, approval, landed, interrupted, ready) since a cursor; MCP has no streaming, so the caller polls with a cursor | no |
| Pending approval tickets (job tickets) | `pending --json` | none (`approvals_pending` is the projects task-approval queue, a different queue) | gap: `bridge_pending` | no |
| Approve / reject a job ticket | `approve`, `reject --reason` | none | gap: `bridge_approve`, `bridge_reject`; rule: the approving principal may not approve a ticket raised by a job it launched itself, unless the owner's standing policy allows that ticket class (no self-approval) | yes |
| Cancel a job | `cancel JOB --reason` | none | gap: `bridge_cancel` | yes |
| Apply / land a cloud result | `apply JOB [--land]` | none (landing happens inside `wait`/`cloud-sync`) | gap: `bridge_apply(job, land)` | yes |
| Reconcile cloud jobs | `cloud-sync` | none | gap: `bridge_cloud_sync` | yes |
| Stop cloud agents | `cloud-stop` | none | gap: `bridge_cloud_stop` | yes |
| Explain a quota pick | `route --tier T --json` | `projects_quota` (raw readings only) | gap: `bridge_route` | no |
| Dispatch a project graph | `dispatch --project ID [--once] [--dry-run] --json` | none | gap: `dispatch_run(project, once=True)` for one pass; long-running dispatch stays a supervised service (see below), the tool reads its state | yes |
| Watch PRs to merge-ready | `watch owner/repo#N [--no-ci] [--request-review]` | none | gap: `pr_watch_poll(pulls, no_ci)`: one poll returning events; state kept in the bridge Store like the CLI | no (yes with `request_review`) |
| PR navigator loop | `navigate --repo --pr --reviewer-cmd` | none | gap: `pr_navigate` | yes |
| Independent review | `run --review-of JOB`, MCP `codex_code_review`, `claude_code_review` | have (review tools) | single-provider fix needed (below) | no |
| Publish reviewed commit | `publish --task --repo --branch` | none | gap: `bridge_publish` | yes |
| Verify receipt | `verify-receipt` | `receipts_get`, `receipts_verify` | have | no |
| Autonomy report | `autonomy --week` | none | gap: `bridge_autonomy` | no |
| Capabilities catalog | `capabilities [QUERY]` | none | gap: `capabilities_search` | no |
| Lessons (knowledge) | `dispatch --knowledge-db` (write on failure) | none | gap: `lessons_search(query)`, `lessons_record(...)` over LazyBridge `DurableKnowledgeBase` | record = yes |
| Named sessions | `run --session` | `code_sessions_*` | have | mixed |
| Repo map | library | `codemap_*` | have | no |
| Projects: plan, contracts, Gantt, board, brake | `lazytools-projects` | `projects_*`, `tasks_*`, `approvals_*` | have | mixed |
| Models audit | `models --json` | none | gap: `bridge_models` (no probe) | no |
| Synchronous write tools | n/a | `codex_write`, `claude_code_write` | keep out of the orchestration server (denied here; the bridge replaces them) | yes |

Count: 7 capabilities already have MCP tools; 17 gaps; 1 deliberate exclusion.

## Server split

- `lazytools-orchestration-mcp` (new entry point) mounts: `code_bridge` (submit/status/result plus the new bridge tools), `code_review`, `claude_review`, `code_sessions`, `codemap`, `projects` (task queue, approvals, receipts), plus new providers `fleet` (pending/approve/reject/cancel/events/jobs/route/autonomy/models), `pr` (watch poll, navigate, publish), `dispatch`, `knowledge` (capabilities, lessons).
- `lazytools-mcp` keeps the data providers. The coding providers move out of it; for one release they stay mounted there too, with a deprecation note, so existing clients keep working (single-user, so one release is enough).
- Both servers share the provider registry; the split is two lists of provider ids and two console scripts, not two codebases.

## Single-provider requirement

- Today `run --review-of JOB` demands the opposite engine and stops with "human review required" when it is not available. LazyBridge already has `select_reviewer` with mode `opposite-or-fresh` (`same-provider-fresh`, independence recorded). Fix: the bridge uses that selection when the opposite engine is not installed (not merely busy), and records the independence level in the job and the receipt.
- Every tool must work when only Claude Code or only Codex is installed: routing excludes the missing engine; review falls back to a fresh same-provider reviewer; nothing imports an engine package at server start.

## Dispatcher durability

`dispatch` is long-running; an MCP call is bounded. The server exposes one-pass `dispatch_run(once=True)` and read tools; the continuous dispatcher runs as a supervised service (watchdog, restart on exit) outside the MCP process. Tickets from it reach the agent through `bridge_events`.

## Autonomy modes (owner, 26/11)

The CEO agent runs in one of two modes, chosen by the owner, never by the agent:

| Mode | Who decides job tickets | Owner queue |
|---|---|---|
| `supervised` (default) | CEO decides the routine classes the owner delegated (in-checkout git add/commit, push to the job branch, gh pr create); everything else waits for the owner | yes |
| `total` | CEO decides every ticket itself, with its reason recorded in the ticket and in the lessons | only for the hard floor below |

- The mode is a setting stored outside the agent's writable area and changed only through an owner-credentialed command (`approver-credential` owner role). No MCP tool can switch it: the system may never widen its own authority (no-widen rule).
- Every self-decision in `total` mode is logged with ticket, command, reason and outcome, and counted in the `autonomy` report, so the owner can audit and switch back.
- The delegated classes (supervised) and the floor (total) are not hard-coded: they depend on context and are chosen at setup (owner, 26/11). Setup writes an autonomy policy (ticket classes mapped to `ceo` or `owner`, per mode) that the owner owns; the installer offers presets (for example "solo developer, no production" with an empty floor, and "machine with production jobs" with production, payments, external posts and data deletion on the owner). The policy follows the same rule as the mode: owner-credentialed change only.

## Decisions (owner, 26/11)

1. Self-approval and floor: per setup, through the autonomy policy above.
2. Deprecation window for coding tools in `lazytools-mcp`: one release (default, not contested).
3. Event delivery: both. MCP notifications where the client supports them, plus periodic cursor polling (`bridge_events(since)`) that catches anything a notification missed. Tickets are idempotent by id, so a duplicate from the two channels is harmless.

## Next steps

1. SPEC with numbered requirements and acceptance examples, validation matrix, task DAG (project-start method).
2. Tasks in dependency order: server entry point + split; `fleet` read tools; ticket decide with no-self-approval; `bridge_events` cursor; `pr` tools; `dispatch` and `knowledge`; single-provider review; installer profile; clean-machine trial.
