# Cloud setup for the code bridge (Codex Cloud and Claude Code)

This page is a step-by-step guide to running `lazytools-code-bridge` jobs **in
the cloud** with either engine. It assumes no prior experience with Codex
Cloud or Claude Code on the web. Each part ends with a first test command and
the most common errors.

!!! tip "Let an assistant walk you through it"
    LazyTools ships an agent skill for this setup,
    `code-bridge-cloud-setup` (see [Code bridge cloud setup skill](skills-code-bridge-cloud-setup.md)).
    Point Claude Code (or any assistant that reads `SKILL.md` files) at it. It
    checks each prerequisite with a command, asks you only for what it cannot
    check, and fills in `code-bridge.toml`.

What a cloud job is, and what it can and cannot see, is described in
[Async Code Bridge → Cloud jobs](code-bridge.md#cloud-jobs-run-where-cloud).
The short version: the cloud agent works on a fresh clone of your GitHub
repository and **cannot read anything on your machine**. Its work comes back
as a diff (Codex) or a pushed `claude/...` branch (Claude), which you check
out with `lazytools-code-bridge apply` into a new worktree. The bridge never
pushes your branches and never merges.

## Before you start

You need:

- the repository on **GitHub**, with the branch you want the agent to start
  from **pushed** (`git push -u origin <branch>`);
- `lazytools-code-bridge` installed (`pip install "lazytoolkit[engines] @ git+https://github.com/selvaz/LazyTools.git"`);
- the bridge config file, `~/.lazytools/code-bridge.toml` (or the file named by
  `LAZYTOOLS_CODE_BRIDGE_CONFIG`). Create it empty if it does not exist.

Check what you have:

```bash
git -C <repo> remote get-url origin   # must be a github.com URL
git -C <repo> status -sb              # "## main...origin/main" with nothing ahead
codex --version                       # Codex part only
claude --version                      # Claude part only
gh auth status                        # optional: helps with the GitHub steps
```

## Part 1: Codex Cloud

### 1. Create an environment

1. Open <https://chatgpt.com/codex> and sign in with your ChatGPT account.
2. Go to **Settings → Environments** (<https://chatgpt.com/codex/settings/environments>)
   and click **Create environment**.
3. **Connect GitHub** when asked, and grant access to the repository (you can
   limit the ChatGPT GitHub connector to selected repositories).
4. **Pick the repository.** Use one environment per repository: the bridge
   maps `owner/name` to exactly one environment.
5. **Setup script.** Commands run once when the container is built, for
   example for a Python project:

    ```bash
    pip install -e '.[dev]'
    ```

    (For LazyTools itself: `pip install -e '.[test]'`.)
6. **Internet access.** Leave it **off** unless tasks need to download
   packages after setup. If you turn it on, prefer an allow-list of domains.
7. **Secrets / environment variables.** Add only what the setup script needs.
   Do **not** add push credentials or production secrets: the agent's
   commands are not gated one by one.
8. Save.

### 2. Find the environment id

Any one of these works:

- **CLI:** `codex cloud` opens an interactive browser of tasks and
  environments; the environment id is shown when you pick an environment.
  Recent CLI versions also accept `codex cloud exec --env <id> ...`, so the id
  you copy is exactly what the bridge passes.
- **URL:** open the environment in the web UI; the id is the last part of the
  page URL (`.../environments/<env id>`).

### 3. Map it in `code-bridge.toml`

The key is the `owner/name` of the repository's `origin` remote:

```toml
[cloud]
concurrency = 4               # live cloud jobs at once, both engines together

[cloud.codex.envs]
"owner/name" = "<env id>"
```

### 4. Log in and test

```bash
codex login        # once; uses your ChatGPT account
lazytools-code-bridge run --where cloud --allow-cloud --engine codex \
    --cwd <repo> --task "Add a one-line docstring to the main module"
lazytools-code-bridge wait <job>
lazytools-code-bridge result <job>
lazytools-code-bridge apply <job>      # new worktree + branch; nothing pushed
```

### Common errors (Codex)

| Message | Fix |
| --- | --- |
| `no Codex Cloud environment mapped for owner/name` | Add the `[cloud.codex.envs]` line; check the key matches `git remote get-url origin` |
| `Codex CLI not found` | Install the Codex CLI, or set `CODEX_BIN` to its path |
| `codex cloud exec exited ...: not logged in` | Run `codex login` |
| `... is on a detached HEAD` | Check out a branch, or pass `--branch` |
| `origin/<branch> has moved ...` on `apply` | Someone pushed since submit; re-run with `--rebase` after reviewing |
| `--where cloud requires --allow-cloud` | Add `--allow-cloud` (an explicit acknowledgement) |
| `the brief asks for gated action(s) push` | Remove the request from the brief, or add `--approve-cloud push` |

## Part 2: Claude Code cloud sessions

`--engine claude` runs `claude --cloud "<brief>"`: Claude Code starts a hosted
session on claude.ai, which clones the repository from GitHub, does the work,
and pushes a `claude/...` branch.

### 1. Install the Claude GitHub App

Install the app on the repository: <https://github.com/apps/claude>
(**Configure → Only select repositories → your repo**). The hosted session
uses it to clone and to push its `claude/...` branch. If you cannot install
apps on the organization, ask an owner.

### 2. Log in with your claude.ai account

```bash
claude --version
claude auth login      # opens the browser; sign in with your claude.ai account
claude auth status     # if your CLI has it: shows the logged-in account
```

- Cloud sessions need the **claude.ai login** (a Pro, Max, Team or Enterprise
  plan). **API keys are not accepted** for `claude --cloud`; unset
  `ANTHROPIC_API_KEY` in that shell if it is set.
- The token is stored in `~/.claude/.credentials.json`. It expires: **log in
  again every 30 days**. A submit that fails with a login/auth error tells you
  to run `claude auth login`.

### 3. Choose the default cloud environment

There is **no command-line flag to choose the environment** for
`claude --cloud`. It uses the default environment selected for your account
(or organization) at <https://claude.ai/code>:

1. Open <https://claude.ai/code>, then the environment selector (**Environment
   → Add / Edit**).
2. **Setup script:** for example `pip install -e '.[dev]'`.
3. **Network access:** keep the default limited access unless tasks need
   more; avoid full access.
4. **Environment variables:** only what the setup needs; never push tokens or
   production secrets.
5. Make it the **default** environment.

The bridge records this on every job (`env_id = "claude.ai-default"` and an
`environment_note`), so it is clear later which settings applied. No
`[cloud.claude.envs]` table exists, and the config loader refuses one.

### 4. Test

From a checkout whose branch is pushed:

```bash
lazytools-code-bridge run --where cloud --allow-cloud --engine claude \
    --cwd <repo> --task "Add a one-line docstring to the main module"
```

The bridge:

1. checks that the branch (current, or `--branch B`) is on `origin` **at the
   same commit** as your local branch. If you have unpushed commits it refuses
   and shows the `git push` command. It never pushes for you;
2. if your checkout is on a different branch, runs `claude` from a temporary
   worktree on `B` and removes it afterwards (`claude --cloud` has no
   `--branch` flag: it uses the current branch);
3. records the session id and URL that `claude` prints.

### 5. How results arrive

Claude cloud sessions have **no status API**: the bridge cannot ask whether
a session finished, and cannot read its transcript.

- `status <job>` shows `submitted (no status API): open <session url>` until
  a **new `claude/...` branch** appears on `origin`. That means a branch that
  did not exist, or pointed elsewhere, when the job was submitted. If the brief
  names a branch (`... leave the result on claude/fix-login`), only that one counts.
- Once such a branch appears, the job becomes `done`. `result <job>`
  summarises its diff against the base commit, and `apply <job>` checks it out
  in a **new worktree on a new local branch** (`cloud/<job8>`), never pushing.
  The session may still be adding commits: open the session URL to confirm it
  has finished, then `git -C <worktree> pull origin <claude branch>` if needed.
- If several new `claude/` branches appear, the newest is used and the others
  are listed. Pick another with `apply <job> --source claude/<name>`. Two
  bridge jobs never claim the same branch.
- A session that never pushes keeps its job `running` and holds a cloud
  concurrency place until you `cancel` it. `cancel` stops tracking only: stop
  the session itself at its URL.

### 6. Optional: a routine with an API trigger

A **routine** is a saved Claude Code cloud task with its own repository,
branch, environment and prompt. Firing one over HTTP needs no local `claude`
CLI and no local checkout of the branch.

1. At <https://claude.ai/code>, create a routine for the repository and add an
   **API trigger**. Copy the routine id (`trig_...`) and the **token** shown
   once.
2. Store the token in an **environment variable**, never in the config:

    ```bash
    export LAZYTOOLS_ROUTINE_NIGHTLY_TOKEN='<token>'     # Windows: setx LAZYTOOLS_ROUTINE_NIGHTLY_TOKEN "<token>"
    ```

3. Name the routine in `code-bridge.toml`:

    ```toml
    [cloud.claude.routines.nightly]
    id = "trig_0123"
    token_env = "LAZYTOOLS_ROUTINE_NIGHTLY_TOKEN"   # the variable's NAME
    ```

4. Fire it with the brief as its text:

    ```bash
    lazytools-code-bridge run --where cloud --allow-cloud --engine claude --routine nightly \
        --cwd <repo> --task @brief.md
    ```

The bridge sends `POST https://api.anthropic.com/v1/claude_code/routines/<id>/fire`
with `Authorization: Bearer <token>`, `anthropic-version: 2026-10-21` and
`{"text": "<brief>"}`, and records the returned `claude_code_session_id` and
`claude_code_session_url`. Results arrive the same way (a `claude/...` branch
on the repository in `--cwd`). A config entry with a literal `token` key is
refused.

### Common errors (Claude)

| Message | Fix |
| --- | --- |
| `Claude Code CLI not found` | Install Claude Code, or set `CLAUDE_BIN` |
| `claude --cloud failed: ... -- run claude auth login` | Log in again (the login lasts 30 days); unset `ANTHROPIC_API_KEY` |
| `branch 'X' is not on origin` | `git push -u origin X`, then resubmit |
| `local X (...) differs from origin/X` | Push (or pull) so local and remote match |
| `... is already checked out at ...` | The branch is open in another worktree: run from there, or use another branch |
| `[cloud.claude.envs] is not supported` | Remove it; choose the environment at claude.ai/code |
| `environment variable ... is not set` (routine) | Export the token variable named by `token_env` |
| `routine ... fire returned HTTP 401` | The routine token is wrong or was regenerated |
| `status` stays `submitted (no status API)` | Open the session URL. If the session did not push a `claude/` branch, ask it to there, or `cancel` the job |

## Part 3: the end-to-end loop (no hand-offs)

With one engine set up (or both), a lead never has to copy a diff, push or open
a PR by hand. You also need the GitHub CLI logged in for the PR steps
(`gh auth login`, or `GH_TOKEN` in the environment that runs the bridge).

```bash
# 1. Submit. --land: land the result on its own when the task finishes
#    (--open-pr: a new branch + PR; --pr 42: work on, and push to, PR 42's branch).
lazytools-code-bridge run --where cloud --allow-cloud --engine codex --cwd <repo> --task @brief.md --land

# 2. Follow it. `wait` lands this one job; `cloud-sync` reconciles ALL live cloud jobs.
lazytools-code-bridge wait <job>
lazytools-code-bridge cloud-sync          # safe to run from cron / Task Scheduler / a routine

# 3. Independent review by the OPPOSITE engine, posted to the PR as one comment.
lazytools-code-bridge run --where cloud --allow-cloud --cwd <repo> --review-of <job>
lazytools-code-bridge cloud-sync          # posts the review comment when the reviewer finishes
```

What happens at each step is in [Cloud agents as part of the
workflow](code-bridge.md#cloud-agents-as-part-of-the-workflow). In short:

* Codex Cloud's container has no git remote, so it **cannot push**. The bridge
  applies its diff in a worktree, commits, pushes the branch (or the PR's head
  branch, fast-forward only) and opens or updates the PR with `gh`. That is the
  manual step `--land` replaces.
* Claude Code cloud pushes its own `claude/...` branch; `--land` verifies it,
  records the commit and opens or updates the PR.
* Landing is refused, with the reason, when the base branch moved since submit
  (add `--rebase-ok` to the submit to allow it) and never force-pushes.
* A failed cloud task is marked `failed` with the provider's reason by `wait` or
  `cloud-sync`.

To run `cloud-sync` on a schedule, any scheduler that can run a command works,
for example `*/10 * * * * lazytools-code-bridge cloud-sync` in cron. It exits 1
when a landing or review post failed, so the scheduler can alert on it.

Check which environments the bridge knows with `lazytools-code-bridge cloud-envs`
(Codex environments and the repo each serves, the Claude cloud environment and
any routines). To let jobs choose where to run by themselves, set
`default_where = "auto"` and your production windows in `code-bridge.toml`:

```toml
[cloud]
default_where = "auto"                 # run goes to the cloud when busy, else local
busy_windows = ["21:00-01:30"]         # local time; empty (the default) means never "busy by the clock"

[cloud.claude.models]                  # Claude cloud model per tier (default: claude-sonnet-5-5)
critical = "claude-opus-5-5"
```

`lazytools-code-bridge route --tier writing --where auto` shows the decision and
why. Codex Cloud runs the model its environment is set to; the CLI ignores
`-c model` for cloud tasks, so there is nothing to configure here (change it in
the environment's settings).

### Optional and untested: letting Codex Cloud push by itself

By default a Codex Cloud task cannot push because its container has no git
credentials. In principle it could, if you provide credentials to the
environment through its **setup script** (for example a fine-grained,
single-repository token with write access to branches, written into a
credential helper by the script) and tell the agent which branch to push. This
is **untested** here and not needed: landing from the diff is the supported
path, and it keeps the token out of the container. If you try it, treat that
token as exposed to everything the agent runs (the same data-boundary rules
apply), scope it to one repository, and prefer short-lived tokens.

## Safety rules (both engines)

- `--allow-cloud` is required for every cloud submit.
- Briefs asking for push, PR creation, merge, delete, reset, commit or `git -C`
  are refused unless approved up front with `--approve-cloud <action>`.
- Provider output and the stored brief are redacted (tokens, keys, JWTs).
- Cloud jobs share one limit, `[cloud] concurrency` (default 4), and never take
  a local engine slot.
- `apply` creates a new worktree and branch and never pushes. Pushing happens
  only when you ask for it: `apply --land`, or a job submitted with `--land`
  landed by `wait` / `cloud-sync`. Landing refuses a moved base unless
  `--rebase-ok`, never force-pushes, and never merges: merging stays a human,
  approved step.
