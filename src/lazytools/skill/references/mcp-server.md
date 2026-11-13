# MCP server

Install the MCP extra from GitHub (add `engines` for Claude-backed coding and tier routing):

```sh
python -m pip install "lazytoolkit[engines,mcp] @ git+https://github.com/selvaz/LazyTools.git"
```

Register the read-only server in Claude Code:

```sh
claude mcp add --transport stdio lazytools -- lazytools-mcp
```

For Codex, add to `~/.codex/config.toml`:

```toml
[mcp_servers.lazytools]
command = "lazytools-mcp"
```

Restart the host to pick up configuration changes. The executable must be on the host's PATH. With no provider arguments, available read-only providers are loaded; unavailable optional providers are skipped. To select a subset, pass provider IDs as positional arguments, e.g. `lazytools-mcp projects`. Core projects and registry need no additional connector; other providers may require their own extras or dependencies.

The default server is read-only. Project mutations and explicitly selected coding providers require `--allow-unsafe`; this enables writes without an MCP confirmation gate. Choose only needed providers and trust the workspace. For persistent project state set `LAZYTOOLS_PROJECTS_STORE_DB` to a SQLite path in the host environment, or pass `--config` pointing to JSON containing `projects_store_db`; otherwise project state is in memory. Set `LAZYTOOLS_CODE_ROOT` or config `code_root` to the intended workspace. Claude Bash is not path-confined. For long coding jobs use the [bridge](code-bridge.md); MCP coding calls are synchronous.
