from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
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


def test_project_requirements_maps_distribution_names(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["python-docx>=1", "pywin32", "google-api-python-client", '
        '"Google_Auth", "beautifulsoup4", "PyYAML", "ordinary-package"]\n'
        '[project.optional-dependencies]\ntest = ["python-docx", "pytest-asyncio"]\n',
        encoding="utf-8",
    )
    assert _cloud_preflight.project_requirements(tmp_path).imports == [
        "bs4", "docx", "google.auth", "googleapiclient", "ordinary_package", "pytest_asyncio", "win32api", "yaml",
    ]


PROBE_REQUIREMENTS = _cloud_preflight.Requirements(["httpx", "private_lib"], ["acme/private-lib"])
PROBE_RESULTS = {
    "network": True,
    "python_version": "3.12.4",
    "imports": {"httpx": True, "private_lib": False},
    "repositories": {"acme/private-lib": False},
}


def _fence(payload: object) -> str:
    return f"```json\n{json.dumps(payload)}\n```"


def test_cloud_probe_parses_partial_failures() -> None:
    evidence = _cloud_preflight.parse_probe_output(_fence(PROBE_RESULTS), PROBE_REQUIREMENTS)
    assert evidence == _cloud_preflight.ProbeEvidence(
        True, "3.12.4", {"httpx": True, "private_lib": False}, {"acme/private-lib": False},
    )


@pytest.mark.parametrize("output", [
    json.dumps(PROBE_RESULTS),
    "Report:\n" + _fence(PROBE_RESULTS),
    _fence(PROBE_RESULTS) + "\nExtra text",
    _fence(PROBE_RESULTS) + "\n" + _fence(PROBE_RESULTS),
    "```json\n{bad json}\n```",
    _fence([]),
    _fence({**PROBE_RESULTS, "extra": False}),
    _fence({**PROBE_RESULTS, "network": 1}),
    _fence({**PROBE_RESULTS, "python_version": "Python 3.12.4"}),
    _fence({**PROBE_RESULTS, "imports": {"httpx": True}}),
    _fence({**PROBE_RESULTS, "imports": {"httpx": True, "private_lib": "false"}}),
    _fence({**PROBE_RESULTS, "repositories": {"acme/private-lib": False, "acme/extra": True}}),
    _fence(PROBE_RESULTS).replace('"httpx": true', '"httpx": false, "httpx": true'),
])
def test_cloud_probe_rejects_malformed_or_incomplete_output(output: str) -> None:
    with pytest.raises(ValueError, match="probe"):
        _cloud_preflight.parse_probe_output(output, PROBE_REQUIREMENTS)


def test_cloud_probe_manifest_replaces_evidence_and_keeps_other_environments(tmp_path: Path, monkeypatch) -> None:
    stamped = date(2026, 11, 25)
    monkeypatch.setattr(_cloud_preflight, "_today", lambda: stamped)
    path = tmp_path / _cloud_preflight.MANIFEST
    path.parent.mkdir()
    _manifest(path)
    evidence = _cloud_preflight.ProbeEvidence(False, "3.12.4", {"httpx": True, "old_lib": False},
                                             {"acme/present": True, "acme/missing": False})

    assert _cloud_preflight.write_probe_manifest(root=tmp_path, env_id="env_new", job_id="abc123", evidence=evidence) == path
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["environments"]["env_test"]["imports"] == ["pytest", "pytest_asyncio"]
    assert data["environments"]["env_new"] == {
        "revision": "probe:abc123", "attested_by": "probe:abc123", "attested_on": "2026-11-25",
        "network": False, "python_version": "3.12.4", "imports": ["httpx"], "repositories": ["acme/present"],
    }

    _cloud_preflight.write_probe_manifest(
        root=tmp_path, env_id="env_new", job_id="abc124",
        evidence=_cloud_preflight.ProbeEvidence(False, "3.12.4", {"httpx": False}, {"acme/present": False}),
    )
    refreshed = json.loads(path.read_text(encoding="utf-8"))["environments"]["env_new"]
    assert refreshed["attested_by"] == "probe:abc124"
    assert refreshed["imports"] == refreshed["repositories"] == []


def test_cloud_preflight_age_expires_even_with_cached_verdict(tmp_path: Path, monkeypatch) -> None:
    manifest = _manifest(tmp_path / "manifest.json")
    today = datetime.now(UTC).date()
    monkeypatch.setattr(_cloud_preflight, "_today", lambda: today + timedelta(days=_cloud_preflight.MAX_AGE_DAYS))
    assert _check(tmp_path, "ordinary task", manifest).eligible
    assert _check(tmp_path, "ordinary task", manifest).cache_hit
    today += timedelta(days=1)
    result = _check(tmp_path, "ordinary task", manifest)
    assert not result.eligible
    assert not result.cache_hit
    assert "cloud-preflight probe" in result.reason


@pytest.mark.parametrize("stamp", [None, "invalid", "2999-01-01"])
def test_cloud_preflight_refuses_missing_invalid_or_future_date(tmp_path: Path, stamp: str | None) -> None:
    manifest = _manifest(tmp_path / "manifest.json")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    entry = data["environments"]["env_test"]
    entry.pop("attested_on")
    if stamp is not None:
        entry["attested_on"] = stamp
    manifest.write_text(json.dumps(data), encoding="utf-8")

    result = _check(tmp_path, "ordinary task", manifest)
    assert not result.eligible
    assert "cloud-preflight probe" in result.reason
