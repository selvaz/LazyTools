# Daily ecosystem realignment plan

`lazytools-ecosystem-sync check` inspects the selvaz workspace and writes a
dated plan. It makes no model calls and never applies a proposed action.
The only Git write it can perform is `git fetch --prune origin`; use
`--no-fetch` to inspect the currently cached remote refs. Git status uses
optional locks disabled, and the sibling audit runs with bytecode writes
disabled. Output files and an explicitly requested Telegram notification are
the other side effects. The `LazyTools-mcp-main` production editable checkout
is excluded from filesystem/Git inspection, including when listed as a worktree.

```powershell
lazytools-ecosystem-sync check
lazytools-ecosystem-sync check --no-fetch --out-dir .tmp\ecosystem-plan
lazytools-ecosystem-sync check --root C:\Users\Administrator\Documents\GitHub --stale-days 7 --telegram
```

From a source worktree, without changing the shared editable install:

```powershell
$env:PYTHONPATH = "$PWD\src"
& C:\ProgramData\spyder-6\python.exe -B -m lazytools.ecosystem_sync.cli check --no-fetch --out-dir .tmp\ecosystem-plan
```

Run the command once per day using the scheduler owned by your environment.
The command does not register a scheduled task. Repeated runs on the same
local calendar day replace that day's plan. The default destination is
`D:\ecosystem-sync` when D: exists, otherwise `~/.lazytools/ecosystem-sync`.
Files are `YYYY-MM-DD-plan.md` and `YYYY-MM-DD-plan.json`. The Markdown starts
with exactly ten summary lines, then action sections and informational facts.
Every action has a repository, fact, exact PowerShell command and risk.

| Class | Meaning |
| --- | --- |
| `auto_safe` | Cleanup candidate: merged clean worktree, unchanged merged remote branch, or old temporary files. Revalidate before applying. |
| `session` | Owning development session must reconcile dirty/unpushed work, PR findings, review coverage, CI or a new release. |
| `owner` | Deliberately behind LazyBricks checkout, production drift, runtime cleanup or disk capacity decision. |

`auto_safe` is a classification for a future executor; **check never executes
it**. Worktree removal commands omit `--force` and preserve local branches.
Remote branch deletion proposals include an exact SHA lease and are omitted
when an open PR uses the branch, the merged head differs from the cached
remote tip, or the PR comes from a fork. Closed but unmerged PRs, locked
worktrees, dirty worktrees and production runtimes never receive automatic
worktree removal proposals. Temporary directories must be older than
`--stale-days` according to their newest observed mtime; they still need a
check for active processes and artifacts to preserve before removal.

The inspection covers:

- Every direct child with a `.git` directory or file, grouped by common Git
  directory to fetch once and collect branch/PR evidence once. Every direct
  checkout has its own branch/detached HEAD, origin default-branch divergence,
  tracked dirty count and untracked count. GitHub supplies the default branch;
  local non-GitHub origins use `origin/HEAD`.
- Library checkouts exactly at the manifest release tag are informational,
  even when behind the default branch. Authority is
  `ecosystem/compatibility.toml` from this checkout, also included in wheels;
  `--manifest` selects another file. LazyBridge's tag derives from
  `[verified].lazybridge`; SHA-pinned LazyFin remains outside the release train.
- Local branches with commits unreachable from any cached remote branch.
  Dirty files older than seven days are marked stale; `--stale-days` changes
  the threshold. File mtimes are an age proxy, not the original edit date.
  Deleted-only changes have unknown age. Linked worktrees outside the root,
  including `_wt` and `D:\wt`, come from `git worktree list --porcelain -z`.
- Worktree branches and their latest matching PR's open/closed/merged state,
  using `gh pr list --state all --head ...`. Dirty linked worktrees receive
  their own preservation actions.
- Open PR ages, `gh pr checks` buckets and the latest Codex connector summary
  containing `has_unresolved_findings`, across paginated conversation comments,
  reviews and inline comments. A review covers the current head only with an
  exact full SHA in its summary or matching `commit_id`. Unknown coverage or
  an unparseable findings flag blocks readiness. Every CI check must pass.
  A ready PR proposes the read-only invocation of `safe_merge.sh`; the actual
  merge still requires reading every comment and supplying its current ack.
- Latest locally known `v*` release tag versus the manifest, plus runtime tag
  and installed environment evidence collected by the complementary audit.
  `--no-fetch` can therefore show stale release/branch evidence.
- Free GB on C: and D:, `_wt/*/.tmp` sizes and leftover subdirectories under
  `D:\pytest-tmp`. Symlinks are not followed during temporary-file sizing.

When present, `investmentcommittee/scripts/ecosystem_audit.py:drift()` is
imported in an isolated child and called with this command's manifest. Its
existing runtime/env/MCP/news-origin checks remain authoritative; this command
does not implement a second version of those checks. The JSON includes its
raw problems, observed runtime tags and installed package versions. Audit
coverage follows the installed sibling script. As in its watchdog, the adapter
also calls the existing `news_origin_drift()` helper when present, with the
cutover from `config/job_health.toml` and DB path from `config/databases.toml`.
The adapter does not implement origin SQL or modify either configuration.

Git and GitHub runners are injectable, and every subprocess has a timeout:
60 seconds per Git/GitHub read or fetch, 300 seconds for the entire audit
child (the sibling's own probes are also bounded). A failed repo or PR read
becomes an incomplete-evidence action while other repositories continue.
PR lists are bounded to 1,000 results; reaching the bound marks coverage
incomplete instead of silently claiming completeness. Authenticated `gh` and
Git access are required for a complete real-workspace report.

Exit codes follow G6: **0** aligned (informational observations allowed),
**2** actions found, **1** check failure/incomplete evidence or output/delivery
failure. Check failures take precedence over actions. JSON carries all raw
structured observations as well as the action list and errors.

Telegram is opt-in with `--telegram`. It uses the existing
`TelegramClient.from_token`, `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
The ten-line summary is sent as a message up to 4,000 characters, otherwise
as a document. Delivery failure preserves the local plan and records exit 1;
credential values are never included in the failure finding.

Offline tests use temporary real Git repositories and local bare origins:

```powershell
$env:PYTHONPATH = "$PWD\src"
& C:\ProgramData\spyder-6\python.exe -m pytest tests\test_ecosystem_sync.py -q -x -p no:cacheprovider --basetemp D:\pytest-tmp\eco-sync
```
