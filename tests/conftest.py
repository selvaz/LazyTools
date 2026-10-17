"""Suite-wide isolation that every test needs regardless of which module it lives in."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_code_bridge_config(tmp_path_factory, monkeypatch):
    """Never read the operator's real ``~/.lazytools/code-bridge.toml`` nor touch the
    machine-wide engine-slot directory: each test gets its own config file whose
    ``[concurrency]`` table names a private ``slot_dir``. Tests that need other
    limits APPEND keys to this file (it ends inside the ``[concurrency]`` table)."""
    base = tmp_path_factory.mktemp("code-bridge-config")
    slots = base / "engine-slots"
    path = base / "code-bridge.toml"
    path.write_text(f"[concurrency]\nslot_dir = '{slots}'\n", encoding="utf-8")
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_CONFIG", str(path))
