"""Isolated adapter for investmentcommittee's existing production audit.

Run in a bounded child with -B: importing the sibling must not write bytecode
or accidentally select an already imported ``scripts`` package.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
from pathlib import Path


def collect(root: Path, manifest: Path) -> dict:
    original_path = sys.path[:]
    try:
        return _collect(root, manifest)
    finally:
        sys.path[:] = original_path


def _collect(root: Path, manifest: Path) -> dict:
    script = root / "investmentcommittee" / "scripts" / "ecosystem_audit.py"
    sys.path.insert(0, str(script.parent))
    spec = importlib.util.spec_from_file_location("ecosystem_audit", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    old_root = module.GITHUB if hasattr(module, "GITHUB") else root
    module.GITHUB = root
    if hasattr(module, "APP_RUNTIMES"):
        module.APP_RUNTIMES = tuple(root / p.relative_to(old_root) if p.is_relative_to(old_root) else p
                                    for p in module.APP_RUNTIMES)
    runtimes = []
    environments = {}

    def tag_reader(path):
        tag = module.exact_tag(path)
        runtimes.append({"path": str(path), "tag": tag})
        return tag

    def version_reader(python, names):
        versions = module.installed_versions(python, names)
        environments[str(python)] = versions
        return versions

    problems = module.drift(manifest_path=manifest, tag_reader=tag_reader, version_reader=version_reader)
    news_origin = {"checked": False}
    # The watchdog calls this existing helper separately from drift(). Reuse the
    # same declarative DB registry and policy, never open a DB ourselves.
    if hasattr(module, "news_origin_drift"):
        try:
            config = root / "investmentcommittee" / "config"
            policy = tomllib.loads((config / "job_health.toml").read_text(encoding="utf-8"))["watchdog"]
            databases = tomllib.loads((config / "databases.toml").read_text(encoding="utf-8"))["databases"]
            news = Path(databases["crawler_raw"]["path"])
            cutover = policy.get("news_origin_cutover", "")
            problems.extend(module.news_origin_drift(news, cutover))
            news_origin = {"checked": True, "path": str(news), "cutover": cutover}
        except Exception as exc:
            problems.append(f"news origin: cannot check configuration ({type(exc).__name__})")
    return {"problems": problems, "runtimes": runtimes, "environments": environments,
            "news_origin": news_origin, "source": str(script)}


if __name__ == "__main__":
    print(json.dumps(collect(Path(sys.argv[1]), Path(sys.argv[2]))))
