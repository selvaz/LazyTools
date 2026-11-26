"""Read the capabilities catalog and inspect the code bridge command list."""

import argparse
import re
from pathlib import Path


def catalog_entries(path: str | Path) -> list[dict[str, str]]:
    """Read capability and provider rows in document order, preserving inline Markdown."""
    entries: list[dict[str, str]] = []
    section = ""
    header: list[str] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("## "):
            section = line.removeprefix("## ").strip()

        if not (line.startswith("|") and line.endswith("|")):
            header = []
            continue

        cells = [cell.strip().replace(r"\|", "|") for cell in re.split(r"(?<!\\)\|", line[1:-1])]
        if cells[:2] in (["Capability", "How to call"], ["Provider id", "Tools"]):
            header = cells
            continue
        if len(cells) != len(header) or "Status" not in header:
            continue
        if all(re.fullmatch(r":?-+:?", cell) for cell in cells):
            continue

        entries.append({
            "section": section,
            "capability": cells[0],
            "call": cells[1],
            "status": cells[header.index("Status")],
        })
    return entries


def bridge_subcommands() -> list[str]:
    """Return the bridge parser's top-level command names in alphabetical order."""
    from lazytools.code_bridge.cli import build_parser

    parser = build_parser()
    subcommands = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    return sorted(subcommands.choices)
