---
name: code-bridge-cloud-setup
description: Walk the user through setting up lazytools-code-bridge cloud jobs (Codex Cloud and/or Claude Code cloud sessions), checking every prerequisite with commands, asking only for what cannot be checked, and filling ~/.lazytools/code-bridge.toml. Use when the user wants `run --where cloud` to work, asks how to set up Codex Cloud or Claude cloud for the bridge, or a cloud submit fails with a setup error.
---

# Code bridge cloud setup

You are helping a possibly non-expert user make
`lazytools-code-bridge run --where cloud --engine codex|claude` work. The full
human guide is `docs/code-bridge-cloud-setup.md` in the LazyTools repository.
Read it if you need detail. This file tells you how to run the session.

## Rules

- **Check, don't ask.** Run a command for every fact a command can establish.
  Ask the user only for things that exist only in a web UI: environment ids,
  routine ids, whether an app is installed when `gh` cannot tell, and which
  engines they want.
- **One step at a time.** After each check, say in one line what you found
  and what comes next. When a web step is needed, give the exact URL and the
  exact buttons to click, then wait for the user.
- **Never handle secrets.** Do not ask the user to paste tokens into the chat
  and never write a token into any file. Routine tokens go in an environment
  variable that the user sets. The config stores only the variable's NAME
  (`token_env`).
- **Never push, merge or change branches for the user.** If a branch is
  unpushed, show the `git push` command and let the user run or approve it.
- **Use structured briefs.** Include `Goal`, `Context`, `Constraints`, `Done
  when`, and `Report` headings so `brief-check` does not warn.
- **Edit `code-bridge.toml` minimally.** Read it first, keep every existing
  table (`[concurrency]` and others), add or update only the cloud keys, and
  show the diff before writing.

## Step 0: Which engines, which repository

Ask: "Codex Cloud, Claude cloud, or both?" and "Which repository (local path)?"
Then check:

```bash
git -C <repo> remote get-url origin        # must be github.com/<owner>/<name>
git -C <repo> rev-parse --abbrev-ref HEAD
git -C <repo> status -sb                   # ahead/behind origin?
lazytools-code-bridge run --help           # installed, and output must include --where
python -c "from lazytools.code_bridge import _concurrency as c; print(c.config_path())"   # config file path
gh auth status                             # optional; enables the GitHub checks below
```

Derive `owner/name` from the remote URL yourself. If the branch is ahead of
origin, tell the user that cloud agents only see pushed commits. If `run
--help` has no `--where`, update the editable LazyTools checkout or reinstall
`lazytoolkit[engines]`; the installed bridge predates cloud support.

Discover and record the real default branch rather than assuming `main`:

```bash
gh repo view <owner/name> --json defaultBranchRef --jq .defaultBranchRef.name
```

Passing `--branch main` to a repository without that branch makes the task
error with no diff.

## Step 1: Codex Cloud (if chosen)

Check:

```bash
codex --version                 # missing -> install the Codex CLI, or set CODEX_BIN
codex login status              # if unsupported, try a harmless `codex cloud --help`; not logged in -> `codex login`
codex cloud list --json         # environment_id and environment_label
```

Then the web part. Ask the user to do it and report back:

1. <https://chatgpt.com/codex/settings/environments> → **Create environment**.
2. Connect GitHub and select `<owner/name>`, **one repository per
   environment**. Verify a separate environment exists for every repository
   the user will map. Multi-repository web environments are invisible to the
   CLI and their tasks have no retrievable diff.
3. Setup script: suggest one from the repo, but make it non-interactive. For a
   Python project start with:

   ```bash
   set -e
   export GIT_TERMINAL_PROMPT=0
   timeout 600 python -m pip install -e '.[dev]' || timeout 600 python -m pip install -e . || echo 'full install needs private sibling repos; run the tests that do not import them'
   ```

   Read `pyproject.toml` / `package.json` and adapt the install (for LazyTools,
   use `.[test]`) without removing the prompt guard or bounded fallback.
   Otherwise a private-sibling prompt can cause `startup_script_timed_out`
   after roughly 20 minutes.
4. Internet access: off unless needed. Secrets: none that can push or reach
   production.
5. Ask for the **environment id** (`env_...`). It is the last part of the
   environment's settings URL or the `environment_id` beside its
   `environment_label` in `codex cloud list --json`.

Write:

```toml
[cloud.codex.envs]
"<owner/name>" = "<env id>"
```

## Step 2: Claude cloud (if chosen)

Check:

```bash
claude --version                # missing -> install Claude Code, or set CLAUDE_BIN
claude auth status              # if the subcommand exists; else check ~/.claude/.credentials.json exists (do NOT print it)
echo "${ANTHROPIC_API_KEY:+set}" # if "set": API keys are not accepted for cloud sessions; tell the user to unset it for these runs
gh api repos/<owner>/<name>/installation --jq .app_slug 2>/dev/null   # may need admin rights; if it fails, ask
```

- Not logged in → the user runs `claude auth login` (claude.ai account,
  Pro/Max/Team/Enterprise). Tell them the login must be renewed **every 30
  days**.
- GitHub App: if not confirmed, send them to <https://github.com/apps/claude>
  → Configure → select `<owner/name>`.
- Environment: there is **no CLI flag**. Ask the user to open
  <https://claude.ai/code>, edit or add an environment (setup script as in
  Step 1, limited network access, no secrets), and make it the **default**.
  Ask them to confirm it is done. Do **not** add `[cloud.claude.envs]`: the
  bridge refuses it.

Optional routine (only if the user wants HTTP-triggered runs): they create a
routine with an API trigger at claude.ai/code and tell you the routine id
(`trig_...`). They put the token in an environment variable themselves; you
suggest the name `LAZYTOOLS_ROUTINE_<NAME>_TOKEN`. Write:

```toml
[cloud.claude.routines.<name>]
id = "trig_..."
token_env = "LAZYTOOLS_ROUTINE_<NAME>_TOKEN"
```

## Step 3: Shared settings and validation

Make sure `[cloud]` exists (`concurrency = 4` is the default; keep the user's
value if present). Explain that this limit counts all providers together.
Then validate the file:

```bash
python -c "from lazytools.code_bridge import _cloud; c = _cloud.load_config(); print(c.concurrency, c.envs, sorted(c.routines))"
```

A `ValueError` names the bad key. Fix it and re-run.

## Step 4: First real job

Propose a harmless brief and let the user approve the submit (it starts a
billed cloud task):

```bash
lazytools-code-bridge run --where cloud --allow-cloud --engine codex  --cwd <repo> --branch <default-branch> --task @brief.md
lazytools-code-bridge run --where cloud --allow-cloud --engine claude --cwd <repo> --branch <default-branch> --task @brief.md
lazytools-code-bridge wait <job>
lazytools-code-bridge result <job>
lazytools-code-bridge apply <job>
```

Explain what they will see:

- Cloud jobs use the local lifecycle: they appear in `jobs`; `wait` waits and
  `result` reads the result. Reviews use the opposite engine, as locally.
- `--where cloud` requires `--allow-cloud`, takes `--branch`, and does not
  accept `--session`.
- Codex: `wait` polls the task. A ready task with zero files can be correct if
  the requested change is already present; check the base before calling it a
  failure. Then `apply <job>` creates a worktree at the task's base commit.
  Codex normally has no Git remote and cannot push or open a PR. Review with
  the opposite engine, then have the user commit, push, and open the PR from
  the applied worktree. Landing automation is tracked separately.
- Claude: `status` says `submitted (no status API): open <url>` until the
  session pushes a `claude/...` branch. Then `result` shows its diff and
  `apply` checks it out in a new worktree. Nothing is pushed.

Optional and untested: Codex might push only if Git credentials reach it
through environment setup (secrets are visible only to setup) and agent
internet access permits `github.com`. Never put a token in setup-script text;
do not recommend this over the supported `apply` workflow. A Codex GitHub app
comment saying “create an environment for this repo” means that repository
does not yet have its own environment.

After `apply`, guide the user through this explicit handoff without executing
the write operations yourself:

1. Inspect the result and run the repository tests in the new worktree.
2. Request a review with the opposite engine.
3. Show the appropriate `git add` and `git commit` commands.
4. Show `git push -u origin <new-branch>` and the `gh pr create` command.

If a step fails, match the error against the "Common errors" tables in
`docs/code-bridge-cloud-setup.md` and fix only that step.

## Done

Summarise: engines configured, repository, environment id (Codex), default
environment confirmed (Claude), routines (names only), and the 30-day
re-login reminder for Claude.
