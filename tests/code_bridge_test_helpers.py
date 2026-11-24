"""Cross-platform helpers for code-bridge subprocess tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def write_python_executable(
    directory: Path,
    name: str,
    source: str,
    *,
    platform: str | None = None,
) -> Path:
    """Write a Python fake executable and return its platform launcher.

    Windows cannot execute extensionless shebang scripts.  Keep the Python
    implementation separate there and invoke it through a ``.cmd`` shim.
    ``platform`` is injectable so the Windows output is covered on Linux CI.
    """
    directory.mkdir(parents=True, exist_ok=True)
    platform = os.name if platform is None else platform
    if platform == "nt":
        script = directory / f"{name}.py"
        script.write_text(source, encoding="utf-8")
        launcher = directory / f"{name}.cmd"
        launcher.write_text(
            f'@echo off\n"{sys.executable}" "%~dp0{name}.py" %*\n',
            encoding="utf-8",
        )
        return launcher

    launcher = directory / name
    launcher.write_text(f"#!{sys.executable}\n" + source, encoding="utf-8")
    launcher.chmod(0o755)
    return launcher


def preserve_windows_shell_environment(env: dict[str, str]) -> dict[str, str]:
    """Add Windows shell-discovery variables to an intentionally small env."""
    for name in ("COMSPEC", "SYSTEMROOT"):
        if name in os.environ:
            env.setdefault(name, os.environ[name])
    return env


PUBLICATION_REMOTE = "https://github.com/example/publication.git"
FAKE_GH = """
import json, os, subprocess, sys
from pathlib import Path

state_path = Path(os.environ["FAKE_GH_STATE"])
state = json.loads(state_path.read_text()) if state_path.exists() else {"prs": [], "comments": []}
args = sys.argv[1:]
if args[0] == "repo":
    assert args == ["repo", "view", "github.com/example/publication", "--json", "defaultBranchRef"]
    branch = os.environ.get("FAKE_GH_DEFAULT_BRANCH", "main")
    ref = None if os.environ.get("FAKE_GH_DEFAULT_BRANCH_MISSING") else {"name": branch}
    print(json.dumps({"defaultBranchRef": ref}))
    sys.exit(0)
assert args[0] == "pr"
assert args[args.index("--repo") + 1] == "github.com/example/publication"
if args[1] == "create":
    assert "--draft" in args
    head = args[args.index("--head") + 1]
    base = args[args.index("--base") + 1]
    def sha(branch):
        return subprocess.check_output(
            ["git", "--git-dir", os.environ["FAKE_GH_BARE"], "rev-parse", "refs/heads/" + branch],
            text=True,
        ).strip()
    number = len(state["prs"]) + 1
    url = f"https://github.com/example/publication/pull/{number}"
    state["prs"].append({
        "number": number, "url": url, "headRefName": head, "baseRefName": base,
        "headRefOid": os.environ.get("FAKE_GH_HEAD") or sha(head), "baseRefOid": sha(base),
        "isDraft": not os.environ.get("FAKE_GH_NON_DRAFT"), "isCrossRepository": False, "state": "OPEN",
    })
    print(url)
elif args[1] == "view":
    print(json.dumps(state["prs"][int(args[2]) - 1]))
elif args[1] == "comment":
    assert "--body-file" in args and args[args.index("--body-file") + 1] == "-"
    state["comments"].append(sys.stdin.read())
else:
    raise AssertionError(args)
state_path.write_text(json.dumps(state))
"""
