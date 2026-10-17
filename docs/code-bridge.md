# Async Code Bridge

`lazytools-code-bridge` (`lazytools.code_bridge`, also runnable as
`python -m lazytools.code_bridge`) is an **asynchronous** path to the same
Codex / Claude Code write access that `codex_write` / `claude_code_write`
(see [Code Support Agent](code-support/index.md)) expose over MCP.

Install with the `engines` extra — `pip install "lazytoolkit[engines] @ git+https://github.com/selvaz/LazyTools.git@v0.10.2"` —
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
| `approve` / `reject` | Decision on a pending ticket; rejection requires `--reason` |
| `status` / `result` | Status or full job record/text; `--json` preserves metadata |
| `wait` | Terminal result; exit 3 on waiter timeout, 1 when process disappears before final record |
| `jobs --all` | History, including finished jobs, for recovering a lost id |
| `cancel` | Stop process tree, reject open tickets, retain interruption and workspace evidence |

Read each ticket's command and reason before deciding. An unanswered ticket
expires as rejection after `LAZYTOOLS_CODE_BRIDGE_TICKET_TTL` seconds.
Tickets and results use `~/.lazytools/code-bridge.sqlite` by default;
`LAZYTOOLS_CODE_BRIDGE_DB` or `--db` selects another Store. Use the same Store
for every lifecycle command. Logs are under the Store's results directory
(default `~/.lazytools/results/`).

`--task @brief.md` reads a UTF-8 task file rather than treating it as literal
text; detached launch resolves its path before passing it to the worker.
The final text is also saved to a result file under the results directory;
retain the path printed at completion as an independent copy if the Store is
unavailable. The log contains worker output, while `result --json` contains
the complete job record. Recover a lost id with `jobs --all`.

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
only for an authorized action; otherwise use `reject APPROVAL_ID --reason "Outside the task"`.
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
or crashed turn; the bridge does not resume or retry that turn.

Stop an active job by full id or unique prefix:

```console
lazytools-code-bridge cancel JOB_ID --reason "operator stopped the task"
```

`--reason` is optional (default `requested cancellation`). Cancellation
kills the process tree (`taskkill /PID ... /T /F` on Windows; the CLI's
private process group on POSIX, falling back to the single pid if no
private group is available), marks the job `interrupted` with
`cancelled by operator: <reason>` and workspace evidence, and rejects its
still-open approval tickets. A dead process is also marked interrupted.
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

Both engines can use the web: Claude Code gets `WebSearch`/`WebFetch`
(allowed by the rule table, so they never ask), and Codex runs with live web
search set explicitly per job rather than inherited from `~/.codex/config.toml`.

The `--cwd` argument must resolve inside `$LAZYTOOLS_CODE_ROOT` / `--root`
(default: the launching directory). Codex uses workspace-write; Claude file
hooks confine file tools to `cwd`, while Bash is not path-confined. Requests
are gated by one `BridgeGate` (a `lazybridge.ext.approval.TieredGate`
with the structural steps described below) whose rule table
(`lazytools.code_bridge._policy.CODING_RULES`) mirrors the generic coding
rules in LazyCEO's own agent: reads/`Write`/`Edit`/`Bash` run free; `git
add`/`commit`/`push`/`gh pr create` get a one-time-per-run session grant;
dangerous push forms, pushing to `main`/`master`, `gh pr merge`, `git reset
--hard`, and anything matching `*rm *`/`*delete*` ask every time; and any
command a coding engine escalates beyond its own sandbox (Codex's
`approval_policy="on-request"`) asks, except read-only GitHub queries and
the session-granted simple `git add`/`git commit` described below. The channel behind "ask" is a
durable, `Store`-backed `ApprovalQueue`
(default `~/.lazytools/code-bridge.sqlite`, override with
`LAZYTOOLS_CODE_BRIDGE_DB` / `--db`) — the ticket survives this process
exiting, which is the whole point: a human answers it from an entirely
different process (this bridge's `approve`/`reject`, or any other surface
that reads the same queue).

### Structural approval rules (what no longer asks, and why it is safe)

On 17/10/2026 one day of delegated work filed about fifteen tickets in
fifteen minutes, nearly all harmless. Three changes, all in the bridge
itself (`_approvals.py`, `_policy.py`, `_engines.py`), remove that noise
without loosening anything that matters. The gate is a
`BridgeGate` — a `TieredGate` that consults explicit, unit-tested
classifiers on the **whole** command before the stock fnmatch rules.

| Request | Before | Now | Why it is safe |
| --- | --- | --- | --- |
| Codex runs `pytest` (needs `tmp_path`) | escalation → ask | runs **inside** the sandbox | The OS temp dir (`tempfile.gettempdir()`, plus `%TEMP%`/`%TMP%` when different) is an extra Codex writable root. It is per-user scratch space: no repo history, no other repository, no network. |
| Codex escalates a read-only GitHub query: `gh pr view\|checks\|diff`, `gh run view\|list`, `gh api` GET on `repos/...`, bare or inside `powershell.exe -Command '...'` | ask | **allow** | An allow-list over a strict tokeniser (see below). Codex escalates these only because its sandbox has no network; they read and never write. |
| Codex escalates `git add ...` / `git commit ...` | ask, every time | **session**: first asks, rest of the run granted | The same session semantics the Claude `Bash` rules already give `git add`/`commit`, for one simple command only — no chaining, no `git -C`, no `--amend`/`--no-verify`. Scoped to cwd and to that exact rule. |
| Claude `cat > file <<'EOF' ... EOF` whose *body* contains `delete`/`rm ` | ask | **allow** | Only the body of a *literal data* heredoc is hidden from the patterns (see below). The command line itself and everything after the terminator are matched as before. |

**The read-only `gh` classifier** (`is_readonly_gh_query`) returns "allow"
only when every check passes; anything it does not positively recognise
falls through to the catch-all `ask`. A false "ask" is cheap; a false
"allow" is not.

- One command only. Outside quotes, only `[A-Za-z0-9_./:,=+-]` are allowed —
  so `;`, `&&`, `||`, `|`, `>`, `<`, `(`, `{`, globs and `#` all mean "ask".
  `|` and `{}` are fine **inside** a quoted `--jq '...'` filter.
- `$`, backtick, `\` and any non-ASCII character mean "ask" anywhere, quoted
  or not (bash/PowerShell substitution; PowerShell treats Unicode smart
  quotes as quotes).
- The PowerShell wrapper is unwrapped only in its exact form:
  `powershell.exe|pwsh [-NoProfile|-NonInteractive|-NoLogo] -Command '<one payload>'`
  with nothing after the closing quote and no quote of the same kind inside.
- Per subcommand, an explicit list of permitted flags. `gh api` may carry only
  output/pagination flags — `-X`/`--method`, `-f`/`-F`/`--field`/`--raw-field`,
  `--input`, `-H` all mean "ask" — and exactly one positional matching
  `repos/<owner>/<repo>/...` (no `..`, no query string, no `graphql`).
- Any other `gh` subcommand (`pr merge`, `pr comment`, `release create`,
  `run rerun`, `auth token`, ...) and anything that is not `gh` mean "ask".

**Heredoc bodies** (`strip_literal_heredocs`, Claude `Bash` only). Stock
`TieredGate` splits a command on newlines, so every line of a heredoc body
was matched as if it were a command. A body is now hidden from matching only
when it is *data written to a file*: the heredoc feeds `cat`/`tee` with a
plain redirect target, nothing is piped, the delimiter is quoted (or the
body has no `$`, backtick or `\`), and the terminator line exists.
`bash <<EOF`, `python - <<EOF`, `cat <<EOF | sh` and `python -c "..."`
payloads are **code** and stay fully visible: hiding a `python -c` payload
would turn `python -c "os.system('rm -rf x')"` from ask into allow, so it was
deliberately not done (the brief's option A, restricted to data). The human
shown a ticket always sees the original command, body included.

**What still asks, unchanged:** every escalated command not covered above
(including `git push`, `gh pr create`, any `gh` write, `pytest` if it ever
escalates); merge, `git reset --hard`, `*rm *`, `*delete*`, pushes to
`main`/`master` and every dangerous push form for Claude `Bash`; `rm` of a
job's own scratch files (deliberately — `*rm *` is not weakened). Codex
sandbox **network stays off**: that is what forces every `git push` and `gh`
write to escalate, and so to reach a human.

## Engine concurrency limit (machine-wide)

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
- A slot is the same reclaimable lock file as the repository lock
  (`<store dir>/locks/engine-<engine>-<n>.lock`): a slot whose pid died is
  reclaimed on the next launch and its job marked `interrupted`. Slots live
  next to the job Store, so every launch sharing a Store shares the limit.

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
`~/.lazybridge/sessions.json` by default); a known name resumes it. Scoped
per repository, so the same name in two different repos is two different
sessions.

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

## Known limits

- A session grant for an escalated `git commit` runs the repository's commit
  hooks outside the sandbox. Codex's sandbox keeps `.git` read-only, but a
  repository configured with `core.hooksPath` pointing at a tracked
  directory lets a job edit the hook it will later trigger — the first
  commit escalation is the moment to notice that.

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
- `TieredGate`'s compound-command splitter is a text heuristic, not a shell
  parser (see its own module docstring): it does not see `$(...)`/backtick
  substitution inside a single non-compound command. This is the exact same
  property LazyCEO's own rule table already accepts for the same reason.
