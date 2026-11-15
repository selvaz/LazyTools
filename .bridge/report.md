# Claude report-cleanup test strengthening

## Tests changed

- Added a shared assertion proving the cleaned commit is the immediate child of the Claude result commit and changes only `.bridge/report.md` by deleting it.
- Strengthened local apply coverage to verify the result branch starts with the report, the returned clean commit has the exact cleanup shape, and local apply leaves the remote Claude result branch untouched.
- Strengthened new-PR landing coverage to verify the exact cleanup refspec is pushed and the remote result branch resolves to the clean descendant.
- Strengthened existing-PR landing coverage to verify both the Claude result branch and PR head resolve to the same clean descendant while preserving the original PR ancestry.

## Fail-before / pass-after proof

The three focused tests were run with `src/lazytools/code_bridge/_cloud.py`, `_cloud_claude.py`, and `_cloud_land.py` temporarily checked out from `47d1456^`, the parent of the PR #292 squash commit. Result: **3 failed, 112 deselected**. The source files were then restored to main and the same selection returned **3 passed, 112 deselected**.

## Final checks

- Focused current-source tests: **3 passed, 112 deselected**.
- Full pytest suite: **3,897 passed, 46 skipped**.
- Ruff: **all checks passed**.
