# Async Code Bridge

Search before you build: check the [capabilities catalog](CAPABILITIES.md) for existing commands and tools before implementing a new one.

`lazytools-code-bridge` (`lazytools.code_bridge`, also runnable as
`python -m lazytools.code_bridge`) is an **asynchronous** path to the same
Codex / Claude Code write access that `codex_write` / `claude_code_write`
(see [Code Support Agent](code-support/index.md)) expose over MCP.

Install with the `engines` extra — `pip install "lazytoolkit[engines] @ git+https://github.com/selvaz/LazyTools.git@v0.10.13"` —
when you use `--engine claude` or `--tier`: both need the Claude Agent SDK (Claude engine and
Claude quota reading). Without it Claude jobs fail and quota-aware routing sees Claude as unreadable.

## Job lifecycle

Long synchronous MCP calls block the caller and can outlast the host's transport
timeout. `run --detach` launches an independent worker, prints the job id, pid
and log path, and returns immediately. Follow it with `wait`; a killed waiter
can be restarted without stopping the worker. OS shutdown can still stop jobs.

### Foreground jobs and a host's background waiter

A foreground `run` remains a child of its launching shell. If a coding host
reaps that shell or exits, the worker can die with it; a later run reclaims the
stale repository lock and records interruption. `--detach` starts a separate
worker process/group (outside the launcher's Windows job object where allowed).
Its launch command finishes immediately, so a host's launch-completion notice
does not mean the work has finished.

When the host supports background shell work, launch `wait JOB_ID` there and
let the host's normal process-completion notice report that waiter's exit.
In Claude Code this is the Bash tool's background option. Keep polling
`pending --json` while it waits; the notification channel belongs to the host,
not a bridge push service. A killed waiter can be restarted independently.
Foreground job ids are printed before completion so they can be polled too;
detached launch JSON gives the id, pid and log path at once.

| Command | What to retain |
| --- | --- |
| `run` | Foreground: job id on stdout's first line, then final result; detached `--json`: one launch object |
| `pending --json` | `approval_id`, `job_id`, gist, command/reason when available, creation and expiry |
| `approver-credential init` | Provision the local owner credential for a typed approval registry |
| `approver-credential provision-automatic PROJECT_ID` | Provision the project's automatic approval cards |
| `approver-credential autonomy --mode MODE [--preset NAME]` or `--show` | Configure or inspect the owner-delegated [autonomy modes](#autonomy-modes) |
| `approve` / `reject` | Decide a pending ticket. Rejection requires `--reason` |
| `status` / `result` | Status or full job record/text; `--json` preserves metadata |
| `wait` | Terminal result; exit 3 on waiter timeout, 1 when process disappears before final record |
| `jobs --all` | History, including finished jobs, for recovering a lost id |
| `cancel` | Stop the process tree, close open tickets, retain interruption and workspace evidence |
| `autonomy` | Report the percentage of finished jobs with no manual intervention and group intervention causes |

### Machine-readable ids and multi-job waiting

Pass `--json` to `run` for a single JSON object on the **first stdout line**.
It contains the full `job_id`, eight-character `short_id`, `where`, `engine`,
and `cwd`; detached launches also include `pid`, while cloud submissions
include `task_url`. Without `--json`, the existing human output is unchanged.
For cloud submissions, `task_url` is the machine-readable alias of `cloud_url`.
`jobs --json` emits one job per JSON line, which is both stream-friendly and
safe from host-console encoding limitations. Human `jobs` output replaces
unencodable characters rather than crashing on legacy Windows consoles.

An orchestrator can wait on several jobs without polling each one:

```console
lazytools-code-bridge wait-any JOB_ID JOB_ID --timeout 600 --json
lazytools-code-bridge wait-any --all-active --events done,failed,approval,landed,interrupted --json
lazytools-code-bridge wait-any JOB_ID JOB_ID --loop --json
```

`wait-any` snapshots the named jobs (or all jobs active at startup) and emits
`{"event", "job_id", "state", "approval", "pr_url"}`. It exits 0 after the
first selected event, 3 on timeout, and 2 for invalid arguments or unknown
jobs. Approval details contain `approval_id`, `command`, `summary`, and
`reason`, and a pending ticket is emitted only once per invocation. `--loop`
prints JSON lines until every watched job is terminal. For cloud jobs the
waiter refreshes provider state, reports failures, and performs an owed
`--land` operation just like `wait`, producing `landed` with its PR URL.
Its output is always JSON lines; `--json` is accepted for consistency and does
not change that default format.

`wait-any` is the single event stream for coordinating multiple jobs: consume
its JSON lines for completion, failures, approvals, landings, and interruptions
instead of maintaining a separate poller per job. The dispatch command also
supports `--json` to emit structured task events as JSON lines.

The event computation lives in `lazytools.code_bridge._events.iter_events`.
`wait-any` calls it in its polling loop. The `bridge_events` MCP tool calls the
same function through `_events.poll(cursor)`, which keeps the stream state in a
cursor between calls (see [mcp-server.md](mcp-server.md#fleet--read-only-code-bridge-views)).
There is one implementation, so both channels report the same events.

## Dispatch task graphs

`dispatch` runs a project task graph through bridge jobs. A shared review type
defines the review task contract used across task creation and dispatch. Use
`--knowledge-db PATH` to enable the lessons loop: matching lessons are supplied
to tasks, and useful outcomes or rejected approval reasons can be saved back
for later dispatches.

To convert a finished bridge job into a LazyBridge replay session, run:

```console
python tools/bridge_job_to_session.py JOB_ID --db JOBS.sqlite --output REPLAY.sqlite
```

The converter rejects unfinished jobs and reports event types it could not
normalize; the resulting session can be inspected with LazyBridge replay tools.

Read each ticket's command and reason before deciding. An unanswered ticket
expires as rejection after `LAZYTOOLS_CODE_BRIDGE_TICKET_TTL` seconds.
Tickets and results use `~/.lazytools/code-bridge.sqlite` by default;
`LAZYTOOLS_CODE_BRIDGE_DB` or `--db` selects another Store. Use the same Store
for every lifecycle command. Logs are under the Store's results directory
(default `~/.lazytools/results/`).

## Publish an accepted review

`publish` opens a draft pull request for a completed writer task whose current
attempt has an accepted independent review. Use the full writer task ID
(`project/task`), its local repository with a GitHub `origin`, and the reviewed
branch. Configure `LAZYTOOLS_PROJECTS_STORE_DB` for the projects task service
and initialize the selected approval registry's owner credential once from the
operator shell. Publication uses `lazybridge.ext.receipt` with Ed25519 support.

```console
lazytools-code-bridge approver-credential init
lazytools-code-bridge publish --task project/task --repo ./repo --branch feature/reviewed
lazytools-code-bridge approve TICKET_ID
lazytools-code-bridge publish --task project/task --repo ./repo --branch feature/reviewed
lazytools-code-bridge verify-receipt RECEIPT_ID --live
```

The first invocation files a typed ticket and exits 1 with a signed
`approval_not_decided` receipt. Read the repository, branch, commit and base
in the pending approval before deciding. The default base is `main`. Use
`--base` to specify another base or `--ticket ID` to resume with a specific
ticket. Use the same `--db` and `--registry-db` selections for initialization,
approval, and publication when using an isolated registry.

The executor refuses `main`, `master`, the chosen base and the repository's
default branch as publication heads. The head must match the writer task's
assigned branch when one is recorded. Otherwise, the receipt records an
`assigned_branch` check with status `not_configured`. The executor checks the
local head again before consuming the decision.
The executor looks up PRs from the same repository and head branch before
pushing. It refuses a closed, merged, or ready-for-review PR, and requires an
existing open draft to target the approved base. It pushes the reviewed object
ID with an ordinary fast-forward push, opens a draft if needed, then polls
GitHub for the reviewed head for up to 30 seconds. A published receipt records
the PR number and head.
A head mismatch or a PR that is no longer an open draft produces a signed
refusal with the PR URL and a null `pr_number`. The projects store records
the observed PR number alongside the signed refusal. A replay returns the
stored receipt or an `approval_already_consumed` refusal when the first process
has yet to save its outcome. The ticket stays consumed if the push fails, draft
creation fails, or the process stops after consuming approval. A retry requires
a new approval.

Use `publish --retry-ticket OLD_TICKET_ID` to file a fresh approval for the same
reviewed head after `pr_create_failed`, `pr_head_mismatch`, or `pr_head_unavailable`.
Approve the new ticket and publish with `--ticket NEW_TICKET_ID`. After either
head read-back refusal, the new ticket verifies the same draft PR and records
the result without pushing again.
The governed runner accepts the same `--retry-ticket` option and resumes with
`publish --resume` after approval.

The projects store retains both published and refused receipts. Each also has a
`receipt.json` under `results/<writer-job-id>/publication/<receipt-id>/`.
For attempts without a writer job, the path starts at `results/publication/`.
After saving a successful receipt, the executor posts its ID and issuer
fingerprint on the draft PR.

The issuer private key defaults to
`~/.lazytools/receipt-issuer/ed25519.pem`. Set `LAZYTOOLS_RECEIPT_ISSUER_KEY`
to use a different location. The executor exports the public key beside it
with a `.pub.pem` suffix. Windows does not enforce the `0o600` private-key
mode. Private-key protection relies on the containing folder's ACL, which
should restrict access to the issuer's user account. The offline verifier
accepts a stored receipt ID or a `receipt.json` file and an optional
`--public-key PEM` from a trusted issuer.
It uses the local public-key export by default. For published receipts,
`--live` additionally compares the signed head with the PR head.

The receipt reports the accepted review's independence mode, launch and
admission evidence, and policy metadata available in the bridge job store.
For a refusal, the publication head records the observed local branch head
or, for `pr_head_mismatch`, the head read back from GitHub. The review and
approval target fields keep their original values. A reviewer verdict of
`unavailable` indicates absent independence evidence and leaves
the required version 1 independence field at its placeholder value. The
projects MCP provider exposes `receipts_get` and `receipts_verify` as
read-only tools.

## Governed task runner

Run `tools/governed_task.py` with `python -I` from an interpreter that has a
released LazyBridge installed. Use a separate checkout for the candidate.
Isolated Python startup excludes the current directory and `PYTHONPATH` from
imports. The script adds the tools and LazyTools source directories from the
runner checkout and calls the bridge in that same interpreter. The runner
clears the caller's `PYTHONPATH` before launching subprocesses and passes the
saved paths only to checks.

Required checks run as subprocesses in a fresh worktree of the candidate
commit. Each check gets that worktree as its working directory and gets the
worktree root and its `src` directory, if present, on `PYTHONPATH`. Absolute
entries in the caller's `PYTHONPATH` under the writer checkout are remapped to
the check worktree. Match the repository's CI gate in the brief's `Checks`
block or with repeated `--check` options. Include the full test suite and
any lint, format and pre-commit commands the repository runs in CI. The
default timeout is 600 seconds per command. Use `--check-timeout SECONDS`
on `start`, `review` or `rework` to set the default for the task. Append
`::timeout=N` to a command in the brief or a `--check` option to override
its timeout individually.

The check environment preserves `HOME` and `USERPROFILE` when present so
tools can resolve the user's home directory.

Run `publish --resume --ci-gate` after approval to wait for GitHub's check
rollup for the exact published commit. The runner waits up to 30 minutes
for a nonempty result, exits nonzero if the gate fails, and writes the
result to the trial ledger with the PR number, head SHA, and receipt ID.
Repeat the command after a GitHub CI rerun to refresh the ledger without
pushing again. The signed publication receipt records the draft and its head.

If the bridge refuses a launch before it creates a job, the runner records the
launch error and releases the task claim. Use `recover` if a writer or review
claim is still open after an interruption. Recovery handles claims with no job
and claims with a verified terminal bridge job. It refuses while a job is
active. When recovering a writer, it commits any staged, unstaged, and
untracked changes in a local WIP commit on the assigned branch and records its
SHA in the recovery evidence. Ignored files stay in the worktree. Recovery is
safe to run again. Retry a review with `review`, or use `rework` to continue the
writer from the current branch head.

```console
python -I tools/governed_task.py recover --project trial-example
python -I tools/governed_task.py review --project trial-example
python -I tools/governed_task.py rework --project trial-example --brief @fix.md
```

To start a rework from an operator-supplied failure, such as a CI log after
publication, run `rework --project trial-example --reason @ci.log`. This
reuses the original brief and retained checks. Pass `--brief @fix.md` if
the writer needs updated instructions.

The first review starts the opposite-engine conversation `<project>-review`.
Both `review` and the review in `rework` reuse the latest named reviewer
conversation. Under NC-16, a re-review may block on unresolved previous
findings, issues introduced by the fix, or critical security issues. Use
`--fresh-review` for a full review in a new conversation. State from earlier
runners without a named review starts a new conversation on its next review.

## Navigator R1: the merge-ready loop

Run the production loop with one check flag per required command:

```console
lazytools-code-bridge navigate --repo OWNER/REPO --pr 277 \
  --reviewer-cmd "review-wrapper" --checks "pytest" --checks "ruff check" \
  --max-rounds 3 --fix-engine codex
```

The reviewer must post `<!-- independent-review v1 -->` and print a JSON object
with an explicit `findings` list. The bridge reads the marker and Codex bot
inline findings for the exact head through `gh api`. Checks run only in a
temporary detached worktree. Exit 0 means merge-ready, 3 means owner escalation
(P0/P1, an unknown severity, or the round cap), and 1 means an operational,
review-contract, independence, or check failure. The command never merges and
never pushes the default branch.

`lazytools.code_bridge._navigator.Navigator` is the provider-neutral state
machine for taking a delegated cloud job to **merge-ready**. A backend first
resyncs a legacy poll-failed job from provider truth and lands it on its pull
request. The navigator then requests an independent review of the exact pushed
head. Every new head is reviewed once. The navigator accepts `different-model`,
`opposite-engine`, and `same-provider-fresh` and records `review_independence`
in its state and any escalation. A backend may use `run_reviewer_command` for a
configured reviewer executable. The command receives `LAZYTOOLS_REVIEW_HEAD`,
`LAZYTOOLS_REVIEW_PR`, and `LAZYTOOLS_AUTHOR_MODEL` and returns JSON such as:

```json
{"reviewer_model":"claude","findings":[{"severity":"P2","message":"add a boundary test"}]}
```

Include the reviewer job's `review_independence` field when available. Without
routing evidence, the navigator compares the reviewer and author model names.

Non-critical findings produce a focused fix brief and a relaunch on the same
PR, followed by a review of the new head. The default cap is three review
rounds. Reaching it creates an explicit `round-cap` escalation record and exits
nonzero. A P0, P1, or unrecognised-severity finding immediately creates an `escalated` owner record and
exits nonzero; it is never silently sent through another autonomous fix round.
Changing PRs, reusing a reviewed head, or failing to establish reviewer
independence also fails closed.

A clean review is not enough to close the task. The backend runs all contract
checks, recording every result. Navigator R1's standard checks are
`pytest tests -k code_bridge`, `ruff check`, and
`mypy src/lazytools/code_bridge`. The first failure stops the run with
`checks-failed`; only a clean review on the current head plus all-green checks
produces `merge-ready` and exit status zero. The loop never merges and never
pushes to `main`: the existing `safe_merge` gate remains the only landing path.

`--task @brief.md` reads a UTF-8 task file rather than treating it as literal
text; detached launch resolves its path before passing it to the worker.
The final text is also saved to a result file under the results directory;
retain the path printed at completion as an independent copy if the Store is
unavailable. The log contains worker output, while `result --json` contains
the complete job record. Recover a lost id with `jobs --all`.

## Watching pull requests

Lead sessions and LazyCEO can follow pull requests without maintaining their
own polling loop:

```console
$ lazytools-code-bridge watch owner/repository#123 owner/other#456 --until 18:30 --interval 60
```

The watcher uses `gh api` REST calls (not GraphQL) and prints one line for each
new comment, inline review comment, independent-review verdict, completed
success/failure check, head change, merge, or close. Head-bound review events
on an obsolete PR head are ignored; verdict markers for an older head are
reported with `stale=true`. Add `--json` for one JSON object per line. `--until HH:MM`
uses the local clock and exits **0** at that time; it exits **2** when every
watched PR merges or closes first, and **1** on an error.
The date for `--until` is always today in the local timezone; if that clock
time has already passed, the watcher exits immediately rather than waiting
until tomorrow.

`ready` needs green CI, confirmed mergeability and a clean or non-blocking
bot review (the bot's latest structured state wins over receipts). For a
repository without CI by policy, pass `--no-ci`: `ready` then reports
`checks=none` and needs only the review and mergeability; CI that does report
must still be green.

Progress is stored per PR in the same bridge Store selected by `--db` or
`LAZYTOOLS_CODE_BRIDGE_DB`, so restarting the command does not repeat events.
On the first poll with a fresh Store, comments and other matching events that
already exist on the current head are emitted before progress is recorded.
The default polling interval and minimum are 60 seconds. Smaller `--interval`
values are clamped to the minimum. Administrators can override that minimum
with `LAZYTOOLS_CODE_BRIDGE_WATCH_MIN_INTERVAL`; its value is a positive
number of seconds.

The first stdout line from a non-JSON `run` is always the 32-hex job id. This
is a stable scripting contract: pre-launch brief-check diagnostics, including
warnings, are written only to stderr, so commands such as `run ... | head -1`
can safely capture the id.

## Autonomy report

Navigator measures a job as autonomous when it reaches a terminal state
(`done`, `partial`, `failed`, or `interrupted`) without an operator command
touching it. After an action succeeds, the bridge appends an intervention entry
for `resync`, manual `cloud-sync`, `apply`, `apply --land`, manual
`approve`/`reject`, `cancel`, a session rename, or a relaunch matching a failed
or interrupted job's repository and brief in the previous 24 hours. Scheduled
syncs use `cloud-sync --scheduled` (also the default) and do not count; use
`--manual` for an operator-triggered sync. Each operator command accepts an optional
`--reason` (a rejection continues to require one), so the durable record keeps
both the command kind and the operator's explanation. Existing Store rows need
no migration and count as having no interventions.

Report an ISO week or a half-open UTC date range. Dates and default bounds use
UTC, and `--until` is the first date excluded:

```console
lazytools-code-bridge autonomy --week 2026-W46
lazytools-code-bridge autonomy --since 2026-11-10 --until 2026-11-11 --json
```

The denominator is every job that finished in the range. The report prints the
autonomous count and percentage against the 90 percent target, followed by the
number of intervention events grouped by command kind. JSON output contains
`finished_jobs`, `autonomous_jobs`, `autonomy_percent`, `target_percent`,
`target_met`, and `intervention_causes`. With no finished jobs, the percentage
and target result are `null`. The report also includes `card_approvals_by_class`,
which counts card-use receipts by approval time in the same UTC range. A
running job can contribute receipts as soon as the queue records them.

## Claude writes, Codex reviews

This toy flow starts with a deliberately incomplete deployment guard and an
independent boundary test. Use a fresh directory, local runtime logins and the
`engines` extra. The tutorial's model turns are real when run normally; the
repository's docs verifier executes these steps with mock engines/telemetry.
It is a functional example, not a benchmark.

Create the fixture from your workspace root (save this block as `setup_toy.py`
and run `python setup_toy.py`):

```python
from pathlib import Path
import subprocess

repo = Path("toy-tandem")
repo.mkdir()  # refuses to overwrite an existing fixture
(repo / "deployment.py").write_text(
    "def deployment_allowed(day):\n    return True\n", encoding="utf-8",
)
(repo / "test_deployment.py").write_text(
    "import unittest\nfrom datetime import date\n"
    "from deployment import deployment_allowed\n\n"
    "class Boundaries(unittest.TestCase):\n"
    "    def test_week_boundary(self):\n"
    "        for day, allowed in [(9, True), (10, False), (11, False), (12, True)]:\n"
    "            with self.subTest(day=day):\n"
    "                self.assertEqual(deployment_allowed(date(2026, 10, day)), allowed)\n",
    encoding="utf-8",
)
for args in (["init"], ["add", "."],
             ["-c", "user.name=Docs Demo", "-c", "user.email=demo@example.invalid",
              "commit", "-m", "Add deployment fixture"]):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
```

Launch Claude with an explicit model/effort and a named native session. Run
these commands from the workspace root. Copy the printed job id into
`WRITER_JOB_ID` in later commands (these uppercase ids are placeholders):

```sh
lazytools-code-bridge run --detach --engine claude --model sonnet --effort high --cwd toy-tandem --task "Implement deployment_allowed(day): weekdays true, weekends false. Preserve the independent test; run python -m unittest discover -v. Do not commit." --session deployment-writer --timeout 120 --json
lazytools-code-bridge pending --json
```

If a ticket appears, inspect it and run `lazytools-code-bridge approve APPROVAL_ID`
only for an authorized action. Use `reject APPROVAL_ID --reason "Outside the task"`
for a refusal. Typed tickets require [one-time operator setup](#operator-credentials).
No ticket is required for every edit: the approval policy governs escalations.
Poll pending while the job runs, then:

```sh
lazytools-code-bridge wait WRITER_JOB_ID --timeout 180
lazytools-code-bridge result WRITER_JOB_ID --json
lazytools-code-bridge route --tier writing --review-of WRITER_JOB_ID --cwd toy-tandem --json
lazytools-code-bridge run --detach --tier writing --review-of WRITER_JOB_ID --cwd toy-tandem --task "Review the uncommitted deployment guard against Friday, Saturday, Sunday and Monday. Report defects and test gaps. Do not edit or commit." --session deployment-review --timeout 120 --json
```

Wait for the writer before launching its reviewer: both jobs use the same
repository lock. Routing chooses Codex because the producer was Claude.
If Codex quota is unreadable/exhausted, review refuses with
`human_review_required`; do not substitute a self-review. Copy the new id into
`REVIEW_JOB_ID`, handle any new tickets, and finish:

```sh
lazytools-code-bridge wait REVIEW_JOB_ID --timeout 180
lazytools-code-bridge result REVIEW_JOB_ID
python -m unittest discover -s toy-tandem -v
git -C toy-tandem diff -- deployment.py test_deployment.py
lazytools-code-bridge jobs --all --json
```

Expected check output ends in `OK` and covers Friday/Saturday/Sunday/Monday.
Inspect the actual diff and reviewer findings; a successful job is not an
acceptance verdict. Record those checks and the opposite-engine review in
[project verification](projects.md#contracts-and-verification).
The bridge's review job is still write-capable: "do not edit" is a task
instruction. Use the [read-only MCP reviewers](code-support/index.md) when
review permissions must prohibit writes.

For rework, launch another Claude job with `--session deployment-writer` and
include the findings in its task. Reuse `deployment-review` for the Codex
follow-up. Native history stays with its engine; share the diff and findings
between engines. To reverse the flow, choose Codex as writer and a fresh
writer alias: tier-based `--review-of` will then require Claude.
For an active job outside the accepted scope, use
`cancel ACTIVE_JOB_ID --reason "Stop this attempt"`. A 120-second engine
request timeout records failure evidence; `wait --timeout 180` only bounds
waiting and does not cancel the job. Read that evidence before starting over.

## Timeout, workspace evidence, and cancellation

### Committed progress survives turn timeouts (R12)

`--timeout` is a **per-turn** budget (default 3600 seconds), including time
spent waiting for approvals. A durable turn timeout is not itself a job
failure. Before each turn the worker snapshots HEAD and reads origin's ref
for the job's starting branch with `git ls-remote --heads origin refs/heads/BRANCH`; after the turn it records the new
commit SHAs and changed remote refs. These reads do not fetch or change the
workspace. A failed snapshot is recorded as unavailable evidence, never as
an empty baseline that would count historical commits as new work.

| Transition | Meaning |
| --- | --- |
| `running` → `awaiting_approval` → `running` | An ordinary ticket needs an answer |
| `running` → `done` | The engine finished successfully |
| Timeout with a new commit or pushed ref → `checkpoint` → `running` | Resume the same durable thread with a fresh per-turn budget |
| Timeout without a new commit or pushed ref → `failed` | Error begins `no progress:`; dirty files alone do not count |
| Timeout with progress after the continuation budget is spent → `partial` | Committed work is retained for review |
| Wall cap reached → `partial` after progress, otherwise `failed` | No further continuation starts |
| Explicit cancellation or worker interruption → `interrupted` | Existing cancellation behavior |

`run --max-continuations N` defaults to **3** automatic continuations after
the initial turn; `0` disables continuation and **10** is the maximum.
Each continuation sends exactly:

> continue from the last commit; re-read the brief's Done-when; do not redo committed steps

The worker keeps the same engine instance, thread/session, approval gate,
repository lock and engine slot. A timeout closes that turn's outstanding
tickets. Before another turn, it seals approvals, cancels/drains outstanding
callbacks, kills and verifies exit of any remaining async subprocesses owned
by that turn, then closes tickets again to catch cancellation-time producers.
Cleanup failure stops continuation (`partial` after progress, else `failed`).
Git snapshots and commit probes run through `asyncio.to_thread`, keeping the
event loop responsive. `wait` also reports a dead checkpoint owner from the
job's stored pid when there is no detached pid file.
Without a durable handle it records `partial` rather than starting
a fresh thread. Non-timeout engine errors still fail normally.

`run --max-wall SECONDS` adds a total wall-clock budget (default **unlimited**).
Approval and engine-slot waits count toward it; the next turn receives at
most the remaining budget. If it expires before slot admission, launch exits
2 without starting a job. Final Git evidence and result persistence are
best-effort cleanup and may outlast the engine deadline. `--timeout` and
`wait --timeout` retain their separate meanings: the
latter only stops the waiter. `wait` keeps polling through `checkpoint` and
all continuations. `partial`, `failed` and `interrupted` return exit **1**;
`done` returns **0**.

`status` and `result` show each initial/continuation turn and its commit SHAs
and pushed refs. Their JSON records include `continuations` (continuations
started), `max_continuations`, `max_wall`, and `turns` with `turn` (initial is
0), timestamps, durable handle, outcome, commit list and ref before/after
SHAs. The result file retains the same history, including for `partial` jobs.
Only the job's starting branch at origin is inspected for pushed-ref progress;
pushes to other branches never count. Detached jobs use only local HEAD progress.
An unavailable remote read
still permits continuation on independently observed local commit progress.

`run --timeout SECONDS` sets the engine request timeout for Codex or Claude
(a positive, finite float; default **3600** seconds). It also applies to
`run --detach`: the parent validates it before spawning and forwards it to
the child. `status JOB_ID --json` includes the configured `timeout` and
`head_at_start` (the repository's HEAD when the job acquired its lock, or
`null` for a non-git directory or a repository with no commits). Invalid
timeouts exit **2**. `wait --timeout` only limits how long the waiter polls;
it does not stop the job.

When a job fails or is interrupted, its error and result file include a
one-line workspace snapshot, for example:

```text
workspace: HEAD unchanged; 3 uncommitted path(s)
workspace: HEAD abc1234 -> def5678 (2 commit(s) not reachable from start); 1 uncommitted path(s)
```

The snapshot counts commits reachable from the final HEAD but not from the start
(they may predate the job if it switched to an existing branch) with `git rev-list --count <start>..HEAD`
and uncommitted paths with `git status --porcelain=v1 -uall`. It describes
the repository as a whole, including work that was already dirty at launch.
JSON metadata includes `workspace_at_end` with `head`, `commits_since_start`, and
`uncommitted`. If the initial Git snapshot fails, metadata also includes
`head_at_start_error`; evidence reports
`workspace: unavailable (start snapshot failed: <reason>)` and leaves the
new commit count unknown, even if Git is available again when the job ends.
Git checks are best-effort, with a ten-second timeout per
subprocess; a non-git directory reports `workspace: not a git repo`, and
an inspection failure reports `workspace: unavailable (<reason>)` with
unavailable fields set to `null`. This evidence helps assess a timed-out
or crashed turn; only the progress-aware timeout continuation described above
resumes automatically.

Stop an active job by full id or unique prefix:

```console
lazytools-code-bridge cancel JOB_ID --reason "operator stopped the task"
```

`--reason` is optional (default `requested cancellation`). The CLI reports
the phase it is waiting on and returns exit code **3** if the cancellation
reaches `--timeout SECONDS` (default 60). Cancellation
kills the process tree (`taskkill /PID ... /T /F` on Windows; the CLI's
private process group on POSIX, falling back to the single pid if no
private group is available), marks the job `interrupted` with
`cancelled by operator: <reason>` and workspace evidence, and expires its
still-open typed approval tickets. A dead process is also marked interrupted.
Cancelling a terminal job prints its status and exits **0** without changing
it, including when the job finishes while cancellation is stopping the process.
Cancellation also works immediately after `run --detach`, before the child
writes its job record: the PID file resolves the full id and the interruption
is recorded as `cancelled by operator during startup: <reason>`.
A later `run` on the same repository automatically reclaims the dead
owner's lock and preserves the cancellation result.

Before killing, the bridge checks for a Python process running the bridge
when command-line inspection is available through optional `psutil` or
POSIX `/proc`; neither WMI nor PowerShell CIM is required. When identity
inspection is unavailable, it requires a non-terminal job record and a
live pid. There is residual pid-reuse risk in that fallback and between
inspection and killing. Jobs embedded through `run_job` in a shared Python
process do not own a CLI process group and should be stopped by their host.

## Choose a model from quota

**Benchmark in progress.** The ladder is shipped policy; no comparative
quality, time, quota-efficiency or subscription-cost result is claimed.


Preview a choice without launching a job:

```console
lazytools-code-bridge route --tier writing --cwd /path/to/repo
lazytools-code-bridge route --tier thinking --cwd /path/to/repo --json
```

For Python callers, `route()` is pure: pass catalogue, readings, budgets,
in-flight counts and a fixed time. `recommend()` adds bounded live quota
reads and configured budgets, or uses supplied readings. This executable
example uses synthetic readings and launches nothing:

```python
from datetime import UTC, datetime
from lazytools.projects.admission import EngineBudget, TelemetryReading, WindowReading
from lazytools.routing import load_default_tiers, recommend, route

now = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
readings = {
    "codex": TelemetryReading("codex", "synthetic demo", now,
                              (WindowReading("codex/10080m", 20, 10080),)),
    "claude_code": TelemetryReading("claude_code", "synthetic demo", now,
                                    (WindowReading("weekly/all models", 20, 10080),)),
}
catalogue = load_default_tiers()
decision = route("writing", catalogue=catalogue, readings=readings,
                 budgets={engine: EngineBudget(engine) for engine in readings},
                 in_flight={}, writer_provider_for_review="claude_code", now=now)
assert decision.provider == "codex"
print(decision.model, decision.effort, decision.reason)
print(recommend("basic", catalogue=catalogue, readings=readings, in_flight={}, now=now))
```

The human output starts with the engine, model, effort, winning rung (numbered
from 1), and reason. It includes each engine's weekly and 5-hour percentages
and reset times, forecasts and informational warning lines, plus every exclusion. JSON includes the decision, scores,
exclusions, telemetry timestamps, quota windows and any session notice.
`route` creates no job and launches no coding engine.

Use the same computation to launch work:

```console
lazytools-code-bridge run --tier basic --cwd /path/to/repo --task @brief.md --session fixes
lazytools-code-bridge run --detach --tier writing --engine codex --cwd /path/to/repo --task @brief.md
lazytools-code-bridge run --tier thinking --needs images --cwd /path/to/repo --task @brief.md
lazytools-code-bridge run --tier writing --review-of JOB_ID --cwd /path/to/repo --task @review.md
```

| Tier | Rung 1 | Rung 2 | Rung 3 |
| --- | --- | --- | --- |
| `basic` | Luna 6 medium / Haiku 5.5 medium | | |
| `writing` | Sol 6.1 high / Sonnet 5.5 high | Sol 6.1 xhigh / Opus 5.5 high | |
| `thinking` | Sol 6.1 xhigh / Opus 5.5 medium | Opus 5.5 high | Astra high |
| `critical` | Opus 5.5 high / Astra high | Opus 5.5 max / Astra xhigh | |

The router walks the ladder in order and picks from the first rung with an
eligible engine. It ranks weekly admission margins, using its preserved
5-hour tie-break and then provisional API-price/provider ordering for exact
ties. For parity with the original router, Claude's 5-hour session window is
displayed but does not contribute to its score. These are capability ladders;
there is no per-model subscription-quota cost estimate. Fable is available
only by an explicit `--model` override.

`--engine codex` or `--engine claude` with a tier restricts the eligible
engines. Explicit `--model` and `--effort` replace the selected values, are
validated against the chosen engine and effective model, and are recorded in
`routing.override`. Known Codex models have their own effort sets: both Luna
models support `low` through `max`, without `ultra`. Models without a specific
set use the policy's engine-wide fallback. The catalogue and explicit `--effort`
checks use the same policy, including engine-only runs. A model override also
revalidates any effort inherited from the selected catalogue entry.
With `--tier` and a model but no engine, the model restricts routing to its
provider: allow-listed Codex identifiers select Codex, while `claude-*` and
known Claude aliases select Claude. An ambiguous model needs an explicit engine.
Routing scores and admission use the effective override's weekly bucket; the
routing record retains the catalogue offering alongside the explicit override.
An absolute-ceiling or telemetry refusal still refuses the automatic route. An engine-only `run`
retains the existing manual behavior and does not read quota. A `run` with
neither `--tier` nor `--engine` fails with a clear error.

`--session NAME` already used by a bridge job in the same repository pins
the engine to that conversation's engine. The router receives the number of
consecutive session failures; after two, output suggests a new `--session`.
The existing conversation still cannot migrate engines. A session alias
in another repository does not pin this one.
Engine-only and detached launches enforce the same session pin. A conflict
names the session and its engine and suggests a new `--session`; it fails
before reading quota or launching a process.

`--review-of JOB` (full id or unique prefix) uses the engine opposite the
writer when that CLI is installed. If the writer's CLI is the only installed
engine, it starts a new conversation and records `same-provider-fresh` in the
route and job result; `--session` is refused. If the opposite CLI is installed
but ineligible, the command still fails with `human_review_required`.
Executables are resolved from `CODEX_BIN`/`CLAUDE_BIN` or `PATH`, with
`[engines] installed = [...]` as a config override. `--needs images` permits only Codex. Conflicting
engine, session, review and capability constraints cannot silently relax
these rules. `--needs` on `run` requires a tier. `--review-of` without a tier
keeps the explicit `--engine` and does not apply the opposite-engine rule.

#### Reviewer decision table

One function, `_review_choice.choose_reviewer`, decides the reviewer for local
routing (`route`, `run --tier`), cloud reviews and the job launcher. The first
matching row wins:

| row | condition | outcome |
| --- | --- | --- |
| 1 | the reviewer's native conversation id equals the writer's | refused: not independent |
| 2 | the opposite engine is installed, or `--engine` names it | opposite engine, `opposite-engine` |
| 3 | the writer's engine is the only one installed | writer's engine, `same-provider-fresh` |
| 4 | neither 2 nor 3 | refused: opposite engine unavailable |
| 5 | row 2 or 3 chose, but `--engine` names the other engine | refused: the review runs on the chosen engine |
| 6 | row 3 chose and `--session` already has a conversation | refused: needs a new session name or none |

An explicit `--engine` naming the opposite engine counts as installed: discovery
only decides automatic selection. A local refusal is reported as
`human_review_required`; a cloud refusal is an error. The launcher's engine is
already fixed, so for it only row 1 can refuse (which is why `--review-of`
without a tier keeps its `--engine`). After a reviewer is chosen, local routing
can still end in `human_review_required` when that engine is ineligible.

### Native conversation ids and independence

A local job records `native_id`, the Claude Code session id or Codex thread id
of its conversation, and `result JOB --json` shows it. It is written when a turn
ends, or before the engine starts when a `--session` alias resumes a
conversation. It is `null` for a job stopped during its first new turn, or when
the engine reported none, for example a Claude job without `--session` whose
turn failed. A `--review-of` job also records `review_of` (the writer's full job
id) and `writer_native_id`.

A review whose `native_id` equals `writer_native_id` fails as
`review refused as not independent`. When a `--session` alias resumes the
writer's conversation, it is refused before the engine starts; if the engine
only reports the id after a turn, the review fails then and its text is not
returned as a result. Use a new `--session` alias for the reviewer.

The check needs both ids: with `writer_native_id: null`, it proves nothing. It
compares the writer job's latest id exactly, so it does not detect an earlier or
later id of the same conversation. It applies to local jobs launched with
`--review-of`, with or without a tier. A task-claim review launched without
`--review-of` (as `dispatch` does) records no writer id and is not checked.

Running bridge jobs with a recorded, live PID are counted per engine across the Store and reserve
quota in the score. Missing, unreadable or stale weekly quota excludes the
engine in tier routing without an explicit `--engine`. With `--tier` and an
explicit `--engine`, including for `--review-of`, missing or unreadable quota
produces a `telemetry_unreadable` warning, and stale quota produces a
`telemetry_stale` warning in the route decision and CLI output. A reading that
shows an applicable window at its ceiling before its reset still blocks the job
regardless of the reading's age.
After each router pick, the bridge calls admission
on the effective model's weekly bucket plus Codex's account-wide short window
or Claude's session window. Sonnet/Opus use `weekly/all models`; an explicit
Fable override uses `weekly/Fable`. Other model-specific buckets are ignored,
including extra Codex limit buckets regardless of their order in telemetry.
The bridge filters recommendation inputs too, preserving the shared router's
matching/scoring rules while retaining raw quota for display.
An override with no applicable weekly telemetry is refused unless it also
names an explicit `--engine`. An absolute ceiling on an applicable window
still excludes that engine and repeats the routing
calculation with the remaining engines and the same constraints. Every rejected
pick keeps its admission reason in the routing record. This additional bridge
gate does not change the shared router's weekly scoring or parity behavior.

If nothing is eligible, human output lists each distinct exclusion once.
Ceiling failures name the applicable windows and reset times, and advise waiting
for quota to reset. Other ordinary failures can suggest an engine-only
`run --engine E` when no engine/model restriction was already supplied; a pinned
session failure suggests a new session. Image/session conflicts explain that
images need Codex and name the session pinned to Claude.
Reviews state why no opposite-engine reviewer
is eligible and require human review, without suggesting an unguarded manual review.

Bridge jobs are direct operator work: `route` and `run --tier` use
`operator_directed=True`, so the autonomous boundary and forecast brake do
not block them. Forecast margins still rank eligible engines, and human output
shows the projected end-of-window use (including job reservations), its forecast
limit and a `warning:` line when the projection exceeds that limit. A warning
about a forecast does not prevent launching. Any window at its ceiling,
including job reservations, also gets a warning even without reset/forecast
data; an applicable window's ceiling prevents launching. The quota display
retains all reported buckets, even those unrelated to the chosen model.
Forecasts use the reading's observation time, just
as the router does; unavailable reset/duration data is shown as unavailable.

The bridge has no project attribution today. If it gains attribution, a project
whose brake is enabled must use autonomous admission at the routing call.
The shared `route()` and `recommend()` APIs keep `operator_directed=False`
by default, preserving LazyCEO's autonomous behavior and parity.

Both engines' quota is read concurrently, with a 45-second timeout per engine,
so two slow reads share the same wait. A file cache
at `~/.lazytools/quota-cache.json` shares successful readings between CLI
processes for at most 120 seconds; writes are atomic. A corrupt, missing,
expired or unwritable cache does not prevent fresh reads, and failed reads
never become spare capacity. Failed readings retain the provider's real cause
(for example, a timeout or missing login) in decisions, JSON and human errors,
and are never cached. `LAZYTOOLS_QUOTA_CACHE` overrides the cache path.

The default catalogue is shipped in the wheel. `~/.lazytools/model_tiers.toml`,
when present, replaces it; `--tiers PATH` on `route` or `run` takes precedence.
A bad override fails visibly. All four tiers must be present, each rung must
name at least one provider (`codex` or `claude_code`), and model/effort values
must satisfy `lazytools.routing.ModelPolicy`.

The first human launch line explains the chosen model and reason. Job records
retain `routing` (tier, original pick, reason, scores, exclusions, rung and
overrides), while `model`/`effort` store the values actually launched.
`status` and `jobs` display the tier, model and effort. `--json` retains the
existing job-id-first protocol for foreground runs and includes the routing
record in the final JSON; detached JSON includes it in the launch record.
Detached children use the parent's frozen decision, without a second quota
read or a different pick.

## Prune old automated sessions

Every engine session leaves a transcript on disk, and automated ones pile up:
Claude Code under `~/.claude/projects`, Codex under `~/.codex/sessions` (which
the Codex desktop app also lists). No tagging is needed to tell them apart,
because both engines record who started a session. Claude Code transcripts carry
`"entrypoint": "sdk-py"` for Python-SDK sessions (LazyBridge, LazyCEO, this
bridge), against `cli`, `claude-desktop` and `sdk-cli` (print mode and Remote
Control) for a person's own.
Codex rollouts carry `"originator": "lazybridge"` against `Codex Desktop` /
`codex_exec`.

```console
lazytools-code-bridge prune-sessions                      # dry run: what would go, and why the rest stays
lazytools-code-bridge prune-sessions --older-than-days 14 --engine claude
lazytools-code-bridge prune-sessions --apply              # delete
```

Only automated sessions older than the cut-off (default 7 days) are deleted,
together with a Claude session's sub-agent folder. A transcript whose origin
cannot be read is kept, as is any session a named alias (`--session`, `code_sessions_*`)
still points at. Your own sessions are never candidates. `--json` prints the
summary for scripts. The Codex desktop app also keeps its own thread index;
pruning rollouts frees the transcripts but does not edit that index.

## Check live model availability

```console
lazytools-code-bridge models
lazytools-code-bridge models --probe-claude
lazytools-code-bridge models --tiers /path/to/model_tiers.toml --probe-claude --json
```

`models` reads Codex App Server's `model/list` after the same
`initialize` → `initialized` handshake used for quota. It starts no Codex
thread or turn and spends no Codex quota. The table shows the offered model
ids, supported reasoning efforts, default model flag and default effort.
Hidden models returned by the server are marked as hidden.

Claude has no model-list endpoint. Without `--probe-claude`, its availability
is shown as unknown and no Claude turn runs. **The opt-in probe consumes a
small amount of quota:** it asks `Reply with just: ok` once for each distinct
Claude model in the active catalogue, plus `sonnet` and `opus`. Each probe uses
JSON output and `--max-turns 1`, with tools disabled and session persistence
disabled, in an empty temporary directory. `--strict-mcp-config` loads no MCP
servers, `--setting-sources ""` skips user/project/local settings, and
`--safe-mode` disables hooks and customizations (these flags were checked in
the installed CLI's help). The CLI is resolved from the
Agent SDK's bundled executable first, matching the bridge's Claude engine,
then from PATH or the native installation under `~/.local/bin`.

The table records the model that actually answered (`modelUsage`), along with
any `unrecognized_model` stderr diagnostic. A successful answer with that
diagnostic remains available. Alias resolution is reported explicitly:
`sonnet` can resolve to `claude-sonnet-5` even when the full
`claude-sonnet-5-5` identifier also works. Claude effort capabilities and the
default flag remain unknown; an availability probe does not verify them.

The audit cross-checks every catalogue entry and the default policy against
Codex's live model/effort sets, reports policy models missing from the server,
new offered models missing from policy, and differences in either direction
between policy and live effort sets. With Claude probing enabled, failed
catalogue probes and concrete model identifiers that answer as another model
are reported too. The audit loads structurally valid catalogue entries even
when normal routing validation would reject them, so it can report every
capability mismatch instead of stopping at the first one. It changes no
catalogue, policy, bridge job or session.

The active catalogue follows the same `--tiers`, home override, packaged
default precedence as routing. Codex discovery has a 30-second deadline;
each Claude probe has a 60-second deadline. A probe timeout kills the whole
process tree on Windows (the process group on
POSIX), and every cleanup communication/wait has a five-second deadline.
On Windows the CLI is created suspended, assigned to a private kill-on-close
Job Object and then resumed. Cleanup retains ownership of descendants even
when the CLI has already exited while a descendant holds the output pipe open.
Exit code is **0** when the
checked capabilities match, **2** for mismatches or discovery/probe errors.
Without Claude probing, exit 0 covers Codex and local policy checks only.
`--json` emits one object with `models`, `probe_claude`, `mismatches` and
`errors`; unknown capabilities are `null`.

## What is gated, and how

When the policy grants web access, Claude Code exposes `WebSearch` and
`WebFetch`, and Codex uses live web search. In ungoverned legacy mode the
legacy writer retains its default web access.

The `--cwd` argument must resolve inside the selected confinement root. Root
precedence is `--root`, `LAZYTOOLS_CODE_ROOT`, the top-level `root` in
`~/.lazytools/code-bridge.toml`, then the launching directory. A relative
configured root is resolved beside the configuration file. The top-level
`root` is a workspace setting, read separately from the `[concurrency]` table
(`load_workspace` versus `load_config`). A confinement error
reports the selected root and whether it came from the flag, environment,
configuration, or current working directory. The compiled profile controls
native reads, writes, shell and web exposure. In legacy mode, local writer callbacks and Codex command escalations use
the coder hand and the typed ticket queue within the native tool grants.
The legacy mode label indicates that runtime confinement has not been verified.

### Operator credentials

Operator decisions use a typed LazyBridge `ApprovalQueue` in the projects Store. Set
`LAZYTOOLS_PROJECTS_STORE_DB` to the same file used by the projects tools.
The bridge also honors `LAZYCEO_CEO_STORE_DB` and defaults to
`~/.lazytools/projects.sqlite` when neither is set. Job logs and routing use
`LAZYTOOLS_CODE_BRIDGE_DB` or `--db`.

Run the following once from the operator's shell before deciding typed tickets.
Use the same `--db` and `--registry-db` selections as the job if it uses a
custom registry. The command is safe to repeat for the same registry.

```console
lazytools-code-bridge approver-credential init
lazytools-code-bridge pending
lazytools-code-bridge approve APPROVAL_ID
```

LazyBridge stores the credential's hash in the selected registry. The bridge
stores the bearer secret in a current-user-only file under
`~/.lazytools/approver-credentials/`, keyed by the resolved registry path.
The CLI prints only initialization status. Run decisions under the account
that initialized the credential. Keep the state directory outside job
workspaces. The bridge keeps the credential out of job environments and
excludes credential paths from automatic read approvals. Typed decisions are
recorded as `owner`. `--actor` applies to historical prompt-only tickets.
Only a person should run credential initialization and operator decisions.

### Approval profiles

Local jobs resolve and compile LazyBridge's `read`, `edit`, or
`edit-and-run` profiles. Existing `--profile default` and `--profile review`
map to `edit-and-run` and `read`. Intentional reviews use `read` even
when the caller supplies writer options. Claude exposes its repo read tools
and Codex uses read-only sandbox settings with escalations disabled. In
ungoverned legacy mode both engines get `review_command` for repository Git
reads and `review_read_file` for workspace text files. GitHub PR reads use the
repository's origin and require network access. Legacy profile names keep
that access by default, and callers can select `--network off`.

`--launch-mode governed` requires matching runtime release evidence.
Configure it through `LAZYTOOLS_CODE_BRIDGE_CODEX_RELEASE_REPORT` or
`LAZYTOOLS_CODE_BRIDGE_CLAUDE_RELEASE_REPORT`. The default `auto` uses
governed mode when eligible. Without a configured report it accepts the
legacy profile names as ungoverned migration inputs. A configured but
ineligible report causes a refusal. Pass `--launch-mode legacy` to select
the explicitly ungoverned path. The job records its launch mode, requested
network, effective profile, policy revision and digest, and release receipt
when applicable. An operator can select a profile document with
`LAZYTOOLS_CODE_BRIDGE_PROFILES`. Each continuation checks the same binding.

For a claimed Projects task, pass `--task-id PROJECT/TASK --claim-token TOKEN`.
Configure `LAZYTOOLS_CODE_BRIDGE_TASK_PRINCIPAL` with the trusted executor's
task principal, or use the dedicated MCP stdio host's `projects_task_principal`
configuration. The claim controls placement, engine and the requested
profile and network when specified. The bridge records the dispatch
acknowledgement and local job ID. The MCP tool accepts the corresponding
`profile`, `launch_mode`, `network`, `task_id`, and `claim_token` arguments.

### Governed Projects approvals

The governed Projects effect registry checks the deciding principal, project
scope, required role, and exact request digest. By default, the `owner`
principal can decide both owner-tier and orchestrator-tier requests with the
credential from `approver-credential init` for the Projects Store. Pass the
credential through the trusted Python adapter or set
`projects_approver_credential_file` to the registry's private credential file
on a dedicated Projects stdio host bound to the owner. Tool calls carry the
request ID, digest, verdict, and reason. The credential stays in the host.

A deployment can set `orchestrator_actor` on `ProjectTaskApprovals` to assign
new orchestrator-tier requests to a separate human principal. Existing tickets
retain their recorded assignee. Older tickets without a recorded route go to
the registry owner. For a separate human approver, provision a credential for
the assigned principal with both the LazyBridge `owner` role and the Projects
`orchestrator` role. The following command uses `operator` as an example.

```console
python -m lazybridge.ext.approval --db PROJECTS_DB issue-approver-credential operator --role owner --role orchestrator --output PRIVATE_DIR/operator.credential
```

Before enabling automatic Projects effects, run these commands from the
operator shell. Select the same Projects Store as the effect adapter, either
through `LAZYTOOLS_PROJECTS_STORE_DB` or with `--registry-db` on both commands.

```console
lazytools-code-bridge approver-credential init --registry-db PROJECTS_DB
lazytools-code-bridge approver-credential provision-automatic PROJECT_ID --registry-db PROJECTS_DB
```

The two project-scoped, nontransferable cards cover `read` and `local_commit`.
An automatic request requires a matching card and must pass the effect policy,
task claim, and target checks. Only the provisioning command reads the owner
credential for this flow.
The Projects registry records the card use and retains the one-time effect
consumption and outcome record.

### Coder approval cards

For the original coder hand, provision it once per approval registry from the
owner shell. See [autonomy modes](#autonomy-modes) to choose a preset.
Use the same store options for provisioning, running the job and inspecting
its audit. With a custom \`--db\` alone, the bridge uses the sibling
\`<db-stem>.registry.sqlite\` registry.

\`\`\`console
lazytools-code-bridge approver-credential init --db JOBS_DB --registry-db REGISTRY_DB
lazytools-code-bridge approver-credential provision-coder --db JOBS_DB --registry-db REGISTRY_DB
\`\`\`

Each local writer that uses bridge approval callbacks receives nontransferable
copies scoped to its job and workspace. A separate copy can cover its own
temporary directory. The bridge revokes the copies when the job finishes or is
cancelled, and expires them if an interrupted worker cannot be recovered. A
revoked original also invalidates its copies.

Cards cover direct local commands such as Git status, diff and metadata reads,
staging explicit file paths, committing the index with an inline or scoped file message,
running pytest and creating scratch virtual environments. They cover ordinary
native file edits and literal cleanup targets created during the job. Cleanup
checks the worktree at launch and checks the live path and its contents for
links and reparse points. Use separate commands with literal paths. Examples:

\`\`\`console
git add -- src/example.py
git commit -m "Update example"
python -m pytest -q -p no:cacheprovider tests
python -m venv --copies .trial-venv
\`\`\`

Use \`rm -rf -- PATH\` on POSIX or
\`Remove-Item -LiteralPath PATH -Recurse -Force\` in PowerShell for scratch cleanup.
Network-enabled jobs can use cards for `gh pr view`, `gh pr list`, `gh run list`,
`gh run view` and GET-only `gh api` queries. Pushes, fetches, network installs,
GitHub writes, messages, cross-repository
\`git -C\`, production paths, external writes and deletions, and requests that
the bridge cannot scope require a one-use operator ticket. Native sandbox
policy still governs actions that execute without a bridge callback.

Use \`status JOB --json\` or \`result JOB --json\` to see \`card_approvals\`, including
the copied card ID, its original, the request digest and the command or file
paths. The plain-text output lists each approved action. Use the coder hand
with trusted local repositories and toolchains. Tests execute repository code.
Git hooks and custom execution settings can require operator approval.

### Autonomy modes

Use the owner shell to configure the CEO's delegated card classes in the
approval registry used by local writer jobs. The registry and the owner
credential belong outside the job checkout. Mode changes require that
registry's owner credential and are available through the operator CLI.

```console
lazytools-code-bridge approver-credential init --registry-db REGISTRY_DB
lazytools-code-bridge approver-credential autonomy --mode supervised --preset with-production --registry-db REGISTRY_DB
lazytools-code-bridge approver-credential autonomy --mode total --preset with-production --registry-db REGISTRY_DB
lazytools-code-bridge approver-credential autonomy --show --registry-db REGISTRY_DB
```

`supervised` is the default mode. With either preset it delegates
`git_local`, `git_push_job_branch`, `gh_pr_create`, and `checks_in_checkout`
once the owner has provisioned the cards. In `total` mode the CEO can use cards for classes
outside the preset's owner-only floor.

| Preset | Classes that stay with the owner in total mode |
| --- | --- |
| `solo-dev` | `unknown`, `payment_or_external_post` |
| `with-production` | `production_task`, `payment_or_external_post`, `delete_files`, `push_default_branch`, `unknown` |

Omitting `--preset` keeps the current preset and defaults to
`with-production` on first setup. Configuring a mode replaces the original
coder hand if one was provisioned. A mode or preset change replaces the cards
and invalidates copies issued to running jobs. New writer jobs get the new
hand. Requests without a matching card enter the normal `pending`,
`approve`, and `reject` flow.

Each class applies to recognized direct commands within their native grants
and the card's path, branch, and network restrictions. For Codex, the same
rules apply to one literal PowerShell `-Command` or cmd.exe `/c` wrapper.
Job-branch pushes use
`git push origin JOB_BRANCH` from the job checkout and require a GitHub
`origin` with its default branch recorded in `refs/remotes/origin/HEAD`.
Supported PR commands use the checkout's repository and explicit safe flags.
For example, `gh pr merge 17 --squash` can use a total-mode card. Delegated
install tickets can use an isolated wheel-only pip install into a new
workspace target. Shell scripts, unscoped installs, and writes to external
paths require an operator ticket. The `checks_in_checkout` class covers
verification run from the job checkout or the job temp directory:
`python -m pytest`, `python -m ruff check`, and `python -m mypy` on paths in
the checkout, and the read-only commands of the allow-list (`git status`,
`log`, `diff`, `show`, `branch --list`, `grep`, plus `grep`, `sed -n Np`,
`cat`, `ls`, `head`, `tail`, `find`, and `wc` on relative paths). These
commands may end with `2>&1`, `> /dev/null` (Bash), or a `>`/`>>` redirection
into the job directory they run in. The class also covers `rm` and
`Remove-Item` when every target is inside the job temp directory. Any other
redirection, removal, network command, or unrecognized command stays pending.
`python -c` programs stay pending because a card's command cannot contain
parentheses or quotes. The `git_local` class covers explicit-path `git add`,
`git commit`, and `git merge origin/BRANCH` into the job branch when the
tracking ref and changed paths pass the checkout checks. Owners with an
existing hand can rerun `approver-credential autonomy` with their current
mode to issue the merge card for new jobs. The `workspace_tools` class
covers native file edits, directory creation, and `git worktree`.

Use `status JOB --json` or `result JOB --json` to see the actor, mode,
class, and match reason on each card-use receipt. The
[autonomy report](#autonomy-report) counts those decisions by class.

### Per-job temp directory

Each job gets `<os temp>/lazytools-bridge/<job_id>`. It is created at job
start and removed (best effort; a cleanup failure never fails the job) at
job end. The directory is:

- the Codex sandbox's **only** extra writable root;
- a second Claude file root, so Write can create helpers there;
- what `TEMP`, `TMP` and `TMPDIR` point at for the engine subprocess, so
  pytest's `tmp_path` lands there.

The engines take no `env` argument; the variables are set in the job
process, which is the engine's parent, for exactly the engine run, and then
restored. The job record keeps `temp_dir`.

The base comes from the environment, which can say anything (`TMP=C:\` once
made the whole drive writable). **Every** configured `TMPDIR`/`TEMP`/`TMP`
value, plus Python's default, is checked. If any one of them is:

- a drive, filesystem or UNC share root;
- inside the Windows directory or `ProgramData`;
- the user profile root;
- an ancestor of the job's cwd or of the bridge DB;
- shorter than 3 path components (`C:\Temp`, `/tmp`);

then the job gets **no** extra root. The reason is logged and stored as
`temp_dir_refused` in the job record, and pytest in the Codex sandbox will
then escalate (and ask), which is the safe failure. On Linux and macOS, `/tmp`
is refused by the length rule; Codex's own workspace-write sandbox already
allows `/tmp` and `$TMPDIR` there.

**What still asks, unchanged:** every escalated command not covered above
(including `git push`, `gh pr create`, any `gh` write, `pytest` if it ever
escalates); merge, `git reset --hard`, `*rm *`, `*delete*`, pushes to
`main`/`master` and every dangerous push form for Claude `Bash`; any Bash
text containing `rm `/`delete`, heredoc bodies included; `rm` of a
job's own scratch files (deliberately — `*rm *` is not weakened). Codex
sandbox **network stays off**: that is what forces every `git push` and `gh`
write to escalate, and so to reach a human.

## Engine concurrency limit (machine-wide)

(Cloud jobs have a separate limit; see [Cloud jobs](#cloud-jobs-run-where-cloud).)

Besides the per-repository lock, each engine has a machine-wide limit on live
jobs. Defaults: **codex = 1**, claude = unlimited. Configure them in
`~/.lazytools/code-bridge.toml` (or the file named by
`LAZYTOOLS_CODE_BRIDGE_CONFIG`) — not in `model_tiers.toml`, whose loader
rejects unknown tables:

```toml
[concurrency]
codex = 1    # 0 = unlimited
claude = 2   # absent or 0 = unlimited
```

- `run` with the engine at its limit is refused with a message naming the
  running job id(s) and pid(s). `run --wait-for-slot` queues instead: it
  takes the repository lock, then polls until a slot frees, then starts (it
  prints `waiting for a codex slot ...`; a detached child prints it to its
  log, and `wait` reports it as still starting).
- `--detach` refuses in the launcher, where you see it, unless
  `--wait-for-slot` is given; the child holds the slot for as long as it runs.
- `--tier` routing excludes an engine at its limit, so a busy Codex sends the
  job to Claude when Claude is eligible. `route` shows why:
  `excluded codex: engine_at_concurrency_limit: codex 1/1 (running job ...)`.
  If no free engine is eligible (for example the session is pinned to Codex),
  the error names the job; with `--wait-for-slot` the busy engine is picked
  and the launch queues.
- **Machine-wide, whatever `--db` says.** Slots live in one canonical
  directory: `%LOCALAPPDATA%\lazytools\engine-slots` on Windows, else
  `~/.lazytools/engine-slots`. Only the `slot_dir` key under `[concurrency]`
  moves it; that key is meant for tests.
- **Every check is serialised.** Inspect, reclaim, acquire and release all
  run under one OS-backed exclusive lock on `guard.lock` in that directory
  (`msvcrt.locking` on Windows, `fcntl.flock` elsewhere), held only for that
  short section. Two launches can never both reclaim one dead slot, or both
  take the last free one.
- **Every live slot counts.** There is one file per job,
  `<engine>-<job_id>.slot`, with no slot indices. Admission counts every live
  file of the engine, so lowering a limit from 2 to 1 while jobs run still
  sees them all. Jobs take a slot file even while their engine is unlimited,
  so they count once a limit is set.
- **Reclaim.** A slot whose pid died stops counting at once. The slot records
  its owner's Store path, and the next *successful* launch marks that job
  `interrupted` in the right Store (even a different `--db`) and closes its
  pending tickets. Typed tickets are marked `expired`. Historical prompt-only
  tickets are rejected while actionable. Bookkeeping counts as complete only
  when the job has **no pending tickets left**, and only then is the slot file
  deleted. If closing tickets fails after the status was saved, the next launch
  sees an already-`interrupted` job and retries. A refused launch
  leaves stale slot files untouched, and an incomplete bookkeeping attempt
  keeps the file for a later launch, so the record of a dead job is never lost.

## Cloud jobs (`run --where cloud`)

Every local job runs Codex or Claude Code on this machine, where it competes
with production jobs during production windows. `--where cloud` hands the
brief to the engine's **cloud agent** instead: the bridge submits it, records
the returned task handle, and returns at once. No engine process runs locally
and no local engine slot is taken.

New to this? Start with **[Cloud setup for the code bridge](code-bridge-cloud-setup.md)**,
a step-by-step guide for both engines, or install the bundled agent skill
(`lazytools-code-bridge setup-skill --install`) so an assistant can walk you
through it.

```
lazytools-code-bridge run --where cloud --allow-cloud --engine codex|claude --cwd <repo> --task @brief.md \
    [--branch B] [--routine NAME]
lazytools-code-bridge status <job>      # polls `codex cloud status <task>`
lazytools-code-bridge wait <job>        # polls until the task is terminal
lazytools-code-bridge resync <job>      # recover a legacy job failed by a poll error
lazytools-code-bridge result <job>      # final message + `codex cloud diff <task>` summary
lazytools-code-bridge apply <job> [--rebase]  # `codex cloud apply <task>` in a NEW worktree/branch; no push
```

Without the options below a cloud job is exactly what it was: a task you
`wait` for and `apply` by hand. The options that make cloud agents a
first-class part of a workflow (PR targets, `--land`, `cloud-sync`, cloud
reviews, `--where auto`) are described in
[Cloud agents as part of the workflow](#cloud-agents-as-part-of-the-workflow).

`--engine codex` uses Codex Cloud, through the official `codex cloud` CLI
(the binary is found the same way the local Codex engine finds it:
`CODEX_BIN`, then `PATH`, then the Codex desktop app's install directory).
`--engine claude` uses Claude Code cloud sessions (`claude --cloud`, or a
routine's API trigger); see [Claude cloud sessions](#claude-cloud-sessions-engine-claude)
below for how it differs. Claude has no status API, so its results are read
from git.

### Final-report contract

The bridge appends one fixed instruction to every Codex and Claude cloud
brief: before finishing, the agent must always write its Markdown final report
to `.bridge/report.md`, including when it changed no other file. On `wait` or
landing, the bridge extracts that file from the task result into the job's
`report` field. Both `wait` and `result <job>` print it; an older agent that
returns no report is shown as `report: (none provided)`.

`.bridge/report.md` is transport, not a repository change. The bridge removes
it before staging, pushing, or opening a pull request. A result containing
only the report, and a READY result with an entirely empty diff, are successful
no-code-change jobs: they finish as `done`, exit zero, and create no branch or
pull request.

### Data boundary: cloud jobs cannot read local data

A cloud agent sees **only the repository as its Codex Cloud environment
clones it from the git remote**, at the branch you name. It cannot read local
files, uncommitted or unpushed changes, local databases, mapped drives, or
anything else on this machine — including **production data**. A brief that
needs local data (or a production connection) must stay a local job. Push the
branch you want the agent to start from before submitting; `--branch`
defaults to the current local branch name, and the agent uses the remote's
copy of it. Every cloud brief is prefixed with a short preamble telling the
agent the same thing.

### Configuration

Each repository must be mapped to a **single-repo Codex Cloud environment**
(create one per repository at chatgpt.com → Codex → Settings →
Environments). The key is the `owner/name` read from the repository's
`origin` remote (https, ssh and scp-style URLs all work; matching ignores
case). In `~/.lazytools/code-bridge.toml`, beside `[concurrency]`:

```toml
[cloud]
concurrency = 4            # live cloud jobs at once (default 4; 0 = unlimited)
default_where = "local"    # what `run` does without --where: local | cloud | auto
busy_windows = []          # local-time ranges in which `auto` prefers the cloud, e.g. ["21:00-01:30"]

[cloud.codex]
concurrency = 2            # optional: Codex cloud jobs at once, on top of the global limit

[cloud.codex.envs]
"selvaz/LazyTools" = "env_0123abcd"

[cloud.claude]
concurrency = 2            # optional: Claude cloud jobs at once

[cloud.claude.models]      # tier -> model for Claude cloud sessions
critical = "claude-opus-5-5"
default = "claude-sonnet-5-5"   # used for any tier not listed (this is also the built-in default)
```

A repository with no mapping is refused before anything is submitted or
recorded (before any `gh` call too), with a message telling the operator to
create that environment, showing the exact lines to add, and pointing at
[the setup guide](code-bridge-cloud-setup.md). `lazytools-code-bridge cloud-envs`
shows what is configured (see below).

### Job record

A cloud job is an ordinary job row with `where = "cloud"` plus
`cloud_handle` (the task URL `codex cloud exec` printed, or the bare task id
if it printed no URL), `cloud_url`, `repo`, `env_id` and `branch`. `jobs`
shows `where=cloud task=<url>`. Statuses: `submitting` (holding a place while
`codex cloud exec` runs), `running`, then `done` / `failed` / `interrupted`.

- `status`, `wait` and `result` poll the provider and persist what they see.
  The status output is parsed tolerantly (JSON `status`, a `status: X` line,
  or a known status word); a poll never declares a job *done* on output it
  cannot read. A failed poll (the CLI errs without naming a status, or its
  output names no status at all) is recorded as `last_poll_error` and the
  job stays `running`, regardless of how many polls fail. HTTP 429 and 5xx
  responses, connection failures and timeouts are retried with exponential
  backoff and jitter; `Retry-After` is honoured. Poll times are reserved in
  the shared Store per provider account, so concurrent waiter processes keep
  a minimum interval instead of polling in lockstep. These rules also cover
  the git transport used to observe Claude cloud result branches.
  A **non-zero exit is not automatically a poll failure**: `codex cloud
  status` exits non-zero for a task that ended in error, so its output is
  parsed whatever the exit code and a recognised status wins (`error` → the
  job is `failed`). Only a non-zero exit whose output names no status counts
  as a poll failure. Without an explicit `status:` field, only the **leading
  token** of the first line is read (`[READY] Fix error handling` is ready):
  the rest is a task title.
- Only an explicit provider terminal state (`completed`, `failed`, or
  `cancelled`, including their provider aliases) changes the job's status.
  A cloud `wait` without `--timeout` gives up after 7200 s. It exits 3 when
  the task is still known to be running, or 4 when failed polls leave its
  state unknown; neither timeout changes the stored running status.
- Older bridge versions could incorrectly store a transport failure as a
  failed job. `resync <job>` re-polls such a job; a completed task becomes
  `done` (so `apply --land` works), and the recovery transition is appended
  to the job's `job_log`. A still-unreadable/non-terminal task remains
  unchanged and exits 4.
- Provider diagnostics (errors, apply output) **and the brief** are passed
  through a redaction filter (bearer/basic credentials, `token=`/`api_key=`-
  style pairs, `sk-`/`ghp_`/`github_pat_`/`xox*`/`AKIA`/`glpat`/`AIza`
  prefixes, JWTs) before they are stored or printed. The provider still
  receives the brief as written, and `run` warns when it masked something:
  do not put secrets in a brief.
- `result` prints the final message and a diff summary (files, +/− lines).
  Its exit code follows the local rule: non-zero for `failed`/`interrupted`.
- Every status write re-reads the record under the machine-wide guard and
  never overwrites a terminal status: a poll or submit whose answer arrives
  after a `cancel` cannot bring the job back (or take its place again).
- `cancel` on a cloud job uses the same provider-stop path as `cloud-stop`.
  It reports both the provider result and the local tracking result, and only
  marks the job `interrupted` after the provider confirms it stopped or had
  already finished. A provider-stop failure leaves the job tracked and exits
  non-zero; an unconfirmed stop request also leaves it tracked.

### `apply`: never in your checkout, never pushed

`apply <job>` requires a `done` job. At submit the bridge fetches the
branch and records the remote tip as `base_commit` — the commit the cloud
environment starts from. `apply` fetches the branch again; if its head has
**moved** since submit, `apply` is **refused** unless you pass `--rebase`, in
which case the worktree starts at the new head (`rebased_from` records the
old base; the diff may not apply cleanly). Otherwise it runs `git worktree add
-b cloud/<job8> <dir> <base_commit>`. There is no unchecked fallback: a job
with no recorded `base_commit` (the fetch at submit failed, or the branch was
not on the remote), or whose `base_commit` is not in the local repository, is
**refused** -- re-submit it from a pushed branch. Then it runs `codex cloud
apply <task>` **inside that new worktree**. Your own checkout, its branch and
its index are never touched. `<dir>` defaults to `cloud-worktrees/<job>`
beside the Store; `--branch` and `--worktree` override both names. A job is
applied once; a second `apply` is refused. If `codex cloud apply` fails and
left the worktree untouched, the worktree and branch are removed so the same
`apply <job>` can be retried; if it left partial changes, both are kept, their
location is recorded (`apply_failed_worktree`) and printed, and nothing is
deleted. The command stops before any
commit or push and prints the push command: **pushing stays a separate,
approved step**.

### Safety boundary for cloud jobs

A cloud agent's tool calls happen on the provider's machine, where no local
command gate can see them one by one. `--where cloud` therefore requires the
explicit operator flag **`--allow-cloud`**; without it nothing is submitted or
recorded. Brief text is not scanned for English action keywords.

Cloud results reach the repository only through the local landing step
(`apply`, `land`, or `cloud-sync`). That step keeps its own checks before it
changes local or remote repository state.

Beyond that, the approval boundary is the diff:

- The cloud task's only output the bridge accepts is its **diff**. Nothing
  reaches any checkout until `apply`, which an operator runs, into a new
  worktree, and which never pushes.
- **Pushing stays a separate, human step** after reviewing the applied
  worktree; the bridge never pushes a cloud job's work.
- What the agent may do *inside* the cloud (network access, setup script,
  secrets) is governed by the Codex Cloud environment's own settings. Keep
  each mapped environment single-repo, without push credentials or
  production secrets, and with internet access off unless a task needs it.

- `apply` itself runs only **local, non-destructive** steps (a new worktree
  and branch, then `codex cloud apply` inside it) and never pushes. Pushing or
  merging the applied result goes through the **normal gate**: by the operator
  by hand, or as a local bridge job whose `BridgeGate` files the usual ticket.

Work that needs gated actions performed step by step under tickets should
run as a local job.

### Input validation

`--job-id` (internal) must be 8–32 lowercase hex digits or a canonical UUID,
and must not already exist. `apply` re-checks the stored id and resolves the
default worktree path (`Path.resolve` + `is_relative_to`) to make sure it
stays inside `cloud-worktrees/`.
`--branch` and `apply --branch` must pass `git check-ref-format --branch`.
On Windows a `codex.cmd`/`.bat` npm shim is **never executed** (`cmd.exe`
would re-parse the brief and branch as shell syntax): the bridge runs the
package's `bin/codex.js` beside it with `node`, or refuses and asks for
`CODEX_BIN` pointing at `codex.exe`.

### Concurrency

Cloud jobs do **not** count against the local per-engine limit (they use no
local CPU), and a busy local Codex never blocks a cloud submit. They have
their own limit, `[cloud] concurrency` (default **4**, across providers),
counted from the job Store: records with `where = "cloud"` still
`submitting` or `running`. `[cloud.codex] concurrency` and
`[cloud.claude] concurrency` add an optional limit per provider (0 =
unlimited); a submit must fit under both the global and its provider's limit,
and the refusal names the one that was hit. Before refusing, the bridge polls the active
tasks once, so a task that finished since its last poll frees its place.
Admission runs under the same machine-wide `guard.lock` as the engine slots,
so two submits cannot both take the last place. The count is per Store: jobs
recorded under a different `--db` are not seen.

### Claude cloud sessions (`--engine claude`)

Setup (GitHub App, routine API trigger, and token environment variable) is in
[Cloud setup → Part 2](code-bridge-cloud-setup.md#part-2-claude-code-cloud-sessions).

**Submit, routine mode (`--routine NAME` or configured default).** The bridge is
non-interactive, while `claude --cloud` requires an interactive terminal.
Therefore unattended Claude jobs must use a routine API trigger; the bridge
fails before invoking Claude when none is configured. Set an optional default:

```toml
[cloud.claude]
default_routine = "nightly"

[cloud.claude.routines.nightly]
id = "trig_0123"
token_env = "LAZYTOOLS_ROUTINE_NIGHTLY_TOKEN"   # env var NAME; a literal token key is refused
repo = "owner/repository"                        # repository selected in the routine
branch = "main"                                  # branch selected in the routine
```

Alternatively pass `--routine nightly`. The bridge posts the brief to the
routine fire endpoint and records its session id and URL. The token is read
from the named environment variable, never stored, and masked in errors. The
bridge verifies that `--cwd` and `--branch` match the routine settings.

**No status API (impossible by design).** Anthropic documents no endpoint
to poll a cloud session's state or read its transcript, so the bridge cannot
know when a session has finished. Instead:

- At submit it assigns an unnamed job the unique branch
  `claude/lazytools-<job-id>` and includes that exact branch in the brief. If
  the brief already names exactly one `claude/...` branch, that branch is used.
- `status`, `wait` and `result` run `git ls-remote` and look only for that
  job's recorded branch becoming new or moved since submission. They never
  infer ownership from another new `claude/...` branch.
- While none exists, `status` prints `submitted (no status API): open <url>`
  and the job stays `running`. It holds its cloud concurrency place until a
  branch appears or you `cancel` it.
- When one appears the job becomes `done` with `result_branch` and
  `result_commit`. Other candidates are listed in `result_branch_candidates`.
  The session may still be pushing; the session URL is the authority on
  whether it finished.
- `result` prints `git diff <base_commit>...origin/<result_branch>` as a
  summary.
- `apply <job> [--source claude/<name>]` fetches the branch and runs
  `git worktree add -b cloud/<job8> <dir> <tip>`: a **new worktree on a new
  local branch**, never your checkout, never pushed. `--rebase` does not apply:
  the agent's branch already contains its base.

The same safety rules apply as for Codex: `--allow-cloud`, redaction, the
shared `[cloud] concurrency` limit, and no local engine slot. Results enter the
repository only through the checked local landing step.

### Provider protocol

`lazytools.code_bridge._cloud` defines a `CloudProvider` protocol
(`env_for`, `submit`, `status`, `diff`, `apply`) and a `PROVIDERS` registry.
A provider without a status API (Claude) additionally implements
`job_status(job, claimed=...)`, `job_diff(job)` and `apply_job(...)`, which
read the job's result from git. `refresh`, `result_text` and `apply_job` use
those hooks when present.

The same data boundary applies to both: a cloud session sees only its
environment's clone of the repository, never this machine.

## Cloud agents as part of the workflow

### Stopping cloud agents

`lazytools-code-bridge cloud-stop <task-url|task-id|job-id>` is the emergency
stop path. For a bridge job id it resolves the recorded cloud handle, asks the
provider to stop it, and only then marks the bridge job interrupted. Codex is
cancelled and polled for up to 60 seconds; if it remains active, the result is
`stop requested, not confirmed` rather than a false failure. Claude queues the
stop instruction through `claude --cloud`, allowing the CLI to manage its own
authentication, and reports `stop requested`, never `stopped`.

The ordinary `cancel <job-id>` command uses this same stop path for cloud jobs
and prints both the provider and local tracking outcomes. Use `cloud-stop`
directly for provider tasks that have no bridge job or for bulk stopping.

Use `cloud-stop --all-running` to combine running tasks reported by
`codex cloud list --json` (including tasks created outside this bridge) with
running bridge cloud jobs. Add `--dry-run` to list the deduplicated targets
without sending requests, and `--json` for Agent Radar or other automation.
Routines are not disabled; this command affects current sessions only.

Tokens are read in memory at call time and are never printed or persisted by
the bridge. The exact credential files are:

| Platform | Credential file read by `cloud-stop` |
|---|---|
| Linux | `$HOME/.codex/auth.json` |
| macOS | `$HOME/.codex/auth.json` |
| Windows | `%USERPROFILE%\\.codex\\auth.json` |

`CODEX_HOME` replaces the `.codex` directory when that standard CLI override is
set. No Claude credential file is read. A missing or
rejected credential is reported as `login needed` for that provider; an
`--all-running` operation continues with other providers/targets.

A lead (a person or an LLM) should not have to babysit cloud tasks: submit
through raw CLIs, copy diffs, push, open PRs, ask for reviews in PR comments.
The bridge does those steps. The loop:

```
# 1. submit: work on an existing PR's branch (--pr) or on a new branch + PR (--open-pr); --land lands it for you
lazytools-code-bridge run --where cloud --allow-cloud --engine codex --cwd <repo> --task @brief.md --land
lazytools-code-bridge run --where cloud --allow-cloud --engine claude --cwd <repo> --task @brief.md --pr 42 --land

# 2. follow it: `wait` lands it when it finishes; `cloud-sync` does the same for ALL live cloud jobs (run it from a scheduler)
lazytools-code-bridge wait <job>
lazytools-code-bridge cloud-sync

# 3. review it with the opposite engine; the verdict lands on the PR as one comment
lazytools-code-bridge run --where cloud --allow-cloud --cwd <repo> --review-of <job>
```

A review job is posted, never landed. Everything in this section is
`--where cloud` only and builds on the options above.

### PR targets: `--pr N`, `--open-pr`

* `--pr N` works on pull request N's **head branch**: the cloud agent starts
  from it and the job records the PR (`pr_number`, `pr_url`, `pr_head`,
  `pr_base`). A closed or merged PR, a PR from a fork, and a `--branch` that
  disagrees with the PR's head are refused before anything is submitted.
* `--open-pr` records that landing opens a **new branch and a PR** against the
  base branch (`--branch`, default the current one).
* Neither: behaviour is unchanged and `gh` is never called. `--pr` and
  `--open-pr` are alternatives. All the options in this section are
  `--where cloud` only. `gh` (logged in, or `GH_TOKEN`) is needed for PR work;
  `GH_BIN` overrides its path.

### Landing: `apply <job> --land`

`apply --land` performs, for a finished job and without anyone typing git
commands: apply the diff in a fresh worktree (as `apply` does), **commit**
(subject = explicit `--title`, otherwise the first non-empty line under
`# Goal`, shortened to 72 characters at a word boundary; an unstructured brief
falls back to its first sentence; body = the job id and the
provider's summary; no attribution lines or trailers), **push**, then **update
the recorded PR** (it is the PR's head branch that is pushed to, in a
fast-forward) **or open a new one** (`gh pr create`; the PR body quotes the
brief and carries the job id, never a local path). With no recorded PR, a new
one is opened. The pushed commit and PR URL are stored in the job
(`pushed_sha`, `landed_branch`, `pr_url`) and shown by `result`; the landing
worktree is removed afterwards.
The same subject is used for the PR title and thus GitHub's default squash
subject. Section headings such as `Goal` are never used as a title.

Guarantees:

* **A moved base is refused, with the reason**, unless the job was submitted
  with `--rebase-ok` (or `apply --land --rebase-ok` is passed): if the branch the
  agent started from moved on `origin` since submit, nothing is applied,
  committed or pushed. With `--rebase-ok` the diff is applied onto the new head
  and may not apply cleanly.
* **It never force-pushes.** The only push form is
  `git push origin <commit>:refs/heads/<branch>`, which git rejects when it is
  not a fast-forward. A PR head that moved meanwhile is a refusal, and the other
  person's commits are untouched. A new branch never overwrites an existing
  branch of the same name on `origin`.
* **It is resumable and idempotent.** Each step is recorded (`land_commit`,
  `pushed_sha`, `pr_url`); a landing that failed at `gh pr create` is finished by
  running it again without pushing twice, and a landed job is never landed again.
  A claim in the job record keeps two schedulers from landing the same job at
  once.
* **Provider diff paths are authoritative.** Landing stages exactly those paths
  with literal pathspecs: present files use `git add -f`, while tracked paths absent from the
  worktree use `git rm --cached`. This also handles a file replaced by a same-stem package
  directory (and the reverse), deleted files, renames (with or without edits), and a new file covered by `.gitignore` is not lost.
  The landing report explicitly lists ignored paths that needed force;
  it never force-adds a directory, `.`, or an unrelated worktree file. A retry
  reuses the recorded `cloud/<id>` worktree/branch left by a failed landing.
* A task that changed nothing pushes nothing and says so.

**Claude cloud** pushes its own `claude/...` branch, so `--land` only verifies
it on `origin` and records its commit (`pushed_sha`). With `--pr N` the PR's
head is fast-forwarded to the session's commit when the session built on it
(otherwise the landing is refused, the work stays on the `claude/` branch, and
nothing is forced); without `--pr` a PR for the `claude/` branch is found or
opened.

### Automatic landing: `--land`, `wait`, `cloud-sync`

`run --where cloud ... --land` marks the job to be landed when it finishes
(`--land` without `--pr` implies `--open-pr`). Two things then land it, with the
same code as `apply --land`:

* `wait <job>`: when the job ends, `wait` lands it (the landing is printed on
  stderr, and `result` shows the pushed branch and PR). A refused landing makes
  `wait` exit 1.
* `cloud-sync [--json]`: reconciles **every** live cloud job: polls the
  provider, lands finished jobs that were submitted with `--land`, posts
  finished reviews, and records a failed task as `failed` with the provider's
  reason (reported once). It prints one line per job
  (`waiting`, `landed`, `land-failed`, `review-posted`, `review-failed`,
  `failed`) and exits 1 when a landing or review post failed, so it can run
  unattended from a scheduler (cron, Task Scheduler, a Claude Code routine).
  A refused landing is retried on the next run, so a later `--rebase-ok`
  landing or a fixed `gh` login still completes it.

### Cloud reviews: `run --where cloud --review-of <job>`

`--review-of` prefers the **opposite engine** of the reviewed job's author (Codex
work is reviewed by Claude and vice versa). If the author's CLI is the only
installation, it instead uses a fresh same-provider cloud conversation and
records `same-provider-fresh`; `--session` is refused. An installed opposite
engine remains mandatory even when ineligible. The review runs on the
reviewed job's **pushed branch**, so the job must be landed first (or, for
Claude, have pushed its result branch). The reviewer is asked to write
`REVIEW.md` (the only file it may create) ending with `verdict: blocking` or
`verdict: no-blocking`; extra text from `--task` is appended to the brief. When the review
finishes (`wait`, `cloud-sync`), the bridge reads `REVIEW.md` (from the pushed
`claude/review-<job8>` branch, or from `codex cloud diff`) and, when the
reviewed job has a PR, posts it as **one PR comment**:

```
<!-- independent-review v1 -->
author: codex
reviewer: claude
verdict: no-blocking
head: <sha of the reviewed commit>

<the review's findings>
```

There is no footer. A review without a `verdict:` line is posted as `blocking`
and says so; one whose `REVIEW.md` cannot be read is retried three times, then
given up (visible in `result`). A repeated `cloud-sync` posts nothing twice (the
comment is also looked for on the PR before posting). Without a PR the review
stays in the job's `result`.

### Placement: `--where auto`

`--where` is `local`, `cloud` or `auto`; without it, `[cloud] default_where`
decides (default `local`, so nothing changes until you opt in). `auto` runs in
the cloud when

* the current **local** time is inside a production window of
  `[cloud] busy_windows` (`["21:00-01:30", "05:00-06:00"]`; a window may wrap
  midnight; the start is inclusive and the end exclusive; **empty by default**),
  or
* **no local engine slot is free** (every candidate engine is at its
  machine-wide limit, see [Engine concurrency limit](#engine-concurrency-limit-machine-wide)),

and locally otherwise. `run --where auto` prints the decision and why on stderr
(`placement: cloud (requested auto): local time 22:05 is inside the busy window
21:00-01:30`) and `--json` adds a `placement` object. `route --tier T --where
auto` shows the same decision without launching anything, and MCP submissions use
the same repository-aware decision. When Codex auto placement chooses cloud, an
unmapped repository fails with `cloud_env_unmapped` and its repository slug in
JSON and MCP results. Explicit `--where cloud` follows the same policy. Add the
`[cloud.codex.envs]` mapping or use `--where local`; set `[cloud] unmapped = "local"`
to opt in to fallback. Auto still chooses local normally when a slot is free
outside busy windows. Unavailable repository metadata falls back locally for auto.
A cloud decision still needs `--allow-cloud` and an explicit `--engine`, like any cloud job.

### Models for cloud jobs

* **Claude cloud** runs `claude --cloud ... --model M`. `M` is `--model`, else
  the `[cloud.claude.models]` entry for `--tier` (`basic`, `writing`,
  `thinking`, `critical`), else its `default` entry, else **`claude-sonnet-5-5`**.
  A routine fixes its own model, so `--model` is refused with `--routine`.
* **Codex Cloud** always uses the model its **environment** is set to (change it
  in the environment's settings at chatgpt.com/codex). The Codex CLI ignores
  `-c model` for cloud tasks, so the bridge neither passes nor lets you set one:
  `--model` is refused, `--tier` is accepted and noted, and
  `[cloud.codex.models]` is rejected in the config.

### Environments: `cloud-envs`

`lazytools-code-bridge cloud-envs [--json] [--cwd <repo>] [--no-probe]` lists the
Codex Cloud environments with the repository each serves, and the Claude cloud
environment(s). Codex environments come from `codex cloud list --json` (each
task's `environment_id` / `environment_label`) joined with
`[cloud.codex.envs]`: a mapped environment shows its repo; an environment seen
in Codex but not mapped is marked `unmapped` (its label is shown as a hint). The
Claude rows are the account's default environment (chosen at claude.ai/code; the
CLI has no flag for it) and every `[cloud.claude.routines]` entry (the token's
environment-variable **name** and whether it is set, never its value). With
`--cwd` it also says which environment the current repo maps to.

### For other agents: MCP

`lazytools-mcp code_bridge --allow-unsafe` serves `code_bridge_submit`,
`code_bridge_status` and `code_bridge_result`, which run the commands above and
return their JSON, so another agent uses local and cloud jobs the same way (see
[MCP server](mcp-server.md#code_bridge-local-and-cloud-coding-jobs-for-other-agents)).

## One job per repository at a time

`run` takes a file lock keyed on the git repository root containing the resolved
`--cwd` (or the directory itself outside git); a second `run` against
the same repository while the first is still alive is refused with a clear
message naming the job and pid already holding it. A lock left behind by a
process that died (crash, kill, machine restart) is detected by checking
whether that pid is still alive and reclaimed automatically — the old job's
record is marked `interrupted` rather than left stuck at `running` forever.

## Session reuse

`--session NAME` behaves exactly like `codex_write`/`claude_code_write`'s own
`session_name`: an unused name opens a new Codex thread / Claude Code
session and remembers it (in LazyBridge's own `SessionRegistry`,
`~/.lazybridge/sessions.json` by default). Legacy launches bind an existing
pre-migration alias on its first resume. Subsequent resumes require the
same policy and task claim, when present. Governed launches require a
previously bound native handle. LazyBridge 1.9.0 refuses governed Claude
resume across bridge jobs because the bindings live in the original SDK
client. In-job continuation can reuse that client. Session aliases are
scoped per repository and engine.

## Delegation method

The reusable workflow for handing a piece of work to an engine and getting it
merged safely. The orchestrator is a human or a Claude Code session; the
bridge never merges on its own.

1. **Brief file.** Write the task to a file (`brief.md`): context, the exact
   change, files in and out of scope, the checks to run, "do not push to
   main", and what "done" means. A file, not a long `--task` string: it is
   reviewable, reusable for rework, and survives the session.
2. **Launch detached, routed by tier.**

    ```text
    lazytools-code-bridge route --tier writing --cwd REPO          # dry run: who, which model, why
    lazytools-code-bridge run --detach --tier writing --cwd REPO --task @brief.md --session feat-x --json
    ```

    The router respects quota and the engine concurrency limit (a busy Codex
    goes to Claude). Add `--wait-for-slot` to queue rather than re-route when
    the session is pinned to a busy engine.
3. **Wait, and watch approvals.** Run `wait JOB_ID` in the host's background
   mechanism and poll `pending --json` meanwhile. Read each ticket's command
   and reason; `approve` only what the brief authorises, `reject --reason`
   anything else. With the structural rules above, expect tickets only for
   real decisions: pushes, PR creation, first `git add`/`commit` escalation,
   deletes.
4. **Orchestrator verification.** `result JOB_ID`, then check the work
   yourself: read the diff, run the full test suite and linters in the
   repository, confirm the scope matches the brief. A `done` job is not an
   acceptance verdict.
5. **Opposite-engine review.**

    ```text
    lazytools-code-bridge run --detach --tier writing --review-of JOB_ID --cwd REPO --task @review-brief.md --json
    ```

    `--review-of` always picks the engine that did *not* write; if it is not
    eligible the bridge refuses with `human_review_required` rather than
    letting the writer review itself. Feed findings back to the writer's
    `--session` for rework, then review again.
6. **PR with green CI.** The writer (or the orchestrator) opens a PR from a
   feature branch (`gh pr create` asks, once per run). Wait for every CI
   check to pass; read-only `gh pr checks`/`gh run view` escalations no
   longer ask.
7. **Merge.** A human merges (or explicitly approves `gh pr merge`, which
   always asks). Never push to `main`.

## Freshness check

Before a local `run` launches, the bridge fetches the current branch's upstream
and counts the commits `HEAD` is behind it. A stale branch builds the fix on old
code, so the count is printed on **stderr** (stdout keeps the job id first):

```text
freshness: warn branch is 6 commits behind origin/main; pull or rebase first, or the job builds on stale code
```

- **Default:** warn and launch.
- **`--require-fresh`:** exit 2 before a job record, session or lock exists.
- **`--no-freshness-check`:** skip the fetch and the check.

It is advisory: a detached HEAD, a branch without an upstream, a non-git
directory or a failed fetch (offline) is silent, or notes that the count may be
out of date. Like the brief check, it runs once in the launcher (also for
`--detach`); the detached child does not repeat it.

## Brief check

`run` checks the task text before it launches anything, because a few shapes of
brief reliably cause rework: a missing section, a path the engine cannot read, a
quoting pattern that files an approval ticket, a persona line. Each finding is
printed on **stderr**, one line, before the launch (stdout keeps its contract:
the job id first, then the result or the `--json` line):

```text
brief-check: error missing-goal: no 'Goal' section heading
brief-check: warn brief-too-short: brief too short to carry context
```

- **Default:** findings are printed and the job still launches.
- **`--strict`:** any `error` stops the launch with exit code 2, before a job
  record, a session or a lock is created. Warnings alone never stop it.
- **`--no-brief-check`:** skips the check and prints
  `brief-check: skipped (--no-brief-check)`.

The same check runs for `--tier`/`--review-of` jobs (after the engine is
chosen, since the path rule depends on it) and for `--detach` (once, in the
launcher; the detached child does not repeat it). For `--task @brief.md` the
file is read once: the launcher checks that text and hands the child a snapshot
of exactly that text (`<job-id>.brief.md` beside the job's `.log`), so editing,
replacing or deleting the file after launch changes nothing the child runs. The
check reads no other file and does not check that a path exists.

| Code | Severity | Raised when |
| --- | --- | --- |
| `missing-goal` | error | no `Goal` heading (Italian: `Obiettivo`) |
| `missing-done-when` | warn | no `Done when` heading (`Fatto quando`) |
| `missing-context` | warn | no `Context` heading (`Contesto`) |
| `missing-constraints` | warn | no `Constraints` heading (`Vincoli`) |
| `missing-report` | warn | no `Report` heading (`Rapporto`) |
| `path-outside-cwd` | error | `--engine claude` only: an absolute Windows or POSIX path that resolves outside `--cwd`; the message names it |
| `quoting-jq` | warn | a command in the brief runs `gh ... --jq '<filter>'` |
| `quoting-commit-message` | warn | a command in the brief runs `git ... -m "<text>"` |
| `persona-line` | warn | a line starting `You are a/an ...` |
| `step-by-step` | warn | the brief says "think step by step" |
| `brief-too-short` | warn | fewer than 200 characters (once) |

Details that decide a match:

- Headings are matched case-insensitively and may carry a colon or markup:
  `## Done when`, `## Done when:`, `**Done when**`. A heading inside a fenced
  code block does not count.
- **Paths.** A Claude job can read only its own repository (`--cwd`); a Codex
  job can read the whole `--root`, so Codex briefs never raise
  `path-outside-cwd`. Comparison is lexical and by path component (a sibling
  `repo-copy` is outside `repo`). Ignored: URLs, `C:\ProgramData\spyder-6\`
  (interpreters) and placeholders such as `C:\...\dir` or `C:\Users\<name>`.
- **Paths with spaces.** Prose has spaces too, so a path is cut as follows,
  most exact first: a path in quotes or backticks is taken whole; a path that
  starts with `--cwd` itself is matched against the cwd text, so spaces in the
  cwd (or below it) never truncate it; any other path is joined across a space
  only to a next word that carries a separator (`C:\Program Files\Tool\a.exe`,
  `repo copy/file.py`), so ordinary prose after a path is not absorbed.
  Trade-off: a sibling of the cwd whose own name has several unquoted words
  (`/home/me/my repo two words/x`) reads as the cwd followed by prose and is
  not caught; **quote paths that contain spaces** and it is. A path cut short
  elsewhere is still outside the cwd, so the verdict stays `path-outside-cwd`.
- **Quoting.** Any argument containing quotes, backslashes, backticks or `$`
  files an approval ticket, so a brief should ask for `gh ... --json <fields>`
  (filter the output afterwards) and `git commit -F <file>` instead of `-m`.
  The pattern is flagged only inside a code span that is itself a `gh`/`git`
  command, inside a fenced block, or on a line that starts with `gh`/`git`; a
  bare flag mentioned in prose is not.

A template that passes with no findings:

```text
# Brief: <one line>

## Goal
What must be true afterwards, in a sentence or two.

## Context
- Where the code lives; what already exists; what the engine cannot see.

## Constraints
- Scope in and out; what must not change; branch name; no push to main.
- Use `gh ... --json <fields>` without a jq filter, and `git commit -F <file>`.

## Done when
- A command that passes, with its expected result (a test, a linter, an output).
- Every criterion is something the orchestrator can check without asking.

## Report
What to hand back: the diff summary, the command output, anything undecided.
```

## Known limits

- Under the `review` approval profile, every `gh pr comment` command is held
  for operator approval, including comments that read a body from the job
  scratch directory and comments embedded in compound shell commands. The
  bridge deliberately does not inspect a body file and auto-approve the post:
  command-time file changes would make that check vulnerable to a
  time-of-check/time-of-use race.

- The per-`cwd` lock's "is this pid alive, and is the stale lock still the
  same lock file" check is two separate steps, not one atomic operation —
  two `run`s racing to reclaim the exact same stale lock at the exact same
  instant could theoretically both decide to reclaim it. Not a practical
  concern for a single human driving this from one Claude Code session at a
  time.
- If a job's process is killed outside `cancel` hard enough that
  `run_job`'s own `finally` never
  executes, the record stays at `running`/`awaiting_approval` until the
  *next* `run` against the same repository reclaims the lock and marks it
  `interrupted` — there is no background sweeper watching for this on its
  own.
- Lock liveness checks the pid only, not the process identity. After a crash
  or reboot, an unrelated process that reuses the recorded pid keeps the lock
  looking held; `run` then refuses with the lock path, which can be deleted
  by hand once `jobs` shows nothing running on that repository. `wait` has
  the same blind spot: if a detached job dies and its pid is reused before
  the next poll, `wait` keeps waiting (use `--timeout`, and `status`/`jobs`
  to check by hand).
- Shell parsing is intentionally conservative rather than complete. Obscure
  constructs such as process substitution, shell functions, custom command
  aliases, nonstandard interpreters, encoded programs, and platform-specific
  quoting can be held even when harmless, and newly installed command wrappers
  are not classified until added to the policy. The gate is an approval aid,
  not a shell sandbox.
- The coder hand classifies complete native callback commands. Substitution
  and ambiguous syntax ask. Installed executables and repository configuration
  remain part of the execution trust boundary.
- Residual risk of the allow-listed git reads: they carry no `-c` and `git config`
  asks, but a `core.fsmonitor`, `textconv`/`filter` driver or `ext::` remote that
  already exists in the repository's config before the job starts (or is written
  through a route the gate does not see, such as a tool other than
  Write/Edit/Bash) still executes when such a read runs. Operand checks are
  lexical: a symlink or junction inside the cwd that points at a credential
  location is not resolved, and recursive searches (`grep -r`, `find`) inside the
  cwd can still reach dotfiles there (only `rg` skips hidden/ignored files by
  default). The `Read`, `Glob` and `Grep` tools are not covered by these checks.
- The gate checks command text and operand names, never file contents. A
  credential file that is tracked or sits inside the cwd can therefore still be
  read by commands that name no path: `git grep -e KEY` without a path,
  `git log -p --all`, `git show <sha>` and `grep -r KEY .`. `Read`, `Glob` and
  `Grep` remain unconditional allow; Claude Code's own cwd boundary is the only
  limit on them.

## Dispatching a project

Run every ready task on an open Projects board with the graph dispatcher:

```bash
lazytools-code-bridge dispatch --project PROJECT_ID --parallel 2
```

The dispatcher claims ready work, launches bridge jobs using each task's tier and
placement, and records terminal job evidence before completing or failing the task.
Completing a task unlocks its dependents; failure blocks only the dependent branch.
It polls every 15 seconds until no task is ready or running. Use `--once` for one
launch/settlement pass, or `--dry-run` to print the topological launch order without
claiming work. A repeated command adopts submitted jobs recorded on active claims
rather than launching duplicates. `--parallel` is an additional project-wide ceiling;
the bridge's configured per-engine local concurrency limits still apply.

Events are line-oriented on stdout: `ready TASK`, `claimed TASK`, `launched JOB`,
`done TASK`, `failed TASK REASON`, and `blocked TASK`.

The dispatcher loop, routing, and heartbeat checks live in
`lazytools.code_bridge._dispatch`. Job launch and conversation resume helpers live in
`lazytools.code_bridge._dispatch_launch`. Checkout settlement, acceptance checks,
publication, and landing live in `lazytools.code_bridge._dispatch_workspace`.

### Local tasks

Before creating a new task clone, the dispatcher fetches the source repository's `origin`
and fast-forwards its base branch (origin's default branch) if the source checkout is
clean and on that branch. It never resets or forces. If the source stays behind
(dirty, diverged, on another branch, or the fetch failed), it is left alone, the task
starts from its current commit, and `waiting TASK source base is stale: WHY` is emitted
once until the reason changes. A task with dependencies still starts from their accepted
commits. An existing clone, or a clone recreated for preserved work, is not refreshed.
The dispatch receipt records the starting commit as `base_commit`.

A local job that stops for a system reason (engine crash, stale heartbeat, database
lock) is retried once. Before the retry, the dispatcher commits the attempt's
uncommitted work in the task clone (never tool litter such as `*.log` or
`__pycache__`) and points `dispatch-attempt/PROJECT/TASK/N` at it, in the clone
and, when it can, in the source repository. It emits `preserved TASK BRANCH` and
records the branch in the attempt's evidence. The retry starts from that work.
Each attempt runs under a `--session` alias. When the failed job recorded a
native conversation id and the retry uses the same engine, the retry resumes
that conversation and tells the agent to continue. If the engine cannot load it,
one more attempt starts a fresh conversation whose prompt lists the kept files
and last commit message. A resume the bridge refuses before submitting the job
is settled as a failed launch and starts that fresh attempt too. This extra
attempt does not count against the retry limit. How the retry continues (kept
branch and commit, session, native id, fresh-conversation flag) is stored with
the prepared attempt in the task record, so a dispatcher restart before the
launch still resumes the kept work. Check failures and other task failures are not retried. Review tasks
never resume a writer's conversation.

The dispatcher publishes the local branch `dispatch/<project>/<task>` in the source
checkout (`published TASK BRANCH`) before completing the task (`done TASK`). With
`--land` the dispatcher then runs `git push origin <branch>` from the task's clone
(fast-forward only, excluding the repository's default branch) and runs `gh pr create --head
<branch> --base <default branch>`. The PR title is the task's `title`; without one it is
the first sentence of the first line of its text, cut at a word boundary to at most 72
characters. The PR body lists the task id, its acceptance criteria and the checks that
passed, with absolute local paths hidden. If an open PR for the branch already exists, the dispatcher only pushes, so a
later task that revises the branch updates that PR. Landing emits a follow-up event,
`landed TASK PR_URL` on success or `land-failed TASK REASON` on failure. The task is
already accepted and its dependants are eligible to run. `gh` must be
installed and authenticated (`gh auth login` or `GH_TOKEN`).

### Cloud tasks

A task whose `requested_placement` is `cloud` runs on a cloud agent through the
same path as `run --where cloud`; the dispatcher adds no cloud logic of its own.

- **Placement.** The quota router picks the engine for the task's tier, and the
  cloud placement decision (`--where cloud` for the task's repository, see
  `--repo-root`) supplies the environment: the mapped Codex environment, or the
  Claude routine (`[cloud.claude] default_routine`). Cloud placement evidence
  needs an environment id; without one the task waits (`waiting TASK cloud
  placement failed: ...`) and does not fall back to local.
- **Limits.** Before claiming, the dispatcher checks the `[cloud] concurrency`
  limits (global and per provider). When they are full the task stays unclaimed
  (`waiting TASK cloud concurrency limit reached`). Cloud jobs take no local
  engine slot.
- **Launch.** `lazytools-code-bridge run --where cloud --allow-cloud --engine E
  --branch BASE --open-pr --land --task-id TASK`, where `BASE` is the branch the
  source checkout is on (it must exist on `origin`; dependency work must already
  be on it). The job id is derived from the claim, so a restart between the
  submit and its acknowledgement adopts the job instead of submitting again.
  The job reference is recorded on the dispatch receipt.
- **Settlement.** Each pass polls the job and lands it when it is done, as
  `wait-any` does. The task is settled once the job is terminal and its landing
  has resolved. It is **accepted** only if the job landed (PR url and pushed
  commit recorded) and every `checks` command passes in a fresh local clone of
  the landed branch at the pushed commit. The pushed commit is the task's
  artifact. Otherwise the task fails with the reason: the job's error, `cloud job
  did not land: ...`, or the failing check and its output. A restarted dispatcher
  settles running cloud jobs on its next pass. Cloud tasks are not retried.
- **Lease.** The claim lease (`--lease-minutes`, default 60) also bounds a cloud
  job. When it expires, the job is cancelled and the task fails. Raise the
  lease for long cloud tasks.
- Review and contract tasks are still not dispatched, locally or in the cloud.
