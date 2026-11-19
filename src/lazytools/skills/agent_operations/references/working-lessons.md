# Working lessons

Twenty operating rules for a lead running coding agents. Each has the reason
in one line. They are general: apply them whatever the repository.

## Roles and review

1. **Writer and reviewer are different models from different providers;
   fix review findings in at most one or two rounds, then the lead decides
   and documents the rest.**
   Why: the same model shares its own blind spots, and unbounded review
   loops burn quota without converging.
2. **The lead coordinates and verifies: agents write, the lead checks results
   against real data and systems before merging.**
   Why: a confident agent report is not evidence; only a check you ran is.

## Briefs and results

3. **Briefs: one deliverable, decisions already taken, explicit "implement
   now", paths rather than pasted context, a first commit within minutes and
   then a commit per step, and an explicit out-of-scope list.**
   Why: long turns without commits are lost on timeout, and open decisions
   come back as rework.
4. **Ask for a final line that starts with a verdict or status keyword**
   (`STATUS: DONE`, `VERDICT: APPROVE`, ...).
   Why: results become machine-checkable instead of prose to interpret.
5. **Use a synchronous review tool for read-only reviews and the async
   bridge for writing.**
   Why: continuation-based runners need commits to progress, and a review
   makes none, so it times out as "no progress".
6. **Cloud sandboxes often cannot reach some hosts: say so in the brief,
   provide fixtures or recorded data, and run live smoke tests locally after
   the agent pushes.**
   Why: an agent that cannot reach a host either fakes the call or stalls.

## Merging

7. **Never merge with red checks; when CI is unavailable a full test run
   elsewhere replaces it and is posted on the PR; read every PR comment
   before merging; bind a review to the exact head commit.**
   Why: each of these is a way an unverified change reaches main.
8. **Rebase or merge main into a stacked PR before its base branch is
   deleted.**
   Why: deleting a base branch closes the PRs stacked on it.
9. **With squash merges, strip attribution trailers from PR bodies when the
   project forbids them, and check every PR body an agent wrote.**
   Why: the body becomes the commit message on main, claims included.
10. **After a batch of merges, run the full suite on main.**
    Why: branches green on their own can make a red main together.

## Data and production

11. **One source per concept: prefer an ordered fallback cascade with a
    declared source and status over reconciling several sources; never
    splice histories across sources; do not trust period-over-period values
    computed across different data refreshes.**
    Why: mixed-source numbers look plausible and are silently wrong.
12. **Production changes need explicit approval; check nothing is running
    before moving runtime code; record each deploy with a rollback line;
    run an official job once after deploy and verify its output.**
    Why: an unverified deploy is discovered by its users.

## Shell and environment

13. **Do not truncate gate output with pipes; remember that a `grep -c`
    returning zero exits non-zero and can break a shell chain silently.**
    Why: a hidden red step looks green.
14. **Clean up after yourself: temporary worktrees, database copies, test
    temp dirs.**
    Why: disk is a silent constraint that fails unrelated jobs later.
15. **Never poll in a tight loop: wait on the process or schedule a wake-up;
    run long jobs detached so they survive the session.**
    Why: polling wastes quota and context, and attached jobs die with the
    session.
16. **Limit parallel heavy jobs: many agents in parallel are fine in the
    cloud; locally keep heavy suites serial.**
    Why: parallel heavy suites on one machine starve each other into
    timeouts and flaky failures.
17. **State times with their time zone, and take the clock from the OS, not
    a shell `TZ` variable.**
    Why: a shell variable can silently differ from the system clock's zone.

## Structure

18. **Keep the plan and Gantt updated at each milestone; close tasks only
    with recorded evidence (checks + review).**
    Why: the plan is the hand-off to the next session; an unevidenced
    "done" is a guess.
19. **Turn what works into structure: code with tests, skills, templates.**
    Why: a written rule without a gate gets forgotten.
20. **Agents may silently push follow-up fixes to their own PRs after review
    comments; check the branch before launching a second fixer.**
    Why: two fixers on one branch duplicate work and conflict.
