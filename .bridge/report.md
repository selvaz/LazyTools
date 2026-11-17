# Code Bridge H2 + H3 Report

## CLI shapes

- `lazytools-code-bridge run ... --json` prints one JSON object as its first stdout line with `job_id`, `short_id`, `where`, `engine`, and `cwd`; detached local launches add `pid`, and cloud submissions add `task_url`.
- `lazytools-code-bridge jobs [--all] --json` prints one complete job record per line (JSON Lines).
- `lazytools-code-bridge wait-any [JOB_ID ...] [--all-active] [--timeout SECONDS] [--json] [--events done,failed,approval,landed,interrupted] [--loop]` watches a fixed job set. It returns 0 for an event, 3 for timeout, and 2 for usage errors.
- Wait events have `event`, full `job_id`, `state`, `approval`, and `pr_url`. Approval payloads contain `approval_id`, `command`, `summary`, and `reason`.

## Implementation notes

- Pending approval tickets are deduplicated within a waiter invocation.
- Loop mode emits JSON Lines and exits after every watched job is terminal.
- Cloud watches refresh provider status and run owed landing through the same cloud landing path used by `wait`.
- Human job listings use encoding-safe output for legacy Windows consoles.
- Existing non-JSON output remains unchanged.

## Tests

- Added 6 focused `wait-any` tests covering done, failed, partial-as-failed, interrupted, timeout, usage validation, approval details, loop output, and approval deduplication.
- Updated CLI coverage for the machine-readable run identity and JSON Lines job listing.
- 169 CLI/brief/routing compatibility tests pass.
- 30 CLI tests pass.
- 6 new wait-any tests pass.
- Ruff and mypy pass for changed Python files.
- The complete repository suite cannot collect in this environment because optional test dependencies `httpx` and `hatchling` are absent.
