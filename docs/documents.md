# Documents

Read local documents — `.txt`, `.md`, `.pdf`, `.docx`, `.html`/`.htm` — from a
single file or a whole folder, and return their text in a shape ready for LLM
consumption. `lazytools.documents` works as a plain Python function, as a
sandboxed agent tool, or as a one-shot CLI.

!!! info "Status & install"
    **Status: alpha.** Plain text and Markdown need no extra dependencies. For
    PDF / DOCX / HTML parsing:
    ```bash
    pip install "lazytoolkit[docs] @ git+https://github.com/selvaz/LazyTools.git@v0.3.2"   # adds pypdf, python-docx, trafilatura
    ```
    The package is `lazytoolkit` (installed from GitHub — see Install); the import root is `lazytools`. Each
    format reader degrades gracefully — a missing optional dependency yields a
    `"[… unavailable — pip install …]"` placeholder per file rather than an
    exception, so a folder of mixed types still reads what it can.

## Synopsis

`read_folder_docs` is one function that accepts **either a single file or a
folder**. Given a folder it scans for matching extensions (optionally recursive),
reads each file with the right per-format reader, and concatenates the results
into a single human/LLM-readable string — or a structured JSON object. It is
built for the agent setting: when exposed as a tool the `path` argument is
LLM-controlled, so a `base_dir` sandbox, per-file size caps, a file-count cap, and
symlink rejection are all first-class.

Two entry points:

- **`read_folder_docs(...)`** — the function. Call it directly from trusted code.
- **`read_docs_tools(base_dir=...)`** — returns a `[Tool]` with `path` sandboxed to
  `base_dir`. `base_dir` is **required**; passing nothing raises `ValueError`.

## How it works

```
read_folder_docs(path, ...)
  │
  ├─ resolve path  ── if base_dir set: reject anything escaping the sandbox
  ├─ file?  → read that one file (extensions filter ignored)
  ├─ dir?   → glob matching extensions (recursive optional), skip symlinks,
  │           sort, cap at max_files
  └─ per file → pick reader by suffix:
        .txt/.md → plain text
        .pdf     → pypdf            (page text joined)
        .docx    → python-docx      (paragraphs + tables as " | " rows)
        .html    → trafilatura      (parsed | full | both)
     skip files over max_file_bytes; record per-file errors inline
  → output_format "text" (headered string) or "json" (records + truncation meta)
```

- **Optional deps, graceful degradation.** `pypdf`, `python-docx`, and
  `trafilatura` are imported lazily inside their readers; if missing, that file's
  content becomes a placeholder string and the scan continues.
- **HTML modes.** `parsed` (default) extracts clean body text via trafilatura —
  stripping nav/ads/boilerplate; `full` returns raw HTML; `both` returns the
  parsed body then the raw source.
- **Robust scanning.** A file that vanishes between glob and `stat`, or is
  unreadable, is recorded as an error entry rather than aborting the whole scan.

## Signature

```python
from lazytools.documents import read_folder_docs, read_docs_tools

read_folder_docs(
    path,                          # str — a single file OR a folder to scan
    extensions="txt,md,pdf,docx,html",  # str — comma-separated; folder mode only
    html_mode="parsed",            # "parsed" | "full" | "both"
    recursive=False,               # bool — recurse into subfolders (folder mode)
    output_format="text",          # "text" | "json"
    *,
    base_dir=None,                 # str | None — sandbox; reject paths escaping it
    max_file_bytes=10_000_000,     # int | None — per-file size ceiling (DEFAULT_MAX_FILE_BYTES)
    max_files=500,                 # int | None — files read per scan (DEFAULT_MAX_FILES)
) -> str


# As an agent tool — base_dir is REQUIRED.
read_docs_tools(
    *,
    base_dir,                      # str — sandbox directory (required; "" / None → ValueError)
    max_file_bytes=10_000_000,
    max_files=500,
) -> list[Tool]
```

### `read_folder_docs` parameters

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `path` | `str` | — | A single file (read directly, extensions ignored) or a folder (scanned). |
| `extensions` | `str` | `"txt,md,pdf,docx,html"` | Comma-separated extensions to include in folder mode. Selecting `html` also matches `.htm`. |
| `html_mode` | `str` | `"parsed"` | `parsed` = clean body text; `full` = raw HTML; `both` = parsed then raw. |
| `recursive` | `bool` | `False` | Recurse into subfolders (folder mode only). |
| `output_format` | `str` | `"text"` | `text` = headered human/LLM string; `json` = structured object (see below). |
| `base_dir` | `str \| None` | `None` | Sandbox. When set, any `path` resolving outside raises `PermissionError`. Required via `read_docs_tools`. |
| `max_file_bytes` | `int \| None` | `10_000_000` | Files larger than this are reported as skipped, not read. `None` disables the cap. |
| `max_files` | `int \| None` | `500` | Cap on files read per scan; extras are truncated and flagged. `None` disables the cap. |

### `output_format="json"` shape

```json
{
  "records": [
    {"filename": "q4.pdf", "relative_path": "q4.pdf", "extension": "pdf",
     "size_bytes": 81234, "char_count": 5120, "content": "…"}
  ],
  "truncated": false,
  "max_files": 500,
  "total_found": 1
}
```

Parse with `json.loads` and index `["records"]`. **One case always returns a plain
(non-JSON) string** even when `output_format="json"`: a folder with no matching
files (`"[No documents found …]"`). A nonexistent path raises
`FileNotFoundError`. Guard your `json.loads` accordingly (e.g. only parse
output starting with `"{"`).

## When to use it

- **"Summarise these reports / this folder."** Point an agent at a directory of
  PDFs, Word docs, and Markdown and let it read them in one tool call.
- **RAG-lite ingestion** from a trusted local corpus, without standing up a vector
  store.
- **Mixed-format reading** where you want one consistent text surface across
  `.pdf`, `.docx`, `.html`, and plain text.

## When NOT to use it

- **Untrusted, un-sandboxed file access.** Always go through `read_docs_tools`
  with a `base_dir` when an LLM controls `path` — never expose
  `read_folder_docs` directly to an agent.
- **Large-scale / persistent retrieval.** For thousands of docs with ranked
  retrieval, build a [Skill](skills.md) (BM25 index) instead of slurping a folder
  each call.
- **Binary or exotic formats** (xlsx, pptx, images). Only the five listed suffixes
  are supported; extend the reader map yourself for more.

## Example

=== "Direct call"

    ```python
    from lazytools.documents import read_folder_docs

    # Read every PDF and Word doc under /reports, recursively, as text.
    text = read_folder_docs("/reports", extensions="pdf,docx", recursive=True)
    print(text)
    ```

=== "As a sandboxed tool"

    ```python
    from lazybridge import Agent
    from lazytools.documents import read_docs_tools

    # base_dir is REQUIRED — the agent can only read inside /safe/docs.
    tools = read_docs_tools(base_dir="/safe/docs")
    agent = Agent("claude-opus-4-8", tools=tools)
    agent("Summarise every PDF in the reports subfolder")
    ```

=== "Structured JSON output"

    ```python
    import json
    from lazytools.documents import read_folder_docs

    out = read_folder_docs("/reports", output_format="json")
    if out.startswith("{"):                    # guard the not-found / empty cases
        data = json.loads(out)
        for rec in data["records"]:
            print(rec["filename"], rec["char_count"])
        if data["truncated"]:
            print(f"capped at {data['max_files']} of {data['total_found']} files")
    ```

=== "CLI"

    ```bash
    python -m lazytools.documents /path/to/folder
    python -m lazytools.documents /path/to/file.pdf
    python -m lazytools.documents /path/to/folder --extensions pdf,docx --recursive
    python -m lazytools.documents /path/to/folder --html-mode both --format json
    ```

## Security & safety

- **`base_dir` sandbox.** With `base_dir` set, a `path` that resolves outside it
  raises `PermissionError`. `read_docs_tools` *requires* it because the tool's
  `path` is LLM-controlled — without a sandbox an agent could read `/etc/passwd`,
  SSH keys, or `.env` files. Call `read_folder_docs` directly only from trusted,
  non-LLM code if you genuinely need un-sandboxed access.
- **Symlinks are not followed.** Folder scans skip symlinked files, closing
  symlink-loop hangs and preventing a symlink from silently widening the read
  surface to other directories.
- **Size and count caps.** `max_file_bytes` (default 10 MB) bounds per-file memory;
  `max_files` (default 500) bounds how much a single call can slurp. Note a small,
  heavily-compressed file (e.g. a PDF) can still expand on extract.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `"[PDF unavailable — pip install pypdf]"` (and similar) | Optional dep missing | `pip install "lazytoolkit[docs] @ git+https://github.com/selvaz/LazyTools.git@v0.3.2"` |
| `PermissionError: … escapes base_dir …` | `path` resolved outside the sandbox | Pass a `path` inside `base_dir`, or widen `base_dir` for trusted code |
| `ValueError: read_docs_tools(base_dir=...) is required` | Built the tool without a sandbox | Pass `base_dir="/safe/dir"`, or use `read_folder_docs` directly for trusted use |
| `"[No documents found … matching extensions: …]"` | No files matched in folder mode | Check `extensions` and `recursive`; remember `html` also matches `.htm` |
| `"[Skipped: file is N bytes, exceeds max_file_bytes=…]"` | File over the size cap | Raise `max_file_bytes`, or set it to `None` to disable |
| `"[Error reading file: …]"` in output | Per-file read failure | Inspect the message; the scan continues for other files |

## Pitfalls

- **`extensions` is ignored for single files.** Point `path` at a file and it's
  read regardless of suffix (using the matching reader, or an "unsupported
  extension" note).
- **JSON output isn't always JSON.** The empty-folder case returns a plain
  string (and a nonexistent path raises `FileNotFoundError`) — guard
  `json.loads`.
- **`parsed` HTML can drop content.** trafilatura strips boilerplate aggressively;
  use `full` or `both` if you need the raw markup.
- **Compression bombs.** The byte cap is a first line of defence, not a guarantee —
  a tiny PDF can expand massively on extract.

## See also

- [Skills](skills.md) — for ranked retrieval over a corpus, build a BM25 skill
  bundle instead of reading a whole folder per call.
- [Safety](safety.md) — the sandbox/guard philosophy shared across LazyTools.
- [Tools overview](connectors.md) — every connector at a glance.
