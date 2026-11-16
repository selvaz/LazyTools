# Studio MCP provider report

## Completed

- Added the `studio` MCP provider and registered it in `PROVIDER_FACTORIES`.
- Implemented bearer-authenticated discovery, published-tool calls, and run polling against the fixed LazyStudio API.
- Kept server startup resilient when Studio or its operator token is unavailable.
- Applied the existing provider-level unsafe gate to agent execution while leaving run status available.
- Added fake-HTTP-server coverage and documented Claude Code and Codex setup.

## Validation

- `python -m pytest -q tests/test_studio_provider.py tests/test_mcp_server.py`
- `ruff check src/lazytools/mcp_server/studio.py src/lazytools/mcp_server/providers.py tests/test_studio_provider.py`
