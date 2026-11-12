# Quota-aware routing and independent review

## Tiers

The bridge picks an engine, model and effort from a capability ladder and
the live subscription quota of both engines.

| Tier | Use it for |
| --- | --- |
| `basic` | Mechanical edits, renames, small fixes, data plumbing with an exact spec. |
| `writing` | Normal feature work and most reviews. The default choice. |
| `thinking` | Design-heavy changes, tricky concurrency, migrations, debugging with unclear cause. |
| `critical` | Security-sensitive, irreversible or production-facing logic; final reviews of risky changes. |

The concrete models on each rung change over time. Do not hard-code them in
briefs or plans: ask the router.

```text
lazytools-code-bridge route --tier writing --cwd REPO           # human-readable explanation
lazytools-code-bridge route --tier thinking --cwd REPO --json   # decision, scores, exclusions, quota windows
lazytools-code-bridge models                                    # live availability + catalogue audit
```

`route` launches nothing. Its output starts with engine, model, effort,
rung and reason, then each engine's weekly and 5-hour usage, forecasts,
warnings and every exclusion (for example
`excluded codex: engine_at_concurrency_limit: codex 1/1 (running job ...)`).

## Launching with a tier

```text
lazytools-code-bridge run --detach --tier writing --cwd REPO --task @brief.md --session feat-x --json
lazytools-code-bridge run --detach --tier writing --engine codex --cwd REPO --task @brief.md --json
lazytools-code-bridge run --tier thinking --needs images --cwd REPO --task @brief.md
```

- `--engine codex|claude` restricts the eligible engines.
- `--model` / `--effort` override the pick; they are validated for the
  engine and recorded under `routing.override`. A model with a tier but no
  engine restricts routing to that model's provider.
- `--needs images` permits only Codex.
- `--tiers PATH` uses another catalogue; otherwise
  `~/.lazytools/model_tiers.toml`, then the packaged ladder.
- Missing, stale or exhausted quota **excludes** an engine — unknown is never
  treated as spare capacity. An applicable ceiling refuses the launch;
  forecast warnings do not.
- The job record keeps the full routing decision; `status` and `jobs` show
  tier, model and effort actually launched.

## Opposite-engine review: `--review-of`

```text
lazytools-code-bridge route --tier writing --review-of WRITER_JOB_ID --cwd REPO --json
lazytools-code-bridge run --detach --tier writing --review-of WRITER_JOB_ID --cwd REPO --task @review.md --json
```

- `--review-of JOB` (full id or unique prefix) allows only the engine that
  did **not** write that job. It requires `--tier`.
- If the opposite engine is ineligible (quota, ceiling, session pin), the
  command fails with `human_review_required`. Do not substitute a
  self-review: wait for quota, or get a human review.
- The bridge review job is still write-capable; "do not edit" is only an
  instruction. When the review must be strictly read-only — or when the
  runner would time out because a review makes no commits — use a
  synchronous MCP reviewer: `codex_code_review`, `codex_review_changes`
  (native diff review, `scope` = `uncommitted` | `branch` | `commit`),
  or `claude_code_review`. Pass the reviewer a model from the provider that
  did not write.

**Why:** the same model reviewing its own output shares its blind spots,
and a review that never commits looks like "no progress" to a
continuation-based runner.

## Running the review loop

1. Review brief: the diff or branch to read, the contract's acceptance
   criteria, what to look for, "report findings, do not edit", and a final
   line `VERDICT: APPROVE` or `VERDICT: CHANGES_REQUESTED` (see `briefs.md`).
2. Feed findings back to the writer's `--session` as a new brief listing
   each finding to fix.
3. Re-review once. **Cap at one or two rounds.** Remaining findings are the
   lead's decision: fix, defer as a tracked follow-up, or reject with a
   reason. Document the decision in the PR and the project note.
   **Why:** reviewers always find something; unbounded loops burn quota and
   drift scope without converging.
4. Bind the review to the exact head commit it read. A new push invalidates
   the review for the changed lines.

## Python API

`lazytools.routing.route()` is pure (catalogue, readings, budgets,
in-flight counts, fixed time in; decision out) and `recommend()` adds bounded
live quota reads. Use them when an orchestrator written with LazyBridge must
pick engines programmatically; see LazyTools `docs/code-bridge.md`,
"Choose a model from quota".
