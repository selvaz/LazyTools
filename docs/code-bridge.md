# Async Code Bridge

`lazytools-code-bridge` (`lazytools.code_bridge`, also runnable as
`python -m lazytools.code_bridge`) is an **asynchronous** path to the same
Codex / Claude Code write access that `codex_write` / `claude_code_write`
(see [Code Support Agent](code-support/index.md)) expose over MCP.

Install with the `engines` extra — `pip install "lazytoolkit[engines] @ git+https://github.com/selvaz/LazyTools.git"` —
when you use `--engine claude` or `--tier`: both need the Claude Agent SDK (Claude engine and
Claude quota reading). Without it Claude jobs fail and quota-aware routing sees Claude as unreadable.

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

   **Prefer `--detach` for anything long.** A plain `run` is a child of the
   launching shell: if Claude Code reaps that background shell (it does so
   under memory pressure) or the session exits, the job dies with it and is
   only marked `interrupted` on the next run in that repository. With
   `--detach` the job runs in its own process (its own process group,
   outside the launcher's job object where Windows allows it), the command
   prints the job id, pid and log path and returns at once, and you follow
   it with `wait`, which you launch in the background instead:

   ```
   lazytools-code-bridge run --detach --engine codex --cwd /path/to/repo --task @brief.md --session my-task
   Bash(command="lazytools-code-bridge wait <job_id>", run_in_background=true)
   ```

   `wait` exits when the job reaches `done`/`failed`/`interrupted` (printing
   the same output as `result`), exits 1 if the job's process is gone while
   its record still says running, and exits 3 on `--timeout`. If the waiter
   itself is killed the job is not: start `wait` again. The child's output
   goes to `~/.lazytools/results/<job_id>.log`.

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

## Choose a model from quota

Preview a choice without launching a job:

```console
lazytools-code-bridge route --tier writing --cwd /path/to/repo
lazytools-code-bridge route --tier thinking --cwd /path/to/repo --json
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
| `basic` | Sol 6.1 medium / Sonnet 5.5 medium | | |
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

`--review-of JOB` (full id or unique prefix) allows only the engine opposite
the writer. If that reviewer is ineligible, the command fails with
`human_review_required`. `--needs images` permits only Codex. Conflicting
engine, session, review and capability constraints cannot silently relax
these rules. `--needs` and `--review-of` on `run` require a tier.

Running bridge jobs with a recorded, live PID are counted per engine across the Store and reserve
quota in the score. Missing, unreadable, stale or exhausted weekly quota
excludes the engine. After each router pick, the bridge also calls admission
on the effective model's weekly bucket plus Codex's account-wide short window
or Claude's session window. Sonnet/Opus use `weekly/all models`; an explicit
Fable override uses `weekly/Fable`. Other model-specific buckets are ignored,
including extra Codex limit buckets regardless of their order in telemetry.
The bridge filters recommendation inputs too, preserving the shared router's
matching/scoring rules while retaining raw quota for display.
An override with no applicable weekly
telemetry is refused. An absolute ceiling on an applicable window excludes
that engine and repeats the routing
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

Both engines can use the web: Claude Code gets `WebSearch`/`WebFetch`
(allowed by the rule table, so they never ask), and Codex runs with live web
search set explicitly per job rather than inherited from `~/.codex/config.toml`.

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
- Lock liveness checks the pid only, not the process identity. After a crash
  or reboot, an unrelated process that reuses the recorded pid keeps the lock
  looking held; `run` then refuses with the lock path, which can be deleted
  by hand once `jobs` shows nothing running on that repository. `wait` has
  the same blind spot: if a detached job dies and its pid is reused before
  the next poll, `wait` keeps waiting (use `--timeout`, and `status`/`jobs`
  to check by hand).
- `TieredGate`'s compound-command splitter is a text heuristic, not a shell
  parser (see its own module docstring): it does not see `$(...)`/backtick
  substitution inside a single non-compound command. This is the exact same
  property LazyCEO's own rule table already accepts for the same reason.
