"""read_docs: multi-format folder reader (txt/md paths need no extra deps)."""

from __future__ import annotations

from pathlib import Path

import pytest

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


def test_read_docs_tools_returns_a_tool(tmp_path: Path) -> None:
    tools = read_docs_tools(base_dir=str(tmp_path))
    assert len(tools) == 1
    assert tools[0].name == "read_folder_docs"


def test_read_docs_tools_requires_base_dir() -> None:
    # The tool's path argument is LLM-controlled; refusing to build an
    # un-sandboxed tool is the secure default.
    with pytest.raises(ValueError, match="base_dir"):
        read_docs_tools(base_dir="")  # type: ignore[arg-type]


def test_base_dir_rejects_path_escape(tmp_path: Path) -> None:
    (tmp_path / "inside.txt").write_text("ok")
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    # A path resolving outside the sandbox is refused.
    with pytest.raises(PermissionError, match="escapes base_dir"):
        read_folder_docs(str(tmp_path / "inside.txt"), base_dir=str(sandbox))


def test_oversized_file_is_skipped_not_read(tmp_path: Path) -> None:
    big = tmp_path / "big.txt"
    big.write_text("x" * 2000)
    out = read_folder_docs(str(big), base_dir=str(tmp_path), max_file_bytes=1000)
    assert "Skipped" in out
    assert "exceeds max_file_bytes" in out


def test_max_files_truncates_scan(tmp_path: Path) -> None:
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text(f"file {i}")
    out = read_folder_docs(str(tmp_path), extensions="txt", base_dir=str(tmp_path), max_files=2)
    assert "truncated to the first 2 files" in out
