# PR #294 review follow-up

## Files changed

- `tools/check_pins.py` — broadened managed GitHub URL/ref parsing, added credential-safe warnings for unparseable Selvaz URLs, refined build-output exclusions, and recursively discovers nested workflows.
- `tests/test_check_pins.py` — added regression coverage for owner/scheme casing, SSH and SCP syntax, userinfo, archive refs, source directories named `build`, nested workflows, and warning sanitization.

## Tests added

- 5 regression tests (18 pin-check tests total).
- URL variant coverage includes three transport/userinfo cases in one parametrized scenario.

## Verification

- Full pytest suite: 3,885 passed, 46 skipped.
- Ruff: passed.
