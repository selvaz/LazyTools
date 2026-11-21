"""Small subprocess adapter for the optional ``repomap`` executable."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO


class RepoMapError(RuntimeError):
    """RepoMap could not run or returned an invalid response."""


class RepoMapCLI:
    """Invoke RepoMap without allowing its cache to land in a repository."""

    def __init__(self, *, binary: str = "repomap", cache_dir: str | Path | None = None, timeout: float = 120.0) -> None:
        self.binary = binary
        data_dir = Path(os.environ.get("LAZYTOOLS_DATA_DIR", "~/.lazytools")).expanduser()
        self.cache_dir = Path(cache_dir).expanduser() if cache_dir else data_dir / "repomap"
        self.timeout = timeout

    _MAX_STDOUT = 2 * 1024 * 1024
    _MAX_STDERR = 500
    _MAX_SNIPPET = 400
    _READER_JOIN_TIMEOUT = 2.0

    def _command(self, *, windows: bool | None = None) -> list[str]:
        executable = shutil.which(self.binary)
        if executable is None:
            raise RepoMapError(
                "repomap is not installed; install the pinned CLI with "
                "`npm install -g @sylphx/repomap@1.9.0`"
            )
        path = Path(executable)
        windows = sys.platform == "win32" if windows is None else windows
        if not windows or path.suffix.lower() != ".cmd":
            return [str(path)]
        node = shutil.which("node.exe") or shutil.which("node")
        package = path.parent / "node_modules" / "@sylphx" / "repomap"
        try:
            metadata = json.loads((package / "package.json").read_text(encoding="utf-8"))
            launcher = (package / metadata["bin"]["repomap"]).resolve()
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RepoMapError("cannot safely resolve the repomap npm package behind its Windows .cmd shim") from exc
        if node is None or not launcher.is_file() or package.resolve() not in launcher.parents:
            raise RepoMapError("cannot safely resolve node and the repomap JavaScript entry behind its Windows .cmd shim")
        return [node, str(launcher)]

    @staticmethod
    def _read_bounded(stream: BinaryIO, limit: int, destination: bytearray) -> None:
        while chunk := stream.read(64 * 1024):
            if len(destination) < limit:
                destination.extend(chunk[: limit - len(destination)])

    @staticmethod
    def _terminate_tree(process: subprocess.Popen[bytes]) -> None:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _run(self, args: list[str], *, repo: Path) -> Any:
        command = self._command()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        resolved_cache = str(self.cache_dir.resolve())
        env["REPOMAP_CACHE_DIR"] = resolved_cache
        env["XDG_CACHE_HOME"] = resolved_cache
        # Git otherwise refreshes and rewrites .git/index during ostensibly
        # read-only status operations, especially for freshly-created files.
        env["GIT_OPTIONAL_LOCKS"] = "0"
        try:
            process = subprocess.Popen(
                [*command, *args], cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=sys.platform != "win32",
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if sys.platform == "win32" else 0,
            )
        except OSError as exc:
            raise RepoMapError(f"repomap failed: {exc}") from exc
        stdout, stderr = bytearray(), bytearray()
        assert process.stdout is not None and process.stderr is not None
        readers = [
            threading.Thread(target=self._read_bounded, args=(process.stdout, self._MAX_STDOUT, stdout), daemon=True),
            threading.Thread(target=self._read_bounded, args=(process.stderr, self._MAX_STDERR, stderr), daemon=True),
        ]
        for reader in readers:
            reader.start()
        try:
            returncode = process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            self._terminate_tree(process)
            process.wait()
            raise RepoMapError(f"repomap timed out after {self.timeout:g} seconds") from exc
        finally:
            deadline = time.monotonic() + self._READER_JOIN_TIMEOUT
            for reader in readers:
                reader.join(max(0.0, deadline - time.monotonic()))
            if any(reader.is_alive() for reader in readers):
                # A descendant can inherit the pipes even after the CLI exits.
                # Make one last attempt to close the whole process group, but
                # never let a pipe reader make the provider hang indefinitely.
                self._terminate_tree(process)
                raise RepoMapError("repomap output reader did not finish after the process exited")
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        if returncode:
            detail = stderr_text.strip() or stdout_text.strip() or "unknown error"
            raise RepoMapError(f"repomap failed ({returncode}): {detail[: self._MAX_STDERR]}")
        if len(stdout) == self._MAX_STDOUT:
            raise RepoMapError(f"repomap output exceeded {self._MAX_STDOUT} bytes")
        try:
            result = json.loads(stdout_text)
        except json.JSONDecodeError as exc:
            raise RepoMapError("repomap returned invalid JSON") from exc
        return self._sanitize(result, repo)

    def _sanitize(self, value: Any, repo: Path) -> Any:
        if isinstance(value, list):
            return [self._sanitize(item, repo) for item in value]
        if not isinstance(value, dict):
            return value
        cleaned = {key: self._sanitize(item, repo) for key, item in value.items()}
        if isinstance(cleaned.get("snippet"), str):
            cleaned["snippet"] = cleaned["snippet"][: self._MAX_SNIPPET]
        file = cleaned.get("file")
        if isinstance(file, str):
            candidate = (repo / file).resolve() if not Path(file).is_absolute() else Path(file).resolve()
            if candidate != repo.resolve() and repo.resolve() not in candidate.parents:
                cleaned["outside_code_root"] = True
                cleaned.pop("snippet", None)
        return cleaned

    def search(self, query: str, repo: Path, limit: int) -> Any:
        """Return RepoMap's ranked JSON search response."""
        return self._run(["search", "--limit", str(limit), "--json", "--", query], repo=repo)

    def map(self, repo: Path, tokens: int) -> Any:
        """Return RepoMap's budgeted JSON repository map."""
        return self._run(["map", "--tokens", str(tokens), "--json"], repo=repo)
