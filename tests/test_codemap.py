"""Python-only contract tests; no installed repomap or MCP needed."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from lazytools.codemap import RepoMapCLI, RepoMapError, RepoMapTools


@pytest.fixture
def roots(tmp_path: Path) -> dict[str, Path]:
    one = tmp_path / "LazyBridge"
    two = tmp_path / "LazyTools"
    one.mkdir()
    two.mkdir()
    return {"LazyBridge": one, "LazyTools": two}


@pytest.fixture
def fake_bin(tmp_path: Path) -> Path:
    script = tmp_path / "fake-repomap"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys, time\n"
        "args = sys.argv[1:]\n"
        "if args[0] == 'impact' and args[1] == 'bad':\n"
        "    print('Failed inside ' + str(pathlib.Path.cwd()), file=sys.stderr)\n"
        "    sys.exit(4)\n"
        "if args[0] == 'context' and args[1] == 'long':\n"
        "    print('A' * 2000)\n"
        "    sys.exit(0)\n"
        "if args[0] == 'context' and args[1] == 'sleep':\n"
        "    time.sleep(.3)\n"
        "print(json.dumps({'args': args, 'cwd': str(pathlib.Path.cwd()),\n"
        "                  'private': os.getenv('MY_PRIVATE_API_TOKEN'),\n"
        "                  'embed': os.getenv('REPOMAP_EMBED')}))\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_provider_has_four_python_tools_without_mcp(roots, fake_bin):
    provider = RepoMapTools(roots, executable=str(fake_bin), use_embeddings=False)
    assert provider._is_lazy_tool_provider
    tools = {tool.name: tool for tool in provider.as_tools()}
    assert set(tools) == {
        "codemap_overview", "codemap_search", "codemap_context", "codemap_impact",
    }
    result = json.loads(tools["codemap_search"].run_sync(query="Memory", repo="LazyTools"))
    assert result["args"] == ["search", "Memory", "--limit", "10"]
    assert result["cwd"] == str(roots["LazyTools"])
    assert result["embed"] == "0"


def test_multi_repo_workspace_and_single_repo_scopes(roots, fake_bin):
    backend = RepoMapCLI(roots, executable=str(fake_bin))
    workspace = f"{roots['LazyBridge']},{roots['LazyTools']}"
    m = json.loads(backend.overview(tokens=900))
    assert m["args"] == ["map", "--tokens", "900", "--workspace", workspace]
    assert m["cwd"] == str(roots["LazyBridge"])
    search = json.loads(backend.search("agent delegation", limit=4))
    assert search["args"] == [
        "search", "agent delegation", "--limit", "4", "--workspace", workspace,
    ]
    context = json.loads(backend.context("lazybridge/agent.py:42", "LazyBridge", code_lines=12))
    assert context["args"] == ["context", "lazybridge/agent.py:42", "--code-lines", "12"]
    impact = json.loads(backend.impact("Agent", depth=3))
    assert impact["args"] == ["impact", "Agent", "--depth", "3", "--workspace", workspace]


def test_refuses_unknown_roots_traversal_and_cli_flag_injection(roots, fake_bin):
    backend = RepoMapCLI(roots, executable=str(fake_bin))
    with pytest.raises(ValueError, match="Unknown repository"):
        backend.search("abc", repo="AnotherRepo")
    with pytest.raises(ValueError, match="Parent-directory"):
        backend.context("../.ssh/id_rsa")
    with pytest.raises(ValueError, match="repo-relative"):
        backend.context("/etc/passwd")
    with pytest.raises(ValueError, match="repo-relative"):
        backend.context("C:\\secrets\\api.txt")
    with pytest.raises(ValueError, match="Unsafe"):
        backend.search("--workspace /secrets")
    with pytest.raises(ValueError, match="between"):
        backend.search("x", limit=30)
    with pytest.raises(ValueError, match="between"):
        backend.overview(tokens=10000)


def test_child_process_does_not_inherit_agent_secrets(roots, fake_bin, monkeypatch):
    monkeypatch.setenv("MY_PRIVATE_API_TOKEN", "very-secret-value")
    result = json.loads(RepoMapCLI(roots, executable=str(fake_bin)).search("Code"))
    assert result["private"] is None
    assert result["embed"] is None


def test_missing_binary_and_errors_are_actionable(roots, fake_bin):
    backend = RepoMapCLI(roots, executable=str(fake_bin))
    with pytest.raises(RepoMapError, match=r"repomap impact failed \(4\)") as error:
        backend.impact("bad", repo="LazyTools")
    assert str(roots["LazyTools"]) not in str(error.value)
    assert "[LazyTools]" in str(error.value)
    with pytest.raises(RepoMapError, match="not installed"):
        RepoMapCLI(roots, executable="nonexistent_repomap_command_xx").search("hello")


def test_output_caps_and_timeout(roots, fake_bin):
    backend = RepoMapCLI(roots, executable=str(fake_bin), max_result_chars=500)
    result = backend.context("long")
    assert result.startswith("A" * 500)
    assert "[TRUNCATED: 2000 characters originally" in result
    quick = RepoMapCLI(roots, executable=str(fake_bin), timeout_seconds=0.02)
    with pytest.raises(RepoMapError, match="timed out"):
        quick.context("sleep")


def test_root_configuration(roots, tmp_path):
    with pytest.raises(ValueError, match="at least one"):
        RepoMapCLI({})
    with pytest.raises(ValueError, match="Invalid repository alias"):
        RepoMapCLI({"../bad": tmp_path})
    with pytest.raises(FileNotFoundError):
        RepoMapCLI({"missing": tmp_path / "not-found"})
    with pytest.raises(ValueError, match="timeout_seconds"):
        RepoMapCLI(roots, timeout_seconds=0)
