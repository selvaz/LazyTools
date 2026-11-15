## Codex review fixes

Fixed all three blocking findings in `tools/check_pins.py`:

- Git references are consumed through a real requirement delimiter, so a suffix such as `+stale` is compared as part of the ref instead of accepting a matching prefix.
- Discovery again scans every `*.txt` below any `requirements/` directory, in addition to `requirements*.txt` anywhere.
- Only pure GitHub Actions `${{ ... }}` refs receive the runtime-template warning exemption; literal shell defaults using `:-`, `-`, or `:=` are validated against the manifest.

### Files

- `tools/check_pins.py`
- `tests/test_check_pins.py`
- `.bridge/report.md`

### Tests and checks

- `pytest -q tests/test_check_pins.py`: 22 passed.
- `pytest -q`: 3,889 passed, 46 skipped.
- `ruff check .`: passed.
- `python tools/check_pins.py --repo . --manifest ecosystem/compatibility.toml`: passed with 5 expected warnings (4 unrecognised placeholder URLs and 1 runtime CI expression).

### Counts

- Blocking review findings fixed: 3/3.
- Regression tests added: 3.
- Functional files changed: 2 (plus this report).
- Commits for fixes: 3.
