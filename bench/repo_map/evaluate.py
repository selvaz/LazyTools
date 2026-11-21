"""Reproducible, read-only RepoMap versus CodeGraphContext smoke benchmark.

Scope: exact-symbol-to-definition-file retrieval on public LazyBridge/LazyTools.
Not an end-to-end coding/LLM or token benchmark. Backend errors are never scored
as misses. Results include repository SHAs, tool versions and raw excerpts.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time

ROOT = Path.cwd()
OUT = ROOT / "repo-map-results.json"
REPOS = {
    "LazyBridge": ROOT / "_benchmark_checkouts" / "LazyBridge",
    "LazyTools": ROOT / "_benchmark_checkouts" / "LazyTools",
}
# Gold files and symbols checked against checkout AST before running a query.
# The exercise tests file localization, not issue repair or natural-language search.
CASES = [
    ("LazyBridge", "Agent", "lazybridge/agent.py"),
    ("LazyBridge", "Plan", "lazybridge/engines/plan/_plan.py"),
    ("LazyBridge", "Envelope", "lazybridge/envelope.py"),
    ("LazyBridge", "ClaudeCodeEngine", "lazybridge/engines/claude_code/engine.py"),
    ("LazyBridge", "CodexEngine", "lazybridge/engines/codex/engine.py"),
    ("LazyBridge", "DurableBlackboard", "lazybridge/ext/planners/durable_blackboard.py"),
    ("LazyBridge", "run_coroutine_blocking", "lazybridge/_asyncbridge.py"),
    ("LazyBridge", "Tool", "lazybridge/tools.py"),
    ("LazyBridge", "translate_model", "lazybridge/model_tiers.py"),
    ("LazyBridge", "claude_tier", "lazybridge/model_tiers.py"),
    ("LazyTools", "StudioTools", "src/lazytools/mcp_server/studio.py"),
    ("LazyTools", "expand_tools", "src/lazytools/mcp_server/server.py"),
    ("LazyTools", "build_server", "src/lazytools/mcp_server/server.py"),
    ("LazyTools", "RegistryTools", "src/lazytools/registry/tools.py"),
    ("LazyTools", "WorkspaceTools", "src/lazytools/workspace.py"),
    ("LazyTools", "resolve_db", "src/lazytools/registry/db.py"),
    ("LazyTools", "search_everywhere", "src/lazytools/registry/router.py"),
    ("LazyTools", "ReportTools", "src/lazytools/report/tools.py"),
    ("LazyTools", "DataHubTools", "src/lazytools/connectors/datahub/tools.py"),
    ("LazyTools", "is_unsafe_tool_name", "src/lazytools/mcp_server/server.py"),
    ("LazyTools", "_regime_db_path", "src/lazytools/mcp_server/providers.py"),
    ("LazyTools", "_artifact_register", "src/lazytools/registry/tools.py"),
]

def run(args: list[str], cwd: Path, timeout: int = 150, env: dict | None = None) -> dict:
    start = time.perf_counter()
    try:
        p = subprocess.run(args, cwd=cwd, text=True, capture_output=True,
                           timeout=timeout, env=env, check=False)
        return {"ok": p.returncode == 0, "exit": p.returncode,
                "seconds": round(time.perf_counter()-start, 3),
                "stdout": p.stdout[:120_000], "stderr": p.stderr[:5000]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "exit": -1, "seconds": round(time.perf_counter()-start, 3),
                "stdout": "", "stderr": str(exc)[:2500]}

def gold_is_defined(repo: Path, symbol: str, path: str) -> bool:
    target = repo / path
    if not target.is_file():
        return False
    try:
        tree = ast.parse(target.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, SyntaxError):
        return False
    return any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
               and n.name == symbol for n in ast.walk(tree))

PATH_PATTERN = re.compile(r"(?:[A-Za-z]:)?(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.py")

def found_paths(text: str, repo: Path) -> list[str]:
    text = text.replace("\\\\", "/").replace("\\", "/")
    found = []
    for match in PATH_PATTERN.findall(text):
        path = match.lstrip("./")
        # Strip checkout prefix or other absolute directories while preserving
        # a suffix that can be matched against gold repo-relative file names.
        root = str(repo).replace("\\", "/").lstrip("/")
        if root in path:
            path = path.split(root, 1)[-1].lstrip("/")
        if path not in found:
            found.append(path)
    return found

def score(paths: list[str], gold: str) -> dict:
    # The ordered matches are from each tool's result, not the source checkout.
    ranks = [i + 1 for i, p in enumerate(paths) if p == gold or p.endswith("/" + gold)]
    return {"rank": min(ranks) if ranks else None, "hit1": bool(ranks and min(ranks)==1),
            "hit5": bool(ranks and min(ranks)<=5)}

def short_run(r: dict) -> dict:
    return {k: r[k] for k in ("ok", "exit", "seconds", "stderr")} | {
        "sample": r["stdout"][:850], "tail": r["stdout"][-1800:]
    }

def main() -> int:
    environment = dict(os.environ)
    environment["CI"] = "true"
    environment["REPOMAP_NO_STAR_HINT"] = "1"
    # Keep dependencies in separate per-job locations; never touch user data.
    environment["REPOMAP_CACHE_DIR"] = str(ROOT / "_benchmark_cache")
    versions = {}
    for tool, command in {
        "repomap": ["repomap", "version"],
        "codegraphcontext": ["cgc", "version"],
        "ripgrep": ["rg", "--version"],
    }.items():
        versions[tool] = short_run(run(command, ROOT, timeout=30, env=environment))
    result = {"protocol": "exact symbol file localization; no LLM calls",
              "versions": versions, "repos": {}, "cases": [], "summary": {}}
    for label, repo in REPOS.items():
        sha = run(["git", "rev-parse", "HEAD"], repo, timeout=30)
        cgc_root = ROOT / "_benchmark_cgc" / label
        # cgc global options must precede the subcommand.
        experiments = {
            "repomap": ["repomap", "index", ".", "--json"],
            "codegraphcontext": ["cgc", "--database", "kuzudb",
                                 "--db-path", str(cgc_root), "index", "."],
        }
        indexing = {}
        for tool, args in experiments.items():
            indexing[tool] = short_run(run(args, repo, timeout=600, env=environment))
        diagnostics = {}
        db_args = ["cgc", "--database", "kuzudb", "--db-path", str(cgc_root)]
        for check, command in {
            "list": db_args + ["list"],
            "stats": db_args + ["stats"],
            "cypher_count": db_args + ["query", "MATCH (n) RETURN count(n) AS total"],
            "find_fuzzy": db_args + ["find", "name", "Agent", "--fuzzy"],
        }.items():
            diagnostics[check] = short_run(run(command, repo, timeout=45, env=environment))
        result["repos"][label] = {
            "sha": sha["stdout"].strip(), "index": indexing,
            "diagnostics": diagnostics, "valid_cases": 0,
        }
        for name, symbol, gold in CASES:
            if name != label:
                continue
            if not gold_is_defined(repo, symbol, gold):
                result["cases"].append({"repo": label, "symbol": symbol,
                                        "gold": gold, "valid": False,
                                        "reason": "gold symbol not found in checked-out file"})
                continue
            result["repos"][label]["valid_cases"] += 1
            commands = {
                "repomap": ["repomap", "search", symbol, "--limit", "10", "--json"],
                "codegraphcontext": ["cgc", "--database", "kuzudb",
                                    "--db-path", str(cgc_root),
                                    "find", "name", symbol, "--no-fuzzy"],
                "ripgrep": ["rg", "-l", "-F", "--glob", "*.py", symbol, "."],
            }
            row = {"repo": label, "symbol": symbol, "gold": gold, "valid": True,
                   "results": {}}
            for tool, args in commands.items():
                if tool != "ripgrep" and not indexing[tool]["ok"]:
                    row["results"][tool] = {"usable": False, "reason": "index failed"}
                    continue
                r = run(args, repo, timeout=120, env=environment)
                files = found_paths(r["stdout"], repo)
                # A 0 exit but no recognizable file paths is a parser-protocol
                # failure, NOT a scored retrieval miss.
                usable = r["ok"] and bool(files)
                row["results"][tool] = {
                    "usable": usable, "seconds": r["seconds"],
                    "paths": files[:15], "score": score(files, gold) if usable else None,
                    "exit": r["exit"],
                    "sample": r["stdout"][:750],
                    "stderr": r["stderr"][:450] if not r["ok"] else "",
                }
            result["cases"].append(row)
    for tool in ("repomap", "codegraphcontext", "ripgrep"):
        rows = [x["results"].get(tool, {}) for x in result["cases"] if x["valid"]]
        usable = [x for x in rows if x.get("usable")]
        hit5 = sum(x["score"]["hit5"] for x in usable)
        hit1 = sum(x["score"]["hit1"] for x in usable)
        result["summary"][tool] = {
            "valid_gold_tasks": len(rows), "usable_queries": len(usable),
            "hit_at_1": hit1, "hit_at_5": hit5,
            "hit_at_5_rate_usable_only": round(hit5/len(usable), 3) if usable else None,
            "query_median_s": round(statistics.median(x["seconds"] for x in usable), 3)
                              if usable else None,
        }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"versions": {k: v["sample"][:75] for k,v in versions.items()},
                      "repos": result["repos"], "summary": result["summary"]},
                     indent=2, ensure_ascii=False))
    print(f"Detailed results: {OUT}")
    return 0 if all(v["usable_queries"] >= 15 for k,v in result["summary"].items()
                    if k != "ripgrep") else 1

if __name__ == "__main__":
    sys.exit(main())
