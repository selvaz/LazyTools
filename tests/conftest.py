"""Suite-wide isolation that every test needs regardless of which module it lives in."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_web_cache_switch(monkeypatch):
    """The operator's shell may set LAZYTOOLS_WEB_CACHE_DB; tests start from 'unset'."""
    monkeypatch.delenv("LAZYTOOLS_WEB_CACHE_DB", raising=False)


@pytest.fixture(scope="session")
def _codex_test_fake(tmp_path_factory):
    """One copied Python executable for the whole session (copying it per test
    wrote tens of GB on CI runners, where the interpreter is ~31 MB)."""
    tools = tmp_path_factory.mktemp("code-bridge-tools")
    suffix = ".exe" if sys.platform == "win32" else ""
    fake = tools / f"codex-test-fake{suffix}"
    shutil.copy2(sys.executable, fake)
    return fake


@pytest.fixture(autouse=True)
def _isolated_codex_binary(_codex_test_fake, monkeypatch):
    """Never let code-bridge tests discover an operator's Codex installation.

    A copied Python executable is a portable, executable stand-in.  Cloud test
    fixtures replace ``CODEX_BIN`` with their scripted CLI when invocation is
    part of the test; all other tests remain isolated from ``PATH`` (including
    npm's Windows ``codex.cmd`` shim).
    """
    monkeypatch.setenv("CODEX_BIN", str(_codex_test_fake))


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
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_DB", str(base / "jobs.sqlite"))


@pytest.fixture(autouse=True)
def _isolated_bridge_authority(request, tmp_path_factory, monkeypatch):
    if "code_bridge" not in request.node.path.name:
        return
    from lazytools.code_bridge import _jobs

    # The runner may put its pytest temp tree inside its own checkout.
    # Keep Git discovery inside the fixtures' repositories in that case.
    test_root = tmp_path_factory.getbasetemp()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(test_root), prepend=os.pathsep)
    repo_root = _jobs._git_repo_root

    def isolated_repo_root(path):
        root = repo_root(path)
        return path if path.is_relative_to(test_root) and not root.is_relative_to(test_root) else root

    monkeypatch.setattr(_jobs, "_git_repo_root", isolated_repo_root)
    base = tmp_path_factory.mktemp("bridge-authority")
    monkeypatch.setenv("LAZYTOOLS_PROJECTS_STORE_DB", str(base / "projects.sqlite"))
    for name in ("TASK_PRINCIPAL", "PROFILES", "CODEX_RELEASE_REPORT", "CLAUDE_RELEASE_REPORT"):
        monkeypatch.delenv(f"LAZYTOOLS_CODE_BRIDGE_{name}", raising=False)


@pytest.fixture
def provision_bridge_approver(tmp_path, monkeypatch):
    from lazybridge import Store

    from lazytools.code_bridge import _credentials, _registry

    monkeypatch.setattr(_credentials, "credential_directory", lambda: tmp_path / "operator-credentials")

    def provision(registry_db=None):
        _registry.Registry(Store(), registry_db).initialize_credential()

    return provision


@pytest.fixture(autouse=True)
def _fast_cloud_submit(monkeypatch):
    """Cloud submits are spaced and retried with real sleeps; tests skip both.
    ``test_code_bridge_cloud_submit_queue`` exercises the real functions."""
    from lazytools.code_bridge import _cloud

    monkeypatch.setattr(_cloud, "_wait_for_submit_turn", lambda *args, **kwargs: None)
    monkeypatch.setattr(_cloud, "SUBMIT_RETRY_BASE_DELAY", 0.0)


def _helper_python_target(found: str | None) -> Path | None:
    """The ``.py`` behind a ``.cmd`` written by ``write_python_executable``, else None.

    Only the tests' own launcher qualifies: same stem, a sibling ``.py``, and a
    body that runs exactly that file. A real npm shim never matches.
    """
    if not found:
        return None
    launcher = Path(found)
    if launcher.suffix.lower() != ".cmd" or not launcher.is_file():
        return None
    script = launcher.with_suffix(".py")
    if not script.is_file():
        return None
    body = launcher.read_text(encoding="utf-8", errors="replace")
    return script if f"%~dp0{script.name}" in body else None


@pytest.fixture(autouse=True)
def _run_test_fakes_without_cmd_shims(monkeypatch):
    """On Windows the scripted CLI fakes are ``.cmd`` launchers, which the bridge
    rightly refuses to execute (cmd.exe would re-parse the brief). Run the fake's
    Python file directly instead; the product guard is untouched for real shims."""
    if os.name != "nt":
        return
    from lazytools.code_bridge import _cloud, _cloud_claude
    from lazytools.connectors.code_support._codex import resolve_codex_bin

    original_codex = _cloud._codex_argv
    original_claude = _cloud_claude._claude_argv

    def codex_argv() -> list[str]:
        script = _helper_python_target(resolve_codex_bin())
        return [sys.executable, str(script)] if script else original_codex()

    def claude_argv() -> list[str]:
        script = _helper_python_target(os.environ.get("CLAUDE_BIN") or shutil.which("claude"))
        return [sys.executable, str(script)] if script else original_claude()

    monkeypatch.setattr(_cloud, "_codex_argv", codex_argv)
    monkeypatch.setattr(_cloud_claude, "_claude_argv", claude_argv)


@pytest.fixture(autouse=True)
def _isolated_default_slot_dir(tmp_path_factory, monkeypatch):
    """Tests that point LAZYTOOLS_CODE_BRIDGE_CONFIG at their own file (with no
    ``slot_dir``) must still never see the operator's real engine slots, where a
    running job would make the concurrency check refuse the test's run."""
    from lazytools.code_bridge import _concurrency

    slots = tmp_path_factory.mktemp("default-slots") / "engine-slots"
    monkeypatch.setattr(_concurrency, "default_slot_dir", lambda: slots)
