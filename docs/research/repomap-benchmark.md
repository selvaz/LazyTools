# RepoMap code-search benchmark

This note records the benchmark used to decide whether LazyTools should expose a
small, read-only code-search adapter backed by
[SylphxAI/repomap](https://github.com/SylphxAI/repomap).

The adapter expects the optional CLI pinned at version 1.9.0:

```bash
npm install -g @sylphx/repomap@1.9.0
```

If it is absent, tool calls return an installation message; importing
LazyTools does not import or install RepoMap. Indexes are directed to
`$LAZYTOOLS_DATA_DIR/repomap` (by default `~/.lazytools/repomap`), or to the
MCP configuration key `codemap_cache_dir`.

## Results

### Exact symbol to definition file

| Method | Correct (22 cases) | Time |
| --- | ---: | ---: |
| `rg` definition search | 22/22 | 8 ms |
| RepoMap | 21/22 | 127 ms |

RepoMap did not improve exact-symbol lookup. A correct, targeted `rg` search was
both more accurate and faster.

### Issue title to files to change

| Method | Any gold file at 5 | All gold files at 10 |
| --- | ---: | ---: |
| RepoMap | 12/14 | 10/14 |
| Lexical BM25 | 7/14 | 7/14 |

This task showed useful ranking signal on the small sample. A multi-repository
check also succeeded and returned results from both repositories.

## Method

The benchmark used PR #325, workflow run `44944608962`, at head `65aee66`. For
the issue-to-files task, each search ran against the code **before the fix**: the
common ancestor of the PR head and `main`. Gold files were the files subsequently
changed by the fix. This pre-fix protocol prevents the search backend from seeing
the answer in the patched code.

The exact-symbol task measured whether a definition-file search returned the
known definition. The issue task used 14 issue titles and measured whether any
gold file appeared in the first five results and whether all gold files appeared
in the first ten.

## Limits and next step

The issue benchmark contains only 14 curated, held-in cases. It establishes a
promising retrieval signal, not a general result. It also does not measure an
end-to-end coding agent, solution quality, latency across a full task, or token
use. The next step is an end-to-end comparison of agents completing the same
tasks with and without the code-search tool.

## Credits

RepoMap is provided by [SylphxAI/repomap](https://github.com/SylphxAI/repomap)
under the MIT license. The benchmark methodology was informed by
[CodeGraphContext](https://github.com/CodeGraphContext/CodeGraphContext), also
provided under the MIT license.
