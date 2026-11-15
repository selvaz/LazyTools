"""Suite-wide isolation that every test needs regardless of which module it lives in."""

from __future__ import annotations

import shutil
import sys

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_web_cache_switch(monkeypatch):
    """The operator's shell may set LAZYTOOLS_WEB_CACHE_DB; tests start from 'unset'."""
    monkeypatch.delenv("LAZYTOOLS_WEB_CACHE_DB", raising=False)


@pytest.fixture(autouse=True)
def _isolated_codex_binary(tmp_path_factory, monkeypatch):
    """Never let code-bridge tests discover an operator's Codex installation.

    A copied Python executable is a portable, executable stand-in.  Cloud test
    fixtures replace ``CODEX_BIN`` with their scripted CLI when invocation is
    part of the test; all other tests remain isolated from ``PATH`` (including
    npm's Windows ``codex.cmd`` shim).
    """
    tools = tmp_path_factory.mktemp("code-bridge-tools")
    suffix = ".exe" if sys.platform == "win32" else ""
    fake = tools / f"codex-test-fake{suffix}"
    shutil.copy2(sys.executable, fake)
    monkeypatch.setenv("CODEX_BIN", str(fake))


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


@pytest.fixture(autouse=True)
def _fast_cloud_submit(monkeypatch):
    """Cloud submits are spaced and retried with real sleeps; tests skip both.
    ``test_code_bridge_cloud_submit_queue`` exercises the real functions."""
    from lazytools.code_bridge import _cloud

    monkeypatch.setattr(_cloud, "_wait_for_submit_turn", lambda *args, **kwargs: None)
    monkeypatch.setattr(_cloud, "SUBMIT_RETRY_BASE_DELAY", 0.0)
