"""Small subprocess adapter for the optional ``repomap`` executable."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


class RepoMapError(RuntimeError):
    """RepoMap could not run or returned an invalid response."""


class RepoMapCLI:
    """Invoke RepoMap without allowing its cache to land in a repository."""

    def __init__(self, *, binary: str = "repomap", cache_dir: str | Path | None = None, timeout: float = 120.0) -> None:
        self.binary = binary
        data_dir = Path(os.environ.get("LAZYTOOLS_DATA_DIR", "~/.lazytools")).expanduser()
        self.cache_dir = Path(cache_dir).expanduser() if cache_dir else data_dir / "repomap"
        self.timeout = timeout

    def _run(self, args: list[str], *, repo: Path) -> Any:
        executable = shutil.which(self.binary)
        if executable is None:
            raise RepoMapError(
                "repomap is not installed; install the pinned CLI with "
                "`npm install -g @sylphx/repomap@1.9.0`"
            )
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        # RepoMap uses the platform cache directory. Pinning XDG_CACHE_HOME
        # keeps indexes out of the repository even when the child cwd is it.
        env["XDG_CACHE_HOME"] = str(self.cache_dir.resolve())
        try:
            completed = subprocess.run(
                [executable, *args],
                cwd=repo,
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=self.timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RepoMapError(f"repomap failed: {exc}") from exc
        if completed.returncode:
            detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
            raise RepoMapError(f"repomap failed ({completed.returncode}): {detail}")
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RepoMapError("repomap returned invalid JSON") from exc

    def search(self, query: str, repo: Path, limit: int) -> Any:
        """Return RepoMap's ranked JSON search response."""
        return self._run(["search", query, "--limit", str(limit), "--json"], repo=repo)

    def map(self, repo: Path, tokens: int) -> Any:
        """Return RepoMap's budgeted JSON repository map."""
        return self._run(["map", "--tokens", str(tokens), "--json"], repo=repo)
