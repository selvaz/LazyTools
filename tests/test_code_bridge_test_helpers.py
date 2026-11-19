from __future__ import annotations

import os
import sys

import pytest

from code_bridge_test_helpers import preserve_windows_shell_environment, write_python_executable


def test_python_executable_windows_launcher_is_unit_testable(tmp_path):
    launcher = write_python_executable(tmp_path, "fake tool", "print('ok')\n", platform="nt")

    assert launcher.name == "fake tool.cmd"
    assert (tmp_path / "fake tool.py").read_text(encoding="utf-8") == "print('ok')\n"
    command = launcher.read_text(encoding="utf-8")
    assert command.startswith("@echo off\n\"")
    assert '"%~dp0fake tool.py" %*' in command


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shebang launchers cannot run on Windows")
def test_python_executable_posix_launcher_runs(tmp_path):
    launcher = write_python_executable(tmp_path, "fake", "print('ok')\n", platform="posix")

    assert os.popen(str(launcher)).read() == "ok\n"


def test_small_environment_preserves_available_windows_shell_variables(monkeypatch):
    monkeypatch.setenv("COMSPEC", r"C:\\Windows\\System32\\cmd.exe")
    monkeypatch.setenv("SYSTEMROOT", r"C:\\Windows")

    assert preserve_windows_shell_environment({"PATH": "tools"}) == {
        "PATH": "tools",
        "COMSPEC": r"C:\\Windows\\System32\\cmd.exe",
        "SYSTEMROOT": r"C:\\Windows",
    }


def test_suite_codex_fake_wins_over_codex_on_path(tmp_path, monkeypatch):
    """The autouse fake prevents an installed npm shim from affecting tests."""
    from lazytools.connectors.code_support._codex import resolve_codex_bin

    ambient = tmp_path / "codex.cmd"
    ambient.write_text("@echo off\nexit /b 99\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))

    resolved = resolve_codex_bin()

    assert resolved == os.environ["CODEX_BIN"]
    assert resolved != str(ambient)
