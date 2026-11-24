"""Repository reads shared by legacy Claude and Codex review jobs."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from lazytools.code_bridge import _readonly, _routine_commands
from lazytools.code_bridge._approvals import _tokenize

_GIT_RULES = {"git-status", "git-diff", "git-log", "git-show", "git-rev-parse", "git-ls-files"}
_GH_RULES = {"gh-pr-view", "gh-pr-diff"}
_OUTPUT_LIMIT = 64_000


def _run(cwd: Path, executable: str, args: list[str], env: dict[str, str]) -> str:
    binary = shutil.which(executable + ".exe" if os.name == "nt" else executable)
    if binary is None or Path(binary).resolve().is_relative_to(cwd):
        raise ValueError(f"Review requires an installed {executable} binary outside the workspace")
    result = subprocess.run(
        [str(Path(binary).resolve()), *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, encoding="utf-8", errors="replace", timeout=30, check=False,
    )
    output = result.stdout if result.returncode == 0 else f"Exit {result.returncode}\n{result.stdout}{result.stderr}"
    return output[:_OUTPUT_LIMIT]


def review_tools(cwd: Path, *, network: str, validate: Callable[[], None]) -> list[Callable[[str], str]]:
    def review_command(command: str) -> str:
        """Run an allow-listed repository read such as git diff, log, status, show, or gh pr view N."""
        validate()
        rule = _readonly.match(command)
        words = _tokenize(command)
        if not words or rule not in _GIT_RULES | _GH_RULES:
            raise ValueError("Command is outside the review read-only grant")

        args = [word.text for word in words]
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
                   GIT_CEILING_DIRECTORIES=str(cwd.parent))
        if args[0] == "git":
            git_args = ["--no-pager", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "diff.external="]
            if args[1] in {"diff", "log", "show"}:
                git_args.extend([args[1], "--no-ext-diff", "--no-textconv", *args[2:]])
            else:
                git_args.extend(args[1:])
            return _run(cwd, "git", git_args, env)

        if network != "on":
            raise ValueError("GitHub review reads require network access")
        # A number is scoped to the selected origin. URLs and repo overrides
        # could make gh use a different destination.
        if len(args) < 4 or not args[3].isascii() or not args[3].isdigit() or any(
            arg.split("=", 1)[0] in {"--repo", "-R", "--"} for arg in args[4:]
        ):
            raise ValueError("Use a PR number from the workspace's origin")
        origin = _run(cwd, "git", ["config", "--get", "remote.origin.url"], env).strip()
        repo = _routine_commands.repository(origin, remote=True)
        if repo is None:
            raise ValueError("GitHub review reads require a GitHub origin")
        env.update(GH_PROMPT_DISABLED="1", GH_HOST="github.com")
        return _run(cwd, "gh", [*args[1:], "--repo", f"github.com/{repo}"], env)

    def review_read_file(path: str) -> str:
        """Read a UTF-8 text file using a path relative to the review workspace."""
        validate()
        if not _readonly.operand_ok(path.replace("\\", "/"), git=False):
            raise ValueError("File is outside the review read-only grant")
        target = (cwd / path).resolve()
        if (
            not target.is_relative_to(cwd)
            or not _readonly.operand_ok(target.relative_to(cwd).as_posix(), git=False)
            or not target.is_file()
        ):
            raise ValueError("File is outside the review read-only grant")
        with target.open(encoding="utf-8", errors="replace") as stream:
            return stream.read(_OUTPUT_LIMIT)

    return [review_command, review_read_file]
