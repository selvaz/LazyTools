"""Bounded, syntactic Python import evidence across configured Git repositories.

An import is a likely dependency, not proof of behavioral compatibility.
Only tracked, in-root source files are inspected; skipped files are recorded.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from .core import _git

_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")


def python_import_evidence(
    root: Path,
    prefix: str,
    *,
    max_files: int = 1_000,
    max_matches: int = 40,
    max_file_bytes: int = 512_000,
) -> dict[str, Any]:
    """Find direct 'import foo' / 'from foo import bar' occurrences.

    Completeness is explicitly false when limits or source parsing make
    the absence of imports unreliable. Dynamic imports are never covered.
    """
    if not isinstance(prefix, str) or not _MODULE.fullmatch(prefix):
        raise ValueError("import_prefix must be a dotted Python module name")
    raw = _git(root, "ls-files", "-z", "--", "*.py")
    paths = [path.decode("utf-8", errors="replace") for path in raw.split(b"\0") if path]
    incomplete = len(paths) > max_files
    skipped = 0
    inspected = 0
    matches: list[dict[str, Any]] = []
    for relative in paths[:max_files]:
        target = root / relative
        try:
            resolved = target.resolve(strict=True)
            if not resolved.is_relative_to(root) or target.is_symlink():
                skipped += 1
                incomplete = True
                continue
            if resolved.stat().st_size > max_file_bytes:
                skipped += 1
                incomplete = True
                continue
            tree = ast.parse(resolved.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, SyntaxError, ValueError):
            skipped += 1
            incomplete = True
            continue
        inspected += 1
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            for module in modules:
                if module == prefix or module.startswith(prefix + "."):
                    matches.append({
                        "path": relative,
                        "line": getattr(node, "lineno", 0),
                        "module": module,
                        "kind": "import" if isinstance(node, ast.Import) else "from_import",
                    })
                    if len(matches) >= max_matches:
                        incomplete = True
                        break
            if len(matches) >= max_matches:
                break
        if len(matches) >= max_matches:
            break
    return {
        "language": "python",
        "import_prefix": prefix,
        "matches": matches,
        "files_tracked": len(paths),
        "files_scanned": inspected,
        "files_skipped": skipped,
        "incomplete": incomplete,
        "limitation": "Static absolute Python imports only; dynamic/relative imports are not resolved",
    }
