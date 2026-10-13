import csv
from pathlib import Path

from pypdf import PdfReader


ROOT = Path("research/agency-methodology")
OUT = Path("tmp/pdfs/agency-text")
OUT.mkdir(parents=True, exist_ok=True)

rows = []
for pdf in sorted(ROOT.glob("*/*.pdf")):
    agency = pdf.parent.name
    target_dir = OUT / agency
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{pdf.name}.txt"
    status = "ok"
    texts = []
    try:
        reader = PdfReader(pdf)
        for page_no, page in enumerate(reader.pages, 1):
            try:
                text = page.extract_text() or ""
            except Exception as exc:
                text = f"[PAGE EXTRACTION ERROR: {exc}]"
                status = "partial"
            texts.append(f"\n===== PDF PAGE {page_no} =====\n{text}\n")
        target.write_text("".join(texts), encoding="utf-8")
        pages = len(reader.pages)
        chars = sum(len(t) for t in texts)
    except Exception as exc:
        status = f"error: {exc}"
        pages = 0
        chars = 0
    rows.append((agency, pdf.name, pages, chars, status))
    print(f"{agency}\t{pdf.name}\t{pages}\t{chars}\t{status}", flush=True)

with (OUT / "inventory.csv").open("w", encoding="utf-8", newline="") as fh:
    writer = csv.writer(fh)
    writer.writerow(["agency", "file", "pages", "chars", "status"])
    writer.writerows(rows)
