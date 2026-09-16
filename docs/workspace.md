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

`file_path` may be absolute or relative to `cwd`. A path is confined the
same way for all three tools: it must resolve inside one of the configured
`file_roots` (the longest-matching root wins when roots are nested).

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
returns `{"exit_code", "stdout", "stderr", "truncated"}`. `stdout`/`stderr`
are captured up to `max_output_bytes` combined; beyond that, `truncated` is
`True` (bytes are dropped, not the whole call rejected). Decoding never
raises: the captured bytes are first decoded with `errors="replace"`
(inserting `�` for any invalid or boundary-cut UTF-8 sequence), and in
the rare case where that replacement itself pushes the result back over the
byte budget, a second pass re-slices and decodes with `errors="ignore"`
instead (silently dropping the trailing incomplete bytes rather than
re-expanding past the limit). There is no default timeout — a command with
`timeout=None` runs until it exits on its own; `timeout<=0` raises
`ValueError` before starting anything.

On a timeout, the whole process **tree** is torn down, not just the direct
child:

- **Windows** (no POSIX process groups): the command runs inside a real
  kernel **Job Object** created with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, so
  closing the job terminates every process in it, including a grandchild the
  Python `asyncio` layer never directly tracked.
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

```python
from lazybridge import Agent, LLMEngine
from lazytools.workspace import WorkspaceTools

tools = WorkspaceTools(
    file_roots=["/workspace/project"],
    cwd="/workspace/project",
    enable_bash=True,   # opt-in; gate the resulting capability via the engine's approval_gate
)
agent = Agent(LLMEngine("deepseek-v4-flash"), tools=tools.as_tools())
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ValueError: file root does not exist` / `is not a directory` at construction | A `file_roots` entry is missing or not a directory | Create it first, or pass an existing directory |
| `ValueError: ...: path is outside the configured file roots` (and, for `Write` on Windows to a path whose parent exists, `...: parent is outside the configured file roots`) | **POSIX:** `file_path` lexically escapes every `file_root` (any tool). **Windows:** an existing `Read`/`Edit` file's handle-resolved path, or an existing `Write` target's (or its parent's) `Path.resolve()` result, isn't under any root — Windows follows symlinks and checks the *resolved* path rather than rejecting them outright | Use a path inside a configured root |
| `ValueError: ...: path is outside the configured roots or contains a symlink` | **POSIX only.** A parent directory component is a symlink (any tool), or — for `Read`/`Edit` specifically — the file itself is a symlink; rejected outright by the `O_NOFOLLOW` walk regardless of where it points | Replace the symlinked component with the real path |
| `ValueError: Write: path is not a file` | **POSIX**, `Write` to an existing leaf that's a symlink (`Write` checks the leaf with `os.stat(follow_symlinks=False)` rather than the `O_NOFOLLOW` walk, so it reports this instead of the "contains a symlink" message above) | Point `Write` at a real file, not a symlink |
| `ValueError: ...: parent directory does not exist` (`Write`) | `Write`'s direct parent doesn't exist | Create the parent directory first — `Write` never creates directories |
| `ValueError: Edit: old_string matched N times; set replace_all=true ...` | Ambiguous match | Narrow `old_string` for a unique match, or pass `replace_all=True` |
| No `Bash` tool present | `enable_bash` wasn't set | Construct with `enable_bash=True` |
| `TimeoutError: Bash: command timed out after ...` | Command ran past `timeout` | Raise `timeout`, or pass `timeout=None` for no limit |
| Output cut off, `truncated: True` | Combined stdout+stderr exceeded `max_output_bytes` | Raise `max_output_bytes`, or have the command write less |

## See also

- [Safety](safety.md) — the allow-list/confirmation-gate model used by other
  guarded tools in this package (not what gates `Bash` here — that's the
  engine's own approval gate).
- [Tools overview](connectors.md) — every tool provider at a glance.
