"""Architectural guard: ``lazytools`` must never import ``lazypulse``.

Allowed:   lazytools -> lazybridge
Forbidden: lazytools -> lazypulse (and any orchestration concept).

This is the boundary that keeps the dependency arrows acyclic — enforced by
test, not convention.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "lazytools"


def _offending_imports(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "lazypulse" or module.startswith("lazypulse."):
                offenders.append(f"{path}:{node.lineno} from {module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "lazypulse" or alias.name.startswith("lazypulse."):
                    offenders.append(f"{path}:{node.lineno} import {alias.name}")
    return offenders


def test_lazytools_never_imports_lazypulse() -> None:
    offenders: list[str] = []
    for py in SRC.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        offenders.extend(_offending_imports(py))
    assert not offenders, "lazytools must not import lazypulse:\n" + "\n".join(offenders)
