from __future__ import annotations

import os

from code_bridge_test_helpers import preserve_windows_shell_environment, write_python_executable


def test_python_executable_windows_launcher_is_unit_testable(tmp_path):
    launcher = write_python_executable(tmp_path, "fake tool", "print('ok')\n", platform="nt")

    assert launcher.name == "fake tool.cmd"
    assert (tmp_path / "fake tool.py").read_text(encoding="utf-8") == "print('ok')\n"
    command = launcher.read_text(encoding="utf-8")
    assert command.startswith("@echo off\n\"")
    assert '"%~dp0fake tool.py" %*' in command


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
