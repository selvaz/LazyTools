# CodeMap — optional Python-native RepoMap adapter (experimental)

The lazytools.codemap package exposes [SylphxAI/repomap](https://github.com/SylphxAI/repomap) as ordinary LazyBridge tools. It does not fork the engine, install Node or Rust, register MCP tools, or change repository source files. There are no new mandatory LazyTools dependencies. Credit and third-party MIT license: [SylphxAI/repomap LICENSE](https://github.com/SylphxAI/repomap/blob/main/LICENSE). LazyTools remains Apache-2.0.

## Explicit engine installation

On the machine where the agent executes, install a pinned version of the optional local binary:

~~~shell
npm install --global @sylphx/repomap@1.9.0
repomap version
~~~

Node.js 18+ launches repomap's native binary. You can also install the upstream standalone binary. Neither is downloaded by pip install lazytoolkit. Semantic search may download an approximately 33 MB embedding model on first use. Set use_embeddings=False to avoid that network request and use keyword-only mode. The CLI maintains its own cache outside the source repository.

## Python-native usage

~~~python
from lazybridge import Agent, LLMEngine
from lazytools.codemap import RepoMapTools

codemap = RepoMapTools({
    "LazyBridge": "/work/LazyBridge",
    "LazyTools": "/work/LazyTools",
}, use_embeddings=True)

agent = Agent(
    name="code_researcher",
    engine=LLMEngine("your-model"),
    tools=[codemap],
)
~~~

An agent receives exactly four tools:

| Tool | Purpose |
| --- | --- |
| codemap_overview(repo="", tokens=1200) | Summarize modules and important files/symbols within an approximate token budget |
| codemap_search(query, repo="", limit=10) | Find symbols and code by a description or name |
| codemap_context(target, repo="", code_lines=40) | Understand a symbol or file, its callers, imports and tests |
| codemap_impact(target, repo="", depth=2) | Estimate the downstream impact of a changed symbol/file |

The repo value must be one of the operator-configured aliases, e.g. "LazyBridge". Empty repo means all configured repositories via the upstream --workspace argument. Agents cannot add arbitrary filesystem roots. Targets may be repo-relative paths or symbol names; absolute paths and parent-directory traversal are rejected. Responses return compact CLI text with a configurable character limit and explicit truncation markers.

A caller without an agent can use RepoMapCLI(roots).search("...") directly from Python. No MCP server or mirroring is included yet: MCP can wrap the native tools later.

## Safety and operational limits

The adapter invokes a local third-party executable with fixed arguments, no shell, explicit timeouts, and a restricted environment that does not inherit unrelated API credentials. The CLI reads configured code trees and may write its own indexing cache. Its graph and impact suggestions are static-analysis estimates: validate with actual tests before changing source code. The CLI only performs local analysis; the optional model download is the one network access described by the vendor. Pin and inspect the binary for sensitive environments.

This is intentionally a small, opt-in, read-only prototype. No shared project database is introduced, no source files are rewritten by the tool, and no automatic invocation is added to LazyCEO or other agents.

For the proposed reusable, public end-to-end review/refresh/maintenance pipeline, see [Repository Maintenance Pipeline — specification](repo-maintenance-pipeline-spec.md). The spec explicitly distinguishes current capabilities from planned features.

The existing head-to-head benchmark measures search relevance and index timings, not actual token or wall-time savings for full coding agents. Before wider deployment, run paired real coding tasks against the same repository state, with and without this provider, and compare prompt tokens (including cache), wall time, final tests, and agent tool-call counts.


## RepoMap directly available to Claude Code and Codex writers

RepoMap is not limited to the outer coordinator's summary. The existing
`CodeWriteTools` provider now accepts `application_tools=`, using the standard
LazyBridge application-tool interface. This exposes the actual
`codemap_overview`, `codemap_search`, `codemap_context` and `codemap_impact`
operations **during** a native Claude Code / Codex coding turn.

~~~python
from lazytools.codemap import RepoMapTools
from lazytools.connectors.code_support import CodeWriteTools

repo = "/trusted/temporary/checkout"
mapping = RepoMapTools({"project": repo}, use_embeddings=False)
writer = CodeWriteTools(
    base_dir=repo,
    claude=True, codex=True,
    claude_bash=False,
    application_tools=mapping.as_tools(),
)
# Explicit one-shot write approval is still required by default.
# Select writer.as_tools() and call confirm_write() when authorized.
~~~

These tools are read-only Python callbacks executed by LazyBridge. The
existing engines expose them through Claude Code's in-process MCP bridge
and Codex's App Server dynamic-tool channel. No new LLM wrapper is needed.
For the bounded multi-repository improvement runner, the target root maps
to a disposable clone and other explicitly configured workspace roots
are available for read-only lookup. Neither setup grants extra write paths.

For benchmarking the *incremental value of writer access*, use
[PairedMaintenanceBenchmark](maintenance.md#prepared-paired-benchmark-repomap-available-to-writer-vs-unavailable):
the **same RepoMap-enabled coordinator** supplies identical initial context
to both arms; only the mapped writer receives codemap_* tools. Native
registration is covered by CI mocks, not yet by a live authenticated
Claude/Codex head-to-head run.
