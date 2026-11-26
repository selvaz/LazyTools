"""Rename locked ``lazytools-*.exe`` entry points so ``pip install -e`` can replace them.

On Windows a running process (e.g. a bridge job) locks its console-script exe,
which makes pip fail after it has already removed the old metadata. Windows
allows renaming a running exe only if it is not opened without share-delete, so
we probe each file for write access and rename those that fail.

Usage: python tools/rename_locked_entrypoints.py <Scripts dir>
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path


def is_locked(path: Path) -> bool:
    try:
        with open(path, "r+b"):
            return False
    except OSError:
        return True


def rename_locked(scripts_dir: Path) -> list[tuple[Path, Path]]:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    renamed = []
    for exe in sorted(Path(scripts_dir).glob("lazytools-*.exe")):
        if is_locked(exe):
            target = exe.with_name(f"{exe.name}.old-{stamp}")
            exe.rename(target)
            renamed.append((exe, target))
    return renamed


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(__doc__)
        return 2
    for src, dst in rename_locked(Path(args[0])):
        print(f"renamed {src.name} -> {dst.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
