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

# Curated from public PR titles in these repositories. No source paths are
# included in the queries. This is a small exploratory held-in sample, not SWE-bench.
ISSUE_CASES = [["LazyTools","report stuck nested Studio tool execution clearly to caller",["src/lazytools/mcp_server/studio.py"],323],["LazyTools","read-only MCP tools missing annotations are classified as write",["src/lazytools/mcp_server/providers.py","src/lazytools/mcp_server/server.py"],322],["LazyTools","propagate MCP tool annotations through client transports",["src/lazytools/connectors/mcp/_sdk.py","src/lazytools/connectors/mcp/server.py","src/lazytools/connectors/mcp/transports.py"],319],["LazyTools","stdio MCP server hangs when scientific modules import on worker threads",["src/lazytools/mcp_server/__main__.py"],318],["LazyTools","warn when code bridge launches from a git branch behind upstream",["src/lazytools/code_bridge/_freshness.py","src/lazytools/code_bridge/cli.py"],289],["LazyTools","wait for first completed agent job among multiple active jobs and provide machine readable ids",["src/lazytools/code_bridge/cli.py"],304],["LazyTools","recover cloud task polling from transient rate limits without marking job failed",["src/lazytools/code_bridge/_cloud.py","src/lazytools/code_bridge/_cloud_land.py","src/lazytools/code_bridge/cli.py"],307],["LazyTools","fallback to local coding agent execution if cloud environment mapping does not exist",["src/lazytools/code_bridge/_cloud.py","src/lazytools/code_bridge/_cloud_place.py","src/lazytools/code_bridge/_mcp.py","src/lazytools/code_bridge/cli.py"],302],["LazyBridge","report cached input token usage for Claude Code and Codex models",["lazybridge/engines/claude_code/engine.py","lazybridge/engines/codex/engine.py","lazybridge/envelope.py"],234],["LazyBridge","nested plan execution loses incoming envelope context for child steps",["lazybridge/agent.py","lazybridge/engines/plan/_plan.py"],227],["LazyBridge","load provider SDK packages lazily so initial import is fast",["lazybridge/core/providers/__init__.py"],222],["LazyBridge","synchronous agent run hangs when event loop executor thread shutdown begins",["lazybridge/_asyncbridge.py"],213],["LazyBridge","Claude Code SDK stalls and timeout cleanup must kill subprocess tree",["lazybridge/engines/claude_code/engine.py","lazybridge/engines/claude_code/sdk_client.py"],215],["LazyBridge","Codex App Server streaming idle timeout is not enforced between events",["lazybridge/engines/codex/app_server.py","lazybridge/engines/codex/engine.py"],216],["LazyBridge","map bare Claude aliases to currently supported model ids",["lazybridge/model_tiers.py"],220],["LazyBridge","record actual model selected by coding engine rather than requested alias",["lazybridge/engines/claude_code/engine.py","lazybridge/engines/codex/engine.py","lazybridge/envelope.py"],221]]

def run(args: list[str], cwd: Path, timeout: int = 150, env: dict | None = None) -> dict:
    start = time.perf_counter()
    try:
        p = subprocess.run(args, cwd=cwd, text=True, capture_output=True,
                           timeout=timeout, env=env, check=False)
        return {"ok": p.returncode == 0, "exit": p.returncode,
                "seconds": round(time.perf_counter()-start, 3),
                "stdout": p.stdout[:120_000], "stderr": p.stderr[:120_000]}
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

def cgc_paths(output: str, repo: Path) -> list[str]:
    """Decode CodeGraphContext 0.6.13 Rich table on stderr (wrapped file paths).

    Each table row can span several physical lines: the path is the third
    column, with later continuation cells carrying its remaining text.
    Do not treat setup messages or ambiguous matches as repository paths.
    """
    found: list[str] = []
    pieces: list[str] = []

    def flush() -> None:
        if pieces:
            for path in found_paths("".join(pieces), repo):
                if path not in found:
                    found.append(path)
            pieces.clear()

    for line in output.splitlines():
        if "│" not in line:
            continue
        cells = line.split("│")
        if len(cells) < 5:
            continue
        name, location = cells[1].strip(), cells[3].strip()
        if name == "Name" or location == "Location":
            continue
        if name:
            flush()
        if location:
            pieces.append(location)
    flush()
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
                if tool == "codegraphcontext":
                    # CGC writes find results to stderr as a Rich table, even
                    # when the command exits successfully. Wrapped paths must
                    # be joined before evaluating rank.
                    files = cgc_paths(r["stderr"], repo)
                    valid_output = bool(re.search(
                        r"Found [0-9]+ match|No match|No .*found", r["stderr"], re.I))
                elif tool == "repomap":
                    try:
                        json.loads(r["stdout"])
                        valid_output = True
                    except (TypeError, ValueError):
                        valid_output = False
                    files = found_paths(r["stdout"], repo) if valid_output else []
                else:
                    files = found_paths(r["stdout"], repo)
                    valid_output = r["exit"] in (0, 1)
                # A structurally valid empty response counts as a miss.
                # A failed parser/protocol still counts as missing evidence.
                usable = valid_output and (r["ok"] or
                                             (tool == "ripgrep" and r["exit"] == 1))
                row["results"][tool] = {
                    "usable": usable, "seconds": r["seconds"],
                    "paths": files[:15], "score": score(files, gold) if usable else None,
                    "exit": r["exit"],
                    "sample": (r["stderr"] if tool == "codegraphcontext"
                               else r["stdout"])[:750],
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
    # Separate exploratory issue-to-code evaluation: repomap's hybrid search
    # accepts natural-language queries. CGC's find name is not a semantic
    # equivalent, so do not present this as a head-to-head accuracy score.
    issue_rows = []
    for label, query, gold, number in ISSUE_CASES:
        repo = REPOS[label]
        missing = [p for p in gold if not (repo / p).is_file()]
        if missing:
            issue_rows.append({"repo": label, "pr": number, "query": query,
                               "valid": False, "missing_gold": missing})
            continue
        call = run(["repomap", "search", query, "--limit", "10", "--json"],
                   repo, timeout=90, env=environment)
        try:
            json.loads(call["stdout"])
            valid_output = call["ok"]
        except (ValueError, TypeError):
            valid_output = False
        paths = found_paths(call["stdout"], repo) if valid_output else []
        ranks = {p: next((i+1 for i, result_path in enumerate(paths)
                          if result_path == p or result_path.endswith("/"+p)), None)
                 for p in gold}
        issue_rows.append({"repo": label, "pr": number, "query": query,
                           "gold": gold, "valid": True, "usable": valid_output,
                           "seconds": call["seconds"], "ranks": ranks,
                           "hit_any_5": any(v is not None and v <= 5 for v in ranks.values()),
                           "hit_all_10": all(v is not None and v <= 10 for v in ranks.values()),
                           "sample": call["stdout"][:500] if not valid_output else "",
                           "stderr": call["stderr"][:400]})
    valid_issues = [x for x in issue_rows if x.get("valid")]
    usable_issues = [x for x in valid_issues if x.get("usable")]
    result["issue_localization"] = {
        "method": "Repomap semantic search on curated public PR titles; no equivalent CGC semantic run",
        "cases": issue_rows,
        "summary": {"valid": len(valid_issues), "usable": len(usable_issues),
                    "any_gold_in_top5": sum(x["hit_any_5"] for x in usable_issues),
                    "all_gold_in_top10": sum(x["hit_all_10"] for x in usable_issues),
                    "median_query_s": round(statistics.median(x["seconds"] for x in usable_issues), 3)
                                      if usable_issues else None},
    }
    # Check optional multi-repo map and token-budget behavior without
    # asserting a specific output structure or changing production code.
    workspace = ",".join(str(REPOS[n]) for n in ("LazyBridge", "LazyTools"))
    multi = {}
    for label, args in {
        "budgeted_map": ["repomap", "map", "--workspace", workspace,
                         "--tokens", "1200", "--json"],
        "cross_repo_search": ["repomap", "search",
                              "MCP tools and agent integration", "--workspace", workspace,
                              "--limit", "10", "--json"],
    }.items():
        # Run from one of the workspace members: otherwise the implicit
        # current repository is included as a third, unrelated root.
        call = run(args, REPOS["LazyBridge"], timeout=120, env=environment)
        try:
            payload = json.loads(call["stdout"]) if call["ok"] else None
        except (TypeError, ValueError):
            payload = None
        repo_entries = [item.get("repo") for item in payload.get("repos", [])] \
            if isinstance(payload, dict) else []
        multi[label] = short_run(call) | {"stdout_bytes": len(call["stdout"]),
                                         "parsed_json": isinstance(payload, dict),
                                         "repo_entries": repo_entries,
                                         "repo_count": len(repo_entries),
                                         "repo_names_present": {
                                             name: name in call["stdout"] for name in REPOS}}
    result["multirepo"] = multi
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"versions": {k: v["sample"][:75] for k,v in versions.items()},
                      "repos": result["repos"], "summary": result["summary"]},
                     indent=2, ensure_ascii=False))
    print(f"Detailed results: {OUT}")
    return 0 if all(v["usable_queries"] >= 15 for k,v in result["summary"].items()
                    if k != "ripgrep") else 1

if __name__ == "__main__":
    sys.exit(main())
