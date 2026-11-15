## Summary

- Expand `tools/check_pins.py` discovery to cover nested Python projects and lazy-installer TOML manifests while excluding dependency, virtual-environment, and build trees.
- Fail stale managed Git refs and incompatible LazyBridge requirements, but emit non-failing warnings for application repositories outside the compatibility authority.
- Add LazyCEO v0.2.10 to the canonical compatibility manifest and regenerate all derived compatibility views.
- Add regression coverage modelled on lazy-installer `6cd5cd5` and LazyStudio `0a49728`, proving all six stale consumer pins are detected while the LazyStudio commit pin remains an unmanaged warning.
- Document the required lazy-installer regeneration and LazyStudio backend pin bump for every release.

## Testing

- `python -m pytest tests/test_check_pins.py -q` — 13 passed.
- `python -m pytest` — 3880 passed, 46 skipped.
- `ruff check .` — passed.
- `python tools/render_compatibility.py --check` — passed; generated views have no drift.
