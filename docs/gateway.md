# External tool gateway

> **Ships in `lazytoolkit`.** Install: `pip install "lazytoolkit @ git+https://github.com/selvaz/LazyTools.git"`. Part of the
> [LazyTools capabilities](index.md) package, not the core runtime.

`lazytools.connectors.gateway` integrates remote tool catalogues hosted as
HTTP services — Pipedream, Composio, Arcade, your team's internal
gateway, or any service that publishes tools over JSON. Implement the
`ExternalToolClient` protocol once and every tool the gateway exposes
becomes available to a LazyBridge agent through the standard
`tools=[...]` surface.

This is the "remote registry" counterpart to MCP: where MCP runs over
stdio or Streamable HTTP with the protocol-spec wire format, the
gateway is plain JSON HTTP, easier to stand up behind an existing
service.

## Signature

```python
from lazytools.connectors.gateway import (
    ExternalToolSpec,
    ExternalToolError,
    ExternalToolClient,         # Protocol
    JsonHttpExternalToolClient, # default HTTP impl
    ExternalToolProvider,       # ToolProvider — drop into Agent(tools=[...])
)


# 1. Tool spec — the shape every external tool advertises.
ExternalToolSpec(
    name,                          # str — tool identifier
    description,                   # str — LLM-facing
    parameters,                    # JSON Schema (dict)
    strict=False,                  # provider-strict mode opt-in
)


# 2. Client protocol — implement for non-default registries.
class ExternalToolClient(Protocol):
    def list_tools(self) -> Iterable[ExternalToolSpec | Mapping]:  ...
    def call_tool(self, name: str, arguments: Mapping) -> Any:     ...
    # Optional: async def acall_tool(...) — used preferentially when present


# 3. Default HTTP client — works with any gateway following the
#    contract:
#      GET  {base_url}/tools                  → [{...}] or {"tools": [{...}]}
#      POST {base_url}/tools/{name}/call      with {"arguments": {...}}
JsonHttpExternalToolClient(
    base_url,                      # required
    *,
    api_key=None,                  # bearer token (auto Authorization header; https-only)
    headers=None,                  # custom headers (merged with Authorization)
    timeout=30.0,                  # per-request HTTP timeout
    tools_path="/tools",           # override registry endpoint
    call_path_template="/tools/{name}/call",  # override execution endpoint
)


# 4. Tool provider — drops into Agent(tools=[...]).
ExternalToolProvider(
    client,                        # any ExternalToolClient
    *,
    specs=None,                    # Iterable[ExternalToolSpec] — pre-fetched, skips list_tools()
    include=None,                  # Iterable[str] — allowlist tool names
    exclude=None,                  # Iterable[str] — blocklist tool names
    name_prefix="",                # str — prepend to tool names (e.g. "ext.")
    strict=None,                   # bool — override per-spec strict (None = honour spec)
)
```

`ExternalToolError` (subclasses `RuntimeError`) is raised when the
registry or execution call fails. Carries `status` (HTTP code) and
`body` (parsed response payload, when JSON).

## Parameters

### `ExternalToolProvider`

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `client` | `ExternalToolClient` | — | Any object implementing `list_tools()` + `call_tool()` (optional async `acall_tool()`). |
| `specs` | `Iterable[ExternalToolSpec \| Mapping] \| None` | `None` | Pre-fetched specs; when set, skips the `list_tools()` round-trip. |
| `include` | `Iterable[str] \| None` | `None` | Allow-list of **original** tool names (exact match, not glob). |
| `exclude` | `Iterable[str]` | `()` | Deny-list of original tool names (exact match). |
| `name_prefix` | `str` | `""` | Prepended to each tool name (e.g. `"ext."`). Applied *after* include/exclude. |
| `strict` | `bool \| None` | `None` | `None` honours each spec's own `strict`; `True`/`False` overrides all. |

### `JsonHttpExternalToolClient`

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `base_url` | `str` | — | Gateway base URL (trailing slash trimmed). |
| `api_key` | `str \| None` | `None` | Bearer token; auto-sets `Authorization` unless you already provided it. Requires an `https://` base URL (plain `http://` is allowed only for localhost). |
| `headers` | `Mapping[str, str] \| None` | `None` | Extra headers, merged with `Authorization`/`Accept`. |
| `timeout` | `float` | `30.0` | Per-request HTTP timeout (seconds). |
| `tools_path` | `str` | `"/tools"` | Registry endpoint (GET). |
| `call_path_template` | `str` | `"/tools/{name}/call"` | Execution endpoint (POST); `{name}` is URL-escaped. |

### `ExternalToolSpec`

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | `str` | — | Tool identifier (non-empty). |
| `description` | `str` | — | LLM-facing description (defaults to `"Call external tool <name>."`). |
| `parameters` | `Mapping` | `{}`-object schema | Provider-agnostic JSON Schema object. |
| `strict` | `bool` | `False` | Provider-strict mode opt-in. |

`ExternalToolSpec.from_mapping(raw)` accepts both `{"name", "description",
"parameters"}` and OpenAI-style `{"function": {...}}` shapes.

## Synopsis

The gateway turns an HTTP-hosted tool catalogue into LazyBridge
tools. The flow:

1. **Implement `ExternalToolClient`** — or use the default
   `JsonHttpExternalToolClient` if your gateway follows the
   GET `/tools` + POST `/tools/{name}/call` contract.
2. **Wrap in `ExternalToolProvider`** — supports allow/deny lists, a
   name prefix to namespace remote tools, and a `strict` override.
3. **Pass to `Agent(tools=[provider])`** — the framework calls
   `provider.as_tools()` to expand into one `Tool` per remote tool,
   each backed by `Tool.from_schema(...)` since the JSON Schema is
   already published by the gateway.

The integration sits at the **tool boundary**, same as MCP: once
the provider is in `tools=[...]`, the agent treats remote tools
exactly like local Python functions — parallel calls, structured
arguments, cost tracking, session events.

## When to use it

- **Existing HTTP-based tool registry** — you already host an
  internal gateway exposing tools over JSON. The default
  `JsonHttpExternalToolClient` requires no SDK; stdlib only.
- **Pipedream / Composio / Arcade integration** — implement
  `ExternalToolClient` once with their SDK, then every tool they
  expose is available without writing per-tool wrappers.
- **You want allow / deny / prefix** — `ExternalToolProvider`
  lets you scope the surface (e.g. expose only `query_*` tools,
  prefix all with `pipedream.`).
- **Pre-fetched specs** — pass `specs=[...]` to skip the
  `list_tools()` round-trip; useful for static catalogues or for
  injecting test doubles.

## When NOT to use it

- **MCP server already exists** — use [MCP](mcp.md) instead;
  it's the protocol-spec equivalent and has wider tool ecosystem
  support (filesystem, GitHub, Postgres, …).
- **Tools live in your own Python code** — wrap them as plain
  callables; the gateway is only useful for *remote* registries.
- **You need streaming results from a remote tool** — the gateway
  is request/response; for streaming, implement a custom
  `ExternalToolClient` whose `call_tool` returns an async iterator
  and adapt the framework callsite.

## Example

```python
from lazybridge import Agent, LLMEngine
from lazytools.connectors.gateway import (
    ExternalToolProvider,
    JsonHttpExternalToolClient,
)


# 1. Default HTTP client — works for any gateway following the
#    GET /tools, POST /tools/{name}/call contract.
client = JsonHttpExternalToolClient(
    base_url="https://tools.internal.example.com",
    api_key="secret-bearer-token",
    timeout=30.0,
)


# 2. Wrap in a provider with allow/deny + namespace prefix.
gateway = ExternalToolProvider(
    client,
    include=["search", "fetch", "summarise"],   # exact names — allowlist wins
    exclude=["delete_user", "drop_table"],       # exact names; not glob
    name_prefix="ext.",
)


# 3. Pass into Agent — the provider expands into one Tool per remote
#    spec at construction time.
agent = Agent(
    engine=LLMEngine("gpt-5.4-mini"),
    tools=[gateway],
)
result = agent("Find recent papers on bee colony decline")
print(result.text())


# 4. Custom client — implement the protocol for SDK-backed registries.
from collections.abc import Iterable, Mapping
from typing import Any

from lazytools.connectors.gateway import ExternalToolClient, ExternalToolSpec


class PipedreamClient:
    """Adapter for Pipedream's SDK to the LazyBridge protocol."""

    _is_pipedream_client = True   # marker, not required by protocol

    def __init__(self, sdk_client) -> None:
        self._sdk = sdk_client

    def list_tools(self) -> Iterable[ExternalToolSpec]:
        for raw in self._sdk.tools.list():
            yield ExternalToolSpec(
                name=raw["slug"],
                description=raw["description"],
                parameters=raw["jsonSchema"],
            )

    def call_tool(self, name: str, arguments: Mapping) -> Any:
        return self._sdk.tools.run(slug=name, args=dict(arguments))


provider = ExternalToolProvider(client=PipedreamClient(my_sdk))
```

## Security & safety

- **Secrets stay server-side.** The gateway's API key lives on the *client*, not
  on individual tools. LazyTools never sees or forwards the credential to the LLM.
- **Same-origin redirects only.** `JsonHttpExternalToolClient` refuses a redirect
  to a different host and refuses an `https→http` downgrade, so the
  `Authorization` header can't be leaked to a 302 target. Same-host scheme
  *upgrades* (http→https) remain allowed.
- **No result sanitisation.** LazyTools passes each tool's JSON response through
  **unmodified** — stripping secrets/PII/internal identifiers is the *remote*
  gateway's job, done server-side before responding.
- **Scope the surface.** Use `include` / `exclude` / `name_prefix` (and
  `specs=[...]`) to expose only the safe subset of a large catalogue.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ExternalToolError` with `status=4xx/5xx` | Gateway returned an HTTP error | Inspect `err.status` / `err.body`; both error classes surface here |
| `ExternalToolError: … invalid JSON` | Non-JSON response body | Ensure the endpoint returns JSON; check `Accept` handling |
| `ExternalToolError: refusing redirect to different host` | Gateway 302'd to another host | Point `base_url` at the final host; same-origin redirects only |
| `ExternalToolError: registry must return a list or {'tools': list}` | `/tools` returned an unexpected shape | Return `[{...}]` or `{"tools": [{...}]}` |
| `ValueError: must include a non-empty string name` | A spec lacked a valid `name` | Fix the registry payload, or build `ExternalToolSpec` explicitly |
| `exclude=["delete_*"]` didn't block `delete_user` | Filters are exact-name, not globs | List exact names, or pre-filter via `specs=` |

## Pitfalls

- **`include` / `exclude` are exact-name sets, not globs.**
  `exclude=["delete_*"]` does **not** block `delete_user` — it only
  filters a tool literally named `delete_*`. List the names you want to
  block (`exclude=["delete_user", "drop_table"]`), or pre-filter the
  catalogue with `specs=[...]` and pass only the safe subset.
- **`include` / `exclude` match the *original* tool name** (before
  `name_prefix` is applied). Allowlist `["search"]` not
  `["ext.search"]`.
- **`strict` precedence**: `ExternalToolProvider(strict=True)`
  overrides every spec's `strict` value; `strict=None` (default)
  honours each spec's own `strict` field.
- **HTTP errors → `ExternalToolError`**, never raw `urllib`
  exceptions. The error carries `status` and `body` so a caller
  can inspect the response. `4xx` and `5xx` both surface here;
  catch the base class to handle both.
- **Pre-fetched `specs=`** skips the `list_tools()` round-trip
  on every agent invocation, but the registry can drift — flush
  the cached specs out-of-band when the upstream catalogue
  changes.
- **`call_tool` is sync by default**; if your client implements
  `async def acall_tool(...)`, the framework prefers it. Use the
  async path when the SDK supports it to avoid pinning a worker
  thread per call.
- **Credentials.** The gateway's API key is held by the
  *client*, not by individual tools — secrets stay server-side.
  **LazyTools passes each tool's JSON response through to the agent
  unmodified — it does not sanitise, redact, or filter results.**
  Sanitising results (stripping secrets, PII, internal identifiers)
  is the remote gateway's job; do it server-side before responding.
- **Status: alpha.** This module's API may evolve between minor
  releases. Pin a version and read the CHANGELOG before
  upgrading.

## See also

- [MCP](mcp.md) — protocol-spec equivalent for tool
  catalogue integration; richer ecosystem (filesystem, GitHub,
  Postgres, …) but requires the MCP wire format.
- [Tool](https://core.lazybridge.com/guides/basic/tool/) — `Tool.from_schema(...)` is what backs
  every external-tool spec internally.
- [BaseProvider](https://core.lazybridge.com/guides/advanced/base-provider/) — different extension surface;
  for adding new LLM backends, not new tool catalogues.
