# Pull requests, merging and production changes

## Before merging any agent PR

- [ ] All required checks are green **on the current head commit**. Never
      merge with red checks. **Why:** a red check merged is a broken main for
      everyone.
- [ ] If CI is unavailable, run the full test suite elsewhere and post the
      command and its full result on the PR. **Why:** an unrecorded local run
      is indistinguishable from no run.
- [ ] Every PR comment and review thread read, including bot comments.
      **Why:** blocking findings often arrive as plain comments, not reviews.
- [ ] The independent review is bound to the exact head SHA it read; any
      later push is re-checked. **Why:** a review of an older commit says
      nothing about the new lines.
- [ ] Check whether the writer agent already pushed follow-up fixes after the
      review before launching another fixer. **Why:** agents may silently
      push to their own PRs, and two fixers on one branch collide.
- [ ] The PR body is accurate: an agent-written body can claim tests or
      behaviour that are not there. Strip attribution or co-author trailers
      when the project forbids them — with squash merges, the body becomes
      the commit message on main.
- [ ] Gate output read in full. Do not pipe it through `tail`, `head` or
      `grep`. **Why:** a hidden red step looks green, and a `grep -c` that
      finds nothing exits non-zero and can silently break a `&&` chain.

## Stacked PRs

When PR B is based on PR A's branch, merge or rebase `main` into B (and
retarget it to `main`) **before** A's branch is deleted. **Why:** deleting a
base branch closes the PRs stacked on it, and reopening is not always
possible.

## After a batch of merges

Run the full suite on the updated `main`. **Why:** each branch was green
against an older `main`; together they can be red.

## Production changes

- Get explicit approval for each production change; approval for one change
  does not carry over to the next.
- Check that nothing is running (scheduled jobs, workers) before moving or
  replacing runtime code. **Why:** swapping code under a running job
  corrupts its output or state.
- Record each deploy with what changed, when (with time zone), and a one-line
  rollback command.
- After deploy, run one official job and verify its output against
  expectations, not just its exit code.
- Prefer one source per concept: an ordered fallback cascade with a declared
  source and status on every value, rather than reconciling several sources.
  Never splice a history from two sources, and do not compute period-over-
  period changes across different data refreshes. **Why:** spliced or
  cross-refresh values look valid and are wrong in ways no test catches.

## Clean-up

Remove temporary worktrees (`git worktree remove`), applied cloud
worktrees, database copies, test temp directories and stale branches once
merged. **Why:** disk fills silently, and a full disk fails unrelated jobs.
