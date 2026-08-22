"""Tests for the MCP-mode code-support factories (claude_code_mcp, codex_mcp).

These exercise the factory wiring — command, args, namespace, deny-by-default —
without spawning a real CLI. The underlying StdioTransport connects lazily, so
constructing the server never launches a subprocess; we inspect the transport
config directly.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from lazytools.connectors.code_support import claude_code_mcp, codex_mcp
from lazytools.connectors.mcp import MCPServer

# ─── claude_code_mcp ─────────────────────────────────────────────────


class TestClaudeCodeMcp:
    def test_returns_mcp_server(self):
        srv = claude_code_mcp(allow=["*"])
        assert isinstance(srv, MCPServer)
        assert srv._is_lazy_tool_provider is True

    def test_launches_claude_mcp_serve(self):
        srv = claude_code_mcp(allow=["*"])
        t = srv._transport
        assert t._command == "claude"
        assert t._args == ["mcp", "serve"]

    def test_extra_args_appended_after_serve(self):
        srv = claude_code_mcp(allow=["*"], args=["--debug"])
        assert srv._transport._args == ["mcp", "serve", "--debug"]

    def test_default_namespace_prefix(self):
        srv = claude_code_mcp(allow=["*"])
        assert srv._prefix == "claude_code."

    def test_requires_allow_or_deny(self):
        # Deny-by-default: omitting both must raise (same as MCP.stdio).
        with pytest.raises(ValueError, match="allow"):
            claude_code_mcp()

    def test_deny_alone_satisfies_filter_requirement(self):
        srv = claude_code_mcp(deny=["claude_code.Bash"])
        assert srv._deny == ["claude_code.Bash"]

    def test_env_forwarded_to_transport(self):
        srv = claude_code_mcp(allow=["*"], env={"FOO": "bar"})
        assert srv._transport._env == {"FOO": "bar"}


# ─── codex_mcp ───────────────────────────────────────────────


class TestCodexMcp:
    def test_returns_mcp_server(self):
        srv = codex_mcp(allow=["*"])
        assert isinstance(srv, MCPServer)

    def test_launches_codex_mcp_server(self):
        # command is resolve_codex_bin() (falls back to the bare "codex"
        # literal only when nothing resolves) — mocked so this doesn't
        # depend on whether Codex is actually installed on this machine.
        with patch(
            "lazytools.connectors.code_support._codex.resolve_codex_bin",
            return_value="/resolved/codex",
        ):
            srv = codex_mcp(allow=["*"])
        t = srv._transport
        assert t._command == "/resolved/codex"
        assert t._args == ["mcp-server"]

    def test_falls_back_to_bare_codex_when_unresolved(self):
        with patch("lazytools.connectors.code_support._codex.resolve_codex_bin", return_value=None):
            srv = codex_mcp(allow=["*"])
        assert srv._transport._command == "codex"

    def test_env_path_override_is_searched_explicitly(self):
        # A caller passing env={"PATH": ...} is explicitly selecting which
        # Codex install the child subprocess should see. On Windows,
        # CreateProcess resolves a bare command name against the *calling*
        # process's own PATH, not the child env's — verified live
        # (subprocess.run(["foo"], env={"PATH": <dir containing only
        # foo.cmd>}) raises FileNotFoundError) — so leaving a bare "codex"
        # for the child to resolve, or resolving via resolve_codex_bin()
        # (which reads *this* process's environment), would both silently
        # bypass the override. The custom path must be searched explicitly.
        with (
            patch(
                "lazytools.connectors.code_support._codex.resolve_codex_bin",
                return_value="/parent/env/codex",  # must NOT be used
            ),
            patch("shutil.which", return_value="/custom/codex/dir/codex.exe") as mock_which,
        ):
            srv = codex_mcp(allow=["*"], env={"PATH": "/custom/codex/dir"})
        mock_which.assert_called_once_with("codex", path="/custom/codex/dir")
        # Normalized through os.path.abspath() (a relative shutil.which()
        # result must not silently depend on the cwd at first tool call) —
        # compare against the same normalization rather than a raw string,
        # so the assertion doesn't hardcode a slash convention.
        assert srv._transport._command == os.path.abspath("/custom/codex/dir/codex.exe")
        assert srv._transport._env == {"PATH": "/custom/codex/dir"}

    def test_env_path_key_is_case_insensitive_on_windows_only(self):
        # "Path" is the common spelling of the PATH key on Windows, where env
        # var names are themselves case-insensitive — mocked os.name so this
        # is deterministic regardless of which platform actually runs pytest.
        with (
            patch("os.name", "nt"),
            patch("lazytools.connectors.code_support._codex.resolve_codex_bin", return_value="/parent/env/codex"),
            patch("shutil.which", return_value="/custom/codex/dir/codex.exe") as mock_which,
        ):
            srv = codex_mcp(allow=["*"], env={"Path": "/custom/codex/dir"})
        mock_which.assert_called_once_with("codex", path="/custom/codex/dir")
        # Normalized through os.path.abspath() (a relative shutil.which()
        # result must not silently depend on the cwd at first tool call) —
        # compare against the same normalization rather than a raw string,
        # so the assertion doesn't hardcode a slash convention.
        assert srv._transport._command == os.path.abspath("/custom/codex/dir/codex.exe")

    def test_env_path_key_is_case_sensitive_on_posix(self):
        # On POSIX, env var names ARE case-sensitive, so an unrelated "Path"
        # entry must not be misread as a PATH override — it should fall
        # through to resolve_codex_bin() untouched.
        with (
            patch("os.name", "posix"),
            patch("lazytools.connectors.code_support._codex.resolve_codex_bin", return_value="/resolved/codex"),
            patch("shutil.which") as mock_which,
        ):
            srv = codex_mcp(allow=["*"], env={"Path": "/custom/codex/dir"})
        mock_which.assert_not_called()
        assert srv._transport._command == "/resolved/codex"

    def test_env_path_override_raises_when_not_found(self):
        # The override is honored (searched, not ignored) but finds nothing
        # in that specific directory. Falling back to the bare "codex"
        # literal here would let Windows CreateProcess silently resolve a
        # *different* install from this process's real PATH instead of
        # respecting the caller's explicit selection — so this fails loudly.
        with (
            patch("lazytools.connectors.code_support._codex.resolve_codex_bin", return_value="/parent/env/codex"),
            patch("shutil.which", return_value=None),
            pytest.raises(FileNotFoundError, match="/empty/dir"),
        ):
            codex_mcp(allow=["*"], env={"PATH": "/empty/dir"})

    def test_extra_args_appended(self):
        srv = codex_mcp(allow=["*"], args=["--foo"])
        assert srv._transport._args == ["mcp-server", "--foo"]

    def test_default_namespace_prefix(self):
        srv = codex_mcp(allow=["*"])
        assert srv._prefix == "codex."

    def test_requires_allow_or_deny(self):
        with pytest.raises(ValueError, match="allow"):
            codex_mcp()

    def test_custom_name_overrides_prefix(self):
        srv = codex_mcp(name="cdx", allow=["*"])
        assert srv.name == "cdx"
        assert srv._prefix == "cdx."

    def test_namespace_off_keeps_raw_names(self):
        srv = codex_mcp(allow=["*"], namespace=False)
        assert srv._prefix == ""
