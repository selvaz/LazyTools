"""Deterministic Git and repo-map primitives for continuous code maintenance.

No LLM calls or source-file writes happen in this module.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lazytools.codemap import RepoMapCLI

_ALIAS = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_REF = re.compile(r"^[A-Za-z0-9_./^~{}+-]{1,200}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")


class MaintenanceError(RuntimeError):
    """A necessary Git/map fact was unavailable: never claim a clean review."""


@dataclass(frozen=True)
class RepoSpec:
    id: str
    root: Path

    def __post_init__(self) -> None:
        if not _ALIAS.fullmatch(self.id):
            raise ValueError(f"Invalid repository alias: {self.id!r}")
        root = Path(self.root).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError(f"Not a repository directory: {root}")
        object.__setattr__(self, "root", root)


@dataclass(frozen=True)
class Dependency:
    """An operator-declared dependency, not a guessed cross-repo import."""

    consumer: str
    provider: str
    import_prefix: str | None = None


@dataclass(frozen=True)
class Workspace:
    repos: tuple[RepoSpec, ...]
    state_dir: Path
    dependencies: tuple[Dependency, ...] = ()
    require_clean: bool = True

    def __post_init__(self) -> None:
        if not self.repos:
            raise ValueError("Workspace requires at least one repository")
        names = [repo.id for repo in self.repos]
        if len(set(names)) != len(names):
            raise ValueError("Repository aliases must be unique")
        roots = [repo.root for repo in self.repos]
        if len(set(roots)) != len(roots):
            raise ValueError("Each repository must have a distinct root")
        name_set = set(names)
        for dep in self.dependencies:
            if dep.consumer not in name_set or dep.provider not in name_set:
                raise ValueError(f"Unknown dependency endpoint: {dep}")
            if dep.consumer == dep.provider:
                raise ValueError("A repository cannot depend on itself")
            if dep.import_prefix is not None and not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", dep.import_prefix
            ):
                raise ValueError("Dependency import_prefix must be a valid dotted Python module")
        state_dir = Path(self.state_dir).expanduser().resolve()
        for root in roots:
            if state_dir == root or root in state_dir.parents:
                raise ValueError("state_dir must be outside all repository roots")
        object.__setattr__(self, "state_dir", state_dir)

    def paths(self) -> dict[str, Path]:
        return {repo.id: repo.root for repo in self.repos}


def _git(root: Path, *args: str, timeout: int = 30) -> bytes:
    cmd = ["git", "-c", "core.fsmonitor=false", "-C", str(root), *args]
    env = {key: os.environ[key] for key in
           ("PATH", "HOME", "SYSTEMROOT", "SystemRoot", "WINDIR", "TMP", "TEMP", "TMPDIR")
           if key in os.environ}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        proc = subprocess.run(cmd, capture_output=True, check=False, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MaintenanceError(f"Git command failed for {root.name}: {type(exc).__name__}") from exc
    if proc.returncode:
        detail = proc.stderr.decode("utf-8", errors="replace").replace(str(root), "[repository]")[:400]
        raise MaintenanceError(f"Git error for {root.name}: {detail}")
    if len(proc.stdout) > 5_000_000:
        raise MaintenanceError("Git output exceeds 5 MB safety bound")
    return proc.stdout


def _valid_ref(ref: str) -> str:
    if not isinstance(ref, str) or not _REF.fullmatch(ref) or ref.startswith(("-", "/", ".")) or ".." in ref:
        raise ValueError("Invalid Git ref")
    return ref


def resolve_sha(root: Path, ref: str = "HEAD") -> str:
    _valid_ref(ref)
    sha = _git(root, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
    if not _SHA.fullmatch(sha):
        raise MaintenanceError("Git returned an invalid commit SHA")
    return sha


def git_status(root: Path) -> str:
    # Don't persist filenames of untracked files into logs or diagnostics.
    return "dirty" if _git(root, "status", "--porcelain", "--untracked-files=normal") else "clean"


def assert_repo_root(root: Path) -> None:
    top = Path(_git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if root != top:
        raise MaintenanceError("Configured root must be the Git worktree root")


def changed_paths(
    root: Path, base_sha: str | None, head_sha: str, *, max_paths: int = 300
) -> tuple[list[dict[str, str]], bool]:
    if base_sha:
        raw = _git(root, "diff", "--no-ext-diff", "--name-status", "-z", base_sha, head_sha)
        tokens = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
        changes: list[dict[str, str]] = []
        pos = 0
        while pos < len(tokens):
            status = tokens[pos]
            pos += 1
            if "\t" in status:
                status, first = status.split("\t", 1)
            else:
                if pos >= len(tokens):
                    raise MaintenanceError("Malformed Git diff output")
                first = tokens[pos]
                pos += 1
            if status.startswith(("R", "C")):
                if pos >= len(tokens):
                    raise MaintenanceError("Malformed Git rename output")
                dest = tokens[pos]
                pos += 1
                changes.append({"status": status, "path": dest, "from": first})
            else:
                changes.append({"status": status, "path": first})
    else:
        raw = _git(root, "ls-files", "-z")
        changes = [{"status": "inventory", "path": part.decode("utf-8", "replace")}
                   for part in raw.split(b"\0") if part]
    return changes[:max_paths], len(changes) > max_paths


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".part-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@dataclass
class RunState:
    """Run-local data; a new instance is created for each Plan invocation."""

    workspace: Workspace
    base_refs: dict[str, str]
    mapper: RepoMapCLI
    sha: dict[str, str] = field(default_factory=dict)
    base: dict[str, str | None] = field(default_factory=dict)
    dirty: dict[str, str] = field(default_factory=dict)
    snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    changes: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    truncated: dict[str, bool] = field(default_factory=dict)
    impacts: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    review_text: str | None = None
    verification: dict[str, Any] = field(default_factory=lambda: {"status": "not_run", "tests": None})
    report: dict[str, Any] | None = None
    report_path: Path | None = None

    def resolve(self) -> str:
        for repo in self.workspace.repos:
            assert_repo_root(repo.root)
            status = git_status(repo.root)
            if self.workspace.require_clean and status != "clean":
                raise MaintenanceError(
                    f"{repo.id} has uncommitted/untracked changes; refusing to index a mixed revision"
                )
            self.dirty[repo.id] = status
            self.sha[repo.id] = resolve_sha(repo.root)
            self.base[repo.id] = (
                resolve_sha(repo.root, self.base_refs[repo.id]) if repo.id in self.base_refs else None
            )
        return json.dumps({"sha": self.sha, "status": "resolved"})

    def refresh(self) -> str:
        if not self.sha:
            raise MaintenanceError("Resolve must run before snapshot refresh")
        version = self.mapper._invoke("version", [], self.workspace.repos[0].id).strip()[:100]
        for repo in self.workspace.repos:
            key_data = {"root": str(repo.root), "sha": self.sha[repo.id],
                        "dirty": self.dirty[repo.id], "engine": version,
                        "embed": self.mapper._use_embeddings}
            # No content digest is available for a dirty working tree. Never
            # reuse a cache entry keyed only by its committed HEAD SHA.
            if self.dirty[repo.id] != "clean":
                key_data["uncommitted_run_id"] = self.run_id
            key = _digest(key_data)
            path = self.workspace.state_dir / "snapshots" / repo.id / f"{key}.json"
            if path.exists():
                try:
                    cached = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise MaintenanceError(
                        "Existing snapshot is unreadable; refusing to claim a cache hit"
                    ) from exc
                if (cached.get("cache_key") != key or cached.get("sha") != self.sha[repo.id]
                        or cached.get("status") != "ready"):
                    raise MaintenanceError("Snapshot cache metadata mismatch")
                self.snapshots[repo.id] = {**cached, "cache_hit": True}
                continue
            start = time.perf_counter()
            raw = self.mapper.index(repo.id)
            try:
                indexed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise MaintenanceError("RepoMap index did not return valid JSON") from exc
            if not isinstance(indexed, dict) or indexed.get("root") is None:
                raise MaintenanceError("RepoMap index did not identify indexed root")
            snapshot = {
                "repo": repo.id, "sha": self.sha[repo.id], "dirty": self.dirty[repo.id],
                "cache_key": key, "engine": version,
                "status": "ready" if self.dirty[repo.id] == "clean" else "partial",
                "indexed_seconds": round(time.perf_counter() - start, 4),
                "files_count": indexed.get("files"), "symbols_count": indexed.get("symbols"),
                "cache_hit": False,
            }
            _atomic_json(path, snapshot)
            self.snapshots[repo.id] = snapshot
        return json.dumps({"snapshots": {
            key: {"sha": value["sha"], "cache_hit": value["cache_hit"]}
            for key, value in self.snapshots.items()
        }})

    def diff(self) -> str:
        if not self.snapshots:
            raise MaintenanceError("Snapshot refresh is required before diff")
        for repo in self.workspace.repos:
            rows, clipped = changed_paths(repo.root, self.base[repo.id], self.sha[repo.id])
            self.changes[repo.id], self.truncated[repo.id] = rows, clipped
        return json.dumps({"changes": {
            repo: len(rows) for repo, rows in self.changes.items()
        }})

    def impact(self, *, limit: int = 15) -> str:
        if not self.changes:
            raise MaintenanceError("Diff is required before impact")
        for repo in self.workspace.repos:
            evidence: list[dict[str, str]] = []
            for row in self.changes[repo.id][:limit]:
                if row["path"].endswith((".py", ".ts", ".tsx", ".js", ".rs")):
                    try:
                        result = self.mapper.impact(row["path"], repo=repo.id, depth=2)
                        evidence.append({"path": row["path"], "source": "static_graph",
                                         "preview": result[:1200]})
                    except Exception as exc:
                        # An impact failure is unknown, never "no effect".
                        evidence.append({"path": row["path"], "source": "unverified",
                                         "error": type(exc).__name__})
            self.impacts[repo.id] = evidence
        info = {
            "sha": self.sha,
            "changed": {repo: [row["path"] for row in rows[:50]]
                        for repo, rows in self.changes.items()},
            "evidence": {repo: rows[:10] for repo, rows in self.impacts.items()},
            "note": "Static analysis is a hypothesis; do not claim tests passed.",
        }
        return json.dumps(info, ensure_ascii=False)[:12_000]

    def finalize(self, review_text: str = "") -> str:
        if not self.impacts:
            raise MaintenanceError("Impact analysis required before reporting")
        # A checkout can advance while a slow reviewer runs. The external
        # mapper scans the worktree, not an immutable Git object; a moving
        # HEAD would make the cached graph falsely attributable to old SHA.
        for repo in self.workspace.repos:
            if resolve_sha(repo.root) != self.sha[repo.id]:
                raise MaintenanceError(
                    f"{repo.id} HEAD moved during analysis; discard this run and refresh"
                )
            if self.workspace.require_clean and git_status(repo.root) != "clean":
                raise MaintenanceError(
                    f"{repo.id} checkout changed during analysis; discard this run"
                )
        self.review_text = review_text[:6000] if review_text else None
        warnings = []
        if any(self.truncated.values()):
            warnings.append("Changed-path inventory exceeded 300 files in at least one repository")
        if self.verification["status"] in ("failed", "incomplete"):
            warnings.append("Approved tests failed or were unavailable; review the recorded evidence")
        if any(v != "clean" for v in self.dirty.values()):
            warnings.append("Dirty checkout: index may include uncommitted contents; no SHA-only cache reuse or full provenance")
        if any(row.get("source") == "unverified"
               for items in self.impacts.values() for row in items):
            warnings.append("Some impact queries failed or were incomplete")
        cross_repo: list[dict[str, Any]] = []
        for dep in self.workspace.dependencies:
            # An initial inventory is not a diff or evidence of a changed API.
            if self.base[dep.provider] is None or not self.changes.get(dep.provider):
                continue
            candidate: dict[str, Any] = {
                "consumer": dep.consumer, "provider": dep.provider,
                "reason": "manifest_dependency", "confidence": "declared",
                "action": "Check consumer compatibility and tests",
            }
            if dep.import_prefix is not None:
                from .relationships import python_import_evidence

                evidence = python_import_evidence(
                    self.workspace.paths()[dep.consumer], dep.import_prefix
                )
                candidate["import_evidence"] = evidence
                if evidence["matches"]:
                    candidate["confidence"] = "direct_import"
                if evidence["incomplete"]:
                    warnings.append(
                        f"Import scan for {dep.consumer} was incomplete; missing matches are not proof of safety"
                    )
            cross_repo.append(candidate)
        report = {
            "schema_version": 1, "run_id": self.run_id,
            "status": "review_required", "verified": False,
            "sha": self.sha, "base_sha": self.base, "dirty": self.dirty,
            "snapshots": self.snapshots, "changes": self.changes,
            "truncated": self.truncated, "impacts": self.impacts,
            "cross_repo_candidates": cross_repo,
            "reviews": {"status": "unverified_suggestions", "text": self.review_text}
            if self.review_text else {"status": "not_run"},
            "verification": self.verification,
            "warnings": [*warnings, "Static impact suggestions have not been verified by tests"],
        }
        # Each completed run gets a unique immutable artifact path. Reports
        # with the same SHA but different checks/reviews must never overwrite.
        out = self.workspace.state_dir / "runs" / self.run_id / "report.json"
        _atomic_json(out, report)
        summary = ["# Repository maintenance scan", "",
                   "**Status:** review required; structural findings are not a full verification.",
                   f"**Selected test status:** {self.verification['status']}.", ""]
        for repo in self.workspace.repos:
            name = repo.id
            summary += [
                f"## {name}", f"- HEAD: {self.sha[name]}",
                f"- Base: {self.base[name] or 'initial inventory'}",
                f"- Mapping cache hit: {self.snapshots[name]['cache_hit']}",
                f"- Changed/inventory paths shown: {len(self.changes[name])}", "",
            ]
        if cross_repo:
            summary += ["## Cross-repo candidates", ""]
            for item in cross_repo:
                summary.append(
                    f"- {item['provider']} -> {item['consumer']} "
                    f"({item['confidence']}; verification needed)"
                )
        if self.verification.get("tests"):
            summary += ["## Approved checks", ""]
            for check in self.verification["tests"]:
                summary.append(
                    f"- {check['id']} ({check['repo']}): {check['status']} "
                    f"at {check['sha'][:12]} (exit={check['exit_code']})"
                )
        summary += ["", "## Limitations", "",
                    "- Structural evidence is not completed test verification.",
                    "- Any agent output is unverified review advice.", ""]
        md = out.with_suffix(".md")
        md.write_text("\n".join(summary), encoding="utf-8")
        self.report, self.report_path = report, out
        return json.dumps({"report_json": str(out), "report_markdown": str(md),
                           "status": report["status"]})
