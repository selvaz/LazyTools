"""doc_skills: build a BM25 skill bundle and query it (stdlib only)."""

from __future__ import annotations

from pathlib import Path

from lazytools.skills import (
    DocChunk,
    SkillManifest,
    build_skill,
    query_skill,
    skill_builder_tools,
    skill_pipeline,
    skill_tools,
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
