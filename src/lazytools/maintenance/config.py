"""Strict stdlib TOML loader for a portable read-only maintenance workspace.

The manifest declares repository roots and dependency hints, never commands,
reviewer capabilities, Git credentials or source-code write permissions.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core import Dependency, RepoSpec, Workspace

_MAX_MANIFEST_BYTES = 64_000


@dataclass(frozen=True)
class MaintenanceConfig:
    workspace: Workspace
    embeddings: bool = False
    timeout_seconds: float = 45.0


def _keys(value: object, allowed: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a table")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{label} contains unknown keys: {', '.join(sorted(unknown))}")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError(f"{label} must be nonempty text")
    if len(value) > 2000:
        raise ValueError(f"{label} is too long")
    return value


def _bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be true or false")
    return value


def load_config(filename: str | Path) -> MaintenanceConfig:
    """Load an operator-owned TOML manifest; reject unexpected capabilities."""
    path = Path(filename).expanduser().resolve(strict=True)
    if not path.is_file() or path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise ValueError("Manifest must be a readable TOML file under 64 KB")
    try:
        contents = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("Invalid maintenance TOML manifest") from exc
    contents = _keys(
        contents, {"schema_version", "state_dir", "require_clean", "repos",
                   "dependencies", "mapping"}, "manifest",
    )
    if type(contents.get("schema_version")) is not int or contents["schema_version"] != 1:
        raise ValueError("Unsupported maintenance manifest schema_version (expected 1)")
    state_dir = _text(contents.get("state_dir"), "state_dir")
    require_clean = _bool(contents.get("require_clean", True), "require_clean")
    entries = contents.get("repos")
    if not isinstance(entries, list) or not entries or len(entries) > 40:
        raise ValueError("repos must contain 1-40 configured repositories")
    repos: list[RepoSpec] = []
    for i, raw in enumerate(entries):
        row = _keys(raw, {"id", "root"}, f"repos[{i}]")
        name = _text(row.get("id"), f"repos[{i}].id")
        root = _text(row.get("root"), f"repos[{i}].root")
        candidate = Path(root).expanduser()
        if not candidate.is_absolute():
            candidate = path.parent / candidate
        repos.append(RepoSpec(name, candidate))
    raw_deps = contents.get("dependencies", [])
    if not isinstance(raw_deps, list) or len(raw_deps) > 200:
        raise ValueError("dependencies must be a list of at most 200 relations")
    deps: list[Dependency] = []
    for i, raw in enumerate(raw_deps):
        row = _keys(raw, {"consumer", "provider", "import_prefix"}, f"dependencies[{i}]")
        deps.append(
            Dependency(
                consumer=_text(row.get("consumer"), f"dependencies[{i}].consumer"),
                provider=_text(row.get("provider"), f"dependencies[{i}].provider"),
                import_prefix=(
                    _text(row["import_prefix"], f"dependencies[{i}].import_prefix")
                    if "import_prefix" in row else None
                ),
            )
        )
    mapping = _keys(contents.get("mapping", {}), {"embeddings", "timeout_seconds"}, "mapping")
    embeddings = _bool(mapping.get("embeddings", False), "mapping.embeddings")
    timeout = mapping.get("timeout_seconds", 45.0)
    if type(timeout) not in (int, float) or not 0 < timeout <= 600:
        raise ValueError("mapping.timeout_seconds must be within (0, 600]")
    state = Path(state_dir).expanduser()
    if not state.is_absolute():
        state = path.parent / state
    workspace = Workspace(
        repos=tuple(repos), state_dir=state, dependencies=tuple(deps),
        require_clean=require_clean,
    )
    return MaintenanceConfig(workspace, embeddings=embeddings, timeout_seconds=float(timeout))
