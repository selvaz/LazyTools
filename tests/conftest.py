"""Suite-wide isolation that every test needs regardless of which module it lives in."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_code_bridge_config(tmp_path_factory, monkeypatch):
    """Never read the operator's real ``~/.lazytools/code-bridge.toml``: the code
    bridge's engine concurrency limits would otherwise depend on the machine."""
    path = tmp_path_factory.mktemp("code-bridge-config") / "code-bridge.toml"
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_CONFIG", str(path))
