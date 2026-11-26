# lazytools-orchestration-mcp: SPEC

Status: approved direction (owner, 26/11); inventory and decisions in [INVENTORY.md](INVENTORY.md).

## Goal

One MCP server through which a Claude Code or Codex agent (the CEO) performs every orchestration action the lead performs from the CLI, with the same gates, on a machine with one or two coding engines installed.

## Invariants

- Tools are thin wrappers over the CLI or the service the CLI uses; no second implementation of any gate.
- No tool can widen authority: the autonomy mode and policy change only with the owner credential.
- The data server `lazytools-mcp` keeps working for its current clients for one release.
- Both-engines-installed behaviour of routing and review is unchanged.

## Requirements

**R1 Server**
- R1.1 A console script `lazytools-orchestration-mcp` starts an MCP server mounting `code_bridge`, `code_review`, `claude_review`, `code_sessions`, `codemap`, `projects`, `fleet`, `pr`, `dispatch`, `knowledge`.
- R1.2 `lazytools-mcp` keeps the coding providers for one release and logs a deprecation note when they are mounted.
- R1.3 The server starts with only one engine installed and imports no engine package at start.

**R2 Fleet read** (PR #408): `bridge_jobs`, `bridge_pending`, `bridge_route`, `bridge_autonomy`, `bridge_models`, `capabilities_search`.

**R3 Tickets and job control**
- R3.1 `bridge_approve(ticket, reason)`, `bridge_reject(ticket, reason)`, `bridge_cancel(job, reason)`.
- R3.2 When the deciding principal launched the job, the decision is allowed only if the autonomy policy maps the ticket class to `ceo` in the current mode; otherwise it is refused with "owner decision required".
- R3.3 Every decision records actor, mode, policy class and reason in the ticket.

**R4 Events**
- R4.1 `bridge_events(since_cursor, timeout_s<=60)` returns new events (done, failed, approval, landed, interrupted, ready, conflict, findings) after the cursor and a new cursor; repeated calls never lose an event created between calls.
- R4.2 Where the client supports MCP notifications, the server also sends each new ticket as a notification; ticket ids make duplicates harmless.

**R5 PR tools**: `pr_watch_poll(pulls, no_ci, request_review)`, `pr_navigate(...)`, `bridge_publish(...)`, `bridge_apply(job, land)`.

**R6 Dispatch**: `dispatch_run(project, once=True, dry_run)` and `dispatch_status(project)`; the continuous dispatcher runs as a supervised service (R9).

**R7 Knowledge**: `lessons_search(query, k)`, `lessons_record(title, body, tags)` over LazyBridge `DurableKnowledgeBase`.

**R8 Single provider**: when the opposite engine is not installed, reviews use `same-provider-fresh` (fresh session) and record the independence level (job, route JSON, receipt). Installed-but-ineligible still yields `human_review_required`.

**R9 Autonomy modes and policy**
- R9.1 Modes `supervised` (default) and `total`; policy maps ticket classes to `ceo` or `owner` per mode; stored outside agents' writable roots.
- R9.2 Change only through an owner-credentialed CLI command; no MCP tool writes it.
- R9.3 `autonomy` report counts self-decisions per class.

**R10 Install**: an orchestration-only installer profile (Claude Code only, Codex only, both) with autonomy presets; clean-machine trial by the owner.

## Acceptance examples

- Given only Codex installed, when a review of a Codex job is requested, then Codex reviews in a fresh session and the result says `independence=same-provider-fresh`.
- Given mode `supervised` and a ticket of class `git_push_job_branch` from a CEO-launched job, when the CEO approves it, then it is approved with actor=ceo, class recorded. For class `production_task`, the approval is refused with "owner decision required".
- Given a ticket created while no `bridge_events` call is running, when the next call passes the previous cursor, then the ticket is in the result.
- Given a private repo without CI, when `pr_watch_poll(no_ci=True)` sees a clean review and mergeable PR, then it returns `ready checks=none`.

## Validation matrix

| Req | Check |
|---|---|
| R1 | `tests/test_orchestration_mcp_server.py` (provider list, start with one engine faked); `lazytools-orchestration-mcp --list-tools` smoke |
| R2 | `tests/test_code_bridge_mcp_fleet.py` |
| R3, R9 | `tests/test_code_bridge_mcp_tickets.py`, `tests/test_autonomy_policy.py` |
| R4 | `tests/test_code_bridge_mcp_events.py` (event created between calls) |
| R5 | `tests/test_code_bridge_mcp_pr.py` |
| R6 | `tests/test_code_bridge_mcp_dispatch.py` |
| R7 | `tests/test_code_bridge_mcp_knowledge.py` |
| R8 | routing and cloud review tests (cloud job 097fddd2) |
| R10 | installer tests in lazy-installer; owner trial on a clean machine (judgement) |
| All | capabilities guard tests; live E2E: a CEO agent session drives one project end to end through the server only, traced |

## Task graph

| Task | Covers | Depends on | Where |
|---|---|---|---|
| T1 fleet read tools | R2 | — | cloud (PR #408) |
| T3 single-provider review | R8 | — | cloud (job 097fddd2) |
| T2 server entry point and split | R1 | T1 | cloud |
| T9 autonomy policy store + owner CLI | R9 | — | local (credentials) |
| T4 tickets and cancel tools | R3 | T2, T9 | cloud |
| T5 events tool + notifications | R4 | T2 | cloud |
| T6 PR tools | R5 | T2 | cloud |
| T7 dispatch tools + supervised service | R6 | T2 | local (Windows service) |
| T8 knowledge tools | R7 | T2 | cloud |
| T10 installer profile | R10 | T2, T3, T9 | local |
| T11 live E2E with a CEO agent | all | all | local, traced |
