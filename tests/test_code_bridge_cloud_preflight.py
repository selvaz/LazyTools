from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lazytools.code_bridge import _cloud_preflight, cli


def _manifest(path: Path, *, revision: str = "m1", environment_revision: str = "e1", network: bool = False) -> Path:
    path.write_text(json.dumps({
        "revision": revision,
        "environments": {"env_test": {
            "revision": environment_revision,
            "attested_on": datetime.now(UTC).date().isoformat(),
            "network": network,
            "repositories": ["owner/public"],
            "imports": ["pytest", "pytest_asyncio"],
        }},
    }), encoding="utf-8")
    return path


def _check(tmp_path: Path, brief: str, manifest: Path | None = None, *, network: bool = False):
    return _cloud_preflight.check(
        repo="owner/project", env_id="env_test", brief=brief, root=tmp_path,
        manifest_path=manifest or tmp_path / "manifest.json", cache_path=tmp_path / "cache.json", network=network,
    )


def test_missing_manifest_refuses(tmp_path: Path) -> None:
    result = _check(tmp_path, "ordinary task")
    assert not result.eligible
    assert "manifest is missing" in result.reason


def test_local_absolute_path_refuses(tmp_path: Path) -> None:
    result = _check(tmp_path, r"Read C:\\private\\input.csv", _manifest(tmp_path / "manifest.json"))
    assert not result.eligible
    assert "local absolute path" in result.reason


@pytest.mark.parametrize("path", [r"\\server\share\input.csv", "D:/private/input.csv"])
def test_windows_unc_and_forward_slash_drive_paths_refuse(tmp_path: Path, path: str) -> None:
    result = _check(tmp_path, f"Read {path}", _manifest(tmp_path / "manifest.json"))
    assert not result.eligible
    assert "local absolute path" in result.reason


def test_unavailable_private_repository_refuses(tmp_path: Path) -> None:
    result = _check(tmp_path, "Required repositories: owner/private", _manifest(tmp_path / "manifest.json"))
    assert not result.eligible
    assert "owner/private" in result.reason


def test_unavailable_import_refuses(tmp_path: Path) -> None:
    result = _check(tmp_path, "Required imports: proprietary_package", _manifest(tmp_path / "manifest.json"))
    assert not result.eligible
    assert "proprietary_package" in result.reason


def test_requested_network_must_match_manifest(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path / "manifest.json", network=True)
    result = _check(tmp_path, "ordinary task", manifest)
    assert not result.eligible
    assert "requested network off" in result.reason
    assert _check(tmp_path, "ordinary task", manifest, network=True).eligible


def test_eligible_requirements_pass_and_are_cached(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path / "manifest.json")
    assert _check(tmp_path, "Required imports: pytest_asyncio\nRequired repositories: owner/public", manifest).eligible
    result = _check(tmp_path, "Required imports: pytest_asyncio\nRequired repositories: owner/public", manifest)
    assert result.eligible
    assert result.cache_hit


def test_cache_invalidates_when_each_key_component_changes(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path / "manifest.json")
    brief = "Required imports: pytest"
    assert not _check(tmp_path, brief, manifest).cache_hit
    assert _check(tmp_path, brief, manifest).cache_hit

    for kwargs in (
        {"environment_revision": "e2"},
        {"revision": "m2", "environment_revision": "e2"},
        {"revision": "m2", "environment_revision": "e2", "network": True},
    ):
        _manifest(manifest, **kwargs)
        assert not _check(tmp_path, brief, manifest).cache_hit

    other = _cloud_preflight.check(
        repo="owner/other", env_id="env_test", brief=brief, root=tmp_path,
        manifest_path=manifest, cache_path=tmp_path / "cache.json",
    )
    assert not other.cache_hit


def test_cache_invalidates_when_manifest_dependencies_change_without_revision_bump(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path / "manifest.json")
    assert _check(tmp_path, "Required imports: pytest", manifest).eligible
    assert _check(tmp_path, "Required imports: pytest", manifest).cache_hit

    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["environments"]["env_test"]["imports"] = []
    manifest.write_text(json.dumps(data), encoding="utf-8")
    result = _check(tmp_path, "Required imports: pytest", manifest)
    assert not result.cache_hit
    assert not result.eligible

    assert _check(tmp_path, "ordinary task", manifest).eligible
    assert _check(tmp_path, "ordinary task", manifest).cache_hit
    data["environments"]["env_test"]["repositories"] = ["owner/another"]
    manifest.write_text(json.dumps(data), encoding="utf-8")
    assert not _check(tmp_path, "ordinary task", manifest).cache_hit


def test_cache_invalidates_when_selected_environment_changes(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path / "manifest.json")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["environments"]["env_other"] = dict(data["environments"]["env_test"])
    manifest.write_text(json.dumps(data), encoding="utf-8")
    brief = "Required imports: pytest"

    assert _check(tmp_path, brief, manifest).eligible
    result = _cloud_preflight.check(
        repo="owner/project", env_id="env_other", brief=brief, root=tmp_path,
        manifest_path=manifest, cache_path=tmp_path / "cache.json",
    )
    assert result.eligible
    assert not result.cache_hit


@pytest.mark.parametrize("field,value", [
    ("repositories", None),
    ("imports", None),
    ("repositories", ["owner/public", 1]),
    ("imports", ["pytest", False]),
])
def test_manifest_dependency_fields_must_be_lists_of_strings(
    tmp_path: Path, field: str, value: object,
) -> None:
    manifest = _manifest(tmp_path / "manifest.json")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["environments"]["env_test"][field] = value
    manifest.write_text(json.dumps(data), encoding="utf-8")

    result = _check(tmp_path, "ordinary task", manifest)
    assert not result.eligible
    assert f"field '{field}' must be a list of strings" in result.reason


def test_cloud_preflight_init_uses_pyproject_and_configured_environment(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "1"\ndependencies = ['
        '"httpx>=0.27", "shared-lib @ git+https://github.com/acme/shared-lib.git@v1"]\n'
        '[project.optional-dependencies]\ntest = ["pytest-asyncio>=0.23"]\n', encoding="utf-8",
    )
    import subprocess
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/acme/demo.git"], check=True)
    config = tmp_path / "code-bridge.toml"
    config.write_text('[cloud.codex.envs]\n"acme/demo" = "env_demo"\n', encoding="utf-8")
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_CONFIG", str(config))

    assert cli.main(["cloud-preflight", "init", "--repo", str(repo), "--network", "on"]) == 0
    manifest = json.loads((repo / _cloud_preflight.MANIFEST).read_text(encoding="utf-8"))
    entry = manifest["environments"]["env_demo"]
    assert entry == {
        "attested_on": datetime.now(UTC).date().isoformat(),
        "attested_by": "operator", "imports": ["httpx", "pytest_asyncio", "shared_lib"],
        "network": True, "repositories": ["acme/shared-lib"], "revision": "operator-attested-1",
    }
