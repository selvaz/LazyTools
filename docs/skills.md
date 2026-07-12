# Skills

Turn local documentation folders into a portable, queryable **skill bundle**, then
expose it to any agent as a tool or a router→executor pipeline.
`lazytools.skills` indexes your docs with full BM25 and returns grounded context
briefs — accurate retrieval for technical docs, with **zero dependencies beyond
the standard library**.

!!! info "Status & install"
    **Status: alpha.** No extra needed:
    ```bash
    pip install "lazytoolkit @ git+https://github.com/selvaz/LazyTools.git"
    ```
    The package is `lazytoolkit` (installed from GitHub — see Install); the import root is `lazytools`. Add
    `generated_skills/` to your `.gitignore` — `build_skill()` writes there by
    default.

## Synopsis

A *skill* is a folder on disk that captures everything an agent needs to answer
from your documentation: the chunked text, the BM25 weights, and an LLM
instruction file. You build it once (`build_skill`), then either query it directly
(`query_skill`), hand it to an agent as a retrieval tool (`skill_tools`), let an
agent *build* skills (`skill_builder_tools`), or wrap it in a two-step
router→executor pipeline (`skill_pipeline`).

Retrieval uses **full BM25** (Robertson IDF, `k1=1.5`, `b=0.75`) with IDF weights
computed once at index time — substantially more accurate than TF-only matching
for technical docs, where rare terms (class names, parameter names, error codes)
matter most. Chunking is **heading-aware** for Markdown/RST/AsciiDoc so each
section becomes its own semantically-bounded chunk.

## How it works

```
build_skill(source_dirs, name)                 query_skill(skill_dir, task, mode)
  │  iter docs (DEFAULT_EXTENSIONS)               │  tokenize task
  │  heading-aware chunks (md/rst/adoc),          │  BM25 score every chunk vs query
  │  char-split fallback elsewhere                │  take top_k (score > 0)
  │  compute avgdl + Robertson IDF                │  resolve mode (auto detects intent)
  └─ write bundle ↓                               └─ render grounded brief + evidence
        SKILL.md       (LLM instructions)
        manifest.json  (name, description, indexed_files, avgdl, bundle version 3.0.0)
        vocab.json     (IDF weights)
        chunks.jsonl   (one DocChunk per line: path, title, heading, text, …)
```

- **Portable bundle.** The four files are self-contained — copy the folder
  anywhere; querying needs only the standard library.
- **Heading-aware chunking.** For `.md/.mdx/.rst/.adoc`, each section is its own
  chunk (large sections sub-split by char count, tiny adjacent ones merged).
  Other file types use character splitting with paragraph-boundary snapping.
- **Query modes.** `auto` detects intent from the task wording; or force
  `answer` / `extract` / `locate` / `summarize`. Each shapes the returned brief
  (e.g. `locate` lists relevant files, `extract` returns excerpts).
- **Grounded by construction.** When nothing scores above zero, the brief says so
  and instructs the model not to answer beyond the indexed evidence.

## Signature

```python
from lazytools.skills import (
    build_skill, query_skill,
    skill_tools, skill_builder_tools, skill_pipeline,
)

build_skill(
    source_dirs,                   # list[str] — folders to index
    skill_name,                    # str — bundle folder name + title
    output_root="./generated_skills",
    description="",                # str — used in SKILL.md + tool description
    usage_notes="",                # str — extra rules appended to SKILL.md
    include_extensions=[".md", ".mdx", ".txt", ".rst", ".adoc",
                        ".py", ".json", ".yaml", ".yml", ".toml"],
    chunk_size=1800,               # int — max chars per chunk
    chunk_overlap=180,             # int — overlap between char-mode chunks
    copy_sources=False,            # bool — copy originals into bundle/sources/
    overwrite=True,                # bool — replace an existing bundle of the same name
    max_chars_per_file=200_000,    # int — safety cap on chars read per file
) -> dict          # {skill_dir, skill_name, description, indexed_files, total_chunks, avgdl}

query_skill(
    skill_dir,                     # str — path to a bundle
    task,                          # str — question / task
    mode="auto",                   # "auto" | "answer" | "extract" | "locate" | "summarize"
    top_k=8,                       # int — chunks to retrieve
    max_chars=10_000,              # int — cap on the returned brief
    include_quotes=True,           # bool — append full excerpts
) -> str

skill_tools(*, skill_dir, name=None, description=None, strict=False) -> list[Tool]
skill_builder_tools(*, base_dir, name="build_doc_skill", description=..., strict=False) -> list[Tool]
skill_pipeline(*, skill_dir, provider="anthropic", router_model=None,
               executor_model=None, session=None, native_tools=None) -> Tool
```

### `build_skill` parameters

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `source_dirs` | `list[str]` | — | Folders to index. Each must be a directory or `FileNotFoundError` is raised. |
| `skill_name` | `str` | — | Bundle name; slugified into the on-disk folder name and used as the title. |
| `output_root` | `str` | `"./generated_skills"` | Parent directory for the bundle. |
| `description` | `str` | `""` | What the skill covers; surfaces in `SKILL.md` and the tool description. Defaults to a count-based summary. |
| `usage_notes` | `str` | `""` | Extra operational rules appended to `SKILL.md`. |
| `include_extensions` | `list[str]` | `DEFAULT_EXTENSIONS` | File types to index (Markdown, RST, AsciiDoc, text, plus `.py/.json/.yaml/.yml/.toml`). |
| `chunk_size` | `int` | `1800` | Max characters per chunk. |
| `chunk_overlap` | `int` | `180` | Overlap between character-mode chunks. |
| `copy_sources` | `bool` | `False` | Copy original docs into `bundle/sources/`. |
| `overwrite` | `bool` | `True` | Replace an existing bundle of the same name (else `FileExistsError`). |
| `max_chars_per_file` | `int` | `200_000` | Safety cap on characters read per file. |

### `query_skill` parameters

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `skill_dir` | `str` | — | Path to a bundle from `build_skill`. |
| `task` | `str` | — | The question or task to answer from the indexed docs. |
| `mode` | `Literal` | `"auto"` | `auto` detects intent; or `answer` / `extract` / `locate` / `summarize`. |
| `top_k` | `int` | `8` | Number of chunks to retrieve. |
| `max_chars` | `int` | `10_000` | Cap on the returned context brief. |
| `include_quotes` | `bool` | `True` | Append full excerpts after the evidence bullets (answer mode). |

## The five functions

| Function | Returns | Use it to… |
|---|---|---|
| `build_skill(...)` | `dict` metadata | Index folders into a bundle on disk. |
| `query_skill(...)` | `str` brief | Retrieve grounded context for a task (no LLM call — pure retrieval). |
| `skill_tools(skill_dir=…)` | `list[Tool]` | Hand one retrieval tool to an agent (answers only from the skill). |
| `skill_builder_tools(base_dir=…)` | `list[Tool]` | Let an agent *build* skill bundles on demand, sandboxed to `base_dir`. |
| `skill_pipeline(skill_dir=…)` | `Tool` | A router (sharpens the query, preserving identifiers) → executor (calls the skill, synthesises a grounded answer) chain, exposed as one tool. |

## When to use it

- **Ground an agent in your own docs** without a vector DB or embeddings service —
  stdlib BM25 is enough and ships nothing extra.
- **Technical retrieval** where exact identifiers matter (class/method/parameter
  names, error codes); BM25's IDF weighting favours those rare terms.
- **Portable, reproducible skills** you can commit-build, copy between machines,
  or ship inside a container.
- **Agent-built skills**: expose `skill_builder_tools(base_dir=…)` so an agent
  can turn a folder into a queryable skill mid-task. `base_dir` is required —
  source folders must live inside it and bundles are always written to
  `<base_dir>/generated_skills`, so the LLM can neither index arbitrary host
  files nor choose the output location.

## When NOT to use it

- **One-off folder reads.** If you just need "read these files now," use
  [Documents](documents.md) — no index step.
- **Semantic / fuzzy paraphrase matching** at scale. BM25 is lexical; for
  heavy synonym/semantic recall over very large corpora, an embeddings retriever
  may do better.
- **Constantly-changing corpora.** The bundle is a point-in-time index — rebuild
  when the docs change.

## Example

=== "Build + query"

    ```python
    from lazytools.skills import build_skill, query_skill

    meta = build_skill(["./docs", "./reference"], "my-project")
    print(meta["indexed_files"], meta["total_chunks"], meta["avgdl"])

    brief = query_skill(meta["skill_dir"], "How does auth work?")
    print(brief)                              # grounded context brief, no LLM call
    ```

=== "As an agent tool"

    ```python
    from lazybridge import Agent
    from lazytools.skills import build_skill, skill_tools

    meta = build_skill(["./docs"], "my-project")
    tools = skill_tools(skill_dir=meta["skill_dir"])
    agent = Agent("claude-opus-4-8", tools=tools)
    print(agent("How do I rotate credentials?").text())
    ```

=== "Router → executor pipeline"

    ```python
    from lazytools.skills import build_skill, skill_pipeline

    meta = build_skill(["./docs"], "my-project")
    pipe = skill_pipeline(skill_dir=meta["skill_dir"], provider="anthropic")
    # The router sharpens the query (keeping identifiers); the executor calls the
    # skill tool and answers only from the retrieved evidence, naming its sources.
    agent = Agent("claude-opus-4-8", tools=[pipe])
    ```

=== "Agent builds the skill"

    ```python
    from lazybridge import Agent
    from lazytools.skills import skill_builder_tools

    # base_dir is the sandbox: source dirs must resolve inside it, and
    # bundles land in <base_dir>/generated_skills.
    agent = Agent("claude-opus-4-8", tools=skill_builder_tools(base_dir="."))
    agent("Index ./docs and ./api into a skill called 'platform-docs'")
    ```

=== "Query modes"

    ```python
    query_skill(d, "where is retry configured?", mode="locate")     # → relevant files
    query_skill(d, "show the backoff settings",  mode="extract")    # → excerpts
    query_skill(d, "overview of the auth flow",  mode="summarize")  # → condensed bullets
    query_skill(d, "what does k1 control?",      mode="answer")     # → evidence + quotes
    ```

## Bundle format

A bundle is a folder named after the slugified `skill_name`, containing:

| File | Contents |
|---|---|
| `SKILL.md` | LLM instructions — what the skill covers and how to use it. |
| `manifest.json` | Metadata: name, description, `source_dirs`, `indexed_files`, `total_chunks`, `avgdl`, extensions, and bundle-format `version` (`3.0.0`). |
| `vocab.json` | Robertson IDF weights, precomputed at index time. |
| `chunks.jsonl` | One `DocChunk` per line: `path`, `title`, `heading`, `text`, token data, `doc_len`, ordinal. |
| `sources/` | (Optional, when `copy_sources=True`) copies of the original docs. |

The bundle-format `version` is independent of the `lazytoolkit` package version.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `FileNotFoundError: Not a directory` | A `source_dirs` entry isn't a folder | Pass existing directories only |
| `ValueError: No indexable documentation found` | No files matched `include_extensions` | Widen `include_extensions` or check the paths |
| `FileExistsError: Skill already exists` | Bundle exists and `overwrite=False` | Set `overwrite=True` or pick a new `skill_name` |
| `FileNotFoundError: Skill directory not found` | `query_skill` given a bad `skill_dir` | Use the `skill_dir` from `build_skill`'s return dict |
| `"No relevant documentation was retrieved…"` | Nothing scored above zero for the task | Rephrase with the exact identifiers, raise `top_k`, or re-index more docs |

## Pitfalls

- **Rebuild on change.** The index is a snapshot; stale docs mean stale answers.
- **`overwrite=True` is the default** — building over an existing name *replaces*
  it. Set `overwrite=False` to guard against accidental clobbering.
- **BM25 is lexical.** Use the terms that appear in the docs; pure paraphrase may
  under-retrieve. The `skill_pipeline` router exists precisely to sharpen vague
  queries into identifier-preserving ones.
- **Per-file char cap.** `max_chars_per_file` (200k) truncates very large files
  before chunking — raise it if you index big single documents.
- **Add `generated_skills/` to `.gitignore`** — the default `output_root`.

## See also

- [Documents](documents.md) — read a folder once without building an index.
- [Tools overview](connectors.md) — every connector at a glance.
- [Tool](https://core.lazybridge.com/guides/basic/tool/) and
  [Agent.chain](https://core.lazybridge.com/) — what `skill_tools` and
  `skill_pipeline` build on.
