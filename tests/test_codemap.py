from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lazytools.codemap import RepoMapCLI, RepoMapError, RepoMapTools
from lazytools.mcp_server.providers import default_providers
from lazytools.mcp_server.server import build_server, expand_tools
from mcp_test_utils import connected_client, protocol_field


@pytest.fixture
def stub_repomap(tmp_path: Path) -> Path:
    binary = tmp_path / "repomap"
    binary.write_text(
        """#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
if args[0] == "search":
    result = {"results": [{"file": "src/app.py", "reason": "matched retry", "snippet": "def retry():"}],
              "query": args[args.index("--") + 1], "limit": int(args[args.index("--limit") + 1]), "args": args}
else:
    result = {"map": "src/app.py: retry", "tokens": int(args[args.index("--tokens") + 1])}
result["cwd"] = str(pathlib.Path.cwd())
result["cache"] = os.environ.get("XDG_CACHE_HOME")
result["repomap_cache"] = os.environ.get("REPOMAP_CACHE_DIR")
print(json.dumps(result))
"""
    )
    binary.chmod(0o755)
    return binary


def test_search_and_map_use_confined_repo_and_external_cache(
    tmp_path: Path, stub_repomap: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "code"
    repo = root / "project"
    repo.mkdir(parents=True)
    cache = tmp_path / "cache"
    monkeypatch.setenv("REPOMAP_CACHE_DIR", ".")
    backend = RepoMapCLI(binary=str(stub_repomap), cache_dir=cache)
    if os.name == "nt":
        monkeypatch.setattr(backend, "_command", lambda: [sys.executable, str(stub_repomap)])
    tools = RepoMapTools(root=root, backend=backend)

    result = tools.codemap_search("failed requests retry", "project", limit=500)
    assert result["results"][0] == {
        "file": "src/app.py",
        "reason": "matched retry",
        "snippet": "def retry():",
    }
    assert result["limit"] == 50
    assert result["args"][-2:] == ["--", "failed requests retry"]
    assert result["cwd"] == str(repo)
    assert result["cache"] == str(cache)
    assert result["repomap_cache"] == str(cache.resolve())
    assert not any(repo.iterdir())

    mapped = tools.codemap_map(str(repo), tokens=12)
    assert mapped["tokens"] == 100
    assert mapped["map"].startswith("src/app.py")


def test_repo_path_cannot_escape_code_root(tmp_path: Path, stub_repomap: Path) -> None:
    root = tmp_path / "code"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    tools = RepoMapTools(root=root, backend=RepoMapCLI(binary=str(stub_repomap)))

    with pytest.raises(ValueError, match="outside the allowed root"):
        tools.codemap_search("query", str(outside))


def test_cache_cannot_be_inside_code_root(tmp_path: Path, stub_repomap: Path) -> None:
    root = tmp_path / "code"
    root.mkdir()
    with pytest.raises(ValueError, match="cache directory must be outside"):
        RepoMapTools(root=root, backend=RepoMapCLI(binary=str(stub_repomap), cache_dir=root / ".cache"))


def test_option_like_query_is_rejected(tmp_path: Path) -> None:
    tools = RepoMapTools(root=tmp_path, cache_dir=tmp_path.parent / "cache")
    with pytest.raises(ValueError, match="must not start"):
        tools.codemap_search("--help", ".")


def test_external_symlink_is_flagged(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "secret.py"
    outside.write_text("secret")
    try:
        (repo / "linked.py").symlink_to(outside)
    except OSError as exc:
        if getattr(exc, "winerror", None) != 1314:
            raise
        pytest.skip("Windows test account lacks symlink privileges")
    backend = RepoMapCLI(cache_dir=tmp_path / "cache")

    cleaned = backend._sanitize({"file": "linked.py", "snippet": "x" * 1000}, repo)

    assert cleaned == {"file": "linked.py", "outside_code_root": True}


def test_missing_binary_has_installation_message(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    backend = RepoMapCLI(binary="repomap-definitely-not-installed")

    with pytest.raises(RepoMapError, match=r"not installed.*@sylphx/repomap@1\.9\.0"):
        backend.search("query", repo, 10)


def test_windows_cmd_shim_resolves_to_node_and_package_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim = tmp_path / "repomap.cmd"
    shim.touch()
    node = tmp_path / "node.exe"
    node.touch()
    package = tmp_path / "node_modules" / "@sylphx" / "repomap"
    (package / "bin").mkdir(parents=True)
    (package / "package.json").write_text('{"bin":{"repomap":"bin/repomap.js"}}')
    launcher = package / "bin" / "repomap.js"
    launcher.touch()

    monkeypatch.setattr(shutil, "which", lambda name: str(node if name in {"node", "node.exe"} else shim))

    assert RepoMapCLI()._command(windows=True) == [str(node), str(launcher)]


def test_error_detail_and_snippets_are_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    failing = tmp_path / "repomap"
    failing.write_text("#!/usr/bin/env python3\nimport sys\nsys.stderr.write('e' * 2000)\nsys.exit(2)\n")
    failing.chmod(0o755)
    repo = tmp_path / "repo"
    repo.mkdir()
    backend = RepoMapCLI(binary=str(failing), cache_dir=tmp_path / "cache")
    if os.name == "nt":
        monkeypatch.setattr(backend, "_command", lambda: [sys.executable, str(failing)])

    with pytest.raises(RepoMapError) as caught:
        backend.search("query", repo, 1)

    assert len(str(caught.value).partition(": ")[2]) == 500
    assert backend._sanitize({"snippet": "x" * 1000}, repo)["snippet"] == "x" * 400


@pytest.mark.skipif(os.name == "nt", reason="fixture uses a POSIX process group")
def test_inherited_output_pipe_reports_error_instead_of_hanging(tmp_path: Path) -> None:
    hanging = tmp_path / "repomap"
    hanging.write_text(
        "#!/usr/bin/env python3\n"
        "import subprocess\n"
        "subprocess.Popen(['sleep', '30'])\n"
        "print('{}')\n"
    )
    hanging.chmod(0o755)
    repo = tmp_path / "repo"
    repo.mkdir()
    backend = RepoMapCLI(binary=str(hanging), cache_dir=tmp_path / "cache")
    backend._READER_JOIN_TIMEOUT = 0.1

    with pytest.raises(RepoMapError, match="output reader did not finish"):
        backend.search("query", repo, 1)


def test_provider_is_on_read_only_mcp_surface(tmp_path: Path) -> None:
    providers = default_providers(["codemap"], data_source={"code_root": str(tmp_path)})
    assert set(expand_tools(providers, read_only=True)) == {"codemap_search", "codemap_map"}


def test_default_cache_moves_outside_code_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LAZYTOOLS_DATA_DIR", raising=False)

    providers = default_providers(["codemap"], data_source={"code_root": str(home)})

    assert len(providers) == 1
    cache = providers[0]._backend.cache_dir.resolve()
    assert cache != home.resolve()
    assert home.resolve() not in cache.parents


@pytest.mark.asyncio
async def test_server_lists_codemap_as_read_only(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    providers = default_providers(["codemap"], data_source={"code_root": str(tmp_path)})
    server = build_server(providers, read_only=True, name="codemap-test")
    async with connected_client(server) as client:
        listed = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(listed) == {"codemap_search", "codemap_map"}
    for tool in listed.values():
        annotations = tool.annotations
        assert protocol_field(annotations, "read_only_hint", "readOnlyHint") is True


@pytest.mark.skipif(shutil.which("repomap") is None, reason="repomap is not installed")
def test_installed_repomap_smoke(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "requests.py").write_text("def retry_request():\n    return True\n")
    (repo / "src" / "worker.py").write_text("from .requests import retry_request\n")
    (repo / "README.md").write_text("# RepoMap smoke fixture\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Smoke Test", "-c", "user.email=smoke@example.invalid",
         "commit", "-qm", "fixture"],
        check=True,
    )

    def repository_state() -> tuple[dict[Path, tuple[int, int]], set[Path], str]:
        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        ).stdout
        paths = {path.relative_to(repo) for path in repo.rglob("*")}
        files = {
            path.relative_to(repo): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in repo.rglob("*")
            if path.is_file()
        }
        return files, paths, status

    before = repository_state()
    tools = RepoMapTools(root=repo, cache_dir=tmp_path / "repomap-smoke-cache")
    search_result = tools.codemap_search("retry request", ".", limit=1)
    map_result = tools.codemap_map(".", tokens=100)
    after = repository_state()

    assert search_result is not None
    assert map_result is not None
    assert after == before
