# Workspace

`lazytools.workspace.WorkspaceTools` gives **any** engine — not just the
Claude Code CLI — the same kind of file and shell access a coding agent
expects: `Read`, `Write`, `Edit`, confined to a set of directories you choose,
plus an opt-in, unconfined `Bash`. `Read`/`Write`/`Edit` were previously only
native to the Claude Code CLI; `WorkspaceTools` is a plain LazyBridge tool
provider, so an `LLMEngine`-backed agent (DeepSeek, GPT, Grok, …) can have the
same capability.

!!! info "Ships in the core package"
    No extra needed — `pip install "lazytoolkit @ git+https://github.com/selvaz/LazyTools.git"`.
    ```python
    from lazytools.workspace import WorkspaceTools
    ```
    Not re-exported from top-level `lazytools`, and not mounted in the
    `lazytools-mcp` server — construct it directly and drop it into
    `Agent(tools=[...])` or `PulseAgent(tools=[...])`.

## The model in one breath

`Read`, `Write`, and `Edit` are confined to the configured `file_roots` —
confinement is enforced structurally at the moment of each operation (POSIX:
walking open, `O_NOFOLLOW` directory descriptors component-by-component;
Windows: verifying the already-opened handle's kernel-resolved path), never
by trusting a path string checked once at construction time or matched
against an argument pattern. `Bash` is opt-in (`enable_bash=True`) and is
**deliberately not path-confined** — once a command runs, it has the same
filesystem and process access as the host process. `WorkspaceTools` adds
confinement and resource bounds; it is not a second authorization layer.
Authorization is the engine's own approval gate (e.g. LazyBridge's
`TieredGate`/`approval_gate`) — gate dangerous calls there, not here.

## Signature

```python
from lazytools.workspace import WorkspaceTools

WorkspaceTools(
    file_roots: list[str | Path],   # Read/Write/Edit confinement; each must already exist and be a directory
    cwd: str | Path,                # base for relative file_path args, and Bash's working directory
    enable_bash: bool = False,      # exposes Bash when True; omitted entirely otherwise
    max_read_bytes: int = 1_000_000,    # Read: bytes captured before truncation (must be > 0)
    max_output_bytes: int = 1_000_000,  # Bash: combined stdout+stderr bytes captured (must be > 0)
)
tools.as_tools() -> list[Tool]   # 3 tools (Read, Write, Edit), or 4 with enable_bash=True
```

Each `file_root` is resolved with `Path.resolve(strict=True)` at construction
time — a root that doesn't exist, or isn't a directory, raises `ValueError`
immediately rather than failing later on first use.

## Read / Write / Edit

- **`Read(file_path, offset=None, limit=None)`** — UTF-8 text only (invalid
  UTF-8 raises `ValueError`). `offset` is the zero-based starting line;
  `limit` is the maximum number of lines returned; `limit=0` returns `""`.
  Output beyond `max_read_bytes` is cut and a
  `"\n[Read truncated after N bytes]"` notice is appended.
- **`Write(file_path, content)`** — writes to a same-directory temp file,
  `fsync`s the temp file's contents, then `os.replace()`s over the target,
  so the path always contains either the complete old content or the
  complete new content — a reader never observes a partially-written
  target, and a write interrupted before that final replace leaves the
  prior state (the old file, or nothing, if it's new) untouched. This is
  atomic *visibility*, not crash durability: the containing directory
  entry itself isn't `fsync`'d, so it's not a guarantee against data loss
  across an OS crash or power failure. Preserves the mode sampled from the
  target just before the temp file is staged and written — not
  atomically re-sampled at the moment of replacement, so a concurrent
  `chmod` in that window can still race it. The **direct parent must
  already exist** — parent directories are never created.
- **`Edit(file_path, old_string, new_string, replace_all=False)`** —
  `old_string` must match **exactly once** unless `replace_all=True`; zero
  matches or an empty `old_string` raises `ValueError` (file untouched),
  and more than one match without `replace_all` also raises rather than
  guessing which occurrence was meant. Goes through the same temp-file +
  `os.replace()` write, and does one `os.path.samestat` check *before*
  staging/writing the replacement temp file (not at the final
  `os.replace()` itself) confirming the file identity hasn't changed
  since it was opened for reading — this catches a swap to a *different*
  file up to that point, not an in-place content change to the same
  inode, not a swap happening after the check, and it's a single
  point-in-time check, not a lock held for the duration of the edit.

`file_path` may be absolute or relative to `cwd`. On POSIX, its lexically
normalized path must fall under a configured root; when roots are nested,
the longest lexical match supplies the directory descriptor used for the
component walk. On Windows, the opened target or verified parent/temp
handle must resolve under at least one configured root — there is no
longest-match selection there, only membership.

### How confinement actually holds under attack

This isn't just a prefix check on the string — the implementation is built
around the reality that resolving a path and then acting on it are two
different moments an attacker can race or redirect between:

- **POSIX**: each root is opened once, at construction, as an `O_NOFOLLOW`
  directory file descriptor. A requested path is first normalized
  lexically (`os.path.abspath`, so a `..` that stays inside a root
  resolves there — nothing special happens with it) and matched against
  the configured roots; anything landing outside every root is rejected
  before any I/O. The operation then walks the remaining components
  relative to the open root descriptor (`dir_fd=`), rejecting any
  *symlink* encountered along the way or a directory swapped for a
  symlink mid-flight, because that walk never re-resolves the path
  against the live filesystem.
- **Windows**: Python has no `dir_fd`/`openat` there, so instead the already
  *opened* file handle is checked after the fact with
  `GetFinalPathNameByHandleW` (`Read`/`Edit`'s open-and-verify path). `Write`
  and `Edit`'s replacement additionally validate the *parent* directory's
  handle first and reject a reparse-point parent outright, and verify a
  newly created temp file's own handle before any content is written to
  it. The final `os.replace()` still has to name the parent again (Windows
  exposes no handle-relative rename), so an ancestor-reparse swap timed
  exactly around that call is a **known, narrow, unclosed race** on
  Windows — the module fails closed (raises) whenever handle-path
  verification itself is unavailable, but that specific window is not
  fully closed the way the POSIX `dir_fd` walk closes it.

## Bash — opt-in, not path-confined

```python
tools = WorkspaceTools(file_roots=["/workspace"], cwd="/workspace", enable_bash=True)
```

`Bash(command, timeout=None)` runs `command` through a shell from `cwd` and
returns `{"exit_code", "stdout", "stderr", "truncated"}`. `stdout` and
`stderr` share a `max_output_bytes` raw-byte capture budget. Once that
budget is exhausted, later bytes are dropped and `truncated` is `True`.
Decoding does not raise on malformed UTF-8: captured bytes are first
decoded with replacement characters. Because those replacements can expand
the UTF-8 byte length (one malformed byte can become `U+FFFD`, which is
itself 3 bytes), the combined returned strings are capped to the same
budget again; if that second cap cuts a UTF-8 sequence, the incomplete
trailing bytes are dropped. Consequently, `truncated` can also become
`True` when replacement decoding expands invalid input, even if the
captured raw bytes did not exceed the budget. There is no default
timeout — a command with `timeout=None` runs until it exits on its own;
`timeout<=0` raises `ValueError` before starting anything.

On a timeout, cleanup targets the direct child and its process tree, not
only the direct child:

- **Windows** (no POSIX process groups): the command runs inside a real
  kernel **Job Object** created with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`.
  Cleanup calls `TerminateJobObject()` and then closes the job — it does
  not rely on closing alone — so every process assigned to it is
  terminated, including a grandchild the Python `asyncio` layer never
  directly tracked. This tree kill is guaranteed on Windows.
- **POSIX**: the process runs in its own session (`start_new_session=True`);
  on timeout, descendants are snapshotted by walking `/proc/*/stat` PPID
  chains (falling back to `ps -A -o pid=,ppid=` if `/proc` isn't available),
  then `SIGTERM`'d individually plus the whole process group. Up to 1
  second is then spent waiting for the direct child (the shell) to exit;
  as soon as it does — which can be immediately — every snapshotted
  descendant is `SIGKILL`'d right away, so that second is not a
  guaranteed grace period for the descendants themselves, only an upper
  bound on how long cleanup waits before escalating. A daemon that has
  already reparented away before that snapshot is taken cannot be
  reliably found this way and may survive —
  the docstring says so explicitly; this is a best-effort tree kill, not a
  guarantee.

## Design invariants

- **Two independent layers, not two votes.** `file_roots` confines
  Read/Write/Edit; the caller's own approval gate authorizes. `WorkspaceTools`
  never checks or grants approval itself.
- **Bash is deliberately unconfined.** Enabling it is a real capability
  grant — gate it behind an explicit approval, not behind `file_roots`.
- **Fail closed on unverifiable paths.** If handle-path verification is
  unavailable (Windows) or a component can't be safely opened (POSIX), the
  operation raises rather than proceeding on an unverified guess.
- **Atomic mutation, exact-match edits.** `Write`/`Edit` never leave a
  half-written file, and `Edit` never guesses which occurrence to replace.

## Example

Enabling `Bash` without configuring an approval gate hands the model an
immediately executable, host-unconfined shell. `TieredGate` has **no
automatic hook into `LLMEngine`'s tool loop** — it is consulted natively
only inside `ClaudeCodeEngine`/`CodexEngine`'s own action-approval flow
(`CodingAgentConfig.approval_gate`, e.g. via `.writer(gate)`).
`LLMEngine(...)` takes no `cwd`/`approval_gate` keyword at all; passing
either raises `TypeError` at construction. For an `LLMEngine`-backed agent
— the case this whole page is about, "any engine, not just Claude Code" —
gating `Bash` means wrapping the tool's own callable so every call is
checked against the gate *before* `WorkspaceTools`' real implementation
ever runs, using `TieredGate` as a plain policy object rather than an
engine kwarg:

```python
from lazybridge import Agent, LLMEngine
from lazybridge.engines.coding import ApprovalRequest
from lazybridge.ext.approval import Rule, TerminalChannel, TieredGate
from lazytools.workspace import WorkspaceTools

gate = TieredGate(
    channel=TerminalChannel(),
    rules=(
        Rule("allow", "Read"),
        Rule("allow", "Write"),
        Rule("allow", "Edit"),
        Rule("ask", "Bash"),
    ),
)

tools = WorkspaceTools(
    file_roots=["/workspace/project"],
    cwd="/workspace/project",
    enable_bash=True,
)
tool_list = tools.as_tools()
for tool in tool_list:
    if tool.name == "Bash":
        real_bash = tool.func

        # Verified live: passing real_bash as a `_real=real_bash` default
        # parameter instead of a plain closure breaks here -- Tool's
        # pydantic-backed argument validation introspects every parameter
        # including defaults, and deepcopying that bound method fails on
        # an internal threading.Lock inside WorkspaceTools ("cannot pickle
        # '_thread.lock' object"). A closure has no such parameter for
        # pydantic to see.
        async def gated_bash(command: str, timeout: float | None = None) -> dict:
            decision = await gate(
                ApprovalRequest(provider="llm", kind="command", name="Bash", arguments={"command": command})
            )
            if decision.action not in ("allow", "allow_session"):
                raise PermissionError(f"Bash denied: {decision.message or 'no reason given'}")
            return await real_bash(command, timeout=timeout)

        tool.func = gated_bash

agent = Agent(LLMEngine("deepseek-v4-flash"), tools=tool_list)
```

If the engine actually is `ClaudeCodeEngine`/`CodexEngine` (still able to
use `WorkspaceTools` for a uniform Read/Write/Edit surface, even though
those engines ship their own native versions), `CodingAgentConfig.writer(gate)`
(see `lazybridge.engines.coding` in the LazyBridge package itself — this
repo's own docs don't cover LazyBridge's engine API) wires the same
`TieredGate` through the engine's own native approval flow instead; that
path does not need the manual wrapper above.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ValueError: file root does not exist` / `is not a directory` at construction | A `file_roots` entry is missing or not a directory | Create it first, or pass an existing directory |
| `ValueError: ...: path is outside the configured file roots` (and, for `Write` on Windows to a path whose parent exists, `...: parent is outside the configured file roots`) | **POSIX:** `file_path` lexically escapes every `file_root` (any tool). **Windows:** an existing `Read`/`Edit` file's handle-resolved path, or an existing `Write` target's (or its parent's) `Path.resolve()` result, isn't under any root — Windows follows symlinks and checks the *resolved* path rather than rejecting them outright | Use a path inside a configured root |
| `ValueError: ...: path is outside the configured roots or contains a symlink` | **POSIX only.** A parent directory component is a symlink (any tool), or — for `Read`/`Edit` specifically — the file itself is a symlink; rejected outright by the `O_NOFOLLOW` walk regardless of where it points | Replace the symlinked component with the real path |
| `ValueError: Write: path is not a file` | The existing target is not a regular file. On POSIX this covers any existing non-regular leaf — directory, symlink, FIFO, device — even a symlink pointing at a regular file (`Write` checks the leaf with `os.stat(follow_symlinks=False)` rather than the `O_NOFOLLOW` walk). Windows resolves symlinks first and reports this when the resolved target isn't a file | Point `Write` at a real file, not a symlink or other non-regular path |
| `ValueError: ...: parent directory does not exist` | Not `Write`-only: `_open_posix_parent()` is shared by `Read`/`Write`/`Edit`. On POSIX it also covers a component that exists but isn't a directory (`ENOTDIR` maps to the same message). On Windows, `Write` reports this when the requested direct parent can't be resolved | Create the parent directory first — none of the three tools create parent directories |
| `ValueError: Edit: old_string matched N times; set replace_all=true ...` | Ambiguous match | Narrow `old_string` for a unique match, or pass `replace_all=True` |
| No `Bash` tool present | `enable_bash` wasn't set | Construct with `enable_bash=True` |
| `TimeoutError: Bash: command timed out after ...` | Command ran past `timeout` | Raise `timeout`, or pass `timeout=None` for no limit |
| Output cut off, `truncated: True` | Combined raw stdout+stderr exceeded `max_output_bytes`, **or** replacement-decoding of malformed UTF-8 expanded the returned representation beyond that same budget | Raise `max_output_bytes`, or have the command write less / cleaner UTF-8 |

## See also

- [Safety](safety.md) — the allow-list/confirmation-gate model used by other
  guarded tools in this package (not what gates `Bash` here — that's the
  engine's own approval gate).
- [Tools overview](connectors.md) — every tool provider at a glance.
