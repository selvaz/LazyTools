"""Tests for the MCP-server variants (claude_code_mcp, codex_mcp).

These exercise the factory wiring — command, args, namespace, deny-by-default —
without spawning a real CLI. The underlying StdioTransport connects lazily, so
constructing the server never launches a subprocess; we inspect the transport
config directly.
"""

from __future__ import annotations

import pytest

from lazytools.connectors.cli_agents import claude_code_mcp, codex_mcp
from lazytools.connectors.mcp import MCPServer

# ─── claude_code_mcp ───────────────────────────────────────────────────────


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


# ─── codex_mcp ──────────────────────────────────────────────────────────


class TestCodexMcp:
    def test_returns_mcp_server(self):
        srv = codex_mcp(allow=["*"])
        assert isinstance(srv, MCPServer)

    def test_launches_codex_mcp_server(self):
        srv = codex_mcp(allow=["*"])
        t = srv._transport
        assert t._command == "codex"
        assert t._args == ["mcp-server"]

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
