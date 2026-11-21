from __future__ import annotations

import shutil
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
              "query": args[1], "limit": int(args[args.index("--limit") + 1])}
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
    tools = RepoMapTools(root=root, backend=RepoMapCLI(binary=str(stub_repomap), cache_dir=cache))

    result = tools.codemap_search("failed requests retry", "project", limit=500)
    assert result["results"][0] == {
        "file": "src/app.py",
        "reason": "matched retry",
        "snippet": "def retry():",
    }
    assert result["limit"] == 50
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


def test_missing_binary_has_installation_message(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    backend = RepoMapCLI(binary="repomap-definitely-not-installed")

    with pytest.raises(RepoMapError, match=r"not installed.*@sylphx/repomap@1\.9\.0"):
        backend.search("query", repo, 10)


def test_provider_is_on_read_only_mcp_surface(tmp_path: Path) -> None:
    providers = default_providers(["codemap"], data_source={"code_root": str(tmp_path)})
    assert set(expand_tools(providers, read_only=True)) == {"codemap_search", "codemap_map"}


@pytest.mark.anyio
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
    (tmp_path / "example.py").write_text("def retry_request():\n    pass\n")
    result = RepoMapCLI(cache_dir=tmp_path.parent / "repomap-smoke-cache").search("retry request", tmp_path, 1)
    assert result is not None
