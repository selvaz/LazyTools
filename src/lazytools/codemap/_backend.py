"""Optional local repomap CLI backend for code intelligence.

No third-party engine is imported, bundled or installed by this module.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Mapping

_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_DRIVE = re.compile(r"^[A-Za-z]:")
_ENV_KEYS = (
    "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "SystemRoot", "WINDIR",
    "TEMP", "TMP", "TMPDIR", "PATHEXT", "APPDATA", "LOCALAPPDATA",
    "XDG_CACHE_HOME", "XDG_DATA_HOME", "LANG", "LC_ALL", "REPOMAP_CACHE_DIR",
)


class RepoMapError(RuntimeError):
    """The external engine could not return a trustworthy answer."""


class RepoMapCLI:
    """Read-only exploration over an explicitly configured repo allowlist.

    Agents can select repository aliases, but cannot add roots or choose
    arbitrary executable flags. The external engine may write its own cache,
    never source files via this adapter.
    """

    def __init__(
        self,
        roots: Mapping[str, str | Path],
        *,
        executable: str = "repomap",
        timeout_seconds: float = 45.0,
        max_result_chars: int = 16_000,
        use_embeddings: bool = True,
    ) -> None:
        if not roots:
            raise ValueError("Configure at least one repository root")
        if not isinstance(executable, str) or not executable.strip():
            raise ValueError("Executable must be a nonempty command or path")
        if not 0 < timeout_seconds <= 600:
            raise ValueError("timeout_seconds must be within (0, 600]")
        if not 500 <= max_result_chars <= 100_000:
            raise ValueError("max_result_chars must be between 500 and 100000")
        checked: dict[str, Path] = {}
        for name, root in roots.items():
            if not isinstance(name, str) or not _NAME.fullmatch(name):
                raise ValueError(f"Invalid repository alias {name!r}")
            path = Path(root).expanduser().resolve(strict=True)
            if not path.is_dir():
                raise ValueError(f"Repository root for {name!r} is not a directory")
            if "," in str(path) or "\n" in str(path):
                raise ValueError("Repository paths may not contain comma or newline")
            checked[name] = path
        self._roots = checked
        self._executable = executable
        self._timeout = timeout_seconds
        self._max_chars = max_result_chars
        self._use_embeddings = use_embeddings

    @property
    def repository_names(self) -> tuple[str, ...]:
        return tuple(self._roots)

    @staticmethod
    def _text(value: str, *, label: str, limit: int = 500) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f"{label} must contain 1-{limit} characters")
        if "\0" in value or value.lstrip().startswith("-"):
            raise ValueError(f"Unsafe {label}")
        return value.strip()

    @classmethod
    def _target(cls, value: str) -> str:
        target = cls._text(value, label="target")
        normalized = target.replace("\\", "/")
        if normalized.startswith("/") or _DRIVE.match(normalized):
            raise ValueError("Target must be a repo-relative path or symbol")
        if ".." in normalized.split("/"):
            raise ValueError("Parent-directory traversal in target is forbidden")
        return target

    def _select(self, repo: str) -> tuple[Path, list[str]]:
        if not isinstance(repo, str):
            raise ValueError("repo must be a repository alias")
        if repo:
            try:
                return self._roots[repo], []
            except KeyError as exc:
                raise ValueError(
                    f"Unknown repository {repo!r}; choose from {', '.join(self._roots)}"
                ) from exc
        first = next(iter(self._roots.values()))
        if len(self._roots) == 1:
            return first, []
        return first, ["--workspace", ",".join(map(str, self._roots.values()))]

    def _environment(self) -> dict[str, str]:
        # Never send unrelated agent credentials to the external binary.
        env = {key: os.environ[key] for key in _ENV_KEYS if key in os.environ}
        env["REPOMAP_NO_STAR_HINT"] = "1"
        if not self._use_embeddings:
            env["REPOMAP_EMBED"] = "0"
        return env

    def _invoke(self, action: str, args: list[str], repo: str = "") -> str:
        cwd, workspace = self._select(repo)
        command = [self._executable, action, *args, *workspace]
        try:
            completed = subprocess.run(
                command, cwd=cwd, env=self._environment(), capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=self._timeout, check=False,
            )
        except FileNotFoundError as exc:
            raise RepoMapError(
                "repomap is not installed. Install the optional local binary "
                "(for example: npm install -g @sylphx/repomap@1.9.0)."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RepoMapError(
                f"repomap {action} timed out after {self._timeout:g} seconds"
            ) from exc
        except OSError as exc:
            raise RepoMapError(
                f"Could not run repomap {action}: {type(exc).__name__}"
            ) from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[:800]
            for name, root in self._roots.items():
                detail = detail.replace(str(root), f"[{name}]")
            raise RepoMapError(
                f"repomap {action} failed ({completed.returncode}): {detail or 'no detail'}"
            )
        output = completed.stdout.strip()
        if not output:
            raise RepoMapError(f"repomap {action} produced an empty result")
        if len(output) > self._max_chars:
            return (
                output[: self._max_chars]
                + f"\n[TRUNCATED: {len(output)} characters originally; refine the query]"
            )
        return output

    def available(self) -> bool:
        """Check the optional executable without indexing or running it."""
        return bool(shutil.which(self._executable) or Path(self._executable).is_file())

    def overview(self, repo: str = "", tokens: int = 1200) -> str:
        if not isinstance(tokens, int) or not 256 <= tokens <= 4000:
            raise ValueError("tokens must be between 256 and 4000")
        return self._invoke("map", ["--tokens", str(tokens)], repo)

    def search(self, query: str, repo: str = "", limit: int = 10) -> str:
        query = self._text(query, label="query")
        if not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        return self._invoke("search", [query, "--limit", str(limit)], repo)

    def context(self, target: str, repo: str = "", code_lines: int = 40) -> str:
        target = self._target(target)
        if not isinstance(code_lines, int) or not 0 <= code_lines <= 80:
            raise ValueError("code_lines must be between 0 and 80")
        return self._invoke("context", [target, "--code-lines", str(code_lines)], repo)

    def impact(self, target: str, repo: str = "", depth: int = 2) -> str:
        target = self._target(target)
        if not isinstance(depth, int) or not 1 <= depth <= 4:
            raise ValueError("depth must be between 1 and 4")
        return self._invoke("impact", [target, "--depth", str(depth)], repo)
