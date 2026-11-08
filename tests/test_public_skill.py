"""Offline coverage for the distributable assistant skill."""

import argparse
import re
import shlex
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from hatchling.build import build_wheel

from lazytools.code_bridge.cli import build_parser
from lazytools.connectors.code_support._claude_review import claude_reviewer
from lazytools.connectors.code_support._review import codex_reviewer
from lazytools.connectors.projects.tools import ProjectsTools
from lazytools.skill import __main__ as skill_cli
from lazytools.skill import skill_path

EXPECTED = {"SKILL.md", "references/mcp-server.md", "references/code-bridge.md", "references/projects.md"}


@pytest.fixture
def installer():
    import lazybridge.skill

    if not hasattr(lazybridge.skill, "install_skill"):
        pytest.skip("LazyBridge 1.8.7 install_skill is not available")
    return skill_cli.main


def test_install_reinstall_and_foreign(tmp_path, installer, capsys):
    dest = tmp_path / "skill"
    assert installer(["install", "--dest", str(dest)]) == 0
    for name in EXPECTED:
        assert (dest / name).read_bytes() == (skill_path() / name).read_bytes()
    assert not list(dest.rglob("*.py"))
    assert not dest.is_symlink()
    (dest / "stale").write_text("old")
    assert installer(["install", "--dest", str(dest)]) == 0
    assert not (dest / "stale").exists()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "keep").write_text("keep")
    assert installer(["install", "--dest", str(foreign)]) == 1
    assert (foreign / "keep").read_text() == "keep"
    assert "refused:" in capsys.readouterr().err
    assert installer(["install", "--dest", str(foreign), "--force"]) == 0
    assert (foreign / "SKILL.md").exists()


def test_module_entrypoint(tmp_path, installer):
    dest = tmp_path / "installed"
    result = subprocess.run(
        [sys.executable, "-m", "lazytools.skill", "install", "--dest", str(dest)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (dest / "SKILL.md").read_bytes().startswith(b"---\n")


def test_missing_installer(monkeypatch, tmp_path, capsys):
    import lazybridge.skill

    monkeypatch.delattr(lazybridge.skill, "install_skill", raising=False)
    assert skill_cli.main(["install", "--dest", str(tmp_path / "dest")]) == 1
    assert "Upgrade lazybridge to 1.8.7 or newer" in capsys.readouterr().err


def test_default_targets(monkeypatch, tmp_path, installer):
    homes = {name: tmp_path / name for name in ("claude", "codex")}
    monkeypatch.setattr(skill_cli, "HOMES", homes)
    assert installer(["install"]) == 0
    assert (homes["claude"] / "skills/lazytools/SKILL.md").exists()
    homes["codex"].mkdir()
    assert installer(["install", "--to", "all"]) == 0
    assert (homes["codex"] / "skills/lazytools/SKILL.md").exists()


def test_wheel_contains_all_skill_documents(tmp_path):
    wheel = Path(build_wheel(str(tmp_path)))
    if not wheel.is_absolute():
        wheel = tmp_path / wheel
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert {f"lazytools/skill/{name}" for name in EXPECTED} <= names


def test_documented_commands_and_tools_exist(tmp_path):
    texts = "\n".join(path.read_text(encoding="utf-8-sig") for path in skill_path().rglob("*.md"))
    snippets = re.findall(r"`([^`]+)`", texts) + re.findall(r"(?m)^(lazytools-[^\n]+)", texts)
    parser = build_parser()
    sub = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    commands = []
    for snippet in snippets:
        for line in snippet.splitlines():
            if not line.startswith("lazytools-"):
                continue
            parts = shlex.split(line)
            commands.append(parts)
            assert parts[0] in {"lazytools-code-bridge", "lazytools-mcp"}
            if parts[0] == "lazytools-code-bridge" and len(parts) > 1:
                assert parts[1] in sub.choices
                known_options = sub.choices[parts[1]]._option_string_actions
                assert all(p in known_options for p in parts[2:] if p.startswith("--"))
    assert commands
    registered = {
        tool.name
        for tool in ProjectsTools(store_db=str(tmp_path / "p.sqlite"), allow_write=True, profile="full").as_tools()
    }
    registered |= {codex_reviewer().name, claude_reviewer().name}
    mentioned = set(re.findall(r"`((?:projects|codex|claude)_[a-z_]+)`", texts))
    assert mentioned
    assert mentioned - {"projects_store_db"} <= registered
