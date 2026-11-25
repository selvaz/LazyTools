"""Describe confined native requests to the approval queue."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

from lazybridge.engines.coding import ApprovalRequest
from lazybridge.policy import CardAction

from lazytools.code_bridge import _readonly
from lazytools.code_bridge._approvals import (
    _EDIT_PATH_KEYS,
    _EDIT_TOOLS,
    _tokenize,
    is_simple_git_add,
    protected_write_path,
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
_DEVICE = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", re.IGNORECASE)
_GIT_EXEC_CONFIG = (
    r"^(core\.(hookspath|fsmonitor)|diff\.(external|.+\.(command|textconv))|"
    r"filter\..+\.(clean|smudge|process)|lfs\.storage|commit\.gpgsign|maintenance\.prefetch\.enabled)$"
)
_GIT_WINDOWS_DEFAULTS = {
    "diff.astextplain.textconv": "astextplain",
    "filter.lfs.clean": "git-lfs clean -- %f",
    "filter.lfs.smudge": "git-lfs smudge -- %f",
    "filter.lfs.process": "git-lfs filter-process",
}
_GIT_HOOKS = {
    "post-index-change", "pre-commit", "prepare-commit-msg", "commit-msg", "post-commit",
    "reference-transaction", "pre-auto-gc",
}
_SENSITIVE_ADD_NAMES = ("*.jks", "id_*", "*credential*")


def _args(command: str, *, powershell: bool) -> list[str] | None:
    # A backslash is a path separator in PowerShell and an escape in Bash.
    if powershell and os.name == "nt":
        command = command.replace("\\", "/")
        wrapper = _POWERSHELL.fullmatch(command.strip())
        if wrapper:
            command = wrapper[1]
            if command[:1] in ("'", '"') and command[-1:] == command[:1]:
                command = command[1:-1]
        if command.startswith("& "):
            command = command[2:]
    words = _tokenize(command.strip())
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
        if os.name == "nt" and scope in {"system", "global"} and _GIT_WINDOWS_DEFAULTS.get(key) == value:
            continue
        if key in {"core.fsmonitor", "commit.gpgsign", "maintenance.prefetch.enabled"} and value.lower() in {
            "false", "no", "off", "0",
        }:
            continue
        return False
    return not writes or all(
        not os.path.lexists(common_dir / "hooks" / hook) for hook in _GIT_HOOKS
    )


@dataclass(frozen=True)
class Workspace:
    cwd: Path
    temp: Path | None
    initial: frozenset[Path]
    git_metadata: tuple[Path, Path] | None

    @classmethod
    def capture(cls, cwd: Path, temp: Path | None) -> Workspace:
        return cls(cwd, temp, frozenset(path for path, _ in _entries(cwd)), _git_metadata(cwd))

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

    def _cleanup(self, raw: str, cwd: Path) -> Path | None:
        target = self._write(raw, cwd)
        if target is None or target in self.initial:
            return None
        if target.is_dir():
            if os.path.ismount(target):
                return None
            device = target.stat().st_dev
            if any(
                child in self.initial or _linked(info) or not _same_device(info, device) or self._write(str(child), cwd) is None
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

    def action(self, request: ApprovalRequest) -> tuple[Path, CardAction] | None:
        try:
            return self._action(request)
        except (OSError, RecursionError):
            return None

    def _action(self, request: ApprovalRequest) -> tuple[Path, CardAction] | None:
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
            args = _args(command, powershell=request.provider == "codex")
            if args is None:
                return None
            argv = args
            paths = self._shell(argv, cwd) or ()
        if not paths or any(path is None for path in paths):
            return None
        affected = tuple(path for path in paths if path is not None)
        for root in self.roots:
            if all(path.is_relative_to(root) for path in affected):
                # Temp cards cover effects inside the separately scoped job
                # temp directory. The native cwd and operands were checked above.
                return root, CardAction(
                    argv=tuple(argv), cwd=root, paths=affected, network="on" if argv[0] == "gh" else "off",
                )
        return None

    def _shell(self, argv: list[str], cwd: Path) -> tuple[Path | None, ...] | None:
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
            return tuple(self._cleanup(arg, cwd) for arg in args)
        if executable.casefold() == "remove-item" and os.name == "nt":
            argv[0] = "remove-item"
            args = argv[1:]
            if len(args) >= 2 and args[0].casefold() == "-literalpath" and "," not in args[1] and all(
                arg.casefold() in {"-force", "-recurse"} for arg in args[2:]
            ):
                return (self._cleanup(args[1], cwd),)
            return None
        if executable == "mkdir" and len(argv) == 2 and not argv[1].startswith("-") and "," not in argv[1]:
            return (self._write(argv[1], cwd),)
        return self._read(argv, cwd)


def _quoted(argv: list[str]) -> str:
    return " ".join(f'"{arg}"' if " " in arg else arg for arg in argv)
