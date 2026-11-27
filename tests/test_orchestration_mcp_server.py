"""``lazytools-orchestration-mcp``: the orchestration-only MCP server (SPEC R1)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

from lazytools.mcp_server import __main__ as entrypoint
from lazytools.mcp_server import providers as providers_module
from lazytools.mcp_server.providers import ORCHESTRATION_PROVIDERS, PROVIDER_FACTORIES
from lazytools.mcp_server.server import expand_tools

ROOT = Path(__file__).resolve().parents[1]
NOTE = "coding tools move to lazytools-orchestration-mcp; they stay here for one release"
CODEX_TOOLS = {"codex_code_review", "codex_ask", "codex_review_changes"}
CLAUDE_TOOLS = {"claude_code_review", "claude_ask"}
ENGINE_FREE_TOOLS = {
    "bridge_jobs",
    "codemap_map",
    "projects_list",
    "code_bridge_submit",
    "code_sessions_list",
}


def _engines(monkeypatch: pytest.MonkeyPatch, home: Path, *installed: str) -> None:
    """Make exactly ``installed`` of the ``claude`` / ``codex`` CLIs discoverable."""
    real_which = shutil.which

    def which(name: str, *args: Any, **kwargs: Any) -> str | None:
        if name in ("claude", "codex"):
            return f"fake-{name}" if name in installed else None
        return real_which(name, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", which)
    # Codex is also found through CODEX_BIN and the desktop app's directories.
    monkeypatch.delenv("CODEX_BIN", raising=False)
    for name in ("LOCALAPPDATA", "HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in (
        "LAZYTOOLS_MCP_PROVIDERS",
        "LAZYTOOLS_MCP_CONFIG",
        "LAZYTOOLS_PROJECTS_TOOLS",
        "LAZYTOOLS_PROJECTS_STORE_DB",
        "LAZYCEO_CEO_STORE_DB",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LAZYTOOLS_CODE_ROOT", str(tmp_path))
    # The consultants' own data toolset is not part of the served surface.
    monkeypatch.setattr(providers_module, "_consultant_toolset", lambda *args: [])
    _engines(monkeypatch, tmp_path, "claude", "codex")


def _expected(ids: tuple[str, ...], *, allow_unsafe: bool) -> set[str]:
    """Tool names of ``ids``, built the way the server builds them."""
    built: list[Any] = []
    for provider_id in ids:
        try:
            provider = PROVIDER_FACTORIES[provider_id](allow_unsafe, data_source=None)
        except RuntimeError:
            assert not allow_unsafe, provider_id
            continue
        built.extend(provider if isinstance(provider, (list, tuple)) else [provider])
    return set(expand_tools(built, read_only=not allow_unsafe))


def _listed(capsys: pytest.CaptureFixture[str], main: Any, *argv: str) -> list[str]:
    capsys.readouterr()
    assert main(["--list-tools", *argv]) is None
    return capsys.readouterr().out.splitlines()


def _served(monkeypatch: pytest.MonkeyPatch, main: Any, *argv: str) -> Any:
    servers: list[Any] = []

    async def serve(server: Any) -> None:
        servers.append(server)

    monkeypatch.setattr(entrypoint, "serve_stdio", serve)
    main(list(argv))
    assert len(servers) == 1
    return servers[0]


def test_provider_list() -> None:
    assert ORCHESTRATION_PROVIDERS == (
        "code_bridge",
        "code_review",
        "claude_review",
        "code_sessions",
        "codemap",
        "projects",
        "fleet",
        "knowledge",
    )
    assert set(ORCHESTRATION_PROVIDERS) <= PROVIDER_FACTORIES.keys()


def test_console_script_is_declared() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["scripts"]["lazytools-orchestration-mcp"] == "lazytools.mcp_server.__main__:orchestration_main"
    assert project["scripts"]["lazytools-mcp"] == "lazytools.mcp_server.__main__:main"


@pytest.mark.parametrize(("main", "ids", "name"), [
    ("orchestration_main", list(ORCHESTRATION_PROVIDERS), "lazytools-orchestration"),
    ("main", None, "lazytools"),
])
def test_both_entry_points_share_the_launcher_with_their_own_defaults(
    monkeypatch: pytest.MonkeyPatch, main: str, ids: list[str] | None, name: str
) -> None:
    requested: list[list[str] | None] = []
    monkeypatch.setattr(entrypoint, "default_providers", lambda ids, **kwargs: requested.append(ids) or [])
    monkeypatch.setattr(entrypoint, "_preimport_scientific_modules", lambda ids: None)

    server = _served(monkeypatch, getattr(entrypoint, main))

    assert requested == [ids]
    assert server.name == name


def test_http_flags_reach_the_http_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(entrypoint, "serve_http", lambda server, **kwargs: calls.append(kwargs))

    entrypoint.orchestration_main(["fleet", "--http", "--port", "9123"])

    assert calls == [{"host": "127.0.0.1", "port": 9123}]


@pytest.mark.parametrize("argv", [["datahub"], ["code_write"], ["nope"], ["projects", "datahub"]])
def test_explicit_id_outside_the_list_is_refused(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        entrypoint.orchestration_main(["--list-tools", "--allow-unsafe", *argv])

    captured = capsys.readouterr()
    assert exit_info.value.code == 2
    assert captured.out == ""
    assert f"not served by lazytools-orchestration-mcp: {argv[-1]}" in captured.err


def test_env_selected_id_outside_the_list_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LAZYTOOLS_MCP_PROVIDERS", "fleet,datahub")
    with pytest.raises(SystemExit) as exit_info:
        entrypoint.orchestration_main(["--list-tools"])

    assert exit_info.value.code == 2
    assert "datahub" in capsys.readouterr().err


def test_explicit_id_inside_the_list_selects_it(capsys: pytest.CaptureFixture[str]) -> None:
    names = _listed(capsys, entrypoint.orchestration_main, "fleet")
    assert set(names) == _expected(("fleet",), allow_unsafe=False)


def test_list_tools_read_only(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(entrypoint, "serve_stdio", lambda server: pytest.fail("--list-tools must not serve"))

    names = _listed(capsys, entrypoint.orchestration_main)

    assert names == sorted(names)
    assert set(names) == _expected(ORCHESTRATION_PROVIDERS, allow_unsafe=False)
    assert {"bridge_jobs", "capabilities_search", "codemap_map", "projects_list"} <= set(names)
    unsafe_only = {"code_bridge_submit", "code_sessions_list", "projects_create"} | CODEX_TOOLS | CLAUDE_TOOLS
    assert not unsafe_only & set(names)


def test_list_tools_allow_unsafe(capsys: pytest.CaptureFixture[str]) -> None:
    names = _listed(capsys, entrypoint.orchestration_main, "--allow-unsafe")

    assert names == sorted(names)
    assert set(names) == _expected(ORCHESTRATION_PROVIDERS, allow_unsafe=True)
    assert ENGINE_FREE_TOOLS | CODEX_TOOLS | CLAUDE_TOOLS | {"projects_create"} <= set(names)
    assert not {"codex_write", "claude_code_write"} & set(names)


def test_list_tools_applies_a_configured_code_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "code_root": str(tmp_path),
                "code_task_context": {
                    "task_id": "project/review",
                    "actor": "worker",
                    "expires_at": "2099-01-01T12:00:00Z",
                    "folder": str(tmp_path),
                    "profile": "read",
                    "tool_classes": ["read"],
                    "operations": ["code_sessions_list"],
                },
            }
        ),
        encoding="utf-8",
    )

    names = _listed(
        capsys, entrypoint.orchestration_main, "code_sessions", "--allow-unsafe", "--config", str(config)
    )

    assert names == ["code_sessions_list"]


@pytest.mark.parametrize("installed", [(), ("claude",), ("codex",)])
def test_starts_with_one_or_no_engine(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    installed: tuple[str, ...],
) -> None:
    _engines(monkeypatch, tmp_path, *installed)

    assert _served(monkeypatch, entrypoint.orchestration_main, "--allow-unsafe") is not None
    names = set(_listed(capsys, entrypoint.orchestration_main, "--allow-unsafe"))

    assert names >= ENGINE_FREE_TOOLS
    assert names & CODEX_TOOLS == (CODEX_TOOLS if "codex" in installed else set())
    assert names & CLAUDE_TOOLS == (CLAUDE_TOOLS if "claude" in installed else set())


def _run_fresh(code: str, *argv: str) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a new interpreter, where nothing is imported yet."""
    path = os.pathsep.join(p for p in (str(ROOT / "src"), os.environ.get("PYTHONPATH")) if p)
    return subprocess.run(
        [sys.executable, "-c", code, *argv],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONPATH": path},
        check=False,
    )


@pytest.mark.parametrize("installed", ["", "claude", "codex", "claude,codex"])
def test_start_imports_no_engine_package(installed: str) -> None:
    code = """
import shutil, sys
installed = sys.argv[1].split(",")
real_which = shutil.which
def which(name, *args, **kwargs):
    if name in ("claude", "codex"):
        return "fake-" + name if name in installed else None
    return real_which(name, *args, **kwargs)
shutil.which = which
from lazytools.mcp_server.__main__ import orchestration_main
orchestration_main(["--list-tools", "--allow-unsafe"])
loaded = [m for m in ("claude_agent_sdk", "openai_codex") if m in sys.modules]
assert not loaded, loaded
"""
    result = _run_fresh(code, installed)

    assert result.returncode == 0, result.stderr
    assert "bridge_jobs" in result.stdout.splitlines()


def test_deprecation_line_goes_to_stderr_once() -> None:
    code = """
from lazytools.mcp_server.__main__ import main
main(["--list-tools", "fleet", "codemap"])
"""
    result = _run_fresh(code)

    assert result.returncode == 0, result.stderr
    assert result.stderr.count(NOTE) == 1
    assert NOTE not in result.stdout


@pytest.mark.parametrize("argv", [
    ["fleet"],
    ["codemap"],
    ["code_bridge", "--allow-unsafe"],
    ["code_review", "--allow-unsafe"],
    ["claude_review", "--allow-unsafe"],
    ["code_sessions", "--allow-unsafe"],
])
def test_lazytools_mcp_warns_when_it_mounts_a_coding_provider(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, argv: list[str]
) -> None:
    _served(monkeypatch, entrypoint.main, *argv)

    assert caplog.messages.count(NOTE) == 1


@pytest.mark.parametrize(("main", "argv"), [
    ("main", ["projects", "registry"]),
    ("main", ["code_bridge"]),  # not mounted without --allow-unsafe
    ("orchestration_main", ["--allow-unsafe"]),
])
def test_no_deprecation_line_otherwise(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, main: str, argv: list[str]
) -> None:
    _served(monkeypatch, getattr(entrypoint, main), *argv)

    assert NOTE not in caplog.messages
