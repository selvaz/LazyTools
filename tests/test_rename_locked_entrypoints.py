from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("rename_locked_entrypoints", ROOT / "tools" / "rename_locked_entrypoints.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def test_locked_exe_is_renamed_unlocked_left_alone(tmp_path, monkeypatch, capsys):
    locked = tmp_path / "lazytools-code-bridge.exe"
    free = tmp_path / "lazytools-mcp.exe"
    other = tmp_path / "pip.exe"
    for f in (locked, free, other):
        f.write_bytes(b"x")
    monkeypatch.setattr(mod, "is_locked", lambda p: p.name == locked.name)

    assert mod.main([str(tmp_path)]) == 0

    names = sorted(p.name for p in tmp_path.iterdir())
    assert free.name in names and other.name in names
    assert locked.name not in names
    assert any(n.startswith("lazytools-code-bridge.exe.old-") for n in names)
    assert "renamed lazytools-code-bridge.exe" in capsys.readouterr().out
