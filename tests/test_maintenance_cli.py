"""Public read-only CLI and strict TOML configuration tests."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from lazytools.maintenance.cli import main
from lazytools.maintenance.config import load_config


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def manifest(tmp_path: Path) -> Path:
    for name in ("api", "web"):
        root = tmp_path / name
        root.mkdir()
        _git(root, "init", "-q")
        _git(root, "config", "user.name", "CI")
        _git(root, "config", "user.email", "ci@example.test")
        (root / "service.py").write_text("def sample():\n    return 1\n")
        _git(root, "add", "service.py")
        _git(root, "commit", "-qm", "initial")
    file = tmp_path / "maintenance.toml"
    file.write_text(
        'schema_version = 1\n'
        'state_dir = "artifacts"\n'
        'require_clean = true\n'
        '[mapping]\nembeddings = false\ntimeout_seconds = 23.5\n'
        '[[repos]]\nid = "api"\nroot = "api"\n'
        '[[repos]]\nid = "web"\nroot = "web"\n'
        '[[dependencies]]\nconsumer = "web"\nprovider = "api"\n',
        encoding="utf-8",
    )
    return file


def test_public_toml_manifest_resolves_allowlisted_repositories(manifest):
    cfg = load_config(manifest)
    assert cfg.embeddings is False
    assert cfg.timeout_seconds == 23.5
    assert cfg.workspace.paths()["api"] == manifest.parent / "api"
    assert cfg.workspace.state_dir == manifest.parent / "artifacts"
    assert cfg.workspace.dependencies[0].consumer == "web"
    assert cfg.workspace.dependencies[0].provider == "api"


def test_validate_only_resolves_commits_without_binary(manifest, capsys):
    result = main(["--config", str(manifest), "--validate-only", "--json"])
    assert result == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "valid"
    assert set(printed["sha"]) == {"api", "web"}
    assert all(len(value) == 40 for value in printed["sha"].values())
    assert not (manifest.parent / "artifacts").exists()


def test_untrusted_config_cannot_inject_check_commands(manifest):
    bad = manifest.parent / "untrusted.toml"
    raw = manifest.read_text()
    bad.write_text(raw + '\n[verify]\ncommand = "rm -rf /"\n')
    with pytest.raises(ValueError, match="unknown keys"):
        load_config(bad)
    bad.write_text(raw.replace("embeddings = false", 'embeddings = false\nexecutable = "sh"'))
    with pytest.raises(ValueError, match="unknown keys"):
        load_config(bad)
    bad.write_text(raw.replace("schema_version = 1", "schema_version = 99"))
    with pytest.raises(ValueError, match="schema_version"):
        load_config(bad)


def test_cli_rejects_bad_base_refs(manifest, capsys):
    assert main(["--config", str(manifest), "--base", "rogue=HEAD", "--validate-only"]) == 2
    assert "Unknown repositories" in capsys.readouterr().err
    assert main(["--config", str(manifest), "--base", "api=HEAD", "--base", "api=HEAD", "--validate-only"]) == 2
    assert "Duplicate --base" in capsys.readouterr().err
    assert main(["--config", str(manifest), "--base", "api=--malicious", "--validate-only"]) == 2
    assert "Invalid Git ref" in capsys.readouterr().err


def test_cli_produces_real_plan_report_without_llm(manifest, monkeypatch, capsys):
    import lazytools.maintenance.pipeline as module

    class StubCLI:
        _use_embeddings = False

        def __init__(self, *args, **kwargs):
            pass

        def _invoke(self, action, args, repo):
            assert action == "version"
            return "repomap 1.9.0"

        def index(self, repo):
            return json.dumps({"root": "/fixture/" + repo, "files": 1, "symbols": 1})

        def impact(self, target, repo, depth):
            return "Possible dependency of " + target

    monkeypatch.setattr(module, "RepoMapCLI", StubCLI)
    assert main(["--config", str(manifest), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "review_required"
    assert result["verification"]["status"] == "not_run"
    assert result["verified"] is False
    assert set(result["sha"]) == {"api", "web"}
    assert Path(result["report_json"]).is_file()
    assert Path(result["report_markdown"]).is_file()
