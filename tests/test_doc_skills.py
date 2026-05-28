"""doc_skills: build a BM25 skill bundle and query it (stdlib only)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from lazytools.skills import (
    DocChunk,
    SkillManifest,
    build_skill,
    query_skill,
    skill_builder_tools,
    skill_pipeline,
    skill_tools,
)
from lazytools.skills.doc_skills import (
    _bm25,
    _build_idf,
    _heading_chunks,
    _make_chunks,
    _tokenize,
)


def test_public_api_smoke() -> None:
    assert callable(build_skill)
    assert callable(query_skill)
    assert callable(skill_tools)
    assert callable(skill_builder_tools)
    assert callable(skill_pipeline)
    assert DocChunk is not None and SkillManifest is not None


def test_skill_builder_tools_returns_tools() -> None:
    tools = skill_builder_tools()
    assert len(tools) == 1
    assert tools[0].name == "build_doc_skill"


def test_skill_builder_tools_custom_name() -> None:
    tools = skill_builder_tools(name="my_builder")
    assert tools[0].name == "my_builder"


def test_build_then_query_roundtrip(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "auth.md").write_text(
        "# Authentication\n"
        "To authenticate, call login() with an API token. "
        "Tokens are issued from the dashboard settings page."
    )
    (docs / "billing.md").write_text("# Billing\nInvoices are generated monthly and emailed to the account owner.")

    meta = build_skill([str(docs)], "test-skill", output_root=str(tmp_path / "out"))
    assert meta["total_chunks"] >= 1
    skill_dir = meta["skill_dir"]

    brief = query_skill(skill_dir, "How do I authenticate with a token?")
    assert "token" in brief.lower()


def test_skill_tools_bound_to_dir(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "x.md").write_text("# Topic\nsome indexed content about widgets")
    meta = build_skill([str(docs)], "bound-skill", output_root=str(tmp_path / "out"))
    tools = skill_tools(skill_dir=meta["skill_dir"])
    assert len(tools) >= 1


# ------------------------------------------------------------------ #
# Tokenisation
# ------------------------------------------------------------------ #


def test_tokenize_drops_stopwords_and_short_tokens() -> None:
    toks = _tokenize("The quick a fox is here")
    # "the", "a", "is" are stopwords; single-char tokens are dropped.
    assert "the" not in toks
    assert "a" not in toks
    assert "is" not in toks
    assert "quick" in toks and "fox" in toks


def test_tokenize_keeps_dotted_identifiers() -> None:
    # Technical identifiers like module.func must survive tokenisation.
    assert "module.func" in _tokenize("Call module.func to start")


# ------------------------------------------------------------------ #
# Heading-aware chunking
# ------------------------------------------------------------------ #


def test_heading_chunks_split_on_headings() -> None:
    # Use a small max_chunk so each section clears the merge threshold
    # (min_size = max_chunk // 4) and stays its own chunk.
    text = "# A\n" + ("alpha " * 40) + "\n\n## B\n" + ("beta " * 40)
    pairs = _heading_chunks(text, max_chunk=200, overlap=20)
    headings = [h for h, _ in pairs]
    # Both section headings appear (possibly with sub-chunk ordinal suffixes).
    assert any(h == "A" or h.startswith("A [") for h in headings)
    assert any(h == "B" or h.startswith("B [") for h in headings)


def test_heading_chunks_merge_tiny_sections() -> None:
    # A tiny trailing section is merged into the preceding chunk rather than
    # becoming its own sub-threshold chunk.
    big = "x " * 1000
    text = f"# Main\n{big}\n\n## Tiny\nshort"
    pairs = _heading_chunks(text, max_chunk=2000, overlap=100)
    assert any("short" in body for _, body in pairs)


def test_heading_chunks_subsplit_large_section() -> None:
    huge = "word " * 2000  # well over max_chunk chars
    text = f"# Big\n{huge}"
    pairs = _heading_chunks(text, max_chunk=500, overlap=50)
    assert len(pairs) > 1
    # Sub-chunks after the first carry an ordinal suffix on the heading.
    assert any(h.startswith("Big [") for h, _ in pairs)


def test_make_chunks_non_heading_uses_char_split(tmp_path: Path) -> None:
    # A .txt file is not a heading-extension, so chunks have empty headings
    # falling back to the document title.
    chunks = _make_chunks(Path("notes.txt"), "plain body text without headings", 1800, 180)
    assert len(chunks) == 1
    assert chunks[0].heading  # defaults to the doc title, never empty


# ------------------------------------------------------------------ #
# BM25 scoring
# ------------------------------------------------------------------ #


def _chunk(text: str, heading: str = "") -> DocChunk:
    toks = _tokenize(text)
    return DocChunk(
        path="d.md",
        title="Doc",
        heading=heading or "Doc",
        text=text,
        tokens=toks,
        doc_len=len(toks),
        ordinal=0,
    )


def test_bm25_zero_for_empty_query() -> None:
    c = _chunk("anything here")
    assert _bm25(c, [], {}, avgdl=2.0) == 0.0


def test_bm25_ranks_matching_chunk_higher() -> None:
    relevant = _chunk("authentication uses an oauth token to authenticate the user")
    other = _chunk("billing invoices are emailed monthly to the account owner")
    chunks = [relevant, other]
    idf = _build_idf(chunks)
    avgdl = sum(c.doc_len for c in chunks) / len(chunks)
    q = _tokenize("oauth token authentication")
    assert _bm25(relevant, q, idf, avgdl) > _bm25(other, q, idf, avgdl)


def test_bm25_heading_boost() -> None:
    # Two chunks with identical bodies; the one whose heading contains a query
    # term scores higher thanks to the heading boost.
    body = "configure the retry policy for the client"
    with_h = _chunk(body, heading="Retry configuration")
    without_h = _chunk(body, heading="Overview")
    idf = _build_idf([with_h, without_h])
    avgdl = (with_h.doc_len + without_h.doc_len) / 2
    q = _tokenize("retry policy")
    assert _bm25(with_h, q, idf, avgdl) > _bm25(without_h, q, idf, avgdl)


def test_bm25_phrase_bonus() -> None:
    # A chunk containing the exact query phrase outscores one with the same
    # tokens scattered.
    exact = _chunk("the quick brown fox jumps")
    scattered = _chunk("fox the brown quick jumps elsewhere entirely now")
    idf = _build_idf([exact, scattered])
    avgdl = (exact.doc_len + scattered.doc_len) / 2
    q = _tokenize("quick brown fox")
    assert _bm25(exact, q, idf, avgdl) > _bm25(scattered, q, idf, avgdl)


# ------------------------------------------------------------------ #
# query_skill modes
# ------------------------------------------------------------------ #


@pytest.fixture
def built_skill(tmp_path: Path) -> str:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "auth.md").write_text(
        "# Authentication\n"
        "To authenticate, call login() with an oauth token. "
        "Tokens are issued from the dashboard settings page.\n\n"
        "## Refresh\nRefresh tokens rotate every 30 days."
    )
    (docs / "billing.md").write_text("# Billing\nInvoices are generated monthly and emailed to the owner.")
    meta = build_skill([str(docs)], "modes-skill", output_root=str(tmp_path / "out"))
    return str(meta["skill_dir"])


def test_query_locate_lists_files(built_skill: str) -> None:
    out = query_skill(built_skill, "where is authentication described?", mode="locate")
    assert "Relevant files:" in out
    assert "auth.md" in out


def test_query_extract_returns_excerpts(built_skill: str) -> None:
    out = query_skill(built_skill, "extract the oauth token rules", mode="extract")
    assert "Excerpts:" in out
    assert "token" in out.lower()


def test_query_summarize_returns_summary(built_skill: str) -> None:
    out = query_skill(built_skill, "summarize authentication", mode="summarize")
    assert "Summary:" in out


def test_query_answer_includes_quotes_toggle(built_skill: str) -> None:
    with_quotes = query_skill(built_skill, "how do oauth tokens work?", mode="answer", include_quotes=True)
    without_quotes = query_skill(built_skill, "how do oauth tokens work?", mode="answer", include_quotes=False)
    assert "Full excerpts:" in with_quotes
    assert "Full excerpts:" not in without_quotes


def test_query_auto_mode_detects_locate(built_skill: str) -> None:
    out = query_skill(built_skill, "which file documents authentication", mode="auto")
    assert "Relevant files:" in out


def test_query_no_match_returns_grounding_notice(built_skill: str) -> None:
    out = query_skill(built_skill, "quantum chromodynamics lattice gauge theory zzzz", mode="answer")
    assert "No relevant documentation" in out


def test_query_missing_skill_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        query_skill(str(tmp_path / "nope"), "anything")


# ------------------------------------------------------------------ #
# build_skill options
# ------------------------------------------------------------------ #


def test_build_overwrite_false_raises_on_existing(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# A\ncontent here")
    out_root = str(tmp_path / "out")
    build_skill([str(docs)], "dup-skill", output_root=out_root)
    with pytest.raises(FileExistsError):
        build_skill([str(docs)], "dup-skill", output_root=out_root, overwrite=False)


def test_build_overwrite_true_replaces(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# A\nfirst content")
    out_root = str(tmp_path / "out")
    build_skill([str(docs)], "ow-skill", output_root=out_root)
    (docs / "b.md").write_text("# B\nsecond content")
    meta = build_skill([str(docs)], "ow-skill", output_root=out_root, overwrite=True)
    assert meta["total_chunks"] >= 2


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation may need privileges on Windows")
def test_build_skips_symlinked_files(tmp_path: Path) -> None:
    real = tmp_path / "outside"
    real.mkdir()
    (real / "secret.md").write_text("# Secret\nthis should not be indexed")

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "real.md").write_text("# Real\nlegitimate indexed content")
    (docs / "link.md").symlink_to(real / "secret.md")

    meta = build_skill([str(docs)], "sym-skill", output_root=str(tmp_path / "out"))
    indexed = [Path(p).name for p in meta["indexed_files"]]
    assert "real.md" in indexed
    assert "link.md" not in indexed  # symlink skipped


def test_build_max_chars_per_file_truncates(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    # A marker beyond the char cap must not appear in any indexed chunk.
    body = "alpha " * 100 + "UNIQUEMARKER"
    (docs / "big.md").write_text("# Big\n" + body)
    meta = build_skill(
        [str(docs)],
        "cap-skill",
        output_root=str(tmp_path / "out"),
        max_chars_per_file=50,
    )
    chunks_file = Path(meta["skill_dir"]) / "chunks.jsonl"
    contents = chunks_file.read_text()
    assert "UNIQUEMARKER" not in contents


def test_build_empty_docs_raises(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "ignored.unknownext").write_text("not an indexable extension")
    with pytest.raises(ValueError, match="No indexable"):
        build_skill([str(docs)], "empty-skill", output_root=str(tmp_path / "out"))


def test_build_writes_manifest_with_bundle_version(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# A\nsome content for the manifest test")
    meta = build_skill([str(docs)], "manifest-skill", output_root=str(tmp_path / "out"))
    manifest = json.loads((Path(meta["skill_dir"]) / "manifest.json").read_text())
    assert manifest["version"] == "3.0.0"
    assert manifest["created_by"] == "lazytools.skills"
    assert manifest["total_chunks"] == meta["total_chunks"]
