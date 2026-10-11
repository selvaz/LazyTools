"""Read-only API/import audit of every documentation page against this checkout.

This checks import names and statically resolvable call keyword arguments; it
does not execute examples or claim to validate external service behavior.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import re
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def blocks(path: Path) -> list[tuple[str, str]]:
    return [(m[1], textwrap.dedent(m[2])) for m in re.finditer(
        r"^[ \t]*```([^\n]*)\n(.*?)^[ \t]*```", path.read_text(encoding="utf-8"), re.M | re.S
    )]


def audit(path: Path) -> tuple[int, list[str]]:
    problems: list[str] = []
    checked = 0
    bindings = {}
    for lang, code in blocks(path):
        if lang != "python":
            continue
        try:
            tree = ast.parse(code)
        except SyntaxError:
            # Existing API signatures and explicit fragments are reference,
            # not executable Python. Imports in them can still be checked.
            nodes = []
            lines = code.splitlines()
            for index, line in enumerate(lines):
                if not line.startswith("from "):
                    continue
                for end in range(index + 1, len(lines) + 1):
                    try:
                        statement = ast.parse("\n".join(lines[index:end]))
                    except SyntaxError:
                        continue
                    nodes.extend(statement.body)
                    break
            tree = ast.Module(body=nodes, type_ignores=[])
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if not node.module.startswith(("lazytools", "lazybridge")):
                continue
            try:
                module = importlib.import_module(node.module)
                for alias in node.names:
                    try:
                        obj = getattr(module, alias.name)
                    except AttributeError:
                        obj = importlib.import_module(f"{node.module}.{alias.name}")
                    bindings[alias.asname or alias.name] = obj
                    checked += 1
            except (ImportError, AttributeError) as exc:
                problems.append(f"{node.module}: {exc}")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            obj = bindings.get(node.func.id)
            if obj is None:
                continue
            try:
                sig = inspect.signature(obj)
            except (TypeError, ValueError):
                continue
            if any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
                continue
            for kw in node.keywords:
                if kw.arg and kw.arg not in sig.parameters:
                    problems.append(f"{node.func.id}: unknown keyword {kw.arg}")
    return checked, sorted(set(problems))


def main() -> None:
    total = 0
    for path in [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]:
        count, problems = audit(path)
        total += len(problems)
        print(f"{path.relative_to(ROOT).as_posix()}: {count} imports; " + ("; ".join(problems) or "OK"))
    print(f"API audit: {total} findings")


if __name__ == "__main__":
    main()
