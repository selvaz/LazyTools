# Writing briefs

A brief is the only context a delegated agent has. Most rework traces back
to a brief that left a decision open, pasted stale context, or let the agent
run for an hour without committing.

## Rules (each with its reason)

- **One deliverable per brief.** Two deliverables become one half-done PR.
- **Decisions already taken.** List the choices you made (library, schema,
  naming, error policy). An agent left to choose will choose differently
  from the reviewer and the next agent.
- **Explicit "implement now".** Otherwise many agents reply with a plan and
  stop, and the turn is wasted.
- **Paths, not pasted context.** Name files, functions and docs to read; the
  agent reads the current version, and the brief stays short.
- **First commit within minutes, then one commit per step.** The bridge only
  continues a timed-out turn after committed or pushed progress; uncommitted
  work in a long turn is lost on timeout.
- **State what is out of scope.** Agents widen scope to "help"; an explicit
  out-of-scope list keeps the diff reviewable.
- **Done-when is checkable.** Commands with expected results the lead can
  run without asking anyone.
- **End with a verdict line.** Ask for a final line starting with a status
  keyword, so results are machine-checkable:
  `STATUS: DONE`, `STATUS: PARTIAL <what is left>`, `STATUS: BLOCKED <why>`,
  and for reviews `VERDICT: APPROVE` / `VERDICT: CHANGES_REQUESTED`.
- **Say what the sandbox cannot reach.** For cloud jobs: no local data, maybe
  no access to some hosts; point to fixtures or recorded data.
- **No secrets, no personas.** Credentials never go in a brief. "You are an
  expert ..." lines add nothing.

## The bridge's brief check

`lazytools-code-bridge run` checks the task text before launching and prints
findings on stderr (`brief-check: error missing-goal: ...`). `--strict`
refuses on any error; `--no-brief-check` skips it.

| Code | Severity | Raised when |
| --- | --- | --- |
| `missing-goal` | error | no `Goal` heading |
| `missing-done-when` | error | no `Done when` heading |
| `missing-context` / `missing-constraints` / `missing-report` | warn | heading absent |
| `path-outside-cwd` | error | `--engine claude`: an absolute path outside `--cwd` (Claude reads only its repository) |
| `quoting-jq` | warn | a `gh ... --jq '<filter>'` command (quoted arguments file approval tickets) |
| `quoting-commit-message` | warn | a `git ... -m "<text>"` command (use `git commit -F <file>`) |
| `persona-line` / `step-by-step` | warn | "You are a ..." / "think step by step" |
| `brief-too-short` | warn | under 200 characters |

## Writer brief template

```text
# Brief: add retry with idempotency key to the export job

## Goal
Exports retry transient upstream errors and never write a duplicate file.
Implement now; do not stop at a plan.

## Context
- Code: src/service/export.py (ExportJob.run), tests in tests/test_export.py.
- Decisions taken: 3 retries, exponential backoff from 1 s, idempotency key =
  sha256 of (job id, partition). No new dependencies.
- Recorded upstream responses are in tests/fixtures/upstream/; the live
  endpoint is not reachable from your sandbox.

## Constraints
- Branch feat/export-retry from main; never push to main.
- Out of scope: the scheduler, logging format, other jobs.
- Commit within the first few minutes, then once per step, with
  `git commit -F <file>`. Use `gh ... --json <fields>` without --jq.

## Done when
- `python -m pytest -q` passes, including new tests for retry and no-duplicate.
- `ruff check src tests` is clean.

## Report
Diff summary, the full test command output, anything left undecided.
Last line: `STATUS: DONE`, `STATUS: PARTIAL <what is left>` or `STATUS: BLOCKED <why>`.
```

## Review brief template

```text
# Brief: review feat/export-retry at <head sha>

## Goal
Find defects in the retry change against the acceptance criteria. Report
findings only; do not edit, commit or push.

## Context
- Diff: `git diff main...feat/export-retry`. Contract criteria: 3 retries with
  backoff on 5xx; no duplicate file on retry.

## Constraints
- Out of scope: style nits that a linter would catch.

## Done when
- Every criterion has a verdict with file:line evidence.

## Report
Numbered findings with severity (blocker / should-fix / nit).
Last line: `VERDICT: APPROVE` or `VERDICT: CHANGES_REQUESTED`.
```

Prefer a synchronous reviewer for this brief (`routing-and-review.md`):
reviews make no commits, and a continuation-based runner reads that as no
progress.

## Rework brief

Resume the writer's `--session`, list each finding by number with the
decision ("fix", "won't fix: reason"), and keep the same Done-when. Before
launching, check whether the writer already pushed follow-up fixes to its
PR after the review comments — agents sometimes do, and a second fixer would
collide.
