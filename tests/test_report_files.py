"""ReportFiles.save_report: sandboxed write, filename hardening, path return."""

from __future__ import annotations

from pathlib import Path

import pytest

from lazytools.report import ReportFiles


def test_provider_is_tool_provider() -> None:
    assert ReportFiles()._is_lazy_tool_provider is True


def test_as_tools_exposes_save_report() -> None:
    assert {t.name for t in ReportFiles().as_tools()} == {"save_report"}


def test_save_report_writes_under_base_and_returns_path(tmp_path: Path) -> None:
    files = ReportFiles(base_dir=tmp_path / "out")
    path = files._save_report("q3.md", "# Q3\nbody")
    p = Path(path)
    assert p.is_file()
    assert p.parent == (tmp_path / "out").resolve()
    assert p.read_text(encoding="utf-8") == "# Q3\nbody"


def test_traversal_is_reduced_to_basename(tmp_path: Path) -> None:
    files = ReportFiles(base_dir=tmp_path / "out")
    path = Path(files._save_report("../../etc/passwd.md", "x"))
    assert path.parent == (tmp_path / "out").resolve()
    assert path.name == "passwd.md"


def test_refuses_symlink_escape(tmp_path: Path) -> None:
    base = tmp_path / "reports"
    base.mkdir()
    target = tmp_path / "outside.md"
    try:
        # pre-existing symlink escaping the sandbox. On a standard Windows
        # account, creating symlinks requires a privilege the local test run
        # may not have (audit CA-10): skip there — Linux CI always runs this.
        (base / "r.md").symlink_to(target)
    except OSError as exc:  # pragma: no cover - Windows without the privilege
        pytest.skip(f"cannot create symlinks on this account: {exc}")
    files = ReportFiles(base_dir=base)
    with pytest.raises(ValueError, match=r"symlink|sandbox"):
        files._save_report("r.md", "pwned")
    assert not target.exists()


def test_disallowed_extension_gets_md(tmp_path: Path) -> None:
    files = ReportFiles(base_dir=tmp_path)
    path = Path(files._save_report("report.exe", "x"))
    assert path.name.endswith(".md")


def test_unsafe_chars_collapsed(tmp_path: Path) -> None:
    files = ReportFiles(base_dir=tmp_path)
    path = Path(files._save_report("my report (final).md", "x"))
    assert path.is_file()
    assert path.suffix == ".md"
    assert not (set(" ()") & set(path.name))  # no spaces or parens survive
