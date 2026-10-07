# Async Code Bridge

`lazytools-code-bridge` (`lazytools.code_bridge`, also runnable as
`python -m lazytools.code_bridge`) is an **asynchronous** path to the same
Codex / Claude Code write access that `codex_write` / `claude_code_write`
(see [Code Support Agent](code-support/index.md)) expose over MCP.

## Why this exists, next to `codex_write`

`codex_write` / `claude_code_write` are synchronous MCP tool calls: the
delegating Claude Code session is blocked on the call until the job
finishes, and a long multi-file job can outrun the MCP transport's own
timeout — the call then *looks* failed to the caller while the underlying
work keeps running.

The bridge solves that by moving the job **out of the MCP call entirely**:
Claude Code launches it as a plain OS process, in the background, via its own
Bash tool. Claude Code's own harness already notifies the session
automatically when a `run_in_background` process exits, so nothing here has
to build its own notification channel — it only has to give the session
something to poll in the meantime (approval tickets) and somewhere to read
the finished result from.

## The workflow, from a Claude Code session

1. **Launch**, in the background, never foreground:

   ```
   Bash(
     command="lazytools-code-bridge run --engine codex --cwd /path/to/repo "
             "--task 'refactor the retry logic in foo.py' --session my-task",
     run_in_background=true,
   )
   ```

   The job id is printed as the **first line** of stdout — read it
   immediately, before the process exits, so you can poll/approve/cancel by
   id even if the run takes hours.

2. **Poll for approval tickets** while the job runs. The engine's sandbox
   lets it read/write/run most things on its own; anything it escalates
   (a dangerous git command, a Codex sandbox escalation, ...) files a ticket
   instead of blocking on a terminal prompt nobody is watching. Use a
   `Monitor` until-loop (or any periodic check) against:

   ```
   lazytools-code-bridge pending --json
   ```

   Each row has `approval_id`, `job_id`, a short `gist` of what is being
   asked, `created_at`, `expires_at`. Relay the gist to the user in chat.

3. **Answer** what the user decides:

   ```
   lazytools-code-bridge approve <approval_id>
   lazytools-code-bridge reject <approval_id> --reason "<why>"
   ```

   A ticket left unanswered past its TTL (a few hours by default —
   `LAZYTOOLS_CODE_BRIDGE_TICKET_TTL`, in seconds) is treated as rejected and
   the job fails cleanly rather than hanging forever.

4. **Read the result** once Claude Code's own harness notifies you the
   background process exited:

   ```
   lazytools-code-bridge result <job_id>
   ```

   (or `status <job_id>` for a quick status line without the full text, or
   `result --json` for the full job record). The same text is also written
   to the path printed by `run` on completion, for when the Store itself is
   unavailable.

`jobs` (optionally `--all` for history, not just active jobs) lists
everything this bridge knows about, for a session that lost track of a job
id.

## What is gated, and how

Every write call is sandboxed to one repository (`--cwd`, confined to
`$LAZYTOOLS_CODE_ROOT` / `--root`, same convention
`lazytools.connectors.code_support` uses) and gated by one
`lazybridge.ext.approval.TieredGate` whose rule table
(`lazytools.code_bridge._policy.CODING_RULES`) mirrors the generic coding
rules in LazyCEO's own agent: reads/`Write`/`Edit`/`Bash` run free; `git
add`/`commit`/`push`/`gh pr create` get a one-time-per-run session grant;
dangerous push forms, pushing to `main`/`master`, `gh pr merge`, `git reset
--hard`, and anything matching `*rm *`/`*delete*` ask every time; and any
command a coding engine escalates beyond its own sandbox (Codex's
`approval_policy="on-request"`) always asks. The channel behind "ask" is a
durable, `Store`-backed `ApprovalQueue`
(default `~/.lazytools/code-bridge.sqlite`, override with
`LAZYTOOLS_CODE_BRIDGE_DB` / `--db`) — the ticket survives this process
exiting, which is the whole point: a human answers it from an entirely
different process (this bridge's `approve`/`reject`, or any other surface
that reads the same queue).

## One job per repository at a time

`run` takes a file lock keyed on the resolved `--cwd`; a second `run` against
the same repository while the first is still alive is refused with a clear
message naming the job and pid already holding it. A lock left behind by a
process that died (crash, kill, machine restart) is detected by checking
whether that pid is still alive and reclaimed automatically — the old job's
record is marked `interrupted` rather than left stuck at `running` forever.

## Session reuse

`--session NAME` behaves exactly like `codex_write`/`claude_code_write`'s own
`session_name`: an unused name opens a new Codex thread / Claude Code
session and remembers it (in LazyBridge's own `SessionRegistry`,
`~/.lazybridge/sessions.json` by default); a known name resumes it. Scoped
per repository, so the same name in two different repos is two different
sessions.

## Known limits

- The per-`cwd` lock's "is this pid alive, and is the stale lock still the
  same lock file" check is two separate steps, not one atomic operation —
  two `run`s racing to reclaim the exact same stale lock at the exact same
  instant could theoretically both decide to reclaim it. Not a practical
  concern for a single human driving this from one Claude Code session at a
  time.
- A job's `Store` record is only ever updated by the process running it; if
  that process is killed hard enough that `run_job`'s own `finally` never
  executes, the record stays at `running`/`awaiting_approval` until the
  *next* `run` against the same repository reclaims the lock and marks it
  `interrupted` — there is no background sweeper watching for this on its
  own.
- `TieredGate`'s compound-command splitter is a text heuristic, not a shell
  parser (see its own module docstring): it does not see `$(...)`/backtick
  substitution inside a single non-compound command. This is the exact same
  property LazyCEO's own rule table already accepts for the same reason.
