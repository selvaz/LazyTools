# PyPI name placeholders

Only `lazybridge` is published on PyPI. The other ecosystem distributions install from GitHub release tags (see `ecosystem/compatibility.toml`). Two of their names were still free on PyPI (checked 22/11/2026): `lazytoolkit` and `market-data-hub`. An unregistered name can be claimed by anyone, so a mistaken `pip install lazytoolkit` would install someone else's code (dependency confusion). These placeholders reserve the names: metadata only, no importable module, version 0.0.1, pointing to GitHub.

`lazystats` is already owned by an unrelated project on PyPI, so it must always be installed through a pinned git reference.

## Publish (owner, needs PyPI credentials)

```bash
cd tools/pypi-placeholders
python -m build lazytoolkit --outdir dist
python -m build market-data-hub --outdir dist
python -m twine check dist/*
python -m twine upload dist/*
```

After publishing, verify that `pip download --no-deps lazytoolkit==0.0.1` returns the placeholder and that every consumer still installs from the git references (`check_pins.py`).
