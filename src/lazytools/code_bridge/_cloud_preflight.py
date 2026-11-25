"""Codex probe evidence and the local eligibility check before a cloud submit.

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
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

MANIFEST = ".bridge/cloud-preflight.json"
CACHE = ".bridge/cloud-preflight-cache.json"
SCHEMA_REVISION = 1
MAX_AGE_DAYS = 14

_LOCAL_PATH = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Za-z]:[\\/][^\s`'\"]+|\\\\[^\s`'\"]+|file://[^\s`'\"]+|/(?:Users|home|mnt|media|Volumes)/[^\s`'\"]+)",
    re.IGNORECASE,
)
_REPOSITORIES = re.compile(r"(?im)^\s*(?:required[-_ ]?)?repositor(?:y|ies)\s*:\s*(.+)$")
_IMPORTS = re.compile(r"(?im)^\s*(?:required[-_ ]?)?imports?\s*:\s*(.+)$")
_DEPENDENCY_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_GITHUB_REPOSITORY = re.compile(r"github\.com[/:]([^/\s]+/[^/@\s]+?)(?:\.git)?(?:@|$)", re.IGNORECASE)
_PROBE_BLOCK = re.compile(r"```json\r?\n(.+?)\r?\n```", re.DOTALL)
_PYTHON_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:a[0-9]+|b[0-9]+|rc[0-9]+|\+)?")


@dataclass(frozen=True)
class Result:
    eligible: bool
    reason: str
    cache_hit: bool = False


@dataclass(frozen=True)
class Requirements:
    imports: list[str]
    repositories: list[str]


@dataclass(frozen=True)
class ProbeEvidence:
    network: bool
    python_version: str
    imports: dict[str, bool]
    repositories: dict[str, bool]


@dataclass(frozen=True)
class Probe:
    requirements: Requirements
    expected_network: bool | None

    def brief(self) -> str:
        expected = {None: "unspecified", True: "on", False: "off"}[self.expected_network]
        imports = json.dumps(self.requirements.imports)
        repositories = json.dumps(self.requirements.repositories)
        return f"""# Codex cloud preflight probe

## Goal
Measure the capabilities of this cloud environment from the task container.

## Context
Expected network: {expected}. Report the observed result regardless of this expectation.
Import names to test: {imports}
Sibling GitHub repositories to test: {repositories}

## Constraints
- Record Python's version using platform.python_version().
- Make one HTTPS request to https://pypi.org/ with a 10-second timeout and redirects disabled.
  Set network to true only for a successful response. Do not retry.
- Before testing sibling installs, try each named import in its own Python process with a
  15-second timeout. Mark it true only if importlib.import_module(name) succeeds.
- A sibling passes if a matching checkout is present or its declared dependency reference
  in pyproject.toml installs successfully. Verify checkout identity from its Git remote.
  For an install attempt, use the declared source and ref, a temporary install target,
  GIT_TERMINAL_PROMPT=0, pip --no-input, and a 60-second timeout. Never substitute an unpinned source
  or a PyPI package for a sibling. Mark unavailable or unverified checks false.
- Keep the checkout unchanged except for .bridge/report.md. Keep credentials and
  command diagnostics out of the report. Ensure the report appears in the task diff,
  staging just this file if it is ignored.

## Done when
Every listed name has been checked and appears exactly once with a JSON boolean result.

## Report
Print only one fenced ```json block, with no surrounding prose, and copy the same block
to .bridge/report.md. Use exactly these fields: network (boolean), python_version
(major.minor.patch string), imports (object keyed by the listed import names), and
repositories (object keyed by the listed sibling slugs). Use empty objects for empty lists."""


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


def project_requirements(root: Path, *, allow_missing: bool = False) -> Requirements:
    """Read the import names and sibling slugs used by both manifest workflows."""
    pyproject_path = root / "pyproject.toml"
    if allow_missing and not pyproject_path.exists():
        return Requirements(imports=[], repositories=[])
    try:
        pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read project dependencies from {pyproject_path}: {exc}") from exc
    project = pyproject.get("project", {})
    if not isinstance(project, dict):
        raise ValueError(f"{pyproject_path}: [project] must be a table")
    dependencies = project.get("dependencies", [])
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        raise ValueError(f"{pyproject_path}: [project.optional-dependencies] must be a table")
    requirements: list[str] = []
    for group in [dependencies, *optional.values()]:
        if not isinstance(group, list) or not all(isinstance(item, str) for item in group):
            raise ValueError(f"{pyproject_path}: project dependency groups must be lists of strings")
        requirements.extend(group)

    imports = sorted({match.group(1).replace("-", "_") for item in requirements
                      if (match := _DEPENDENCY_NAME.match(item))})
    repositories = sorted({match.group(1) for item in requirements
                           if (match := _GITHUB_REPOSITORY.search(item))})
    return Requirements(imports=imports, repositories=repositories)


def _today() -> date:
    return datetime.now(UTC).date()


def init_manifest(*, root: Path, env_id: str, network: bool) -> Path:
    """Create an operator-attested starter manifest from ``pyproject.toml``."""
    requirements = project_requirements(root)
    path = root / MANIFEST
    if path.exists():
        raise ValueError(f"refusing to overwrite existing cloud preflight manifest: {path}")
    data = {
        "revision": "starter-1",
        "environments": {env_id: {
            "revision": "operator-attested-1", "network": network,
            "repositories": requirements.repositories, "imports": requirements.imports,
            "attested_by": "operator", "attested_on": _today().isoformat(),
        }},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate probe JSON key: {key!r}")
        result[key] = value
    return result


def parse_probe_output(output: str, requirements: Requirements) -> ProbeEvidence:
    """Accept one fenced JSON result with a boolean for every requested check."""
    block = _PROBE_BLOCK.fullmatch(output.strip())
    if block is None:
        raise ValueError("probe evidence must be a single fenced JSON block")
    try:
        payload = json.loads(block.group(1), object_pairs_hook=_unique_object)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid probe JSON at line {exc.lineno}, column {exc.colno}") from exc
    if not isinstance(payload, dict) or payload.keys() != {"network", "python_version", "imports", "repositories"}:
        raise ValueError("probe JSON must have exactly network, python_version, imports and repositories fields")
    network = payload["network"]
    python_version = payload["python_version"]
    if not isinstance(network, bool):
        raise ValueError("probe network result must be a boolean")
    if not isinstance(python_version, str) or not _PYTHON_VERSION.fullmatch(python_version):
        raise ValueError("probe python_version must be a Python version string such as 3.12.4")
    for field, requested in (("imports", requirements.imports), ("repositories", requirements.repositories)):
        results = payload[field]
        if not isinstance(results, dict) or results.keys() != set(requested):
            raise ValueError(f"probe {field} must contain exactly the requested names")
        if not all(isinstance(passed, bool) for passed in results.values()):
            raise ValueError(f"probe {field} results must be booleans")
    return ProbeEvidence(network, python_version, payload["imports"], payload["repositories"])


def write_probe_manifest(*, root: Path, env_id: str, job_id: str, evidence: ProbeEvidence) -> Path:
    """Replace the probed environment entry, preserving other environment entries."""
    path = root / MANIFEST
    environments: dict[str, Any] = {}
    if path.exists():
        # A fresh probe must also recover a manifest that check cannot read.
        try:
            existing = _read_json(path)
        except (UnicodeError, ValueError):
            pass
        else:
            if isinstance(existing.get("environments"), dict):
                environments = existing["environments"]
    environments[env_id] = {
        "revision": f"probe:{job_id}",
        "attested_by": f"probe:{job_id}",
        "attested_on": _today().isoformat(),
        "network": evidence.network,
        "python_version": evidence.python_version,
        "imports": sorted(name for name, passed in evidence.imports.items() if passed),
        "repositories": sorted(name for name, passed in evidence.repositories.items() if passed),
    }
    output = json.dumps({"revision": "probe-1", "environments": environments}, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=path.parent) as directory:
        temporary_path = Path(directory) / path.name
        temporary_path.write_text(output, encoding="utf-8")
        temporary_path.replace(path)
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
    refresh = f"run cloud-preflight probe --repo <path> for environment {env_id!r}"
    if not manifest_path.is_file():
        return Result(False, f"cloud preflight manifest is missing: {manifest_path}; {refresh}")
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

    attested_on = entry.get("attested_on")
    invalid_date = f"cloud preflight manifest needs a valid attestation date; {refresh}"
    if not isinstance(attested_on, str):
        return Result(False, invalid_date)
    try:
        attestation_date = date.fromisoformat(attested_on)
    except ValueError:
        return Result(False, invalid_date)
    age = (_today() - attestation_date).days
    if age < 0 or age > MAX_AGE_DAYS:
        return Result(False, f"cloud preflight manifest date {attestation_date} is outside the last "
                      f"{MAX_AGE_DAYS} days; {refresh}")

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
