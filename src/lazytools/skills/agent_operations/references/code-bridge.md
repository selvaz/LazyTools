# The async code bridge

`lazytools-code-bridge` (also `python -m lazytools.code_bridge`) runs one
Codex or Claude Code job per call, in a background OS process, with
approvals filed to a durable ticket queue instead of blocking a synchronous
tool call. Install `lazytoolkit` with the `engines` extra for `--engine
claude` or any `--tier` routing (both need the Claude Agent SDK). Full
reference: LazyTools `docs/code-bridge.md`.

## Lifecycle at a glance

| Command | Use it to |
| --- | --- |
| `run` | Start a job. Foreground: the job id is the first stdout line, the result follows. `--detach`: start an independent worker and return at once (`--json` prints one launch object with id, pid and log path). |
| `wait-any JOB_ID JOB_ID` / `wait-any --all-active` | Block until the first selected event (`--events done,failed,approval,landed,interrupted`) on several jobs; JSON lines; `--loop` until all are terminal. |
| `wait JOB_ID` | Block until the job ends. Exit 0 `done`; 1 `partial`/`failed`/`interrupted` or the process died; 3 waiter timeout (`--timeout` stops only the waiter, never the job). |
| `pending --json` | List open approval tickets (`approval_id`, `job_id`, gist, command, reason, expiry). |
| `approve APPROVAL_ID` / `reject APPROVAL_ID --reason TEXT` | Decide a ticket. `--actor NAME` records who decided. |
| `status JOB_ID` / `result JOB_ID` | Current status / final text and full record (`--json` keeps metadata: routing, turns, commits, workspace evidence). |
| `jobs [--all]` | Active jobs, or history to recover a lost id. |
| `cancel JOB_ID [--reason TEXT]` | Kill the process tree, mark `interrupted`, reject its open tickets, keep workspace evidence. |
| `route` | Explain a quota-aware pick without launching (see `routing-and-review.md`). |
| `models` | Read live model availability and audit the catalogue. |
| `apply JOB_ID` | Cloud jobs only: apply the diff in a new worktree on a new branch. |
| `prune-sessions [--apply]` | Delete old transcripts of automated sessions (dry run by default). |

Every lifecycle command must use the same Store: default
`~/.lazytools/code-bridge.sqlite`, or `LAZYTOOLS_CODE_BRIDGE_DB`, or `--db`.

## Launching

```text
lazytools-code-bridge run --detach --tier writing --cwd REPO --task @brief.md --session feat-x --json
lazytools-code-bridge run --detach --engine codex --cwd REPO --task @brief.md --json
```

- `--task @brief.md` reads the brief from a file. The launcher snapshots the
  text it checked, so editing the file afterwards changes nothing.
- `--tier` or `--engine` is required; a tier without an engine lets quota
  decide.
- `--session NAME` creates or resumes a named native conversation, scoped to
  the repository. A session already used by a bridge job pins later jobs to
  that engine; pick a new name to switch engines. Use the writer's session
  for rework so it keeps its context.
- `--cwd` must resolve inside `$LAZYTOOLS_CODE_ROOT` / `--root` (default:
  the launching directory).
- `--strict` refuses to launch when the brief check finds an error;
  `--no-brief-check` skips it. See `briefs.md`.
- `--profile review` (local reviewer jobs) auto-approves only a narrow,
  read-only command list (`gh pr view|diff|checks`, read-only git, test
  runners, file readers) and holds everything else for a human.
- `--json` prints one JSON object on the first stdout line with `job_id`,
  `short_id`, `where`, `engine` (plus `pid` when detached, `task_url` for
  cloud) — parse that instead of scraping text.

**Why detach:** a foreground `run` is a child of the launching shell; if the
host reaps that shell or the session ends, the job dies with it. A detached
worker outlives the session. Its launch returning is *not* completion.

## Waiting without polling

Launch `lazytools-code-bridge wait JOB_ID` (or `wait-any` for several jobs)
in the host's background facility
(in Claude Code, the Bash tool's background option) and let the host's
process-exit notification wake you. Meanwhile check `pending --json` when
there is reason to expect a ticket, not in a tight loop. A killed waiter can
be restarted at any time without touching the worker.

## Approvals

Each ticket is a real decision: a push, PR creation, the first `git add` /
`git commit` escalation, a delete, a merge. Read the command and reason,
compare with the brief, then `approve` or `reject --reason`. Unanswered
tickets expire as rejections after `LAZYTOOLS_CODE_BRIDGE_TICKET_TTL`
seconds.

What runs without asking: file reads, `Write`/`Edit`, web search/fetch,
audited routine-auto commands (recorded with `approved_by = routine-auto`),
the packaged read-only allow-list, and — after the first human approval in a
run — simple `git add`, `git commit`, `git push origin <the job's own
branch>` and a narrow `gh pr create`. Always asking: pushes to `main` /
`master`, `gh pr merge`, `git reset --hard`, anything matching `rm` or
`delete`, and arguments with quotes, backslashes, backticks or `$`. That last
rule is why briefs should ask for `git commit -F <file>` and
`gh ... --json <fields>` without `--jq`.

The gate checks command text, not file contents, and Claude's Bash is not
path-confined. It is a capability check, not an operating-system sandbox.

## Timeouts, continuations and evidence

- `--timeout SECONDS` is a **per-turn** budget (default 3600), including
  approval waits.
- A turn that times out **with a new commit or a push to the job's starting
  branch** checkpoints and continues on the same thread with the message
  "continue from the last commit; re-read the brief's Done-when; do not redo
  committed steps". `--max-continuations N` (default 3, max 10, 0 disables).
- A timeout **without** a new commit fails with `no progress:`. Dirty files do
  not count. This is why briefs demand a first commit within minutes and a
  commit per step.
- `--max-wall SECONDS` caps the whole job, approvals and slot waits included.
- Failed, partial and interrupted jobs record a workspace line, e.g.
  `workspace: HEAD abc1234 -> def5678 (2 commit(s) not reachable from start); 1 uncommitted path(s)`.
  Read it before relaunching.

## Locks and concurrency

- **One job per repository.** A second `run` on the same repository is
  refused while the first is alive; a dead owner's lock is reclaimed and its
  job marked `interrupted`. Wait for the writer before launching its
  reviewer on the same checkout.
- **Machine-wide engine slots.** Defaults: Codex 1, Claude unlimited;
  configure under `[concurrency]` in `~/.lazytools/code-bridge.toml`. At the
  limit, `run` is refused, `--wait-for-slot` queues, and `--tier` routing
  sends the job to the other engine when eligible.
- Keep heavy local test suites serial; parallelism belongs in the cloud.

## Cloud jobs: `--where cloud` and `--where auto`

A cloud job hands the brief to the engine's cloud agent and returns at once:
no local engine process, no local engine slot.

| Engine | Cloud agent | How results come back |
| --- | --- | --- |
| `--engine codex` | Codex Cloud (official `codex cloud` CLI) | provider status + diff; `apply` patches it into a new worktree |
| `--engine claude` | Claude Code cloud session, fired through a routine API trigger (`--routine NAME` or `[cloud.claude] default_routine`) | no status API: the job watches for its own `claude/...` branch with `git ls-remote`; the session URL is the authority on whether it finished |

```text
git push -u origin feat-x            # the agent starts from the REMOTE branch
lazytools-code-bridge run --where cloud --allow-cloud --engine codex --cwd REPO --task @brief.md --branch feat-x
lazytools-code-bridge run --where cloud --allow-cloud --engine claude --cwd REPO --task @brief.md --routine nightly
lazytools-code-bridge wait JOB_ID    # cloud waits give up after 7200 s without --timeout
lazytools-code-bridge result JOB_ID  # final message + diff summary
lazytools-code-bridge apply JOB_ID   # new worktree + branch cloud/<job8>; never your checkout, never pushed
```

**Setup.** Map each repository to a single-repo cloud environment in
`~/.lazytools/code-bridge.toml`; `lazytools-code-bridge cloud-envs --cwd REPO`
shows what is mapped, and `lazytools-code-bridge setup-skill --install` installs
a step-by-step setup skill for an assistant.

```toml
[cloud]
concurrency = 4

[cloud.codex.envs]
"owner/repo" = "env_XXXXXXXX"

[cloud.claude.routines.nightly]
id = "trig_XXXXXXXX"
token_env = "ROUTINE_TOKEN_VAR"   # the NAME of an environment variable, never the token
repo = "owner/repo"
branch = "main"
```

**Rules that always hold.**

- `--allow-cloud` is mandatory: the agent's commands run on the provider's
  machine, outside the local gate. A brief that mentions a gated action is
  refused unless approved up front with `--approve-cloud
  push|pr-create|merge|delete|reset|commit|git-option` (repeatable).
  Detection is keyword-based and fail-closed, so a harmless mention also
  needs the flag.
- `--where cloud` needs an explicit `--engine` and refuses `--session`,
  `--effort`, `--needs`, `--detach`, `--wait-for-slot`, `--max-wall` and the
  routine-auto options. Codex Cloud uses the model set on its environment
  (`--model` is refused); Claude cloud takes `--model` or a tier mapping, but
  not with `--routine`.
- **Data boundary.** The agent sees only the repository as cloned from the
  remote at that branch: no local files, unpushed commits, local databases or
  production connections, and some hosts may be unreachable. Tell the agent,
  supply fixtures or recorded data, and run live smoke tests locally after it
  pushes.
- Never put secrets in a brief: records are redacted, but the provider
  receives the brief as written.
- Cloud jobs have their own `[cloud] concurrency` limit (default 4) and take
  no local engine slot. Many cloud agents in parallel are fine; keep each on
  its own branch.

**Turning results into PRs.**

- `--pr N` works on pull request N's head branch; `--open-pr` lands on a new
  branch with a new PR; `--title` sets the commit/PR subject.
- `--land` lands automatically when the job finishes (`wait`, `wait-any` or
  `cloud-sync`): commit, fast-forward push, update or open the PR. It never
  force-pushes; a base that moved since submit is refused unless the job was
  submitted with `--rebase-ok`. `apply JOB_ID --land` does the same by hand.
- `lazytools-code-bridge cloud-sync` reconciles every live cloud job (lands,
  posts reviews, records failures) and is safe to run from a scheduler.
- `run --where cloud --allow-cloud --cwd REPO --review-of JOB_ID` runs the
  opposite engine as a cloud reviewer on the landed branch; its verdict
  (`blocking` / `no-blocking`) is posted as one PR comment bound to the
  reviewed head SHA.
- Check what landed before merging: `result JOB_ID` shows the pushed SHA and
  PR URL. Read the diff yourself; landing is not acceptance.

**Placement: `--where auto`.** Runs in the cloud when the local time is inside
a configured `[cloud] busy_windows` range or no local engine slot is free,
locally otherwise; the decision and reason are printed on stderr.
`route --tier writing --where auto --cwd REPO` previews it. Without `--where`,
`[cloud] default_where` decides (default `local`). A cloud decision still
needs `--allow-cloud` and an explicit `--engine`.

**Stopping.** `cancel JOB_ID` asks the provider to stop the task and then stops
tracking it; `cloud-stop TARGET` (task URL, task id or job id) and
`cloud-stop --all-running --dry-run` cover tasks with no bridge job. A
reported `stop requested` is not proof the task stopped: check its URL.

## Synchronous tools (MCP)

For short turns and strictly read-only reviews, the `lazytools-mcp` server
exposes `codex_code_review`, `codex_ask`, `codex_review_changes`
(`scope` = `uncommitted` | `branch` | `commit`), `claude_code_review`,
`claude_ask`, and the writers `codex_write` / `claude_code_write`
(`--allow-unsafe`). Reviewers run in a read-only sandbox; a review that
could not run is an error, never a clean review.

## Housekeeping

- `prune-sessions --older-than-days 7` lists old automated transcripts;
  add `--apply` to delete.
- Remove worktrees created by `apply` once merged
  (`git worktree remove <dir>`, then delete the branch).
