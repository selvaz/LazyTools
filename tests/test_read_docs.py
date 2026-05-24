"""read_docs: multi-format folder reader (txt/md paths need no extra deps)."""

from __future__ import annotations

from pathlib import Path

from lazytools.documents import read_docs_tools, read_folder_docs


def test_reads_single_text_file(tmp_path: Path) -> None:
    f = tmp_path / "note.txt"
    f.write_text("hello world")
    out = read_folder_docs(str(f))
    assert "hello world" in out


def test_reads_folder_of_markdown(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# Alpha\nfirst")
    (tmp_path / "b.md").write_text("# Beta\nsecond")
    out = read_folder_docs(str(tmp_path), extensions="md")
    assert "Alpha" in out and "Beta" in out


def test_extension_filter_excludes_other_formats(tmp_path: Path) -> None:
    (tmp_path / "keep.md").write_text("keep me")
    (tmp_path / "skip.txt").write_text("skip me")
    out = read_folder_docs(str(tmp_path), extensions="md")
    assert "keep me" in out
    assert "skip me" not in out


def test_read_docs_tools_returns_a_tool() -> None:
    tools = read_docs_tools()
    assert len(tools) == 1
    assert tools[0].name == "read_folder_docs"
