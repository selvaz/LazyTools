"""Cross-platform helpers for code-bridge subprocess tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def write_python_executable(
    directory: Path,
    name: str,
    source: str,
    *,
    platform: str | None = None,
) -> Path:
    """Write a Python fake executable and return its platform launcher.

    Windows cannot execute extensionless shebang scripts.  Keep the Python
    implementation separate there and invoke it through a ``.cmd`` shim.
    ``platform`` is injectable so the Windows output is covered on Linux CI.
    """
    directory.mkdir(parents=True, exist_ok=True)
    platform = os.name if platform is None else platform
    if platform == "nt":
        script = directory / f"{name}.py"
        script.write_text(source, encoding="utf-8")
        launcher = directory / f"{name}.cmd"
        launcher.write_text(
            f'@echo off\n"{sys.executable}" "%~dp0{name}.py" %*\n',
            encoding="utf-8",
        )
        return launcher

    launcher = directory / name
    launcher.write_text(f"#!{sys.executable}\n" + source, encoding="utf-8")
    launcher.chmod(0o755)
    return launcher


def preserve_windows_shell_environment(env: dict[str, str]) -> dict[str, str]:
    """Add Windows shell-discovery variables to an intentionally small env."""
    for name in ("COMSPEC", "SYSTEMROOT"):
        if name in os.environ:
            env.setdefault(name, os.environ[name])
    return env
