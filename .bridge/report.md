# LazyTools code bridge report

## Summary

- Unmapped Codex repositories selected with `--where auto` now fall back to local execution and explain the exact `[cloud.codex.envs]` mapping needed to enable cloud.
- Explicit `--where cloud` remains an error for an unmapped Codex repository, with actionable local/auto alternatives and the exact mapping key.
- Added and documented `[cloud] unmapped = "local" | "error"`; `local` is the default.
- Added offline pytest coverage for fallback, error messaging, validation, and mapped-repository behavior.

## Validation

- Targeted cloud placement tests pass without network access or a real Codex binary.
- The complete code-bridge test suite is run before delivery; see the pull request and final response for results.
