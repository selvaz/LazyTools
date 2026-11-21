"""Public two-repository maintenance demo; local RepoMap 1.9.0 required.

Creates two disposable Git repositories, changes a provider interface,
then uses the public Python Plan pipeline to find the consumer import.
No LLM, MCP, secrets, PR writes, or external repositories are needed.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from lazytools.maintenance import Dependency, MaintenancePipeline, RepoSpec, Workspace


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def commit_initial(root: Path, files: dict[str, str]) -> str:
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Repo Maintenance Demo")
    git(root, "config", "user.email", "demo@example.invalid")
    for name, contents in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "Initial API and consumer")
    return git(root, "rev-parse", "HEAD")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="repo-maintenance-example-") as directory:
        temp = Path(directory)
        provider = temp / "library"
        consumer = temp / "service"
        baseline = commit_initial(provider, {
            "corelib/__init__.py": "",
            "corelib/api.py": "def fetch_data():\n    return 'ok'\n",
        })
        commit_initial(consumer, {
            "service.py": "from corelib.api import fetch_data\n\ndef run():\n    return fetch_data()\n",
        })
        workspace = Workspace(
            repos=(RepoSpec("library", provider), RepoSpec("service", consumer)),
            state_dir=temp / "state",
            dependencies=(
                Dependency("service", "library", import_prefix="corelib"),
            ),
        )
        maintenance = MaintenancePipeline(workspace, use_embeddings=False)
        first = maintenance.execute()
        assert first["verified"] is False
        assert first["cross_repo_candidates"] == []  # inventory is not a change

        (provider / "corelib/api.py").write_text(
            "def renamed_data():\n    return 'ok'\n", encoding="utf-8"
        )
        git(provider, "add", "corelib/api.py")
        git(provider, "commit", "-qm", "Rename public library API")
        update = maintenance.execute(base_refs={"library": baseline})
        assert update["sha"]["library"] != baseline
        assert update["changes"]["library"] == [
            {"status": "M", "path": "corelib/api.py"},
        ]
        relations = update["cross_repo_candidates"]
        assert len(relations) == 1, relations
        assert relations[0]["confidence"] == "direct_import", relations
        assert any(item["path"] == "service.py"
                   for item in relations[0]["import_evidence"]["matches"]), relations
        assert update["verification"]["status"] == "not_run"
        assert update["verified"] is False
        print("Public two-repo maintenance demo passed.")
        print("  library SHA:", update["sha"]["library"])
        print("  service SHA:", update["sha"]["service"])
        print("  candidate:", relations[0]["provider"], "->", relations[0]["consumer"])
        print("  evidence:", relations[0]["import_evidence"]["matches"])
        print("  report:", update["report_json"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
