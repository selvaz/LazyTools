# Capabilities catalog (LazyTools + LazyBridge)

For the architecture and task lifecycle, see the [LazyTools agent orchestrator overview](orchestrator-overview.html).

## How to use this file

1. Search this file (Ctrl-F) BEFORE building any tool, watcher, viewer, queue or gate. If a row looks close, read its doc and run its `--help`.
2. Derived from code on 2026-11-26: LazyTools `origin/main` (`pyproject.toml` scripts, `--help` of every subcommand, `mcp_server/providers.py`, `projects/task_service.py`) and LazyBridge v1.10.0 (`lazybridge/`, `lazybridge/ext/*`, `lazybridge/policy`).
3. Update this file IN THE SAME PR as any new CLI command, MCP tool, provider or LazyBridge extension.
4. Status: `live-tested` = run for real on 25-26/11/2026; `tests-only` = unit/integration tests exist, no real run recorded; `unknown` = could not tell from the code.
5. Not a user manual: each row links to the doc that is. Commands below are exact; add `--db PATH` / `--registry-db PATH` to use a non-default Store.

Entry points (LazyTools `[project.scripts]`): `lazytools-mcp`, `lazytools-orchestration-mcp`, `lazytools-code-bridge`, `lazytools-ecosystem-sync`, `lazytools-projects`. Also `python -I tools/governed_task.py` (repo script, not installed).

Quick map of "things we keep rebuilding" to what already exists:

| You want | Use |
| --- | --- |
| PR watcher | `lazytools-code-bridge watch owner/repo#N` |
| PR navigator (review/fix until merge-ready) | `lazytools-code-bridge navigate` |
| Trace viewer (live + replay) | `lazybridge.ext.viz.Visualizer` |
| Task-graph service (deps, claims, fencing, leases) | `lazytools.projects.task_service.ProjectTaskService`, `lazytools-code-bridge dispatch`; LazyBridge `control_plane.ControlStore` |
| Approval queue / tiers / cards | `lazybridge.ext.approval`, `lazybridge.policy` |
| Quota-aware model pick | `lazytools-code-bridge route`, `lazytools.routing` |
| Signed proof of a publication | `lazybridge.ext.receipt`, `lazytools-code-bridge publish` / `verify-receipt` |

On branches not on main: see "On open branches" near the end.

---

## 1. Coding agents: code bridge (`lazytools-code-bridge`)

Docs: [code-bridge.md](code-bridge.md), [code-bridge-cloud-setup.md](code-bridge-cloud-setup.md). All commands default to `~/.lazytools/code-bridge.sqlite` (`LAZYTOOLS_CODE_BRIDGE_DB`). Invoke as `lazytools-code-bridge <sub>` or `python -m lazytools.code_bridge <sub>`.

| Capability | How to call | What it does | Status | Docs |
| --- | --- | --- | --- | --- |
| Run a local coding job | `lazytools-code-bridge run --engine codex\|claude --cwd REPO --task @brief.md [--tier basic\|writing\|thinking\|critical] [--session ALIAS] [--detach]` | Runs Codex or Claude Code to completion; approvals go to a durable ticket queue; first stdout line is the job id | live-tested | code-bridge.md#job-lifecycle |
| Detached job + follow | `lazytools-code-bridge run --detach --json` then `lazytools-code-bridge wait JOB` | Worker outlives the launching shell; `wait` blocks to terminal result (exit 3 on waiter timeout) | live-tested | code-bridge.md#job-lifecycle |
| Run in the cloud | `run --where cloud --allow-cloud --engine codex\|claude --cwd REPO --task @brief.md [--branch B] [--routine NAME]` | Submits brief to Codex Cloud / Claude Code cloud session, returns at once, uses no local engine slot, cannot read local data | live-tested | code-bridge.md#cloud-jobs-run---where-cloud |
| Auto placement local vs cloud | `run --where auto` | Cloud inside `[cloud] busy_windows` or when no local slot is free, else local | tests-only | code-bridge.md#placement---where-auto |
| Cloud job on a PR / new PR | `run --where cloud ... --pr N` / `--open-pr [--title T]` | Targets an existing PR head branch, or pushes a new branch and opens a PR | tests-only | code-bridge.md#pr-targets---pr-n---open-pr |
| Auto-land cloud result | `run --where cloud ... --land` | Commit, push, update/open PR when the job finishes (`wait` or `cloud-sync` performs it) | tests-only | code-bridge.md#automatic-landing---land-wait-cloud-sync |
| Apply a cloud diff | `lazytools-code-bridge apply JOB [--branch B] [--worktree P] [--rebase]` | Applies the cloud diff in a NEW worktree/branch; never your checkout; never pushes | live-tested | code-bridge.md#apply-never-in-your-checkout-never-pushed |
| Apply and land | `apply JOB --land [--rebase-ok]` | Apply, commit, push, update/open PR via `gh`; refuses a moved base; never force-pushes | live-tested | code-bridge.md#landing-apply-job---land |
| Reconcile all cloud jobs | `lazytools-code-bridge cloud-sync [--scheduled\|--manual]` | Polls providers, lands `--land` jobs, posts finished reviews, records failures; safe from a scheduler | tests-only | code-bridge.md#automatic-landing---land-wait-cloud-sync |
| Resync a poll-failed cloud job | `lazytools-code-bridge resync JOB` | Re-checks a cloud job an older bridge failed after a poll error | tests-only | code-bridge.md#cloud-jobs-run---where-cloud |
| Cloud review (opposite or fresh same-provider) | `run --where cloud --review-of JOB` | Uses the opposite installed engine, or the sole installed writer engine in a fresh conversation; records independence and lands the verdict on the PR | tests-only | code-bridge.md#cloud-reviews-run---where-cloud---review-of-job |
| Review profile (read-only job) | `run --profile review\|read --review-of JOB` | Read-only review job; prefers the opposite engine and falls back fresh only when the writer engine is the sole installation | tests-only | code-bridge.md#approval-profiles |
| List cloud environments | `lazytools-code-bridge cloud-envs [--cwd REPO] [--no-probe]` | Lists Codex Cloud environments and the repo each serves, plus Claude cloud env(s) | tests-only | code-bridge.md#environments-cloud-envs |
| Cloud eligibility probe | `lazytools-code-bridge cloud-preflight probe --repo R \| --all [--network on\|off]` | Measures Codex cloud capabilities and writes repo-owned eligibility evidence | live-tested | code-bridge-cloud-setup.md |
| Starter preflight manifest | `lazytools-code-bridge cloud-preflight init --repo R` | Writes a starter manifest from pyproject dependencies | tests-only | code-bridge-cloud-setup.md |
| Check Claude cloud setup | `lazytools-code-bridge cloud-setup claude [--check] [--json]` | Local check of config, token vars, a model per routine; no network; exit 1 if missing | tests-only | code-bridge-cloud-setup.md |
| Install cloud-setup skill | `lazytools-code-bridge setup-skill [--install [DIR]]` | Prints/installs the SKILL.md that walks an LLM through cloud setup | tests-only | skills-code-bridge-cloud-setup.md |
| Stop cloud agents | `lazytools-code-bridge cloud-stop [TARGET] [--all-running] [--dry-run]` | Stops a Codex Cloud task or requests a Claude cloud session stop; account-wide with `--all-running` | tests-only | code-bridge.md#stopping-cloud-agents |
| Job list / status / result | `lazytools-code-bridge jobs [--all] [--json]`, `lazytools-code-bridge status JOB`, `lazytools-code-bridge result JOB` | History, current status, full record incl. workspace evidence for unfinished jobs | live-tested | code-bridge.md#job-lifecycle |
| Wait on many jobs | `lazytools-code-bridge wait-any JOB... \| --all-active [--events done,failed,approval,landed,interrupted] [--loop]` | One JSON event per call (or stream with `--loop`); cloud jobs refreshed and landed | tests-only | code-bridge.md#machine-readable-ids-and-multi-job-waiting |
| Cancel a job | `lazytools-code-bridge cancel JOB [--reason R]` | Stops the process tree, records workspace evidence, rejects open approvals | tests-only | code-bridge.md#timeout-workspace-evidence-and-cancellation |
| Pre-launch brief check | automatic in `run`; `--strict`, `--no-brief-check` | Lints the delegation brief (Goal/Context/Constraints/Done when/Report); `--strict` refuses to launch on error | tests-only | code-bridge.md#brief-check |
| Branch freshness check | automatic in `run`; `--require-fresh`, `--no-freshness-check` | Fetches and warns/refuses if the branch is behind upstream | tests-only | code-bridge.md#freshness-check |
| Timeout continuations | `run --timeout S --max-continuations N --max-wall S` | Auto-continues after a timeout when committed/pushed progress exists | tests-only | code-bridge.md#committed-progress-survives-turn-timeouts-r12 |
| Machine-wide engine slots | `run --wait-for-slot`; `[concurrency]` in `~/.lazytools/code-bridge.toml` | Per-engine live-job limit (codex 1, claude unlimited by default); queue or refuse | tests-only | code-bridge.md#engine-concurrency-limit-machine-wide |
| One job per repo | automatic (`_lockfile.py`) | Reclaimable file lock per cwd; stale lock reclaimed after a killed worker | tests-only | code-bridge.md#one-job-per-repository-at-a-time |
| Named sessions | `run --session ALIAS`; MCP `code_sessions_*` | Durable, renamable conversation aliases; resumes engine thread | tests-only | code-bridge.md#session-reuse |
| Governed launch | `run --launch-mode auto\|governed\|legacy --profile read\|edit\|edit-and-run --network on\|off` | Compiles a LazyBridge policy profile to native engine settings; governed requires release evidence | tests-only | code-bridge.md#approval-profiles |
| Confinement root | `run --root DIR`, `LAZYTOOLS_CODE_ROOT`, `root` in code-bridge.toml | `--cwd` must resolve inside the root | tests-only | code-bridge.md#what-is-gated-and-how |
| Per-job temp dir | automatic (`_tempdir.py`) | Only extra writable root a job gets | tests-only | code-bridge.md#per-job-temp-directory |
| Model catalogue audit | `lazytools-code-bridge models [--probe-claude] [--json]` | Reads live model availability; audits catalogue and default policy | tests-only | code-bridge.md#check-live-model-availability |
| Search the capabilities catalog | `lazytools-code-bridge capabilities [QUERY]` | Prints catalog rows whose capability or call contains QUERY, ignoring case. Omit QUERY to list every row. | tests-only | CAPABILITIES.md |
| Autonomy report | `lazytools-code-bridge autonomy --week 2026-Www \| --since D [--until D] [--json]` | % of finished jobs with no manual intervention vs 90% target, intervention causes, and card decisions by class | tests-only | code-bridge.md#autonomy-report |
| Prune automated transcripts | `lazytools-code-bridge prune-sessions [--older-than-days N] [--engine all\|claude\|codex] [--apply]` | Lists (dry run) or deletes old SDK/LazyBridge Claude Code and Codex transcripts | tests-only | code-bridge.md#prune-old-automated-sessions |
| Governed task runner (writer + opposite reviewer) | `python -I tools/governed_task.py start\|review\|rework\|recover\|publish\|ledger --project P` | Runs a writer and an independent reviewer on a task, runs required checks in a fresh worktree, publishes, CI-gates, writes a trial ledger row | tests-only | code-bridge.md#governed-task-runner |
| Brief template + delegation method | docs only | Brief structure and delegation method | n/a | code-bridge.md#delegation-method |
| Repo map for LLMs | MCP `codemap_map`, `codemap_search`; `lazytools.codemap` | Rank candidate files, map a repo with RepoMap (needs `repomap` binary) | tests-only | research/repomap-benchmark.md |

## 2. Review

| Capability | How to call | What it does | Status | Docs |
| --- | --- | --- | --- | --- |
| PR navigator (merge-ready loop) | `lazytools-code-bridge navigate --repo O/R --pr N --reviewer-cmd CMD --checks "pytest" --checks "ruff check" [--max-rounds 3] [--fix-engine codex\|claude]` | Review exact head, focused fix brief and relaunch on P2-and-below findings, run checks in a temp worktree; exit 0 merge-ready, 3 escalate (P0/P1/unknown/round cap), 1 operational. Never merges | tests-only | code-bridge.md#navigator-r1-the-merge-ready-loop |
| Navigator state machine (library) | `lazytools.code_bridge._navigator.Navigator` | Provider-neutral state machine; reviewer model must differ from author model; same-head review reused = fail closed | tests-only | code-bridge.md#navigator-r1-the-merge-ready-loop |
| Opposite-engine reviewer choice | `lazybridge.policy.select_reviewer`, `opposite_engine_reviewer`, `make_reviewer`; types `ReviewMode`, `ReviewIndependence`, `ReviewSelection` | Picks the reviewer engine; records independence level `opposite-engine` or `same-provider-fresh` | tests-only | LazyBridge docs |
| Codex review via MCP | MCP `codex_code_review`, `codex_review_changes`, `codex_ask` | Read-only review of a local repo / diff / question; minutes per call | tests-only | code-support/codex.md |
| Claude review via MCP | MCP `claude_code_review`, `claude_ask` | Same, with Claude Code | tests-only | code-support/claude-code.md |
| Review command allow-list | `lazytools.code_bridge._review_policy`, `_readonly.py`, `readonly_allowlist.toml` | Fail-closed tokenized allow-list for review/read profiles and Claude Bash | tests-only | code-bridge.md#approval-profiles |
| Publish a reviewed commit as draft PR | `lazytools-code-bridge publish --task P/T --repo R --branch B [--base main] [--ticket ID\|--retry-ticket ID] [--request-only]` | Needs accepted independent review of the exact head; owner approval ticket; push + draft PR; signed receipt | tests-only | code-bridge.md#publish-an-accepted-review |
| Watch PRs (read-only) | `lazytools-code-bridge watch owner/repo#N [owner/repo#N ...] [--until HH:MM] [--interval S] [--json]` | REST-only poll; one line per comment, inline review, independent-review verdict, completed check, head change, merge/close; progress stored per PR in the Store so restarts do not repeat. Exit 0 at `--until`, 2 all merged/closed, 1 error | live-tested | code-bridge.md#watching-pull-requests |
| Review marker contract | `<!-- independent-review v1 -->` + JSON `findings` list | Format navigator and watch read to recognise an independent review of a head | tests-only | code-bridge.md#navigator-r1-the-merge-ready-loop |
| Structured eval harness | `lazybridge.ext.evals` (`EvalSuite`, `EvalCase`, `llm_judge`, `contains`, `exact_match`, ...) | Batch-grade agent outputs with assertion helpers or an LLM judge | tests-only | LazyBridge docs |

## 3. Task graph and projects

State lives in a LazyBridge `Store` (SQLite). Docs: [projects.md](projects.md). `LAZYTOOLS_PROJECTS_STORE_DB` selects the file (also reads `LAZYCEO_CEO_STORE_DB`); `lazytools-projects` and the code bridge default to `~/.lazytools/projects.sqlite`.

The project dispatcher lives in `lazytools.code_bridge._dispatch`, with job launch
and resume in `lazytools.code_bridge._dispatch_launch` and checkout acceptance and
publication in `lazytools.code_bridge._dispatch_workspace`.

| Capability | How to call | What it does | Status | Docs |
| --- | --- | --- | --- | --- |
| Dispatch a whole project graph | `lazytools-code-bridge dispatch --project ID [--parallel N] [--once] [--dry-run] [--repo-root SLUG=PATH] [--worktree-root P] [--land]` | Claims ready tasks, launches bridge jobs per task tier/placement, records terminal evidence, completes/fails (failure blocks only the dependent branch). Events `ready/claimed/launched/done/failed/blocked TASK`; polls every 15 s; adopts already-submitted jobs, no duplicates. A task with `requested_placement: cloud` runs through `run --where cloud --allow-cloud --open-pr --land` (routed engine, cloud environment required, `[cloud]` concurrency limits respected) and is accepted only if it landed (PR URL and pushed commit) and its `checks` pass in a local clone of the landed branch. After accepting a local task, `--land` pushes its published `dispatch/<project>/<task>` branch to origin (fast-forward only, never the default branch) and opens a PR with `gh` if one does not already exist. The follow-up event is `landed TASK PR_URL` or `land-failed TASK REASON` | live-tested (on branch dispatch-placement; main has the base version); cloud placement and `--land` unit-tested | code-bridge.md#dispatching-a-project |
| Governed task service | `lazytools.projects.task_service.ProjectTaskService` | Queue over the projects board: `next_ready`, `claim`, `prepare_attempt`, `begin_submit`, `record_submit`, `local_claim`, `local_dispatch`, `dispatch_status`, `reconcile_dispatch`, `record_placement`, `record_completion_evidence`, `complete`, `route_clear`, `queue_metrics`, `publication_review`, `fail_local_launch`, `fail_dispatch_launch` | tests-only | projects.md#governed-task-queue |
| Task queue over MCP | MCP `tasks_next_ready`, `tasks_queue_metrics`, `approvals_pending` (read); `tasks_claim`, `tasks_complete`, `tasks_route_clear`, `approvals_decide` (write) | Principal-scoped queue; needs file-backed Store and `projects_task_principal` in config | tests-only | projects.md#governed-task-queue |
| Placement evidence / route quarantine | `record_placement` (`PlacementEvidence`), `tasks_route_clear` | Route = (repo, placement, environment id); three consecutive infrastructure failures quarantine a route until a fresh green preflight and an explicit clear | tests-only | projects.md#governed-task-queue |
| Structured plan fields | task fields `task_id`, `dependency_ids`, `tier`, `requested_profile`, `requested_network`, `requested_placement`, `effective_placement`, `review_of`, `repo` | Dependencies, tier, network, review pairing per task | tests-only | projects.md#governed-task-queue |
| Create / review / promote a project | MCP `projects_create`, `projects_review_plan`, `projects_promote`, `projects_revise_plan`; `projects.intake.install_project_plan` | Intake gate: project is a draft until defined and plan-reviewed; digest-fenced promotion | tests-only | projects.md#project-intake |
| Close a task with evidence (CLI) | `lazytools-projects close-task PROJECT IDX --check "CMD=pass:DETAIL" --review "..." --review-by codex --reason "..." --reviewer operator --worker claude [--db F]` | Claims, records checks and review, accepts, closes the board slot; exit 1 on refusal | tests-only | projects.md#contracts-and-verification |
| Task contracts + verification | MCP `projects_open_contract`, `projects_get_contract`, `projects_find_contract`, `projects_get_verification*`, `projects_accept_verification`, `projects_block_verification`, `projects_request_rework`, `projects_retry_review`, `projects_retry_harness` | Acceptance contract agreed before delegation; task is not done until verification is accepted (full profile: `LAZYTOOLS_PROJECTS_TOOLS=full`) | tests-only | projects.md#contracts-and-verification |
| Plan editing | MCP `projects_retire_task`, `projects_reopen_task`, `projects_reopen_done_task`, `projects_reopen_for_empty_review`, `projects_schedule_task`, `projects_close_accepted_task` | Retire/reopen/replace tasks, set schedule | tests-only | projects.md |
| Gantt / schedule / timeline | MCP `projects_gantt`, `projects_schedule`, `projects_timeline`; `lazytools.projects.gantt`, `schedule`, `timeline` | Deterministic Gantt (no LLM), schedule-health, what is late/stuck | tests-only | projects.md#schedule-and-deterministic-gantt |
| Board and registry reads | MCP `projects_list`, `projects_get`, `projects_board_summary`, `projects_notes`, `projects_add_note`, `projects_jobs`, `projects_repos_for_project` | Read the shared project registry | tests-only | projects.md |
| Owner and deadline | MCP `projects_set_owner`, `projects_set_deadline`; `projects.owner` | Owner model `ceo` / `claude` / `shared`; safe writes alongside a live CEO | tests-only | projects.md#the-owner-model |
| Pause / resume / close project | MCP `projects_pause`, `projects_resume`, `projects_close` | Lifecycle control | tests-only | projects.md |
| Cost and quota views | MCP `projects_cost_report`, `projects_quota`; `projects.cost_report`, `quota_telemetry` | Per-project and fleet cost; engine quota readings (never raise) | tests-only | projects.md#notes-jobs-and-cost-report |
| Quota brake | MCP `projects_brake_status`, `projects_set_brake_enabled`; `projects.admission`, `brake` | Per-project switch; admission refuses new engine calls when quota is low | tests-only | projects.md#the-brake-rule-and-quota-telemetry |
| Records registry | `lazytools.projects.records` | Durable registry for independent pieces of ongoing work | tests-only | projects.md |
| Receipts over MCP | MCP `receipts_get`, `receipts_verify` | Read/verify stored publication receipts | tests-only | code-bridge.md#publish-an-accepted-review |
| Durable blackboard (to-do that survives restart) | `lazybridge.ext.planners.DurableBlackboard`, `durable_blackboard_agent` | Store-backed plan of tasks with CAS, snapshot view | tests-only | planners/blackboard.md |
| Control store (atomic claim, fencing, ledger) | `lazybridge.control_plane.ControlStore` (`create_project`, `assign`, `enqueue`, `claim`, `finish`, `item`, `events`, `counts`), `verify_ledger`, `FenceRejected` | One DB: atomic claim, monotonic fencing token, append-only event ledger. Not yet wired into LazyTools projects | tests-only | LazyBridge docs |
| Concurrency probe for the control store | `lazybridge.control_plane.probe` | Checks the storage engine survives concurrency on this filesystem | unknown | LazyBridge docs |

## 4. Approvals and permissions

| Capability | How to call | What it does | Status | Docs |
| --- | --- | --- | --- | --- |
| List pending approval tickets | `lazytools-code-bridge pending [--json]` | Open tickets with job id, gist, command/reason, creation and expiry | live-tested | code-bridge.md#job-lifecycle |
| Approve / reject a ticket | `lazytools-code-bridge approve TICKET [--actor A --reason R] [--ceo]`, `lazytools-code-bridge reject TICKET --reason R [--ceo]` | Decide a ticket; unanswered tickets expire as rejection (`LAZYTOOLS_CODE_BRIDGE_TICKET_TTL`). `--ceo` (used by the MCP `bridge_approve`/`bridge_reject`) approves only classes the autonomy policy delegates in the current mode and records actor, mode and class with the reason | live-tested | code-bridge.md#what-is-gated-and-how |
| Owner approver credential | `lazytools-code-bridge approver-credential init \| provision-automatic PROJECT \| provision-coder` | Provisions owner credential, automatic approval cards, coder hand | tests-only | code-bridge.md#operator-credentials |
| CEO autonomy modes | `lazytools-code-bridge approver-credential autonomy --mode supervised\|total [--preset NAME] [--registry-db F]` or `--show` | Owner-controlled card delegation with `solo-dev` and `with-production` presets and audited card decisions | tests-only | code-bridge.md#autonomy-modes |
| Issue approver credential (any registry) | `python -m lazybridge.ext.approval --db F issue-approver-credential PRINCIPAL --role R --output FILE` | Writes a private credential file for a principal and roles | tests-only | LazyBridge docs |
| Approval queue | `lazybridge.ext.approval.ApprovalQueue`, `ApprovalTicket`, `TicketKind`, `TicketStatus`, `TypedApprovalRequest` | Durable Store-backed tickets, TTL, renotify, redacted payloads | tests-only | LazyBridge docs |
| Tiered gate | `lazybridge.ext.approval.TieredGate`, `Tier`, `Rule`, `Channel`, `run_gate_sync`; `lazytools.code_bridge._policy` | Declarative per-agent approval policy over tool calls | tests-only | LazyBridge docs |
| Approval routing / escalation | `route_ticket`, `ApprovalRoute`, `EscalationSummary`, `ticket_gist`, `invocation_chain` | Typed routing over invocation ancestry; assignee and escalation chain | tests-only | LazyBridge docs |
| Approval cards | `lazybridge.policy.ApprovalCard`, `CardMatcher`, `CardTier`; `ext.approval.CardApproval`, `CardProposal`, `CardTemplate` | Immutable cards matched deterministically to commands; use committed as audit receipt before the action | tests-only | code-bridge.md#coder-approval-cards |
| Service agents | `lazybridge.ext.approval.ServiceAgent` | Operator-configured service with request step and one-shot dispatch | tests-only | LazyBridge docs |
| Feedback requests | `FeedbackRequest`, `FeedbackResponder`, `FeedbackAnswer`, `FeedbackPriority` | Structured ask-the-human requests via the queue | unknown | LazyBridge docs |
| Profiles and task grants | `lazybridge.policy.load_profiles`, `resolve`, `Profile`, `ProfileDocument`, `TaskGrant`, `EffectivePolicy`; bundled `policy/data/profiles.example.toml` | Profile ceiling (`read`, `edit`, `edit-and-run`); resolves a task grant against it and reports grants removed | tests-only | code-bridge.md#approval-profiles |
| No-widen rule | `lazybridge.policy.child_within_parent`, `PolicyWidening` (raises) | A child's authority may never exceed its parent's | tests-only | LazyBridge docs |
| Tree-wide budget | `lazybridge.policy.TreeBudget`, `PolicyBudgetExceeded`, `PolicyContext`, `policy_scope`, `current_policy_context` | Shared admission bound across a whole invocation tree; policy propagates into nested work | tests-only | LazyBridge docs |
| Compile policy to native engine config | `lazybridge.policy.compile_claude`, `compile_codex` | Claude settings + strict MCP allow-list; session `CODEX_HOME` config | tests-only | code-bridge.md#approval-profiles |
| Governed-launch admission evidence | `lazybridge.policy.claude_admission`, `codex_admission`; `python -m lazybridge.policy.release_check [--engine codex\|claude] [--output F]` | Opt-in local runtime canaries that prove the native sandbox behaves; produces the release report `--launch-mode governed` needs | unknown | code-bridge.md#approval-profiles |
| Task-scoped confirmation grants | `lazytools.safety.ConfirmationGate`, `Allowlist`, `ActionBlocked`, `validate_public_url`; `projects.task_approvals.ProjectTaskApprovals` | One-shot grants bound to a target and scope; SSRF guard for connectors | tests-only | safety.md |
| Sandboxed Read/Write/Edit/Bash tools | `lazytools.workspace.Workspace` | Path-confined file tools for agents (Bash opt-in, not path-confined) | tests-only | workspace.md |
| Terminal / human gates | `lazybridge.ApprovalGate`, `TerminalApprovalGate`, `ApprovalRequest`, `ApprovalDecision`; `ext.hil.HumanEngine`, `SupervisorEngine` | Block a tool call on a human at the terminal / web UI; supervisor can retry an agent | tests-only | hil/index.md |

## 5. Receipts and traces

| Capability | How to call | What it does | Status | Docs |
| --- | --- | --- | --- | --- |
| Signed publication receipt | `lazybridge.ext.receipt` (`PublicationReceipt`, `Issuer`, `IssuerKey`, `sign`, `verify`, `canonical_bytes`) | Ed25519 receipt over repo, head, approval, policy, reviewer, checks, delegation evidence | tests-only | code-bridge.md#publish-an-accepted-review |
| Verify a receipt | `lazytools-code-bridge verify-receipt RECEIPT_ID\|FILE [--live] [--public-key PEM]`; `python -m lazybridge.ext.receipt verify FILE` | Offline (or `--live` against GitHub) verification | tests-only | code-bridge.md#publish-an-accepted-review |
| Receipt live demo | `tools/receipt_live_demo.py` | Demo of issue and verify | unknown | tests/test_receipt_live_demo.py |
| Live trace viewer | `lazybridge.ext.viz.Visualizer(session, store=None, port=0, auto_open=True)` (context manager) | Local HTTP server on 127.0.0.1 with URL token: live agent graph, store, SSE event stream | tests-only | LazyBridge docs |
| Replay a recorded run | `Visualizer.replay(db=..., session_id=...)`; `viz.replay.list_sessions(db)` | Reads the SQLite event log read-only; pause/play/speed/step controls | tests-only | LazyBridge docs |
| Session event log | `lazybridge.Session(db=...)`, `EventLog`, `EventType` | Every agent/tool/LLM event persisted to SQLite; secrets and base64 blobs redacted | tests-only | LazyBridge docs |
| Event exporters | `lazybridge.JsonFileExporter`, `ConsoleExporter`, `StructuredLogExporter`, `CallbackExporter`, `FilteredExporter`, `EventExporter` | Pluggable sinks for Session events | tests-only | LazyBridge docs |
| OpenTelemetry export | `lazybridge.ext.otel.OTelExporter` | Emits Session events as OTel GenAI spans | tests-only | LazyBridge docs |
| Pipeline graph schema | `lazybridge.GraphSchema` (`graph/schema.py`) | Serialisable graph of agents, routers, edges (foundation for GUIs) | tests-only | LazyBridge docs |
| Per-job logs and results | `lazytools-code-bridge result JOB --json`; results dir `~/.lazytools/results/` | Complete job record, log, final text copy outside the Store | live-tested | code-bridge.md#job-lifecycle |
| Cost per project / job | MCP `projects_cost_report`, `projects_jobs` | Cost totals and unmeasured-cost counts | tests-only | projects.md#notes-jobs-and-cost-report |

## 6. Routing and quota

| Capability | How to call | What it does | Status | Docs |
| --- | --- | --- | --- | --- |
| Explain a quota-aware pick | `lazytools-code-bridge route --tier basic\|writing\|thinking\|critical [--engine E] [--where auto] [--review-of JOB] [--needs images] [--json]` | Shows engine/model/effort, winning rung, quota windows, forecasts, exclusions; launches nothing | live-tested | code-bridge.md#choose-a-model-from-quota |
| Launch with a tier | `run --tier T [--tiers catalogue.toml]` | Same computation, then launches | live-tested | code-bridge.md#choose-a-model-from-quota |
| Router library | `lazytools.routing.route`, `recommend`, `load_default_tiers` | Pure deterministic router (no LLM picks a model); `recommend` adds live quota reads | tests-only | code-bridge.md#choose-a-model-from-quota |
| Capability ladder catalogue | `lazytools/routing/default_tiers.toml`, `routing/catalogue.py`; override `~/.lazytools/model_tiers.toml` | Validated tiers; unusable models rejected before launch | tests-only | code-bridge.md#choose-a-model-from-quota |
| Live quota telemetry | `lazytools.routing.live`, `projects.quota_telemetry`; MCP `projects_quota` | Bounded reads of Codex and Claude quota with a cache shared across CLI processes | tests-only | projects.md#the-brake-rule-and-quota-telemetry |
| Engine admission | `lazytools.projects.admission` (`admit`, `decide`, `budget_for`, `release`, `preflight`) | Whether a new engine call may start given remaining quota and in-flight count | tests-only | projects.md |
| Provider/model tier mapping | `lazybridge.model_tiers.translate_model`, `claude_tier`, `openai_tier` | One-to-one Claude <-> OpenAI ladder | tests-only | LazyBridge docs |
| Provider capability matrix | `lazybridge.matrix.provider_capabilities`, `native_tool_support` | Which provider supports which feature/native tool | tests-only | LazyBridge docs |
| Live Claude/Codex usage | `lazybridge.engines.claude_code.usage`, `engines.codex.usage` | Parses `/usage` and App Server quota into budgets | tests-only | LazyBridge docs |

## 7. Cloud

Cloud rows are in sections 1 and 2 (`run --where cloud`, `apply`, `cloud-sync`, `cloud-envs`, `cloud-preflight`, `cloud-setup`, `cloud-stop`, `resync`, `--where auto`). Safety boundary: cloud agent commands run outside the local approval gate and only the diff comes back via `apply`; `--allow-cloud` is required. See [code-bridge.md#safety-boundary-for-cloud-jobs](code-bridge.md#safety-boundary-for-cloud-jobs) and [safety.md#cloud-code-bridge-landing-boundary](safety.md#cloud-code-bridge-landing-boundary).

## 8. MCP tools by provider (`lazytools-mcp`)

Start: `lazytools-mcp [provider ids...] [--allow-unsafe] [--config lazytools.json] [--http --port N] [--list-tools]`. Default: all providers, read-only. The coding providers (`code_bridge`, `code_review`, `claude_review`, `code_sessions`, `codemap`, `fleet`) stay here for one release; mounting one logs a deprecation note (see section 8a). Write tools need `--allow-unsafe`. Docs: [mcp-server.md](mcp-server.md), [connectors.md](connectors.md). Coding providers must be named explicitly. Tool names listed from the code (`default_providers` + `expand_tools`) and the installed server; providers whose optional package was missing here (web, fin, regimes, pulse, telegram, code_bridge, code_sessions, gmail, outlook, studio) are listed from source or the live tool list. Status for all providers: tests-only unless noted.

| Provider id | Tools | What it does | Status | Docs |
| --- | --- | --- | --- | --- |
| `codemap` | `codemap_map`, `codemap_search` | Repo map and issue-text file ranking | tests-only | research/repomap-benchmark.md |
| `studio` | `studio_status`, `studio_run_status` + published LazyStudio agents as tools | Discover and invoke agents published by LazyStudio | unknown | mcp-server.md |
| `datahub` | `datahub_search`, `datahub_describe`, `datahub_resolve_instrument`, `datahub_list_{datasets,symbols,countries,indicators,macro,sectors}`, `datahub_get_{coverage,price_summary,statement,financial_facts,financials_coverage,ingestion_health,job_status}`, `datahub_calendar_{series,vocabulary}`; write: `datahub_ensure_price_history`, `datahub_ensure_financials`, `datahub_register_listing` | market-data-hub discovery, prices, financials | tests-only | datahub.md |
| `statistical` | `statistical_return_{volatility,correlation,outliers}`, `statistical_regression_{ols,ridge,lasso}` | LazyStats analysis | tests-only | statistical-analysis.md |
| `regimes` | `regime_fit`, `regime_fit_window`, `regime_fit_categorical`, `regime_get_{current,changes,summary}`, `regime_compare_{windows,emission_models}`, `regime_scan_state_counts`, `regime_generate_plots`, `regime_load_from_datahub`, `regime_params_{list,load,save}`, `regime_apply_params`, `regime_store_{list,load,delete}`, `regime_db_*`, `regime_init_db` | Fit and inspect market-regime models, persist to the regime depot | tests-only | regimes.md |
| `econ_calendar` | `calendar_list_series`, `calendar_recent_releases`, `calendar_explain_indicator`, `calendar_vocabulary` | Economic release dates and values | tests-only | datahub.md |
| `earnings_calendar` | `earnings_week`, `earnings_for_day`, `earnings_event`, `earnings_aggregate`, `earnings_vocabulary` | Corporate earnings dates | tests-only | datahub.md |
| `cftc_cot` | `cftc_positioning_commodities`, `cftc_positioning_financial` | CFTC Commitments of Traders | tests-only | cftc-cot.md |
| `treasury_fiscal` | `treasury_auctions`, `treasury_cash_balance`, `treasury_debt` | US Treasury fiscal data | tests-only | treasury-fiscal.md |
| `alfred` | `alfred_vintage`, `alfred_vintage_dates` | Point-in-time FRED vintages | tests-only | alfred.md |
| `tradingview` | `tradingview_screen`, `_quote`, `_breadth`, `_fields`, `_resolve`, `_vocabulary` | Live screening, breadth, fund snapshots | tests-only | tradingview.md |
| `polymarket` | `polymarket_{list_markets,search_markets,get_market,price,midpoint,order_book}` | Prediction-market data | tests-only | polymarket.md |
| `manifold` | `manifold_{list_markets,search_markets,get_market,probability,recent_bets}` | Crowd forecasts | tests-only | manifold.md |
| `gleif` | `gleif_{search,fuzzy_search,get_record,children,parents}` | Legal entities and LEIs | tests-only | gleif.md |
| `web` | `web_search`, `web_crawl`, `web_crawl_many`, `get_page`, `get_session_pages`, `get_crawl_graph`, `get_rag_document`, `get_artifacts`, `search_cached`, `get_cache_stats`, `list_presets`, `list_schemas` | LazyCrawler search/crawl/cache (needs `lazycrawler`) | tests-only | connectors.md |
| `fin` | `portfolio_tree_{list,load,save,delete,validate,estimate,backtest}`, `portfolio_optimizer_{run,backtest,list_objectives}` | LazyPortfolio trees and optimisation (needs `lazyportfolio`) | tests-only | portfolio-optimization.md |
| `report` | `render_memo`, `render_memo_html`; write: `save_memo_html`, `save_memo_markdown`, `save_report` | Deterministic memo/report rendering | tests-only | report.md |
| `optimizer_agent`, `report_agent`, `stats_agents`, `calendar_agent` | `portfolio-optimizer-specialist`, `report-specialist`, `stats-supervisor` / `regression-analyst` / `regime-analyst` / `volatility-correlation-analyst`, `macro-calendar-analyst` | Specialist LLM agents exposed as tools (write-enabled only) | tests-only | skills-analyst.md, skills-stats.md |
| `code_review` | `codex_code_review`, `codex_review_changes`, `codex_ask` | Codex review / question | tests-only | code-support/codex.md |
| `claude_review` | `claude_code_review`, `claude_ask` | Claude Code review / question | tests-only | code-support/claude-code.md |
| `code_write` | `codex_write`, `claude_code_write` | Synchronous write access; DENIED in this machine's settings (use the bridge) | tests-only | code-support/index.md |
| `code_sessions` | `code_sessions_list`, `_bind`, `_rename`, `_forget` | Inspect/curate named coding sessions | tests-only | code-bridge.md#session-reuse |
| `code_bridge` | `code_bridge_submit`, `code_bridge_status`, `code_bridge_result` | Same bridge as the CLI, for other agents; accepts `profile`, `launch_mode`, `network`, `task_id`, `claim_token` | tests-only | code-bridge.md#for-other-agents-mcp |
| `fleet` | `bridge_jobs`, `bridge_pending`, `bridge_route`, `bridge_autonomy`, `bridge_models`, `capabilities_search`, `bridge_events`; write: `bridge_approve`, `bridge_reject`, `bridge_cancel` | Read-only bridge fleet, approval, routing, model, autonomy, and capability views; `bridge_events` is the cursor-based job/ticket event channel shared with `wait-any`. The write tools, only with `--allow-unsafe` and never in task-scoped servers, decide tickets and cancel jobs as the CEO: an approval needs the owner's autonomy policy to delegate the ticket class in the current mode (else "owner decision required"); the ticket records actor, mode, class and reason | tests-only | mcp-server.md#fleet-read-only-code-bridge-views |
| `dispatch` | `dispatch_status`; write: `dispatch_start`, `dispatch_stop` | Project dispatcher status and detached supervised lifecycle; write tools require `--allow-unsafe`, and all three are excluded from task-scoped servers | tests-only | mcp-server.md#dispatch--project-dispatcher-control |
| `knowledge` | `lessons_search`; write: `lessons_record` | The lessons store of `dispatch --knowledge-db` (config `knowledge_db`, `LAZYTOOLS_KNOWLEDGE_DB`, default `~/.lazytools/knowledge.sqlite`) | tests-only | mcp-server.md#knowledge-shared-lessons |
| `pr` | `pr_watch_poll`; write: `pr_navigate`, `bridge_publish`, `bridge_apply` | One watcher poll per PR (shares state with `watch`); navigate, publish and apply through the CLI, only with `--allow-unsafe`; never in task-scoped servers | tests-only | mcp-server.md#pr-pull-request-tools |
| `telegram` | `telegram_send_message`, `telegram_send_document` | Allow-listed sends (needs credentials) | tests-only | telegram.md |
| `gmail` / `outlook` | `gmail_{list_emails,get_email,create_draft,send}`, `outlook_{list_emails,get_email,create_draft,send}` | Read/draft/send; only with credentials / `LAZYTOOLS_ENABLE_OUTLOOK=1` | tests-only | gmail.md |
| `projects` | see section 3 (core 10 read / 20 write tools; `full` 16 / 40) | Shared project registry | tests-only | projects.md#tool-profiles |
| `registry` | `registry_status`, `artifact_search`, `artifact_get`; write: `artifact_register` | Inspect ecosystem DBs and the artifact catalog | tests-only | registry.md |
| `pulse` | `pulse_list_backlog`, `pulse_list_pending_approvals`, `pulse_state_snapshot` | LazyCEO backlog and approvals (needs `lazyceo`) | tests-only | mcp-server.md |
| `gmail` | Gmail read, draft; send when opted in | Gmail read / draft / send; opt-in, non-interactive, allow-listed recipients | tests-only | gmail.md |
| `outlook` | Outlook read, draft; send when opted in | Outlook desktop (COM) read / draft / send; opt-in | tests-only | connectors.md |
| `calendar_agent` | `macro-calendar-analyst` | Macro calendar specialist agent exposed as one MCP tool | tests-only | mcp-server.md |
| `optimizer_agent` | `portfolio-optimizer-specialist` | LLM specialist over the `fin` tools, one MCP tool taking a task | tests-only | portfolio-optimization.md |
| `report_agent` | `report-specialist` | LLM specialist over the `report` tools, one MCP tool | tests-only | report.md |
| `stats_agents` | `stats-supervisor`, `regression-analyst`, `volatility-correlation-analyst`, `regime-analyst` | Statistical specialists and their supervisor as MCP tools | tests-only | skills-stats.md |

## 8a. Orchestration MCP server (`lazytools-orchestration-mcp`)

| Capability | How to call | What it does | Status | Docs |
| --- | --- | --- | --- | --- |
| Orchestration-only MCP server | `lazytools-orchestration-mcp [provider ids...] [--allow-unsafe] [--config lazytools.json] [--http --port N] [--list-tools]` | Mounts only `code_bridge`, `code_review`, `claude_review`, `code_sessions`, `codemap`, `projects`, `fleet`, `knowledge`, `pr` (tools in section 8); refuses other ids; starts with one coding CLI or none | tests-only | mcp-server.md#orchestration-server |
| Approval notifications | `lazytools-orchestration-mcp --allow-unsafe` (automatic, per client session) | Sends each new approval ticket as `notifications/message` (level `info`, data = the `bridge_events` `approval` event with the ticket id), read from `bridge_events`; stops with the session; never on read-only or task-scoped servers; best effort, so clients without notifications keep using `bridge_events` | tests-only | mcp-server.md#approval-notifications |

## 9. LazyBridge core (v1.10.0)

Import from `lazybridge`. The mental model: Agent = Engine + Tools + State; everything is a tool.

| Capability | How to call | What it does | Status | Docs |
| --- | --- | --- | --- | --- |
| Agent | `from lazybridge import Agent`; `Agent(...)`, `.run`, `.stream`, `.definition`, `.derive(...)`, `.as_tool(...)` | Single abstraction over engines; supports `fallback`, `verify=` | tests-only | LazyBridge docs |
| Compose agents | `Agent.chain(a, b)`, `Agent.parallel(...)`, `ParallelAgent`, agent in another's `tools=[...]` | Sequential, concurrent, nested | tests-only | composition/index.md |
| Tools | `lazybridge.Tool`, `tool`, `ToolProvider`, `NativeTool` | Wrap callables; signature / llm / hybrid schema modes; time limits (`ToolTimeoutError`) | tests-only | LazyBridge docs |
| Envelope | `lazybridge.Envelope` | The single data type flowing between agents and engines | tests-only | LazyBridge docs |
| Plan / Step | `Plan`, `Step(target, task, context, sources, writes, input, output, parallel, routes, name)` | Compiled multi-step DAG, checkpoint/resume (CAS), pause (`PlanPaused`), fan-out (`run_many`) | tests-only | LazyBridge docs |
| Sentinels and routing | `from_prev`, `from_start`, `from_step`, `from_parallel`, `from_parallel_all`, `from_memory`, `from_agent`, `when` | Step input resolution; declarative `when(...)` routes | tests-only | LazyBridge docs |
| ReplanEngine | `lazybridge.ReplanEngine`, `PlanRound`, `ReplanTask` | Guards a dynamic replan loop with checkpoint/resume | tests-only | LazyBridge docs |
| LLM engine + providers | `LLMEngine`; providers Anthropic, OpenAI, Google, DeepSeek, LM Studio, LiteLLM; `BaseProvider` for custom | Tool-calling loop over any provider | tests-only | LazyBridge docs |
| Coding engines | `ClaudeCodeEngine`, `CodexEngine`, `CodingAgentConfig`, `ClaudeCodePolicy`, `CodexPolicy` | Locally authenticated Claude Code / Codex as standard Engines | tests-only | code-support/index.md |
| Named engine sessions | `lazybridge.SessionRegistry` | JSON store of durable session aliases shared by both coding engines | tests-only | code-bridge.md#session-reuse |
| Memory | `lazybridge.Memory` | Conversation history with compression strategies; shareable | tests-only | LazyBridge docs |
| Store | `lazybridge.Store(db=...)`; `store.encryption.EncryptedStoreAdapter` | Thread-safe KV blackboard with optional SQLite persistence; at-rest encryption wrapper | tests-only | LazyBridge docs |
| Guards | `Guard`, `ContentGuard`, `DeduplicateGuard`, `LLMGuard`, `GuardChain`, `GuardAction`, `GuardError` | Input/output filtering (block / allow / modify) | tests-only | LazyBridge docs |
| AgentPool | `lazybridge.AgentPool` | Name-based routing exposed to an LLM as one `route` tool | tests-only | LazyBridge docs |
| Control-flow signals | `conclude`, `ConcludeSignal` | Non-local exit that crosses nested agent boundaries | tests-only | LazyBridge docs |
| Mock/test helpers | `lazybridge.MockAgent`, `lazybridge.testing.scripted_inputs` | Deterministic test doubles | tests-only | LazyBridge docs |
| Multimodal content | `ImageContent`, `AudioContent` | Media inputs | tests-only | LazyBridge docs |
| Bundled skill installer | `python -m lazybridge.skill install`; LazyTools: `python -m lazytools.skill install [--to claude\|codex\|all]` | Installs the framework / LazyTools skill for Claude Code and Codex | tests-only | mcp-server.md#install-the-assistant-skills |

## 10. LazyBridge extensions (`lazybridge.ext.*`)

| Capability | How to call | What it does | Status | Docs |
| --- | --- | --- | --- | --- |
| `ext.approval` | see section 4 | Tiered gate, queue, routing, cards, service agents | tests-only | LazyBridge docs |
| `ext.delegation` | `make_background_delegate`, `make_parallel_delegate`, `make_plan_delegate`, `make_persistent_consultant`, `make_codex_writer`, `make_claude_writer`, `JobRegistry`, `JobRunner`, `release_admission`, `refund_admission` | Fire-and-forget delegation tools with durable job records, admission/reservation, plan-task claiming | tests-only | LazyBridge docs |
| `ext.evals` | see section 2 | `EvalSuite` and assertion helpers | tests-only | LazyBridge docs |
| `ext.hil` | `HumanEngine`, `SupervisorEngine`, `human_agent`, `supervisor_agent` | Human-in-the-loop engines with terminal and web UI | tests-only | hil/index.md |
| `ext.knowledge` | `DurableKnowledgeBase`, `LessonStatus`, `render_lesson_line`, `slugify` | Store-backed "lessons" an agent records after non-obvious success; keyword search; stale flag after `STALE_AFTER_DAYS` | tests-only | LazyBridge docs |
| `ext.otel` | `OTelExporter` | OpenTelemetry GenAI spans | tests-only | LazyBridge docs |
| `ext.planners` | `orchestrator_agent`, `make_planner`, `make_plan_builder_tools` (five `execute_plan` tools), `PlanSpec`, `StepSpec`, `blackboard_orchestrator_agent`, `durable_blackboard_agent`, `DurableBlackboard` | Give an LLM a planning toolkit: DAG builder, flat blackboard, durable blackboard | tests-only | planners/index.md |
| `ext.receipt` | see section 5 | Signed publication receipts | tests-only | LazyBridge docs |
| `ext.viz` | see section 5 | Live and replay visualizer | tests-only | LazyBridge docs |
| `policy` (not under ext) | see sections 4 and 2 | Profiles, grants, no-widen, review independence, admission, compile | tests-only | LazyBridge docs |
| `control_plane` (not under ext) | see section 3 | Atomic claim + fencing + ledger | tests-only | LazyBridge docs |

## 11. LazyTools libraries and skills

| Capability | How to call | What it does | Status | Docs |
| --- | --- | --- | --- | --- |
| Agent skills (Wizengaimot council) | `lazytools.skills.council`; skill in `skills/agent_skills/wizengaimot` | Round-based discussion between this session, Codex and Claude | tests-only | skills-wizengaimot.md, skills-council.md |
| Analyst / stats / calendar / screener skills | `lazytools.skills.analyst`, `stats_agents`, `calendar_agent`, `macro_views`, `screener`, `stats_report` | Prebuilt specialist agents | tests-only | skills-analyst.md, skills-stats.md |
| Document skills | `lazytools.skills.doc_skills`, `lazytools.documents` | Read and process documents | tests-only | documents.md |
| Agent-operations skill | `python -m lazytools.skills.agent_operations` (SKILL.md + references) | Packaged operating guidance for agents | tests-only | skills.md#packaged-skill-agent-operations |
| MCP client connector | `lazytools.connectors.mcp` | Import another MCP server's tools into an agent | tests-only | mcp.md |
| External tool gateway | `lazytools.connectors.gateway` | External-tool gateway | tests-only | gateway.md |
| Connectors without MCP exposure | `lazytools.connectors.edgar`, `marketdata`, `fin`, `projects`, `pulse`, `regimes`, `web` | Library-level clients | tests-only | connectors.md |
| DB registry and artifact catalog | `lazytools.registry` (`KNOWN_DBS`) | Where each ecosystem DB lives; artifact search | tests-only | registry.md |
| Fake clients for tests | `lazytools.testing.fake_clients` | Deterministic connector fakes | tests-only | connectors.md |

## 12. Ops: ecosystem sync and repo tools

| Capability | How to call | What it does | Status | Docs |
| --- | --- | --- | --- | --- |
| Daily ecosystem realignment plan | `lazytools-ecosystem-sync check [--root R] [--manifest M] [--out-dir D] [--no-fetch] [--stale-days N] [--telegram]` | Read-only evidence on Git/GitHub, worktrees, production audit, disk; writes a dated plan; never applies actions; `--telegram` sends a 10-line summary | tests-only | ecosystem-sync.md |
| Release-train pin check | `python tools/check_pins.py --repo . [--manifest path.toml]` | Fails if a repo pins ecosystem siblings differently from `ecosystem/compatibility.toml` | tests-only | ecosystem-sync.md#release-train-consumer-pins |
| Compatibility manifest render | `python tools/render_compatibility.py`; files in `ecosystem/` | Renders `compatibility.md/json` from the TOML | tests-only | ecosystem-sync.md |
| Wheel metadata check | `python tools/check_wheel_metadata.py` | Checks packaged metadata | unknown | none |
| Install tagged release into job envs | `python tools/install_job_envs.py` | Installs a tagged LazyTools into idle InvestmentCommittee job environments | tests-only | ecosystem-sync.md |
| Docs API audit | `python tools/audit_docs.py` | Checks every doc page's imports and call keywords against this checkout | tests-only | none |
| Docs link check | `python tools/verify_site_links.py` | Verifies site links | unknown | none |

## On open branches (not on main yet)

Branch `dispatch-placement` (not read; listed from the owner's brief). Status for dispatch and watch is `live-tested` on that branch.

| Capability | How to call | What it does | Status | Docs |
| --- | --- | --- | --- | --- |
| `dispatch` upgrades | `lazytools-code-bridge dispatch --project ID ...` | Quota-router placement; per-task local clones; commits finished work outside the read-only `.git` sandbox; rejects done-without-commit; engine-crash retry; 60-minute lease; approval tickets surfaced as `approval <task> <ticket>` events | live-tested (on branch) | branch dispatch-placement |
| `watch` upgrades | `lazytools-code-bridge watch owner/repo#N ... [--request-review TEXT] [--no-ci]` | New events `review <head> clean\|findings`, `conflict`, `ready`; optional `--request-review TEXT`; `--no-ci` for repositories without CI by policy | live-tested (on branch); `--no-ci` unit-tested | branch dispatch-placement; docs/code-bridge.md |

---

## Known overlaps and gaps (read before building)

- Two task stores exist: `lazytools.projects` (Store keys, in use) and `lazybridge.control_plane.ControlStore` (own DB, not wired into LazyTools). Do not build a third.
- Trace viewing is LazyBridge-only (`ext.viz`, Session SQLite). The code bridge keeps its own job log/result files; there is no bridge-job viewer.
- `code_write` MCP tools (`codex_write`, `claude_code_write`) are synchronous and denied here; the bridge is the supported path.
- `lazytools-projects` has one verb (`close-task`); everything else on projects is MCP or library.
