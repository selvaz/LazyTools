"""Check rendered local links/fragments and the four homepage destinations."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1] / "site"


class Page(HTMLParser):
    def __init__(self, path: Path):
        super().__init__()
        self.ids: set[str] = set()
        self.links: list[str] = []
        self.feed(path.read_text(encoding="utf-8"))

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"])
        if tag == "a" and values.get("href"):
            self.links.append(values["href"])


def main() -> None:
    pages = {path.resolve(): Page(path) for path in ROOT.rglob("*.html")}
    checked = 0
    failures = []
    for path, page in pages.items():
        for href in page.links:
            link = urlsplit(href)
            if link.scheme or link.netloc:
                continue
            target = (ROOT / unquote(link.path).lstrip("/") if link.path.startswith("/")
                      else path.parent / unquote(link.path)).resolve() if link.path else path
            if target.is_dir():
                target /= "index.html"
            checked += 1
            if not target.is_file():
                failures.append(f"{path.relative_to(ROOT)}: missing {href}")
            elif link.fragment and target in pages and unquote(link.fragment) not in pages[target].ids:
                failures.append(f"{path.relative_to(ROOT)}: missing fragment {href}")
    for slug in ("code-bridge", "code-support", "projects", "mcp-server"):
        assert (ROOT / slug / "index.html").resolve() in pages
    assert "choose-a-model-from-quota" in pages[(ROOT / "code-bridge" / "index.html").resolve()].ids
    assert "claude-writes-codex-reviews" in pages[(ROOT / "code-bridge" / "index.html").resolve()].ids
    assert not failures, "\n".join(failures)
    print(f"Rendered links: {checked} local links/fragments checked; 0 failures")
    print("All four homepage destinations and the quota-routing anchor exist.")


if __name__ == "__main__":
    main()
