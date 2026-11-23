"""Local, deterministic eligibility check performed before a cloud submit.

The environment manifest is deliberately a repository-owned JSON file.  It is
evidence about a built cloud environment, not a promise that the bridge tries
to verify over the network while launching a job.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MANIFEST = ".bridge/cloud-preflight.json"
CACHE = ".bridge/cloud-preflight-cache.json"
SCHEMA_REVISION = 1

_LOCAL_PATH = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Za-z]:[\\/][^\s`'\"]+|\\\\[^\s`'\"]+|file://[^\s`'\"]+|/(?:Users|home|mnt|media|Volumes)/[^\s`'\"]+)",
    re.IGNORECASE,
)
_REPOSITORIES = re.compile(r"(?im)^\s*(?:required[-_ ]?)?repositor(?:y|ies)\s*:\s*(.+)$")
_IMPORTS = re.compile(r"(?im)^\s*(?:required[-_ ]?)?imports?\s*:\s*(.+)$")
_DEPENDENCY_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_GITHUB_REPOSITORY = re.compile(r"github\.com[/:]([^/\s]+/[^/@\s]+?)(?:\.git)?(?:@|$)", re.IGNORECASE)


@dataclass(frozen=True)
class Result:
    eligible: bool
    reason: str
    cache_hit: bool = False


def _items(pattern: re.Pattern[str], brief: str) -> set[str]:
    found: set[str] = set()
    for match in pattern.finditer(brief):
        found.update(value for value in re.split(r"[,\s]+", match.group(1).strip()) if value)
    return found


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("the JSON root must be an object")
    return value


def init_manifest(*, root: Path, env_id: str, network: bool) -> Path:
    """Create an operator-attested starter manifest from ``pyproject.toml``."""
    pyproject_path = root / "pyproject.toml"
    try:
        pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read project dependencies from {pyproject_path}: {exc}") from exc
    project = pyproject.get("project", {})
    if not isinstance(project, dict):
        raise ValueError(f"{pyproject_path}: [project] must be a table")
    dependencies = list(project.get("dependencies", []))
    optional = project.get("optional-dependencies", {})
    if isinstance(optional, dict):
        dependencies.extend(item for group in optional.values() if isinstance(group, list) for item in group)
    if not all(isinstance(item, str) for item in dependencies):
        raise ValueError(f"{pyproject_path}: project dependencies must be strings")

    imports = sorted({match.group(1).replace("-", "_") for item in dependencies
                      if (match := _DEPENDENCY_NAME.match(item))})
    repositories = sorted({match.group(1) for item in dependencies
                           if (match := _GITHUB_REPOSITORY.search(item))})
    path = root / MANIFEST
    if path.exists():
        raise ValueError(f"refusing to overwrite existing cloud preflight manifest: {path}")
    data = {
        "revision": "starter-1",
        "environments": {env_id: {
            "revision": "operator-attested-1", "network": network,
            "repositories": repositories, "imports": imports, "attested_by": "operator",
        }},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def check(
    *, repo: str, env_id: str, brief: str, root: Path, network: bool = False,
    manifest_path: Path | None = None, cache_path: Path | None = None,
) -> Result:
    """Return cloud eligibility without making network calls.

    Cache identity contains the repository, environment revision, manifest
    revision, network setting, and brief requirements.  Thus changing any
    eligibility input makes an old verdict unreachable.
    """
    manifest_path = manifest_path or root / MANIFEST
    cache_path = cache_path or root / CACHE
    if not manifest_path.is_file():
        return Result(False, f"cloud preflight manifest is missing: {manifest_path}; build/probe the environment first")
    try:
        manifest = _read_json(manifest_path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return Result(False, f"cloud preflight manifest is unreadable: {manifest_path}: {exc}")

    revision = manifest.get("revision")
    environments = manifest.get("environments")
    entry = environments.get(env_id) if isinstance(environments, dict) else None
    if not isinstance(revision, (str, int)) or not isinstance(entry, dict):
        return Result(False, f"cloud preflight manifest has no environment {env_id!r} or revision")
    environment_revision = entry.get("revision")
    manifest_network = entry.get("network")
    if not isinstance(environment_revision, (str, int)) or not isinstance(manifest_network, (str, bool)):
        return Result(False, f"cloud preflight manifest entry {env_id!r} needs revision and network")

    requirements = {
        "paths": sorted(set(_LOCAL_PATH.findall(brief))),
        "repositories": sorted(_items(_REPOSITORIES, brief)),
        "imports": sorted(_items(_IMPORTS, brief)),
    }
    if network != (manifest_network is True):
        requested = "on" if network else "off"
        available = "on" if manifest_network is True else "off"
        return Result(False, f"requested network {requested} does not match cloud environment {env_id} network {available}")

    for field in ("repositories", "imports"):
        dependencies = entry.get(field, [])
        if not isinstance(dependencies, list) or not all(isinstance(item, str) for item in dependencies):
            return Result(False, f"cloud preflight manifest entry {env_id!r} field {field!r} must be a list of strings")

    entry_dependencies = {
        "repositories": sorted(entry.get("repositories", [])),
        "imports": sorted(entry.get("imports", [])),
    }
    key_data = [
        repo, env_id, environment_revision, revision, manifest_network, entry_dependencies, requirements, SCHEMA_REVISION,
    ]
    key = hashlib.sha256(json.dumps(key_data, sort_keys=True).encode()).hexdigest()
    try:
        cached = _read_json(cache_path).get(key)
        if isinstance(cached, dict) and isinstance(cached.get("eligible"), bool) and isinstance(cached.get("reason"), str):
            return Result(cached["eligible"], cached["reason"], True)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        pass

    available_repos = set(entry_dependencies["repositories"])
    available_imports = set(entry_dependencies["imports"])
    if requirements["paths"]:
        result = Result(False, f"brief references a local absolute path unavailable in cloud: {requirements['paths'][0]}")
    elif missing := sorted(set(requirements["repositories"]) - available_repos):
        result = Result(False, f"required repository is not available in cloud environment {env_id}: {', '.join(missing)}")
    elif missing := sorted(set(requirements["imports"]) - available_imports):
        result = Result(False, f"required import is not available in cloud environment {env_id}: {', '.join(missing)}")
    else:
        result = Result(True, "cloud preflight passed")

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        cache = _read_json(cache_path) if cache_path.is_file() else {}
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        cache = {}
    cache[key] = {"eligible": result.eligible, "reason": result.reason}
    cache_path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result
