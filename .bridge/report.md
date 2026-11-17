# Code bridge review profile report

## Rule table summary

- Added the opt-in local `review` profile to `TieredGate`; the default profile is unchanged.
- Auto-approves ordinary reads, tests, linters, builds, Git/GitHub reads, safe compound shell commands, PowerShell wrappers, workspace-local writes, and workspace-local removal (including `node_modules`).
- Holds remote publishing; commits; merge/rebase/reset/checkout/switch/branch removal and other history changes; PR state changes; write-shaped `gh api`; writes/removals outside the workspace or involving credential paths; package publishing; scheduled-task/service control; and sandbox elevation.
- Allows one narrowly shaped write: `gh pr comment <N> --repo <owner/repo> --body-file <file>`.
- Persists `approval_profile: review` in job metadata and every consumed auto-approval audit record.
- Exposes the profile through `lazytools-code-bridge run --profile review` and MCP `code_bridge_submit(profile="review")`; cloud placement rejects the local-only profile.

## Tests

- Added **95 table-driven/profile tests**: 40 harmless command cases, 53 generated deny-list/obfuscation cases, one default-behavior regression, and one audit-record test.
- Targeted bridge suite: **133 passed**.
- Ruff: passed across the repository.
- Mypy: passed for all 177 checked source files.
- Full pytest collection is environment-limited: `httpx` and `hatchling` are not installed in the supplied container (three collection errors).
