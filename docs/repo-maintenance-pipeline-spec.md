# Repository Maintenance Pipeline — public, reusable specification

Status: **proposed architecture / implementation specification**, not shipped functionality.  
Target repository: **LazyTools** (package lazytoolkit), branch feat/codemap-python-adapter.  
Date: 2026-11-21. API and CLI names below are **proposed**, not existing imports or commands.  
Related current prototype: [CodeMap Python adapter](codemap.md). Evaluation evidence: [head-to-head PR #325](https://github.com/selvaz/LazyTools/pull/325).

## 1. Product contract

### Goal

Provide a reusable, open-source Python pipeline for the **continuous architectural maintenance of one or many Git repositories**. Its first consumer may be the Lazy ecosystem, coordinated by LazyCEO and visualized in LazyStudio, but the core must be independently usable by an unrelated developer on a single GitHub repository, a monorepo or a many-repo estate.

The pipeline observes repository revisions, refreshes structural context quickly using an existing open-source mapping engine, determines what changed, estimates effects within and across repositories, runs a bounded verification/review sequence, and produces auditable findings and optionally reviewable patches. It never silently assumes that a static graph proves compatibility.

**Primary product outcome:** agents start each maintenance task with a fresh, commit-addressed, compact architecture snapshot instead of reconstructing repository layout from scratch.

**Not the same as:** a code-search wrapper; a central domain database; a new graph engine; a crawler of arbitrary GitHub organizations; a replacement for tests; or permission for an LLM to merge/publish modifications unilaterally.

### Invariants (MUST)

1. Every report, map and impact assessment identifies the exact repository and **commit SHA** that was analyzed. A branch name alone is insufficient.
2. Repository access is explicitly configured and scoped; a pipeline task cannot expand the set of repositories, invoke arbitrary shell commands or read secrets outside authorized roots.
3. Default mode is **read-only**: index, compare, analyze, report. Writing source code, creating PRs and merging require independent, explicit capabilities and policy.
4. Mapping errors, incomplete coverage or stale snapshots are visible as **unknown/unverified**, never reported as clean or no impact.
5. Expensive inputs and outputs remain in content-addressed artifacts; LLM tools receive bounded summaries, references and precise paths instead of entire repos.
6. The same core pipeline runs without LazyCEO, LazyStudio, MCP, Telegram, Coolify or any specific deployment provider.
7. Mapping engines remain external credited dependencies behind a replaceable Python interface. Do not reimplement parser, graph ranking or embeddings inside LazyTools.
8. No automatic merges, deployments, dependency upgrades or force pushes in the initial public release.
9. Existing LazyTools project/artifact registry and every product's domain databases remain separate from this repository intelligence layer.
10. Fail closed on unknown authorization and run validation against the **same commit** that produced findings; a newer head requires reevaluation.

## 2. Users and entry points

**Local developer:** invokes a Python object or CLI against one checkout, gets a human- and machine-readable report, no LLM/API key required for structural mode.

**GitHub maintainer:** GitHub Actions runs on pull_request/push/dispatch, maps only known repositories from explicit configuration and comments or uploads an evidence report. A PR opened by automation is a proposal, never an auto-merge.

**Coding agent:** receives native LazyBridge ToolProvider calls to request a small fresh architecture map, find symbols, fetch relevant context and inspect possible impacts. No MCP dependency. A stable Python API makes tool selection portable between agents.

**LazyCEO integrator:** schedules and coordinates multiple pipeline jobs and approvals through a *downstream* adapter; LazyTools never imports LazyCEO.

**LazyStudio integrator:** visualizes run state, graph snapshots, findings, checkpoints and proposals through a downstream API/adapter; the core does not depend on its UI.

**Third-party adopter:** can use the framework on an independent repo with a minimal manifest, public documentation, versioned results and ordinary pytest. Lazy naming must not leak into required config.

## 3. Component boundaries and ownership

| Component | Package / layer | Responsibility | Status |
| --- | --- | --- | --- |
| RepoMapCLI | lazytools.codemap | Bounded, read-only wrapper of external RepoMap binary | Initial experimental adapter exists in PR #326 |
| RepoMapTools | lazytools.codemap | Native LazyBridge provider: overview/search/context/impact | Initial experimental adapter exists in PR #326 |
| Mapper protocol | lazytools.maintenance.mapping (proposed) | Engine-neutral index/refresh/search/map/impact API with freshness metadata | To build |
| Workspace and revision resolver | lazytools.maintenance.workspace (proposed) | Allowlisted local roots, immutable ref-to-SHA resolution, per-repo state | To build |
| Snapshot store | lazytools.maintenance.snapshots (proposed) | Atomic, versioned on-disk metadata and artifact references | To build |
| Change/impact planner | lazytools.maintenance.analysis (proposed) | Diff scope, graph neighborhoods, candidates, uncertainty, cross-repo links | To build |
| Pipeline executor | lazytools.maintenance.pipeline (proposed) | Deterministic state machine, retries, concurrency, idempotence, approvals | To build |
| Verification and reporting | lazytools.maintenance.verification, reporting (proposed) | Named checks, evidence, test results, redacted bounded reports | To build |
| Optional agent reviewer | lazytools.maintenance.reviewers (proposed) | Invokes injected LazyBridge agents with bounded context and budgets | Later |
| CLI and GitHub Action | lazytools.maintenance.cli + workflow examples (proposed) | Public no-vendor lock-in entry points | Later |
| LazyCEO / LazyStudio | External consumers | Scheduling, higher-level policy, UI and operator control | Integration later |
| MCP projection | Existing LazyTools MCP server | Mirror selected native tools without changing their semantics | Later, not first |

Python package boundaries MUST be one-directional: maintenance -> codemap and LazyBridge abstractions; integrations into CEO/Studio -> maintenance, never the reverse. The deterministic pipeline must remain usable without an LLM. CodeGraphContext is a possible alternate mapper for deep structural/relationship work, but must not be mandatory.

## 4. Repository inventory and identity

Input is an explicit manifest; do not enumerate a user's private organization by default. Each entry has a unique alias, local root, optional origin URL, ref policy and verification configuration. Resolve all work against exact SHAs, including submodules and generated code policy where applicable. Store public origin URLs and SHAs, never credentials. Resolve remotes with URL userinfo scrubbed.

Proposed v1 manifest:

~~~yaml
schema_version: 1
workspace:
  id: example-maintained-system
  state_dir: .repo-maintenance-state  # ignored; no secrets; not committed
  default_mode: read_only
  mapping:
    engine: repomap
    executable: repomap
    max_map_tokens: 1200
    max_context_chars: 16000
    embeddings: false
  repos:
    - id: api
      root: ../api
      ref: HEAD
      include: ["src/**", "tests/**", "pyproject.toml"]
    - id: web
      root: ../web
      ref: HEAD
      include: ["src/**", "package.json"]
  relationships:
    - from: web
      to: api
      kind: consumes
      evidence: explicit_manifest
  verify:
    - id: api-tests
      repo: api
      command_id: pytest-fast
  policy:
    allow_source_writes: false
    allow_pull_requests: false
    require_human_approval: true
~~~

**Important:** the pipeline must not execute arbitrary string commands from a manifest. A verification command_id resolves to an operator-approved command registry defined outside untrusted repository content; a public GitHub Action may offer a strictly predefined safe set. A manifest in a PR is untrusted unless approved.

All repository ids, roots and allowlisted ref policies are configured by an operator. Repository contents are not themselves instructions to the agent. Disallow symlink escape during walking and when opening changed files; avoid secrets, vendor directories and gigabyte-scale fixtures through explicit ignore settings.

## 5. Snapshot and freshness model

### Immutable snapshot identity

Each snapshot has at least these fields:

| Field | Meaning |
| --- | --- |
| workspace_id, repo_id | Configured identities |
| analyzed_sha | Full Git commit SHA, immutable |
| dirty_state | Clean or working-tree digest; dirty trees are rejected in CI and explicitly tagged locally |
| index_key | Hash of repo id, commit SHA, include/ignore rules, engine name/version/config, relevant model version |
| indexed_at, duration_ms | Observed clock metadata |
| engine, engine_version, capabilities | Provenance and available analysis operations |
| files_count, symbols_count | Reported counts, or null if unavailable |
| map_artifact | Content-addressed ref to bounded structural summary |
| index_artifact | Path/reference to engine-specific local index; never copied into model prompts |
| status | ready, partial, failed, stale or superseded |
| diagnostics | Bounded errors and coverage limitations |

Persistence may use local SQLite for metadata and content-addressed files for immutable artifacts. **This is a CodeMap-specific cache**, not a shared business DB. The adapter's engine cache stays engine-owned. Never duplicate large indexes into LLM-visible state.

### Refresh rules

- On initial run: resolve SHA, verify root allowlist, compute index key, index/refresh engine, persist a snapshot atomically.
- On repeat unchanged SHA with matching engine/config: reuse verified snapshot and report a cache hit; do not rebuild without reason.
- On a new SHA: determine changed paths via git diff between base and head; invoke the external engine's supported refresh/index operation and measure actual incremental behavior. Do **not** claim incremental updates unless the engine provides them and tests verify them.
- If an engine only supports full rebuild, rebuild and record strategy='full'. If the cost is low enough this is acceptable.
- Snapshot publication must be atomic: never expose a half-written index as ready. Lock by (repo, SHA, config) and allow bounded concurrency across different repositories.
- A running analysis pins SHA throughout. If remote HEAD moves, mark the result stale relative to latest head; preserve evidence and queue a new job. Do not silently rewrite the report's SHA.
- Cross-repo workspace snapshots are version vectors: one SHA per repo, not one global timestamp. An impacted repo may have a different revision, recorded explicitly.

**Freshness semantics:** 'current' means matches the requested immutable SHA; 'latest' additionally requires verification against the configured branch head at the time of the check. Offline mode may truthfully provide 'current-at-last-fetch' but not claim latest remote state.

## 6. Pipeline stages and state machine

The deterministic stage order is:

~~~text
EVENT / MANUAL START
    -> RESOLVE (repo allowlist + immutable SHAs)
    -> SNAPSHOT (reuse or refresh structural maps)
    -> DIFF (base SHA vs head SHA or initial inventory)
    -> IMPACT (direct + transitive candidates with provenance)
    -> SELECT (risk and relevant files, tests, dependencies)
    -> REVIEW (optional injected agents; no source writes)
    -> VERIFY (approved checks in pinned isolated worktree)
    -> REPORT (human-readable Markdown + JSON + references)
    -> [OPTIONAL] PROPOSE (patch/PR on separate branch; explicit write grant)
    -> [OPTIONAL] APPROVE -> VERIFY AGAIN -> HANDOFF
~~~

Phases before PROPOSE **must not mutate source trees**; engine caches and report artifacts are permitted in configured state locations. A reviewer may return findings but cannot redefine permissions or overwrite verified evidence.

Job states: queued, running, waiting_approval, completed, completed_with_warnings, failed, cancelled, superseded. Individual stages: pending, running, succeeded, skipped, failed. Every stage has duration, input/output evidence refs, resource counts, attempt number and bounded diagnostics. A failed or skipped critical stage makes the overall conclusion unverified.

Idempotency key: (workspace, vector of SHAs, ruleset hash, pipeline version, stage inputs). Repeated events must not create duplicate proposals or bypass approvals. Cancellation leaves durable provenance, cleans temporary worktrees and never marks the code clean.

### Trigger policy

- Manual: local API or CLI, one repo or whole workspace.
- CI: push and pull_request; compare PR base SHA and head SHA. Never write to an untrusted fork from a privileged context.
- Scheduled: periodic revalidation even without an event, optionally via LazyCEO or cron/GitHub Actions.
- Webhook: event may wake the pipeline but must be authenticated and de-duplicated; webhooks are not the source of truth for SHA.

Polling frequency and retention belong to operator configuration. Avoid doing expensive full maps for every trivial event without snapshot caching.

## 7. Change analysis and cross-repo impact

### Inputs and outputs

Input: list of changed paths and Git rename status, base/head revision, per-repo snapshots and optional user question. Record for each changed symbol/file: direct references, import/call neighbors (with confidence), relevant tests and interface exports.

Output: ordered ImpactFinding records with (repo_id, path, optional symbol/line, category, severity, confidence, evidence links, suggested checks, limitations). Include results even when no LLM is configured.

### Dependency evidence and confidence

No automatic engine guarantees complete cross-repo dependency edges from source AST alone. Sources of relationships in descending confidence:

1. Explicit operator-reviewed workspace manifest or package/dependency metadata with pinned version/range.
2. Direct imports and file-level usages detected by mapping engine and confirmed across configured roots.
3. Static call/import graph links (possible aliases and dynamic dispatch).
4. Heuristic semantic similarity; label as exploratory only.

An inference must not silently become a high-confidence dependency. Distinguish direct import breaks, behavioral coupling, data/schema contracts, APIs and generated artifacts; when the mapper cannot inspect a language or file, include an uncovered-area warning.

### Output minimization

The pipeline chooses smallest relevant graph neighborhood before retrieving source. Include path, symbol, a concise reason and citation to an indexed snapshot. A reviewer gets no more than a configured file/context/token budget by default, with explicit expansion on request and audit trail. A low-confidence guess cannot automatically select destructive checks or patch targets.

### Review roles (plug-ins, not mandatory engine features)

- Architecture reviewer: explains boundary changes and dependency risks.
- Compatibility reviewer: investigates public Python APIs, version constraints, schema migration and cross-repo callers.
- Test reviewer: matches affected features to existing tests and identifies missing regression cases.
- Security reviewer: checks permission escalations, handling of untrusted code/data and sensitive paths.
- Optional synthesizer: consolidates nonduplicated findings and prioritizes checks.

These are **roles**, implementable with ordinary LazyBridge agents injected by the caller, not a hardcoded dependency on Claude Code or Codex. A local deterministic reviewer must also work for CI without API keys. Agent disagreement stays visible.

## 8. Public interfaces (proposed contract, not implemented)

Preserve the existing RepoMapTools as a low-level native ToolProvider. Add a separate Python pipeline API, with versioned JSON result models. Illustrative usage only:

~~~python
from lazytools.maintenance import (
    MaintenancePipeline, Workspace, RepoSpec, RunOptions,
)

workspace = Workspace(
    repos=[
        RepoSpec(id="api", root="../api"),
        RepoSpec(id="web", root="../web"),
    ],
    state_dir=".repo-maintenance-state",
)
pipeline = MaintenancePipeline(workspace=workspace)
result = pipeline.run(
    RunOptions(mode="review", base={"api": "BASE_SHA"}, head={"api": "HEAD_SHA"})
)
print(result.report_markdown_path)
~~~

The initial stable interface should expose typed dataclasses or Pydantic schemas independent of FastAPI. Proposed capabilities:

| Operation | Contract |
| --- | --- |
| snapshot(repo_id, ref) | Explicit SHA and cache/index provenance |
| refresh(repo_id, ref) | Idempotent mapping operation and observed timing |
| compare(repo_id, base_sha, head_sha) | Changed paths/symbols with coverage metadata |
| review(workspace, change_set, reviewers=None) | Findings + suggested verification, no writes |
| verify(findings, command_ids, policy) | Recorded check results under approved execution policy |
| propose(findings, approval) | Creates separate worktree/branch and PR proposal, no merge |
| run(options) | Staged, resumable orchestration and report refs |

Return all run ids, SHAs, stage statuses, durations, version and coverage metadata. No success=true if required stages are unexecuted. Use deterministic JSON schema version 1; additive fields allowed, breaking changes require explicit migration. Error types distinguish engine unavailable, stale snapshot, unauthorized operation, incomplete analysis, verification failure and external provider failure.

**Possible CLI** (future): maintenance scan --config workspace.yml; maintenance review --base X --head Y --repo api; maintenance report --run-id ID. Do not declare these commands installed until implemented.

### Native tools and MCP

Keep low-level codemap_* tools as Python-native entry points. Later expose bounded maintenance_snapshot, maintenance_review and maintenance_report ToolProvider calls. Only after those contracts pass unit and integration tests should the existing LazyTools MCP provider registry mirror them; MCP must be an adapter and not create a separate engine implementation.

## 9. Verification contract and policies

Verification is performed in a detached/isolated checkout at the exact analyzed SHA (or patch proposal SHA), with filesystem scope and resource deadlines. The policy decides which approved command IDs may run; the agent may *suggest* tests but cannot authorize new executable commands. Network access is off by default and optional only through an explicit policy with egress limits.

Required checks for a code-change proposal:

1. Ensure the base SHA and workspace relationship metadata have not changed since impact analysis.
2. Validate the proposed diff applies cleanly in a disposable branch or worktree.
3. Run the operator-approved targeted tests and static checks. Capture exit codes, durations and redacted logs.
4. Run required regression suites or explain which were not executed. A test that failed to collect is not a passing test.
5. Re-map the candidate revision and re-evaluate affected dependencies as a post-change check.
6. Produce a PR body with changed files, affected repos, risks, covered/uncovered tests, exact SHAs and rollback guidance.

No operation may automatically merge or deploy. If no write approval exists, stop after report. If a policy allows auto-propose, it still must require a human to approve the PR. A write capability in a global provider should not automatically grant writes to every reviewer agent.

## 10. Reporting, evidence and observability

Every execution emits:

- report.json with schema_version, run/workspace ids, exact SHAs, engine versions, config digest, job/stage status, findings, coverage warnings, risk bands, verification evidence and costs;
- report.md with an executive summary, impacted symbols/files, likely regressions, test results, uncertain/uncovered areas and recommended actions;
- snapshot-manifest.json with per-repo index keys and cache provenance;
- optional patch.diff and pull-request URL only when explicitly authorized.

Reports must separate **observed facts** (exact Git diff, tests), **static inferences** (call graph), **model-generated suggestions** and **unknowns**. Every finding should point to a source path, snapshot SHA, line/range where available, and a specific reason. The output is bounded for model contexts; full but redacted artifacts are retained separately with a configurable TTL.

Log metrics: index cold/warm time, files/symbols indexed, graph coverage, retrieval latency, candidate-file hit rate, calls to each tool, prompt/completion/cache tokens if provided by engine, LLM cost if known, wall-clock duration, test pass/fail/skip/unavailable counts, invalidation rate, and CPU/memory peak when supported. Missing metrics remain null; do not manufacture zero values.

## 11. Security, privacy and public distribution

- Repositories may contain private code, untrusted instructions and malicious build scripts. Never send source text to remote models or hosted MCP servers without an explicit per-run policy and a documented privacy boundary. RepoMap itself is local by default; embedding model download has its own opt-out.
- Do not pass arbitrary environment variables, tokens or Git credentials into analyzer subprocesses. Credential installation and source fetches are separate trusted setup steps. Source-root symlinks and cache paths require containment checks.
- Executable provenance: pin tested version, record checksum/version when available and document supported platforms. License notices are maintained for upstream projects. No automatic hidden binary download by installing LazyTools.
- Local analysis uses least privilege. Execution of repository test code is **active code execution**, so run only trusted revisions or use a restricted runner; treat fork PRs as hostile.
- Approval applies to the exact proposed action, inputs, target repo, branch, base SHA and permissions; changing any material input invalidates the approval.
- Make retention/export/delete of snapshots and logs explicit, especially for private repositories. No cross-tenant shared caches or default telemetry upload.
- Protect against large outputs, decompression bombs, pathological graphs, infinite recursion, concurrent writes, and runaway costs with configurable limits.
- Preserve a reproducible, offline structural mode with no LLM credentials; optional review engines must declare when content leaves the local machine.

## 12. Performance and evaluation criteria

The observed two-repo benchmark measured fast RepoMap cold indexing and promising file localization, but **did not measure end-to-end saved tokens or patch correctness**. It is not proof that all repositories index in under a second, nor that multi-repo cross-dependency inferences are complete.

Evaluation must include at least:
- 20–30 representative coding/review/maintenance tasks on real code; split single-repo vs multi-repo and small vs large changes.
- Same exact Git SHA, tasks, model, context budget and test environment for baseline agents with and without CodeMap.
- Repeated runs to account for model variance; track cache-cold and cache-warm separately.
- Verified final tests, factual file/impact localization, token use including cached tokens, total wall time, tool calls, setup/refresh cost and peak memory.
- Negative cases: missing modules, renames, dynamic imports, ignored/generated files, cyclic dependencies, stale heads and an empty/unavailable engine.

Decision gates (targets, **not measured claims**):

| Gate | Proposed acceptance |
| --- | --- |
| Correctness | No statistically credible drop in tests passed or actionable finding precision vs baseline |
| Freshness | All findings tied to exact SHAs; stale heads detected in integration tests |
| Repeatability | 100% of test artifacts reproducible with pinned inputs and versions |
| Permissions | No unauthorized source mutation or cross-repo file access in adversarial tests |
| Efficiency | Aim for >=15% lower end-to-end token use on representative complex tasks; report actual estimate and confidence interval |
| Speed | Aim for >=10% lower overall wall time on complex tasks; record indexing and agent startup overhead |
| Usability | Public single-repo demo works with no LazyCEO, LazyStudio, MCP or commercial LLM subscription |

If token savings are small or speed decreases, make the mapping provider opt-in rather than default. Optimize successful task completion, not only raw retrieval accuracy.

## 13. Test plan

### Unit/contract tests
- Manifest schema, duplicate aliases, path traversal, symlink escape, ref validation and secret redaction.
- Deterministic SHA- and config-keyed snapshot hit/miss; cache invalidation; recovery after a truncated cache write.
- Stage status transitions, idempotent retries, cancellation, timeout, lock contention and resumed runs.
- Diff classification: add, remove, modify, rename, binary and generated file.
- Static graph finding provenance, confidence labels, uncovered language and empty search results.
- Multiple repos with independent version vectors and explicit cross-repo relationships.
- Review token and character budgets; fail-closed permissions; no remote data transfer in offline mode.
- Action allowlists, exact-input approvals, no unapproved write or merge.

### Integration tests
- Temporary Git repos with real commits: initial index, unchanged HEAD cache hit, new commit refresh, changed public interface and impacted consumer.
- RepoMap real-binary smoke on Linux plus supported Windows/macOS compatibility runs if claimed.
- End-to-end read-only PR event: SHA resolution -> snapshot -> diff -> impact -> report with no changes to checkout.
- Run a real approved test in sandbox; make one fail and ensure status never says green.
- Simulate branch head moving during review; prohibit stale proposal and trigger re-analysis.
- Optional agent reviewer: context bounds and provenance; deterministic fake LLM for CI, separate labeled live E2E.
- Later optional Studio and CEO integrations with no reverse dependencies.

### Public acceptance fixture
Publish a small, self-contained, **two-repository sample**: library A exposes a Python function; service B imports it. A PR changes the function signature. A new user runs the pipeline and sees a pinned SHA for each repo, candidate B impact, a targeted test suggestion and a reproducible report. No production secrets or Lazy-only assumptions.

## 14. Delivery phases and definition of done

**P0 — Python CodeMap foundation (existing experimental branch):** optional external RepoMap backend and four native LazyBridge tools; no MCP registration or forced runtime dependencies. Keep PR #326 draft until validated; separately track the missing index-refresh API needed by the maintenance pipeline.

**P1 — Public read-only maintenance MVP:** workspace manifest, SHA provenance, snapshot/refresh interface, Git diff, deterministic impact report, CLI and artifacts. Single-repo first, with minimal two-repo example. Definition of done: real Git commits and a second run prove cache freshness; fully offline deterministic mode works; no source mutations.

**P2 — Multi-repo continuous review:** version vectors, cross-repo dependency evidence, configurable triggers, impact ranking, CI integration, read-only report comments, idempotency and concurrency. Definition of done: a changed public API in repo A identifies affected repo B with traceable evidence and verified bounds.

**P3 — Verification and optional coding-agent review:** approved test registry, isolated execution, optional injected reviewer roles, bounded model context, accurate usage/cost metrics, benchmarks against no-map baseline. Definition of done: recorded PASS/FAIL/UNAVAILABLE rather than ungrounded success claims, and published paired experiments.

**P4 — Proposed repairs under operator control:** separate worktree/branch, optional PR generation, impact recheck after patch, human approval; no automatic merge. Definition of done: correct stale-head rejection, rollback behavior and audit evidence for a code proposal.

**P5 — Ecosystem and product integrations:** LazyCEO schedules jobs and manages approvals; LazyStudio exposes an installable, configurable pipeline with status/report/revision review and reusable export; optional MCP projection is added only after Python contracts stabilize. Definition of done: the public CLI example and Python API continue to work unchanged without either product.

Explicit non-goals for MVP: autonomous merging, replacing all CI/CD tools, hosting a shared multi-tenant source-index service, embedding raw entire repositories into prompts, full dependency correctness for dynamic languages, automatic coding-agent self-revision, or SaaS billing.

## 15. Open decisions before implementation

1. Select the versioned workspace manifest location and storage format (YAML requires an optional parser; TOML is available in Python 3.11 stdlib).
2. Choose local snapshot metadata implementation (SQLite vs small atomic JSON manifests) after testing concurrency and crash recovery.
3. Specify whether one reindex call per repository is sufficient for multi-repo semantic search, or whether to run a global workspace refresh; test behavior with RepoMap 1.9.0.
4. Define capability-based approval integration to existing LazyTools projects/CodeBridge without taking a dependency on their domain registries.
5. Decide minimal public CI permissions, supported operating systems and how offline/remote-LLM boundaries are displayed in Studio.
6. Determine retention policy and trace-artifact format before accepting private repository scans.
7. Choose the first public two-repo fixture and record its initial baseline evaluation.

These are intentionally visible decisions, **not unimplemented behavior presented as already working**.

## 16. Suggested first implementation slice

Start with only workspace schema, immutable SHA resolver, a pure mapper protocol and a read-only snapshot/report API. Extend RepoMapCLI internally with an explicit index/refresh operation only after verifying upstream flags against a real binary. Create one integration test with a temporary Git repo and two commits: prove it refreshes at the second SHA, marks the first snapshot stale against HEAD, returns no source changes and reports real index timings.

Once this succeeds, add the simple two-repo dependency fixture. Do not start automatic patch generation before deterministic read-only tests and evidence contracts are green.
