"""Figures in LazyReport: FigureBlock, ArtifactResolvers, HTML embedding."""

from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from lazytools.report import (
    ArtifactResolvers,
    FigureBlock,
    Memo,
    ReportFiles,
    ReportTools,
    Section,
    render_html,
    render_markdown,
)
from lazytools.report.artifacts import sniff_image_mime, split_ref

# A valid 1x1 PNG (same payload as contracts/v1 fixtures in market-data-hub).
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
PNG_BYTES = base64.b64decode(PNG_B64)


def _memo_with_figure(ref: str, caption: str = "A tiny chart") -> Memo:
    return Memo(title="T", sections=[Section(title="S", figures=[FigureBlock(ref=ref, caption=caption)])])


# --- models ------------------------------------------------------------------ #


def test_figure_block_accepts_canonical_refs() -> None:
    for ref in ("regimes:plot_1", "crawler:9f8e", "file:C:\\reports\\f.png", f"bytes:{PNG_B64}"):
        assert FigureBlock(ref=ref).ref == ref


def test_figure_block_rejects_malformed_refs() -> None:
    for bad in ("no-colon", "Upper:key", ":nokey", "scheme:"):
        with pytest.raises(ValidationError):
            FigureBlock(ref=bad)


# --- artifacts --------------------------------------------------------------- #


def test_split_ref_only_first_colon_separates() -> None:
    assert split_ref("file:C:\\reports\\f.png") == ("file", "C:\\reports\\f.png")


def test_sniff_image_mime() -> None:
    assert sniff_image_mime(PNG_BYTES) == "image/png"
    assert sniff_image_mime(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert sniff_image_mime(b"unknown") == "application/octet-stream"


def test_resolve_bytes_scheme() -> None:
    data, mime = ArtifactResolvers().resolve(f"bytes:{PNG_B64}")
    assert data == PNG_BYTES
    assert mime == "image/png"


def test_resolve_file_scheme(tmp_path) -> None:
    p = tmp_path / "fig.png"
    p.write_bytes(PNG_BYTES)
    data, mime = ArtifactResolvers().resolve(f"file:{p}")
    assert data == PNG_BYTES
    assert mime == "image/png"


def test_file_sandbox_refuses_escape(tmp_path) -> None:
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG_BYTES)
    sandbox = tmp_path / "inner"
    sandbox.mkdir()
    resolvers = ArtifactResolvers(file_base_dir=str(sandbox))
    with pytest.raises(ValueError, match="outside the sandbox"):
        resolvers.resolve(f"file:{outside}")
    with pytest.raises(ValueError, match="outside the sandbox"):
        resolvers.resolve("file:../outside.png")


def test_unregistered_scheme_fails_loudly() -> None:
    with pytest.raises(ValueError, match="no resolver registered for artifact scheme 'regimes'"):
        ArtifactResolvers().resolve("regimes:plot_1")


def test_register_custom_scheme() -> None:
    resolvers = ArtifactResolvers()
    resolvers.register("regimes", lambda key: (PNG_BYTES, "image/png"))
    assert resolvers.resolve("regimes:plot_1") == (PNG_BYTES, "image/png")


# --- render ------------------------------------------------------------------ #


def test_render_html_embeds_figure_as_data_uri() -> None:
    out = render_html(_memo_with_figure(f"bytes:{PNG_B64}"))
    assert f'<img src="data:image/png;base64,{PNG_B64}" alt="A tiny chart">' in out
    assert "<figcaption>A tiny chart</figcaption>" in out


def test_render_html_figure_is_deterministic() -> None:
    memo = _memo_with_figure(f"bytes:{PNG_B64}")
    assert render_html(memo) == render_html(memo.model_copy(deep=True))


def test_render_html_escapes_caption() -> None:
    out = render_html(_memo_with_figure(f"bytes:{PNG_B64}", caption='<img onerror="p()">'))
    assert '<img onerror' not in out  # no live markup from the caption
    assert "&lt;img onerror=&quot;p()&quot;&gt;" in out


def test_render_html_rejects_non_image(tmp_path) -> None:
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    with pytest.raises(ValueError, match="non-image or unsafe MIME"):
        render_html(_memo_with_figure(f"file:{p}"))


def test_render_html_rejects_mime_attribute_injection() -> None:
    # A resolver MIME can come from an untrusted remote Content-Type (crawler:).
    # It must never break out of the src="data:..." attribute.
    resolvers = ArtifactResolvers()
    resolvers.register("evil", lambda key: (PNG_BYTES, 'image/png" onerror="alert(1)'))
    memo = _memo_with_figure("evil:x")
    with pytest.raises(ValueError, match="non-image or unsafe MIME"):
        render_html(memo, artifacts=resolvers)


def test_render_markdown_degrades_figure_to_text() -> None:
    out = render_markdown(_memo_with_figure("regimes:plot_1"))
    assert "_Figure: A tiny chart (regimes:plot_1)_" in out
    out_nocap = render_markdown(_memo_with_figure("regimes:plot_1", caption=""))
    assert "_Figure: (regimes:plot_1)_" in out_nocap


# --- ReportTools -------------------------------------------------------------- #


def test_save_memo_html_writes_full_embedded_html(tmp_path) -> None:
    # The render-and-save tool must persist the COMPLETE self-contained HTML —
    # with the base64 figure — without the bytes ever leaving the process. This
    # is what lets an agent produce an image report (render_memo_html's string
    # is far too large to route back through the model into save_report).
    resolvers = ArtifactResolvers()
    resolvers.register("regimes", lambda key: (PNG_BYTES, "image/png"))
    tools = {
        t.name: t
        for t in ReportTools(
            artifacts=resolvers, files=ReportFiles(base_dir=str(tmp_path))
        ).as_tools()
    }
    assert "save_memo_html" in tools and "save_memo_markdown" in tools
    payload = _memo_with_figure("regimes:plot_1").model_dump(mode="json")

    path = tools["save_memo_html"].run_sync(memo=payload, filename="r.html")
    written = (tmp_path / "r.html").read_text(encoding="utf-8")
    assert path.endswith("r.html")
    assert written.rstrip().endswith("</html>")               # complete document
    assert f"data:image/png;base64,{PNG_B64}" in written      # full figure embedded

    md_path = tools["save_memo_markdown"].run_sync(memo=payload, filename="r.md")
    assert md_path.endswith("r.md") and (tmp_path / "r.md").exists()


def test_report_tools_without_files_has_no_save_tools() -> None:
    names = {t.name for t in ReportTools().as_tools()}
    assert names == {"render_memo", "render_memo_html"}


def test_report_tools_renders_figures_with_custom_registry() -> None:
    resolvers = ArtifactResolvers()
    resolvers.register("regimes", lambda key: (PNG_BYTES, "image/png"))
    tools = {t.name: t for t in ReportTools(artifacts=resolvers).as_tools()}
    payload = _memo_with_figure("regimes:plot_1").model_dump(mode="json")
    out = tools["render_memo_html"].run_sync(memo=payload)
    assert f'data:image/png;base64,{PNG_B64}' in out
    # markdown tool stays text-only and needs no resolver
    assert "(regimes:plot_1)" in tools["render_memo"].run_sync(memo=payload)
