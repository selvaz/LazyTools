"""Describe confined native requests to the approval queue."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal

from lazybridge.engines.coding import ApprovalRequest
from lazybridge.policy import CardAction

from lazytools.code_bridge import _readonly
from lazytools.code_bridge._approvals import (
    _EDIT_PATH_KEYS,
    _EDIT_TOOLS,
    _tokenize,
    is_simple_git_add,
    pr_create_grantable,
    protected_write_path,
    push_grantable,
    short_branch,
)

_GH_READS = {
    ("gh", "pr", "view"), ("gh", "pr", "list"), ("gh", "run", "list"), ("gh", "run", "view"), ("gh", "api"),
}
_READS = tuple(rule.command for rule in _readonly.packaged_policy() if rule.command[0] != "gh" or rule.command in _GH_READS)
_GIT_METADATA = {
    ("rev-parse", "--git-common-dir"),
    ("rev-parse", "--path-format=absolute", "--git-common-dir"),
    ("worktree", "list"),
    ("worktree", "list", "--porcelain"),
}
_PYTHON_MODULES = {"pytest", "ruff", "mypy", "venv"}
COMMANDS = tuple(dict.fromkeys((
    *_READS, ("git", "worktree"), ("git", "add"), ("git", "commit"),
    *(("python", "-m", module) for module in sorted(_PYTHON_MODULES)),
    ("rm",), ("remove-item",), ("mkdir",), ("bridge-edit",),
)))

_POWERSHELL = re.compile(
    r'(?:"?C:/Windows/System32/WindowsPowerShell/v1\.0/powershell\.exe"?|powershell(?:\.exe)?|pwsh(?:\.exe)?)'
    r'(?:(?: +-(?:NoProfile|NonInteractive|NoLogo)))* +-(?:Command|c) +(.+)', re.IGNORECASE,
)
_SHELL_META = re.compile(r'[;&|<>()`$%!^*?\[\]{}"\x00-\x1f\x7f]')
# Trailing `> file`, `>> file`, `2> file` and `2>&1`. The targets hold no quotes,
# so a match cannot start inside a quoted argument that is closed later.
_REDIRECTS = re.compile(r"(?: +(?:2>&1|[12]?>>? *[A-Za-z0-9_./:=+-]+))+ *$")
_REDIRECT = re.compile(r"2>&1|([12]?>>?) *([A-Za-z0-9_./:=+-]+)")
_DEVICE = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", re.IGNORECASE)
_GIT_EXEC_CONFIG = (
    r"^(core\.(hookspath|fsmonitor)|diff\.(external|.+\.(command|textconv))|"
    r"filter\..+\.(clean|smudge|process)|lfs\.storage|commit\.gpgsign|maintenance\.prefetch\.enabled)$"
)
# Git LFS installs these same filters at system or global scope on any OS.
_GIT_LFS_DEFAULTS = {
    "filter.lfs.clean": "git-lfs clean -- %f",
    "filter.lfs.smudge": "git-lfs smudge -- %f",
    "filter.lfs.process": "git-lfs filter-process",
}
_GIT_HOOKS = {
    "post-index-change", "pre-commit", "prepare-commit-msg", "commit-msg", "post-commit",
    "reference-transaction", "pre-auto-gc",
}
_SENSITIVE_ADD_NAMES = ("*.jks", "id_*", "*credential*")
# Verification in the job checkout: read-only inspection and the Python checkers.
# Removal inside the job temp dir joins the class in Workspace.classify.
_CHECKS = (
    *(command for command in _READS if command[0] != "gh"),
    ("python", "-m", "pytest"), ("python", "-m", "ruff"), ("python", "-m", "mypy"),
)

# The command prefixes describe candidate effects. Workspace checks still
# have to establish the actual paths, branch and argument restrictions.
TICKET_COMMANDS: dict[str, tuple[tuple[str, ...], ...]] = {
    "git_local": (("git", "add"), ("git", "commit")),
    "git_push_job_branch": (("git", "push"),),
    "gh_pr_create": (("gh", "pr", "create"),),
    "gh_pr_merge": (("gh", "pr", "merge"),),
    "push_default_branch": (("git", "push"),),
    "delete_files": (("rm",), ("remove-item",), ("git", "rm")),
    "package_install": (
        ("pip", "install"), ("python", "-m", "pip", "install"), ("python", "-I", "-m", "pip", "install"),
        ("npm", "install"),
    ),
    "network_other": (
        ("git", "ls-remote"), ("git", "fetch"), ("git", "clone"), *sorted(_GH_READS), ("curl",),
    ),
    "production_task": (("schtasks",), ("start-scheduledtask",)),
    "payment_or_external_post": (("gh", "pr", "comment"), ("gh", "issue", "comment"), ("gh", "issue", "create")),
    # After delete_files, so rm and Remove-Item classify there unless every target is in the temp dir.
    "checks_in_checkout": (*_CHECKS, ("rm",), ("remove-item",)),
    "workspace_tools": tuple(command for command in COMMANDS if command[0] not in {"gh", "python"} and command not in {
        ("git", "add"), ("git", "commit"), ("rm",), ("remove-item",), *_CHECKS,
    }),
    "unknown": (),
}
NETWORK_CLASSES = {"git_push_job_branch", "gh_pr_create", "gh_pr_merge", "push_default_branch",
                   "package_install", "network_other", "payment_or_external_post"}
OWNER_CLASSES = {"unknown", "payment_or_external_post"}
_PRODUCTION_PATH = re.compile(
    r"(?:(?:^|=)[A-Za-z]:/ProgramData(?:/|$)|(?:^|[/=])(?:production|prod)(?:[/.-]|$)|(?:^|/)\.env\.production$)",
    re.IGNORECASE,
)


def ticket_class(
    command: str, *, job_branch: str | None = None, default_branch: str | None = None, powershell: bool = False,
) -> str:
    """Classify one direct command. Unrecognized syntax stays with the owner."""
    redirects: list[tuple[str, str]] = []
    argv = _args(command, powershell=powershell, normalize_windows_paths=True, redirects=redirects)
    if argv is None:
        return "unknown"
    kind = _ticket_class(argv, job_branch, default_branch)
    # Only verification commands may redirect their output (into the job's own directories).
    return "unknown" if redirects and kind != "checks_in_checkout" else kind


def _ticket_class(argv: list[str], job_branch: str | None, default_branch: str | None) -> str:
    binary = argv[0].replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
    if binary == "python3":
        binary = "python"
    words = (binary, *argv[1:])
    if any(_PRODUCTION_PATH.search(word.replace("\\", "/")) for word in argv):
        return "production_task"
    if words[:2] == ("git", "push"):
        if len(words) != 4 or words[2] != "origin":
            return "unknown"
        branch = words[3].removeprefix("refs/heads/")
        if branch.lower() in {"main", "master"} or branch == default_branch:
            return "push_default_branch"
        return "git_push_job_branch" if branch == job_branch else "unknown"
    if words[:2] == ("gh", "api") and not any(
        rule.command == ("gh", "api") and rule.matches(list(words)) for rule in _readonly.packaged_policy()
    ):
        return "payment_or_external_post"
    if binary == "curl" and any(word.startswith("-") for word in words[1:] if word != "-q"):
        return "payment_or_external_post"
    if binary in {"stripe", "paypal", "send-mailmessage"}:
        return "payment_or_external_post"
    for kind, prefixes in TICKET_COMMANDS.items():
        if any(words[:len(prefix)] == prefix for prefix in prefixes):
            return kind
    return "unknown"


def _args(
    command: str, *, powershell: bool, normalize_windows_paths: bool = False,
    redirects: list[tuple[str, str]] | None = None,
) -> list[str] | None:
    """Split one simple command into argv.

    With a ``redirects`` list, trailing output redirections are removed and
    appended to it as (operator, target); otherwise any redirection is refused.
    """
    # A backslash is a path separator in PowerShell and an escape in Bash.
    if powershell and (os.name == "nt" or normalize_windows_paths):
        command = command.replace("\\", "/")
        wrapper = _POWERSHELL.fullmatch(command.strip())
        if wrapper:
            command = wrapper[1]
            if command[:1] in ("'", '"') and command[-1:] == command[:1]:
                command = command[1:-1]
        if command.startswith("& "):
            command = command[2:]
    command = command.strip()
    if redirects is not None and (tail := _REDIRECTS.search(command)):
        redirects.extend((m[1], m[2]) for m in _REDIRECT.finditer(tail[0]) if m[1])
        command = command[:tail.start()]
    words = _tokenize(command)
    if not words or any(_SHELL_META.search(word.text) for word in words):
        return None
    return [word.text for word in words]


def _linked(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _scope(value: str, cwd: Path, roots: tuple[Path, ...]) -> Path | None:
    """Check the literal path and each ancestor before following filesystem aliases."""
    if not value or any(c in value for c in "\x00*?[]{}~") or ("\\" in value and os.name != "nt"):
        return None
    path = Path(value)
    if path.anchor and (not path.is_absolute() or path.anchor.startswith(("//", "\\\\"))):
        return None
    parts = path.parts[1:] if path.anchor else path.parts
    if any(p == ".." or ":" in p or p.endswith((".", " ")) or _DEVICE.fullmatch(p) for p in parts):
        return None
    target = cwd / path
    if not any(target.is_relative_to(root) for root in roots):
        return None
    if os.name == "nt" and any(
        target.is_relative_to(Path(root))
        for root in (os.environ.get("PROGRAMDATA", "C:/ProgramData"), os.environ.get("SYSTEMROOT", "C:/Windows"))
        if root and Path(root).is_absolute()
    ):
        return None
    try:
        for part in (*reversed(target.parents), target):
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue
            if _linked(info):
                return None
        if target.resolve() != target:
            return None
    except (OSError, RuntimeError):
        return None
    return target


def _entries(root: Path) -> Iterator[tuple[Path, os.stat_result]]:
    """Enumerate without following junctions, symlinks or mounted filesystems."""
    device = root.stat().st_dev
    with os.scandir(root) as entries:
        for entry in entries:
            path = Path(entry.path)
            info = entry.stat(follow_symlinks=False)
            yield path, info
            if _same_device(info, device) and not _linked(info) and stat.S_ISDIR(info.st_mode) and path.name != ".git":
                yield from _entries(path)


def _same_device(info: os.stat_result, device: int) -> bool:
    # Windows DirEntry.stat leaves st_dev unset. Its mounts are reparse points.
    return os.name == "nt" or info.st_dev == device


def _git_metadata(cwd: Path) -> tuple[Path, Path] | None:
    """Pin Git reads and writes to this checkout and its linked metadata."""
    if any(os.environ.get(key) for key in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    )):
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--show-toplevel", "--absolute-git-dir", "--git-common-dir"],
            cwd=cwd, capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    lines = result.stdout.splitlines()
    if result.returncode or len(lines) != 3 or Path(lines[0]) != cwd:
        return None
    worktree_dir, common_dir = map(Path, lines[1:])
    if not worktree_dir.is_relative_to(common_dir):
        return None
    metadata = (
        cwd / ".git", worktree_dir / "HEAD", worktree_dir / "index",
        common_dir / "config", common_dir / "objects", common_dir / "refs",
    )
    if any(_scope(str(path), cwd, (path.parent,)) is None for path in metadata):
        return None
    return worktree_dir, common_dir


def _git_execution_is_local(cwd: Path, common_dir: Path, *, writes: bool) -> bool:
    try:
        settings = subprocess.run(
            ["git", "config", "--show-scope", "--get-regexp", _GIT_EXEC_CONFIG],
            cwd=cwd, capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return False
    if settings.returncode not in (0, 1):
        return False
    for line in settings.stdout.splitlines():
        scope, _, entry = line.partition("\t")
        key, _, value = entry.partition(" ")
        if scope in {"system", "global"} and (
            _GIT_LFS_DEFAULTS.get(key) == value
            or (os.name == "nt" and key == "diff.astextplain.textconv" and value == "astextplain")
        ):
            continue
        if key in {"core.fsmonitor", "commit.gpgsign", "maintenance.prefetch.enabled"} and value.lower() in {
            "false", "no", "off", "0",
        }:
            continue
        return False
    return not writes or all(
        not os.path.lexists(common_dir / "hooks" / hook) for hook in _GIT_HOOKS
    )


def _git_value(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5, check=False,
    )
    return "" if result.returncode else result.stdout.strip()


def _network(kind: str | None, argv: list[str]) -> Literal["on", "off"]:
    return "on" if kind in NETWORK_CLASSES or argv[0] == "gh" else "off"


def _push_updates_only(argv: list[str], cwd: Path, ref: str) -> bool:
    """Ask Git which remote refs this exact push would update (after refspec and push configuration)."""
    result = subprocess.run(
        ["git", "push", "--dry-run", "--porcelain", "--no-verify", *argv[2:]],
        cwd=cwd, capture_output=True, text=True, timeout=60, check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
    )
    lines = result.stdout.splitlines()
    if result.returncode or lines[-1:] != ["Done"]:
        return False
    destinations = [line.removeprefix("To ") for line in lines if line.startswith("To ")]
    if destinations != [_git_value(cwd, "remote", "get-url", "origin")]:
        return False
    updates = [line.split("\t") for line in lines[:-1] if not line.startswith("To ")]
    # Accept new, fast-forward and up-to-date refs only. Forced updates,
    # deletions and rejections are left to the owner.
    return bool(updates) and all(
        len(update) == 3 and update[0] in {" ", "*", "="} and update[1].rpartition(":")[2] == ref
        for update in updates
    )


def _gh_default_repo(cwd: Path) -> str | None:
    """Return the repository gh resolves in this checkout without GH_REPO or --repo."""
    gh = shutil.which("gh")
    if gh is None:
        return None
    result = subprocess.run(
        [gh, "repo", "set-default", "--view"], cwd=cwd, capture_output=True, text=True, timeout=15, check=False,
        env={**os.environ, "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"},
    )
    if result.returncode:
        return None
    if repo := result.stdout.strip():
        return repo.casefold()
    # Without a configured default gh picks among the remotes; only a lone origin is unambiguous.
    remote = _GITHUB_REMOTE.fullmatch(_git_value(cwd, "remote", "get-url", "origin"))
    if remote is None or _git_value(cwd, "remote").split() != ["origin"]:
        return None
    return remote[1].removesuffix(".git").casefold()


_GITHUB_REMOTE = re.compile(r"(?:https://github\.com/|git@github\.com:)([\w.-]+/[\w.-]+)")
_PACKAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:==[A-Za-z0-9][A-Za-z0-9_.+-]*)?")


@dataclass(frozen=True)
class Workspace:
    cwd: Path
    temp: Path | None
    initial: frozenset[Path]
    git_metadata: tuple[Path, Path] | None
    branch: str | None = None
    default_branch: str | None = None

    @classmethod
    def capture(cls, cwd: Path, temp: Path | None, *, autonomy: bool = False) -> Workspace:
        metadata = _git_metadata(cwd)
        branch = None
        default = None
        if metadata is not None and autonomy:
            branch = short_branch(_git_value(cwd, "symbolic-ref", "--quiet", "HEAD"))
            default = cls._default_branch(cwd)
        return cls(cwd, temp, frozenset(path for path, _ in _entries(cwd)), metadata, branch, default)

    @staticmethod
    def _default_branch(cwd: Path) -> str | None:
        ref = _git_value(cwd, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
        if ref.startswith("refs/remotes/origin/"):
            return short_branch("refs/heads/" + ref.removeprefix("refs/remotes/origin/"))
        return None

    @property
    def roots(self) -> tuple[Path, ...]:
        return (self.cwd,) if self.temp is None else (self.cwd, self.temp)

    def _write(self, raw: str, cwd: Path) -> Path | None:
        target = _scope(raw, cwd, self.roots)
        if target is None or target in self.roots:
            return None
        root = next(root for root in self.roots if target.is_relative_to(root))
        if protected_write_path(target.relative_to(root).as_posix(), ()):
            return None
        if target.exists() and target.stat().st_nlink > 1 and not target.is_dir():
            return None
        return target

    def _cleanup(self, raw: str, cwd: Path, *, include_initial: bool = False) -> Path | None:
        target = self._write(raw, cwd)
        if target is None or (target in self.initial and not include_initial):
            return None
        if target.is_dir():
            if os.path.ismount(target):
                return None
            device = target.stat().st_dev
            if any(
                (child in self.initial and not include_initial)
                or _linked(info) or not _same_device(info, device) or self._write(str(child), cwd) is None
                for child, info in _entries(target)
            ):
                return None
        return target

    def _git(self, argv: list[str], cwd: Path) -> tuple[Path, ...] | None:
        if argv[1:2] == ["-C"]:
            if len(argv) < 4 or _scope(argv[2], cwd, (self.cwd,)) != self.cwd:
                return None
            del argv[1:3]
            cwd = self.cwd
        if cwd != self.cwd or len(argv) < 2:
            return None
        if self.git_metadata is None or _git_metadata(cwd) != self.git_metadata:
            return None
        subcommand, args = argv[1], argv[2:]
        if not _git_execution_is_local(cwd, self.git_metadata[1], writes=subcommand in {"add", "commit"}):
            return None
        if subcommand == "add":
            if not is_simple_git_add(_quoted(argv)) or not args:
                return None
            files = args[1:] if args[:1] == ["--"] else args
            if not files or any(path.startswith("-") for path in files):
                return None
            paths = [self._write(path, cwd) for path in files]
            if any(
                path is None or not path.is_relative_to(cwd) or path.is_dir() or any(
                    _readonly.sensitive_name(part)
                    or any(fnmatchcase(part.casefold(), pattern) for pattern in _SENSITIVE_ADD_NAMES)
                    for part in path.relative_to(cwd).parts
                )
                for path in paths
            ):
                return None
            return tuple(path for path in paths if path is not None)
        if subcommand == "commit":
            if (len(args) == 2 and args[0] == "-m") or (len(args) == 1 and args[0].startswith("--message=")):
                return (cwd,)
            if len(args) == 2 and args[0] == "-F":
                message = args[1]
            elif len(args) == 1 and args[0].startswith("--file="):
                message = args[0].removeprefix("--file=")
            else:
                return None
            path = self._write(message, cwd)
            return (path,) if path is not None and path.is_file() and not message.startswith("-") else None
        if tuple(argv[1:]) in _GIT_METADATA:
            return (cwd,)
        if subcommand == "rev-parse" and args[:1] == ["--git-path"]:
            return (cwd,) if len(args) == 2 and _readonly.operand_ok(args[1], git=False) else None
        return self._read(argv, cwd)

    def _read(self, argv: list[str], cwd: Path) -> tuple[Path, ...] | None:
        if argv[0] == "gh" or ("-R" in argv[1:] and argv[0] == "grep") or (
            os.name == "nt" and argv[0] != "git" and any("," in arg for arg in argv[1:])
        ):
            return None
        rule = next((rule for rule in _readonly.packaged_policy() if rule.matches(argv)), None)
        if rule is None:
            return None
        # The read parser checks option/path syntax. Check literal operands
        # against live link boundaries as well (Git operands may be revisions).
        paths_only = False
        for word in argv[len(rule.command):]:
            if word == "--" and not paths_only:
                paths_only = True
            elif (paths_only or not word.startswith("-")) and ":" not in word and _scope(word, cwd, self.roots) is None:
                return None
        return (cwd,)

    def _redirect_targets(
        self, redirects: list[tuple[str, str]], cwd: Path, *, powershell: bool,
    ) -> tuple[Path | None, ...]:
        return tuple(
            self._write(target, cwd) for _, target in redirects if powershell or target != "/dev/null"
        )

    def _python(self, argv: list[str], cwd: Path) -> tuple[Path, ...] | None:
        binary = argv[0]
        if binary not in {"python", "python3", "python.exe", Path(sys.executable).as_posix()}:
            executable = _scope(binary, cwd, self.roots)
            if executable is None or executable.name not in {"python", "python3", "python.exe"}:
                return None
            if not (executable.parent.parent / "pyvenv.cfg").is_file():
                return None
        start = 2 if argv[1:2] == ["-B"] else 1
        if len(argv) <= start + 1 or argv[start] != "-m":
            return None
        module, *args = argv[start + 1:]
        argv[:] = ["python", "-m", module, *args]
        if module == "venv":
            while args and args[0] in {"--copies", "--without-pip"}:
                args = args[1:]
            if len(args) != 1:
                return None
            dest = self._write(args[0], cwd)
            return (dest,) if dest is not None and not dest.exists() else None
        if module == "pytest":
            return self._pytest(args, cwd)
        if module == "ruff" and args[:1] == ["check"]:
            args = args[1:]
        elif module != "mypy":
            return None
        if _scope(f".{module}_cache", cwd, self.roots) is None:
            return None
        return (cwd,) if all(
            not arg.startswith(("-", "@")) and _scope(arg, cwd, (self.cwd,)) is not None for arg in args
        ) else None

    def _pytest(self, args: list[str], cwd: Path) -> tuple[Path, ...] | None:
        if _scope(".pytest_cache", cwd, self.roots) is None:
            return None
        dest = None
        index = 0
        while index < len(args):
            arg = args[index]
            index += 1
            if arg in {"-q", "-x", "-v", "-vv", "--no-header", "-s"} or re.fullmatch(
                r"-r[A-Za-z]+|--tb=[A-Za-z]+|--maxfail=[0-9]+", arg,
            ):
                continue
            if arg in {"-k", "-m", "-p", "--basetemp"}:
                if index == len(args) or args[index].startswith(("-", "@")):
                    return None
                value = args[index]
                index += 1
                if arg == "-p" and value != "no:cacheprovider":
                    return None
                if arg == "--basetemp" and (dest is not None or (dest := self._cleanup(value, cwd)) is None):
                    return None
                continue
            if arg.startswith(("-", "@")) or _scope(arg.split("::", 1)[0], cwd, (self.cwd,)) is None:
                return None
        return (dest or cwd,)

    def classify(self, request: ApprovalRequest) -> str:
        paths = [request.cwd]
        if request.kind == "tool" and request.name in _EDIT_TOOLS:
            paths.extend(request.arguments.get(key) for key in _EDIT_PATH_KEYS)
        if any(isinstance(path, str) and _PRODUCTION_PATH.search(path.replace("\\", "/")) for path in paths):
            return "production_task"
        command = request.arguments.get("command")
        if isinstance(command, str):
            kind = ticket_class(
                command, job_branch=self.branch, default_branch=self.default_branch, powershell=request.provider == "codex",
            )
            if kind == "delete_files" and self._temp_cleanup(request, command):
                return "checks_in_checkout"
            return kind
        return "workspace_tools" if request.kind == "tool" and request.name in _EDIT_TOOLS else "unknown"

    def _temp_cleanup(self, request: ApprovalRequest, command: str) -> bool:
        """Whether an rm or Remove-Item only removes paths inside the job temp dir."""
        argv = _args(command, powershell=request.provider == "codex")
        cwd = _scope(request.cwd or str(self.cwd), self.cwd, self.roots)
        if self.temp is None or argv is None or cwd is None or argv[0].casefold() not in {"rm", "remove-item"}:
            return False
        try:
            paths = self._shell(argv, cwd, autonomy=True)
        except (OSError, RecursionError):
            return False
        temp = self.temp
        return bool(paths) and all(path is not None and path.is_relative_to(temp) for path in paths or ())

    def action(self, request: ApprovalRequest, *, ticket_kind: str | None = None) -> tuple[Path, CardAction] | None:
        try:
            return self._action(request, ticket_kind)
        except (OSError, subprocess.SubprocessError, UnicodeError, RecursionError):
            return None

    def _action(self, request: ApprovalRequest, kind: str | None) -> tuple[Path, CardAction] | None:
        cwd = _scope(request.cwd or str(self.cwd), self.cwd, self.roots)
        if cwd is None or not cwd.is_dir():
            return None
        if request.kind == "tool" and request.name in _EDIT_TOOLS:
            argv = ["bridge-edit", request.name]
            paths = tuple(
                self._write(value, cwd) if isinstance(value, str) else None
                for key, value in request.arguments.items() if key in _EDIT_PATH_KEYS
            )
        else:
            command = request.arguments.get("command")
            if (
                not isinstance(command, str)
                or (request.kind != "command" and request.name != "Bash")
                or (request.kind == "command" and request.raw.get("command") != command)
                or (request.kind == "command" and any(
                    request.raw.get(key) for key in ("additionalPermissions", "proposedNetworkPolicyAmendment")
                ))
            ):
                return None
            powershell = request.provider == "codex"
            redirects: list[tuple[str, str]] = []
            args = _args(command, powershell=powershell, redirects=redirects if kind == "checks_in_checkout" else None)
            if args is None:
                return None
            argv = args
            if argv[0] in {"gh", "gh.exe"} and not self._github_overrides_match_origin(argv, cwd):
                return None
            paths = self._shell(argv, cwd, autonomy=kind is not None) or ()
            if kind is not None and not paths:
                paths = self._autonomy_paths(kind, argv, cwd) or ()
            if paths and redirects:
                paths = (*paths, *self._redirect_targets(redirects, cwd, powershell=powershell))
            if kind == "checks_in_checkout" and argv[0] in {"rm", "remove-item"} and not all(
                path is not None and self.temp is not None and path.is_relative_to(self.temp) for path in paths
            ):
                return None
        if not paths or any(path is None for path in paths):
            return None
        affected = tuple(path for path in paths if path is not None)
        for root in self.roots:
            if all(path.is_relative_to(root) for path in affected):
                # Temp cards cover effects inside the separately scoped job
                # temp directory. The native cwd and operands were checked above.
                return root, CardAction(
                    argv=tuple(argv), cwd=root, paths=affected,
                    branch=self.branch if kind == "git_push_job_branch" else None,
                    network=_network(kind, argv),
                )
        return None

    def network(self, request: ApprovalRequest, kind: str | None) -> Literal["on", "off"]:
        """Return the network state a card must carry for this request, without running anything."""
        command = request.arguments.get("command")
        argv = _args(command, powershell=request.provider == "codex", redirects=[]) if isinstance(command, str) else None
        return _network(kind, argv or ["bridge-edit"])

    def _autonomy_paths(self, kind: str, argv: list[str], cwd: Path) -> tuple[Path, ...] | None:
        if kind in {"git_push_job_branch", "push_default_branch", "gh_pr_create", "gh_pr_merge", "network_other"}:
            return (cwd,) if self._network_action(kind, argv, cwd) else None
        if (
            kind == "production_task" and os.name == "nt"
            and argv[0].lower() in {"schtasks", "schtasks.exe"} and len(argv) == 4
            and [value.lower() for value in argv[1:3]] == ["/run", "/tn"]
            and re.fullmatch(r"[\w /.-]+", argv[3]) and not argv[3].startswith(("/", "-"))
        ):
            argv[0] = "schtasks"
            return (cwd,)
        if kind == "package_install":
            return self._pip_install(argv, cwd)
        return None

    def _network_action(self, kind: str, argv: list[str], cwd: Path) -> bool:
        if kind == "network_other" and argv[:2] == ["curl", "-q"]:
            return len(argv) == 3 and re.fullmatch(r"https://[A-Za-z0-9.-]+(?:/[A-Za-z0-9_./-]*)?", argv[2]) is not None
        if cwd != self.cwd or self.git_metadata is None or _git_metadata(cwd) != self.git_metadata:
            return False
        if argv[0] == "git" and argv[1:2] == ["push"]:
            current_default = self._default_branch(cwd)
            if current_default is None or current_default != self.default_branch or self.branch is None:
                return False
            if not self._clean_push_origin(cwd):
                return False
            words = _tokenize(_quoted(argv))
            if kind == "git_push_job_branch":
                if words is None or not push_grantable(words, self.branch):
                    return False
                target = self.branch
            elif argv[3] != current_default or short_branch(
                _git_value(cwd, "symbolic-ref", "--quiet", "HEAD"),
            ) != current_default:
                return False
            else:
                target = current_default
            return _push_updates_only(argv, cwd, "refs/heads/" + target)
        if argv[:3] == ["git", "ls-remote", "origin"]:
            return len(argv) == 3 and _GITHUB_REMOTE.fullmatch(_git_value(cwd, "remote", "get-url", "origin")) is not None
        if argv[0] != "gh" or not _GITHUB_REMOTE.fullmatch(_git_value(cwd, "remote", "get-url", "origin")):
            return False
        if kind == "gh_pr_merge":
            args = argv[3:]
            if args and re.fullmatch(r"[1-9][0-9]*", args[0]):
                args = args[1:]
            return argv[1:3] == ["pr", "merge"] and (not args or args in [["--merge"], ["--squash"], ["--rebase"]])
        if kind == "gh_pr_create" and self.branch and self.branch not in {"main", "master", self.default_branch}:
            words = _tokenize(_quoted(argv))
            return (
                words is not None and pr_create_grantable(words, self.branch, require_head=True)
                and short_branch(_git_value(cwd, "symbolic-ref", "--quiet", "HEAD")) == self.branch
            )
        return False

    def _github_overrides_match_origin(self, argv: list[str], cwd: Path) -> bool:
        host = os.environ.get("GH_HOST", "").lower()
        if host and host != "github.com":
            return False
        overrides = [repo] if (repo := os.environ.get("GH_REPO")) else []
        args = iter(argv[1:])
        for arg in args:
            if arg == "--":
                break
            flag, sep, value = arg.partition("=")
            if flag in {"--repo", "-R"}:
                overrides.append(value if sep else next(args, ""))
        if cwd != self.cwd or self.git_metadata is None or _git_metadata(cwd) != self.git_metadata:
            return False
        remote = _GITHUB_REMOTE.fullmatch(_git_value(cwd, "remote", "get-url", "origin"))
        if remote is None:
            return False
        origin = remote[1].removesuffix(".git").casefold()
        if not overrides:
            return _gh_default_repo(cwd) in {origin, f"github.com/{origin}"}
        # A hostless repo override uses gh's configured default host when GH_HOST is unset.
        allowed = {f"github.com/{origin}"}
        if host:
            allowed.add(origin)
        return all(override.casefold() in allowed for override in overrides)

    def _clean_push_origin(self, cwd: Path) -> bool:
        assert self.git_metadata is not None
        remote = _git_value(cwd, "remote", "get-url", "--push", "--all", "origin")
        return (
            _GITHUB_REMOTE.fullmatch(remote) is not None
            and remote == _git_value(cwd, "remote", "get-url", "origin")
            and _git_execution_is_local(cwd, self.git_metadata[1], writes=True)
            and not os.path.lexists(self.git_metadata[1] / "hooks" / "pre-push")
            and not any(_git_value(cwd, "config", "--get", key) for key in (
                "core.sshCommand", "remote.origin.mirror", "push.followTags", "push.recurseSubmodules",
            ))
        )

    def _pip_install(self, argv: list[str], cwd: Path) -> tuple[Path, ...] | None:
        if argv[:4] != ["python", "-I", "-m", "pip"]:
            return None
        args = argv[4:]
        if args[:5] != ["install", "--isolated", "--no-cache-dir", "--only-binary=:all:", "--target"] or len(args) < 7:
            return None
        target = self._write(args[5], cwd)
        if target is not None and not target.exists() and all(_PACKAGE.fullmatch(name) for name in args[6:]):
            return (target,)
        return None

    def _shell(self, argv: list[str], cwd: Path, *, autonomy: bool = False) -> tuple[Path | None, ...] | None:
        executable = argv[0]
        if executable == "git":
            return self._git(argv, cwd)
        if executable in {"gh", "gh.exe"}:
            argv[0] = "gh"
            if any(rule.command in _GH_READS and rule.matches(argv) for rule in _readonly.packaged_policy()):
                return (cwd,)
            return None
        if Path(executable).name in {"python", "python3", "python.exe"}:
            return self._python(argv, cwd)
        if executable == "rm":
            args = argv[1:]
            while args and re.fullmatch(r"-[rfRd]+", args[0]):
                args = args[1:]
            if args[:1] == ["--"]:
                args = args[1:]
            if any(arg.startswith("-") or (os.name == "nt" and "," in arg) for arg in args):
                return None
            return tuple(self._cleanup(arg, cwd, include_initial=autonomy) for arg in args)
        if executable.casefold() == "remove-item" and os.name == "nt":
            argv[0] = "remove-item"
            args = argv[1:]
            if len(args) >= 2 and args[0].casefold() == "-literalpath" and "," not in args[1] and all(
                arg.casefold() in {"-force", "-recurse"} for arg in args[2:]
            ):
                return (self._cleanup(args[1], cwd, include_initial=autonomy),)
            return None
        if executable == "mkdir" and len(argv) == 2 and not argv[1].startswith("-") and "," not in argv[1]:
            return (self._write(argv[1], cwd),)
        return self._read(argv, cwd)


def _quoted(argv: list[str]) -> str:
    return " ".join(f'"{arg}"' if " " in arg else arg for arg in argv)
