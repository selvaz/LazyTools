"""Pre-dispatch validation for a projects task graph.

This module deliberately only reads project and bridge state.  In particular it
does not create stores, claim tasks, or contact a cloud provider.
"""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from lazytools.code_bridge import _cloud, _cloud_preflight, _coder, _concurrency, _credentials, _registry, _store
from lazytools.code_bridge._dispatch import TERMINAL, _tasks
from lazytools.connectors.projects.tools import _resolve_store_db
from lazytools.projects.task_service import ProjectTaskService
from lazytools.routing.live import read_readings


@dataclass(frozen=True)
class Finding:
    severity: Literal["error", "warning"]
    code: str
    subject: str
    message: str
    fix: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def _finding(severity: Literal["error", "warning"], code: str, subject: str,
             message: str, fix: str) -> Finding:
    return Finding(severity, code, subject, message, fix)


def _mappings(args: argparse.Namespace) -> tuple[dict[str, Path], list[Finding]]:
    roots: dict[str, Path] = {}
    findings: list[Finding] = []
    for value in getattr(args, "repo_root", []) or []:
        slug, separator, raw = str(value).partition("=")
        if not separator or not slug or not raw:
            findings.append(_finding("error", "repo-root-format", str(value),
                "repository mapping must be SLUG=PATH", "Pass --repo-root owner/name=/checkout."))
        else:
            roots[slug.casefold()] = Path(raw).expanduser().resolve()
    return roots, findings


def _git_findings(slug: str, root: Path) -> list[Finding]:
    subject = slug or str(root)
    if not root.is_dir():
        return [_finding("error", "repo-root-missing", subject, f"repository root does not exist: {root}",
                         "Correct --repo-root or create the checkout.")]
    def git(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(root), *argv], text=True, capture_output=True, check=False)
    if git("rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        return [_finding("error", "repo-not-git", subject, f"not a git checkout: {root}",
                         "Clone the repository and map that checkout.")]
    result: list[Finding] = []
    if git("symbolic-ref", "--quiet", "--short", "HEAD").returncode:
        result.append(_finding("error", "repo-detached-head", subject, f"checkout is on a detached HEAD: {root}",
                               "Check out the base branch before dispatching."))
    if git("remote", "get-url", "origin").returncode:
        result.append(_finding("error", "repo-origin-missing", subject, f"checkout has no origin remote: {root}",
                               "Add the repository's origin remote."))
    return result


def _command_findings(task_id: str, task: dict[str, Any], root: Path) -> list[Finding]:
    findings: list[Finding] = []
    text = str(task.get("text") or "")
    for command in task.get("checks", []) or []:
        try:
            words = shlex.split(str(command), posix=os.name != "nt")
        except ValueError as exc:
            findings.append(_finding("error", "check-invalid", task_id, f"cannot parse check {command!r}: {exc}",
                                     "Correct the task's checks command."))
            continue
        if not words:
            continue
        executable = words[0].strip('"')
        exists = ((root / executable).is_file() if any(mark in executable for mark in ("/", "\\"))
                  else shutil.which(executable) is not None)
        if not exists:
            findings.append(_finding("error", "check-executable-missing", task_id,
                f"check executable is unavailable: {executable}", f"Install {executable} or correct the check."))
        pytest_at = next((i for i, word in enumerate(words) if Path(word.strip('"')).name in {"pytest", "pytest.exe"}), None)
        if pytest_at is None and len(words) >= 3 and words[1:3] == ["-m", "pytest"]:
            pytest_at = 2
        if pytest_at is None:
            continue
        options_with_value = {"-k", "-m", "--maxfail", "--tb", "--capture", "--basetemp", "-o", "--override-ini"}
        skip_value = False
        for raw in words[pytest_at + 1:]:
            if skip_value:
                skip_value = False
                continue
            if raw in options_with_value:
                skip_value = True
                continue
            if raw.startswith("-"):
                continue
            path_text = raw.split("::", 1)[0]
            if not path_text or (root / path_text).exists() or path_text in text:
                continue
            findings.append(_finding("error", "check-path-missing", task_id,
                f"pytest path does not exist in {root}: {path_text}",
                "Correct the check path, or name that exact generated path in the task text (F62)."))
    return findings


def _writable_store(path: Path, code: str, label: str) -> Finding | None:
    if not path.exists():
        return _finding("error", code, label, f"store does not exist: {path}", "Initialize the store before dispatching.")
    if not path.is_file() or not os.access(path, os.R_OK | os.W_OK) or not os.access(path.parent, os.W_OK):
        return _finding("error", code, label, f"store is not readable and writable: {path}",
                        "Give the dispatcher read/write access to the store and its directory.")
    return None


def check_project(project: str, args: argparse.Namespace) -> list[Finding]:
    """Return every problem known before any task is claimed."""
    findings: list[Finding] = []
    roots, malformed = _mappings(args)
    findings.extend(malformed)
    projects_raw = _resolve_store_db(None)
    projects_path = Path(projects_raw).expanduser().resolve() if projects_raw else None
    tasks: list[Any]
    if projects_path is None:
        findings.append(_finding("error", "projects-store-missing", "projects", "no persistent projects store is configured",
                                 "Set LAZYTOOLS_PROJECTS_STORE_DB."))
        tasks = []
    else:
        store_problem = _writable_store(projects_path, "projects-store-missing", "projects")
        if store_problem:
            findings.append(store_problem)
            tasks = []
        else:
            try:
                tasks = _tasks(ProjectTaskService(str(projects_path)), project)
            except Exception as exc:
                findings.append(_finding("error", "project-unreadable", project, str(exc),
                                         "Verify the project id and projects store."))
                tasks = []

    active = [task for task in tasks if task.status not in TERMINAL]
    task_names = {task.task_id.rsplit("/", 1)[-1] for task in tasks}
    config = _cloud.load_config()
    try:
        cwd_slug = _cloud.repo_slug(_cloud.repo_root(Path.cwd())).casefold()
    except (OSError, ValueError, _cloud.CloudError):
        cwd_slug = ""
    checked_roots: set[tuple[str, Path]] = set()
    for task in active:
        data = task.data
        slug = str(data.get("repo") or "")
        root = roots.get(slug.casefold())
        if root is None and slug and cwd_slug == slug.casefold():
            root = Path.cwd().resolve()
        if not slug or root is None:
            findings.append(_finding("error", "repo-root-unresolved", task.task_id,
                f"no local root resolves for repository {slug or '(missing slug)'}",
                "Pass --repo-root SLUG=PATH (or run from that repository)."))
        else:
            key = (slug, root)
            if key not in checked_roots:
                findings.extend(_git_findings(slug, root))
                checked_roots.add(key)
            findings.extend(_command_findings(task.task_id, data, root))
        for dependency in data.get("dependency_ids", []) or []:
            name = str(dependency).rsplit("/", 1)[-1]
            if name not in task_names:
                findings.append(_finding("error", "dependency-missing", task.task_id,
                    f"dependency names no task in project: {dependency}", "Correct or create the dependency task."))
        placement = str(data.get("requested_placement") or data.get("effective_placement") or "local")
        cloud_allowed = placement == "cloud" or (placement == "auto" and getattr(args, "allow_cloud", True))
        if cloud_allowed and slug:
            env_id = config.envs.get("codex", {}).get(slug.casefold())
            if not env_id:
                findings.append(_finding("error", "cloud-env-missing", task.task_id,
                    f"no Codex Cloud environment is mapped for {slug}",
                    "Add the slug under [cloud.codex.envs] in code-bridge.toml."))
            elif root is not None:
                result = _cloud_preflight.check(repo=slug, env_id=env_id, brief=str(data.get("text") or ""), root=root,
                                                network=str(data.get("requested_network") or "off") == "on")
                severity: Literal["error", "warning"] = "warning" if "manifest is missing" in result.reason else "error"
                if not result.eligible:
                    findings.append(_finding(severity, "cloud-preflight", task.task_id, result.reason,
                                             f"Run cloud-preflight probe --repo {root}."))
        if placement == "local":
            engines = data.get("engine_preference") or ("codex", "claude")
            if isinstance(engines, str):
                engines = (engines,)
            if not any(shutil.which(str(engine)) for engine in engines):
                findings.append(_finding("error", "engine-cli-missing", task.task_id,
                    f"none of the router's engine CLIs is installed: {', '.join(map(str, engines))}",
                    "Install Codex or Claude Code, or change engine_preference."))

    slot_dir = _concurrency.load_config().slot_dir
    slot_anchor = slot_dir if slot_dir.exists() else next((parent for parent in slot_dir.parents if parent.exists()), slot_dir)
    if not slot_anchor.is_dir() or not os.access(slot_anchor, os.W_OK):
        findings.append(_finding("error", "slot-dir-unwritable", str(slot_dir), "local engine slot directory is not writable",
                                 "Create it and grant the dispatcher write access."))
    registry_path = Path(getattr(args, "registry_db", None) or _registry._registry_path() or "")
    bridge_path = Path(getattr(args, "db", None) or _store.default_db_path()).expanduser().resolve()
    knowledge_path = Path(getattr(args, "knowledge_db", None) or os.environ.get("LAZYTOOLS_KNOWLEDGE_DB", "~/.lazytools/knowledge.sqlite")).expanduser().resolve()
    for path, code, label in ((bridge_path, "bridge-store-missing", "bridge"),
                              (registry_path, "registry-store-missing", "registry"),
                              (knowledge_path, "knowledge-store-missing", "knowledge")):
        problem = _writable_store(path, code, label)
        if problem:
            findings.append(problem)
    if registry_path.is_file():
        try:
            _credentials.load(registry_path)
        except Exception as exc:
            findings.append(_finding("error", "approver-credential-missing", "approver", str(exc),
                                     "Run approver-credential init for this registry."))
        try:
            selection = _coder.show(registry_path)
            if not selection.cards:
                findings.append(_finding("error", "coder-hand-missing", "autonomy",
                                         "coder hand / owner cards are not provisioned",
                                         "Run coder-hand provision, then autonomy-config --mode supervised."))
        except Exception as exc:
            findings.append(_finding("error", "coder-hand-unreadable", "autonomy", str(exc),
                                     "Repair or reinitialize the approval registry."))
        # A list is the legacy, unconfigured hand; configured records have a mode/preset.
        try:
            from lazytools.projects.task_store import transaction
            with transaction(registry_path) as store:
                configured = isinstance(store.read(_coder._HAND_KEY), dict)
            if not configured:
                findings.append(_finding("error", "autonomy-preset-missing", "autonomy", "no autonomy preset is configured",
                                         "Run autonomy-config --mode supervised --preset with-production."))
        except Exception as exc:
            findings.append(_finding("error", "autonomy-unreadable", "autonomy", str(exc), "Repair the approval registry."))
    # This is the same bounded/cache-backed reader used by route and dispatch.
    readings = read_readings(timeout=5.0)
    if not any(reading.error is None and reading.windows for reading in readings.values()):
        detail = "; ".join(f"{engine}: {reading.error or 'no quota windows'}"
                           for engine, reading in readings.items()) or "no telemetry returned"
        findings.append(_finding("error", "quota-telemetry-unreadable", "routing", detail,
                                 "Authenticate at least one engine and refresh its quota telemetry."))
    if getattr(args, "land", False):
        auth = subprocess.run(["gh", "auth", "status"], text=True, capture_output=True, check=False) if shutil.which("gh") else None
        if auth is None or auth.returncode:
            findings.append(_finding("error", "gh-auth-missing", "landing", "GitHub CLI is not authenticated",
                                     "Install gh and run `gh auth login`, or provide GH_TOKEN."))
    return findings


__all__ = ["Finding", "check_project"]
