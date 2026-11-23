from __future__ import annotations

import json
from pathlib import Path

from lazytools.code_bridge import _cloud_preflight


def _manifest(path: Path, *, revision: str = "m1", environment_revision: str = "e1", network: bool = False) -> Path:
    path.write_text(json.dumps({
        "revision": revision,
        "environments": {"env_test": {
            "revision": environment_revision,
            "network": network,
            "repositories": ["owner/public"],
            "imports": ["pytest", "pytest_asyncio"],
        }},
    }), encoding="utf-8")
    return path


def _check(tmp_path: Path, brief: str, manifest: Path | None = None):
    return _cloud_preflight.check(
        repo="owner/project", env_id="env_test", brief=brief, root=tmp_path,
        manifest_path=manifest or tmp_path / "manifest.json", cache_path=tmp_path / "cache.json",
    )


def test_missing_manifest_refuses(tmp_path: Path) -> None:
    result = _check(tmp_path, "ordinary task")
    assert not result.eligible
    assert "manifest is missing" in result.reason


def test_local_absolute_path_refuses(tmp_path: Path) -> None:
    result = _check(tmp_path, r"Read C:\\private\\input.csv", _manifest(tmp_path / "manifest.json"))
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
