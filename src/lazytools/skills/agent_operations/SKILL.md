---
name: agent-operations
description: |
  Use when an LLM assistant (Claude Code, Codex or similar) leads software
  work that other coding agents carry out through LazyBridge and LazyTools:
  delegating writes with the async code bridge (`lazytools-code-bridge run`,
  `--detach`, `wait`, `pending` / `approve` / `reject`, `--where cloud` for
  Codex Cloud and `apply`), quota-aware routing (`route`, `--tier
  basic|writing|thinking|critical`, opposite-engine review with
  `--review-of`), the projects layer (plans, contracts, Gantt, verification,
  `lazytools-projects close-task` with evidence), writing briefs, reviewing
  and merging agent pull requests, and the operating rules that keep a
  multi-agent workflow honest (separate writer and reviewer, evidence before
  merge, capped review loops, clean-up, safe waits). Skip for writing
  LazyBridge application code (use the `lazybridge` skill) and for one-off
  edits the assistant makes itself without delegating.
---

# Agent operations — running software work with coding agents

This skill teaches the **lead** (the assistant the human talks to) how to run
work that other coding agents do: plan it, brief it, route it to the right
engine and model, launch it without blocking, answer approvals, verify the
result against reality, get an independent review, merge safely and record
the evidence. Commands come from LazyTools (`lazytools-code-bridge`,
`lazytools-projects`) on top of LazyBridge engines (`ClaudeCodeEngine`,
`CodexEngine`). When the installed version disagrees with this file, the
installed `--help` and docs win: run `lazytools-code-bridge run --help` (or any other subcommand with `--help`).

All paths below are relative to this skill directory (`references/<file>`).
Examples use placeholders: `owner/repo`, `REPO` (a local checkout),
`JOB_ID`, `APPROVAL_ID`, `PROJECT_ID`, `brief.md`.

## Roles

- **Lead** — coordinates and verifies. Writes briefs, chooses tier and
  engine, answers approval tickets, reads diffs, runs the checks itself,
  decides on review findings, merges (or asks the human to). Never accepts
  an agent's report as proof.
- **Writer agent** — one engine (Codex or Claude Code) doing one deliverable
  in one repository, on a feature branch, committing per step.
- **Reviewer agent** — a *different* model and provider from the writer.
  Reports findings; does not fix them in the same turn.
- **Human** — owns production, approvals the lead may not give, and merges
  when the project requires a human merge.

## The loop (one deliverable)

1. **Plan and contract.** Record the task on the project plan and agree a
   contract before delegating: observable result, acceptance criteria, the
   exact check commands, allowed effects, risk, review required.
   → `references/projects.md`
2. **Brief.** One deliverable, decisions already taken, "implement now",
   paths instead of pasted context, commit within minutes then once per
   step, out-of-scope list, Done-when checks, a final verdict line.
   → `references/briefs.md`
3. **Route.** `lazytools-code-bridge route --tier writing --cwd REPO` shows
   which engine/model/effort quota allows, and why. → `references/routing-and-review.md`
4. **Launch detached.** `lazytools-code-bridge run --detach --tier writing --cwd REPO --task @brief.md --session feat-x --json`.
   Then run `wait JOB_ID` in the host's background mechanism, never a
   tight polling loop. → `references/code-bridge.md`
5. **Answer approvals.** `pending --json`; read each ticket's command and
   reason; `approve APPROVAL_ID` only what the brief authorises,
   `reject APPROVAL_ID --reason "..."` the rest.
6. **Verify yourself.** `result JOB_ID`, read the diff, run the full test
   suite and linters, check outputs against real data or systems. A `done`
   job is not acceptance.
7. **Opposite-engine review.** `run --tier writing --review-of JOB_ID ...`
   picks the engine that did not write; `human_review_required` means no
   eligible reviewer — never self-review. For a strictly read-only review
   use a synchronous review tool (`codex_code_review`,
   `codex_review_changes`, `claude_code_review`). Fix findings in at most
   one or two rounds, then decide and document the rest.
8. **PR and merge.** Green checks bound to the exact head commit, every PR
   comment read, PR body checked. Never merge red.
   → `references/merge-and-release.md`
9. **Close with evidence.** `lazytools-projects close-task PROJECT_ID N --check "..." --review "..." ...`
   and update the plan and Gantt. → `references/projects.md`

## Golden rules (always apply)

Each rule has its reason; the full list of twenty working lessons, each with
its one-line why, is in `references/working-lessons.md`.

1. **Writer ≠ reviewer.** Different model *and* provider — the same model
   shares the same blind spots. Cap review loops at one or two rounds.
2. **Evidence over reports.** Check results against real data and systems
   before merging — a confident summary is not a test run.
3. **Small, decided briefs with frequent commits** — a long turn without a
   commit is lost on timeout; the bridge only continues after committed progress.
4. **Machine-checkable endings.** The agent's last line starts with a status
   keyword (`DONE`, `PARTIAL`, `BLOCKED`, `FAILED`) so results can be parsed.
5. **Async for writing, synchronous for read-only review** — continuation-based
   runners need commits to progress, and a review makes none.
6. **Cloud sandboxes see only the remote repository** — give fixtures,
   run live smoke tests locally after the push.
7. **Never merge red; never truncate gate output.** A piped `| tail` can
   hide a failed step.
8. **Production needs explicit approval**, a check that nothing is running,
   a rollback line, and one verified official run after deploy.
9. **Wait, do not poll.** Detach long jobs; wait on the process or schedule
   a wake-up.
10. **Clean up** worktrees, database copies and temp dirs — disk is a silent
    constraint.
11. **Turn what works into structure** — code, tests, templates, skills. A
    rule without a gate is forgotten.

## Which reference to read

| Read this file | When the task involves |
|---|---|
| `references/code-bridge.md` | Launching, waiting, cancelling and reading jobs; approval tickets; detached jobs; timeouts and continuations; the per-repo lock and engine slots; Codex Cloud (`--where cloud`, `apply`) and the status of Claude cloud. |
| `references/routing-and-review.md` | `route`, `--tier`, `--engine`/`--model`/`--effort` overrides, `--needs images`, `--review-of`, `human_review_required`, `models`; synchronous review tools; capping review rounds. |
| `references/projects.md` | Plans on the board, contracts, schedule and Gantt, claims, verification, `close-task` with evidence, the brake and quota admission. |
| `references/briefs.md` | Writing a brief that passes the bridge's brief check, the verdict line, scope and commit cadence, review briefs, cloud briefs. |
| `references/merge-and-release.md` | PRs, CI, stacked PRs, squash merges, post-merge suite, production deploys and rollback. |
| `references/working-lessons.md` | The twenty operating lessons with their reasons, for review or onboarding. |

## Quick command card

```text
lazytools-code-bridge route --tier writing --cwd REPO                     # dry run: who, which model, why
lazytools-code-bridge run --detach --tier writing --cwd REPO --task @brief.md --session feat-x --json
lazytools-code-bridge wait JOB_ID --timeout 7200                            # in the host's background shell
lazytools-code-bridge pending --json                                        # approval tickets
lazytools-code-bridge approve APPROVAL_ID | reject APPROVAL_ID --reason "outside the brief"
lazytools-code-bridge result JOB_ID --json                                  # final text + workspace evidence
lazytools-code-bridge run --detach --tier writing --review-of JOB_ID --cwd REPO --task @review.md --json
lazytools-code-bridge jobs --all                                            # recover a lost job id
lazytools-code-bridge cancel JOB_ID --reason "stop this attempt"
lazytools-code-bridge run --where cloud --allow-cloud --engine codex --cwd REPO --task @brief.md --branch feat-x
lazytools-code-bridge apply JOB_ID                                          # cloud diff -> new worktree + branch, no push
lazytools-projects close-task PROJECT_ID 0 --check "pytest -q=pass:120 passed" \
    --review "No defects" --review-by codex --reason "checks and review pass" \
    --reviewer lead --worker claude --db projects.sqlite
```

## Installing this skill

The skill ships inside the `lazytoolkit` wheel. Copy it into Claude Code's
and Codex's skill folders with `python -m lazytools.skills.agent_operations install`
(`--to claude|codex|all`, or `--dest DIR`). To query it as a LazyTools
documentation skill instead, build the BM25 bundle with
`lazytools.skills.build_packaged_skill("agent-operations")`.
