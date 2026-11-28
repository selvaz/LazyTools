from __future__ import annotations

import argparse
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from lazytools.code_bridge import _cloud, _dispatch, _doctor
from lazytools.code_bridge._dispatch import Task
from lazytools.projects.admission import TelemetryReading, WindowReading


def _args(root: Path, **changes: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "repo_root": [f"owner/repo={root}"], "registry_db": None, "db": None,
        "knowledge_db": None, "land": False, "allow_cloud": True,
    }
    values.update(changes)
    return argparse.Namespace(**values)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_detached_root_is_an_error(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    (tmp_path / "a").write_text("a")
    _git(tmp_path, "add", "a")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "a")
    _git(tmp_path, "remote", "add", "origin", "https://example.invalid/owner/repo")
    _git(tmp_path, "checkout", "--detach")
    assert "repo-detached-head" in {item.code for item in _doctor._git_findings("owner/repo", tmp_path)}


def test_missing_pytest_path_is_an_error(tmp_path: Path) -> None:
    findings = _doctor._command_findings("p/t", {"checks": ["python -m pytest tests/no.py"]}, tmp_path)
    assert "check-path-missing" in {item.code for item in findings}


def test_pytest_path_named_in_task_text_is_allowed(tmp_path: Path) -> None:
    task = {"checks": ["python -m pytest tests/generated.py"], "text": "Generate tests/generated.py first"}
    assert "check-path-missing" not in {item.code for item in _doctor._command_findings("p/t", task, tmp_path)}


def _fake_project(monkeypatch: object, tmp_path: Path, task: Task) -> None:
    # Kept as a small monkeypatch adapter so these tests exercise the public aggregation.
    monkeypatch.setattr(_doctor, "_resolve_store_db", lambda _value: str(tmp_path / "projects.sqlite"))
    (tmp_path / "projects.sqlite").touch()
    monkeypatch.setattr(_doctor, "ProjectTaskService", lambda _path: object())
    monkeypatch.setattr(_doctor, "_tasks", lambda _service, _project: [task])
    reading = TelemetryReading("codex", "test", datetime.now(UTC), (WindowReading("weekly", 1.0),))
    monkeypatch.setattr(_doctor, "read_readings", lambda **_kwargs: {"codex": reading})


def test_missing_cloud_environment_map(monkeypatch, tmp_path: Path) -> None:
    task = Task("p/t", "1", "todo", {"repo": "owner/repo", "requested_placement": "cloud"}, None)
    _fake_project(monkeypatch, tmp_path, task)
    monkeypatch.setattr(_doctor._cloud, "load_config", lambda: _cloud.CloudConfig())
    assert "cloud-env-missing" in {item.code for item in _doctor.check_project("p", _args(tmp_path))}


def test_missing_projects_store(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(_doctor, "_resolve_store_db", lambda _value: None)
    monkeypatch.setattr(_doctor, "read_readings", lambda **_kwargs: {})
    assert "projects-store-missing" in {item.code for item in _doctor.check_project("p", _args(tmp_path))}


def test_gh_auth_is_checked_only_for_land(monkeypatch, tmp_path: Path) -> None:
    task = Task("p/t", "1", "done", {}, None)
    _fake_project(monkeypatch, tmp_path, task)
    monkeypatch.setattr(_doctor.shutil, "which", lambda name: "fake-gh" if name == "gh" else None)
    monkeypatch.setattr(_doctor._cloud, "repo_root", lambda path: path)
    monkeypatch.setattr(_doctor._cloud, "repo_slug", lambda _path: "owner/repo")
    calls: list[list[str]] = []
    def run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1)
    monkeypatch.setattr(_doctor.subprocess, "run", run)
    without = {item.code for item in _doctor.check_project("p", _args(tmp_path))}
    assert calls == []
    with_land = {item.code for item in _doctor.check_project("p", _args(tmp_path, land=True))}
    assert "gh-auth-missing" not in without
    assert "gh-auth-missing" in with_land
    assert calls == [["fake-gh", "auth", "status"]]

    def unavailable(_command, **_kwargs):
        raise OSError("cannot launch gh")
    monkeypatch.setattr(_doctor.subprocess, "run", unavailable)
    assert "gh-auth-missing" in {item.code for item in _doctor.check_project("p", _args(tmp_path, land=True))}


def test_missing_approver_credential(monkeypatch, tmp_path: Path) -> None:
    registry = tmp_path / "registry.sqlite"
    registry.touch()
    task = Task("p/t", "1", "done", {}, None)
    _fake_project(monkeypatch, tmp_path, task)
    monkeypatch.setattr(_doctor._credentials, "load", lambda _path: (_ for _ in ()).throw(ValueError("missing")))
    findings = _doctor.check_project("p", _args(tmp_path, registry_db=registry))
    assert "approver-credential-missing" in {item.code for item in findings}


def test_dispatch_refuses_errors_before_opening_service(monkeypatch) -> None:
    finding = _doctor.Finding("error", "bad", "p/t", "broken", "fix it")
    monkeypatch.setattr(_doctor, "check_project", lambda _project, _args: [finding])
    monkeypatch.setattr(_dispatch, "ProjectTaskService",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("service opened")))
    args = argparse.Namespace(project="p", skip_doctor=False, json=False)
    assert _dispatch.dispatch(args) == 2


def test_dispatch_starts_when_doctor_has_warnings_only(monkeypatch) -> None:
    finding = _doctor.Finding("warning", "warn", "p/t", "uncertain", "refresh")
    monkeypatch.setattr(_doctor, "check_project", lambda _project, _args: [finding])
    monkeypatch.setattr(_dispatch, "_resolve_store_db", lambda _value: None)
    monkeypatch.setattr(_dispatch, "ProjectTaskService", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(_dispatch, "_dry_run", lambda _service, _project: 0)
    args = argparse.Namespace(project="p", skip_doctor=False, json=False, lease_minutes=None,
                              lease_seconds=None, dry_run=True)
    assert _dispatch.dispatch(args) == 0
