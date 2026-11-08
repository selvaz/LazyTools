# Async code bridge

Install the `engines` extra for Claude jobs and quota-aware tiers. Run from a workspace containing `<repo>`; the default code root is the launch directory. Authenticate the local coding runtimes first.

```sh
lazytools-code-bridge route --tier writing --cwd <repo>
lazytools-code-bridge run --detach --tier writing --cwd <repo> --task @brief.md --session writer --json
lazytools-code-bridge pending --json
lazytools-code-bridge wait <job-id>
lazytools-code-bridge result <job-id> --json
```

Retain the detached launch's job ID; launch a background waiter if the host supports it. A waiter timeout does not cancel the job. Poll pending tickets while waiting; inspect the command and reason and obtain any needed authorization. Then use `lazytools-code-bridge approve <approval-id>` or `lazytools-code-bridge reject <approval-id> --reason "reason"`. Tickets expire as rejections. Recover IDs with `lazytools-code-bridge jobs --all`; inspect with `lazytools-code-bridge status <job-id>`. Use the same `--db` on lifecycle commands if selecting a non-default store. Verify the resulting diff and tests yourself.

Tiers are `basic`, `writing`, `thinking`, and `critical`; `route` previews quota-based selection without launching. `--engine codex` or `--engine claude` selects an engine directly, or restricts a tier. A session alias stays tied to its engine and repository. Run the reviewer after the writer finishes, because jobs share a repository lock:

```sh
lazytools-code-bridge run --detach --tier writing --review-of <job-id> --cwd <repo> --task "Review the changes; report defects and do not edit." --session reviewer --json
```

`--review-of` requires a tier and selects the opposite engine; if unavailable it requests human review rather than self-review. A bridge review is still write-capable; use the read-only MCP review tools `codex_code_review` or `claude_code_review` when permissions must prohibit edits.

Give each job one deliverable. A durable brief should have headings Goal, Context, Constraints, Done when, and Report, with measurable checks. Ask for an early useful commit and incremental commits when appropriate: automatic continuation after a turn timeout requires observed committed progress. `run --timeout` limits a turn; `wait --timeout` only limits waiting. Inspect failure and workspace evidence before restarting work.
