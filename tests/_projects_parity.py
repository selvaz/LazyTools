"""Execute frozen LazyCEO functions with mechanism dependencies supplied by tests.

Only local imports are removed; function logic is compiled unchanged. Fixtures
retain exact source and commit provenance, and need no sibling checkout at run time.
"""
from __future__ import annotations

import ast
import json
import textwrap
from pathlib import Path


def original_function(name: str, **dependencies):
    fixture = json.loads((Path(__file__).parent / "fixtures/projects_parity.json").read_text(encoding="utf-8"))
    tree = ast.parse(textwrap.dedent(fixture[name]))

    class RemoveImports(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            return None

    tree = RemoveImports().visit(tree)
    ast.fix_missing_locations(tree)
    namespace = dict(dependencies)
    exec(compile("from __future__ import annotations\n" + ast.unparse(tree), f"LazyCEO@{fixture['source_commit']}:{name}", "exec"), namespace)
    function = next(node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))
    return namespace[function.name]
