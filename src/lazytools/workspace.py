"""Confined workspace file tools and an opt-in shell tool."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import ctypes
import errno
import os
import secrets
import signal
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from lazybridge import Tool

_TRUNCATION_NOTICE = "\n[Read truncated after {size} bytes]"
_WINDOWS_SHELL_LAUNCHER = (
    "import subprocess, sys; "
    "sys.stdin.buffer.read(1); "
    "raise SystemExit(subprocess.call(sys.argv[1], shell=True, stdin=subprocess.DEVNULL))"
)
_WINDOWS_TEMP_CREATE_ATTEMPTS = 8


class WorkspaceTools:
    """A LazyBridge tool provider for file work and optional shell commands.

    ``Read``, ``Write``, and ``Edit`` are confined to ``file_roots``. ``Bash``
    is exposed only when explicitly enabled and is intentionally not confined
    to those roots; authorization belongs to the engine's approval gate.

    On POSIX, file operations walk from an open root directory descriptor,
    reject symlinks in every path component, and perform replacement relative
    to the verified parent descriptor. This closes pathname-swap races. On
    Windows, Python has no ``dir_fd``/``openat`` support: opened file and parent
    handles are checked with ``GetFinalPathNameByHandleW``, reparse-point parents
    are rejected, and temp handles are checked before they are written. Temp
    creation and the final ``os.replace`` must still name the parent, so an
    ancestor-reparse swap can create an empty, random-named outside temp
    (rejected before content is written) or redirect the final replace in a
    small Windows-only race window. Operations fail closed if handle-path
    verification is unavailable.

    Windows timeout cleanup is kernel-enforced with a Job Object. POSIX cleanup
    signals both the process group and descendants discoverable from a PPID
    snapshot; a daemon that has already reparented before that snapshot cannot
    be identified reliably with the standard library and may survive.
    """

    _is_lazy_tool_provider = True

    def __init__(
        self,
        file_roots: list[str | Path],
        cwd: str | Path,
        enable_bash: bool = False,
        max_read_bytes: int = 1_000_000,
        max_output_bytes: int = 1_000_000,
    ) -> None:
        if max_read_bytes <= 0:
            raise ValueError("max_read_bytes must be greater than zero")
        if max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be greater than zero")

        roots: list[Path] = []
        for root in file_roots:
            try:
                resolved = Path(root).resolve(strict=True)
            except OSError as exc:
                raise ValueError(f"file root does not exist: {root!s}") from exc
            if not resolved.is_dir():
                raise ValueError(f"file root is not a directory: {root!s}")
            roots.append(resolved)

        self._file_roots = tuple(roots)
        self._posix_root_fds: dict[Path, int] = {}
        if os.name != "nt":
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW  # type: ignore[attr-defined]
            try:
                for root in self._file_roots:
                    self._posix_root_fds[root] = os.open(root, flags)
            except BaseException:
                for descriptor in self._posix_root_fds.values():
                    os.close(descriptor)
                raise
        self._cwd = Path(cwd).resolve()
        self._enable_bash = enable_bash
        self._max_read_bytes = max_read_bytes
        self._max_output_bytes = max_output_bytes
        self._mutation_lock = threading.Lock()

    def __del__(self) -> None:
        for descriptor in getattr(self, "_posix_root_fds", {}).values():
            with contextlib.suppress(OSError):
                os.close(descriptor)
        self._posix_root_fds = {}

    def as_tools(self) -> list[Tool]:
        """Return the explicitly exposed workspace tools."""
        tools = [
            Tool.from_schema(
                "Read",
                (
                    "Read UTF-8 text from a file confined to the configured file roots. "
                    "offset is the zero-based line at which to start; limit is the maximum "
                    "number of lines to return. Large results are visibly truncated."
                ),
                {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Absolute path or path relative to cwd."},
                        "offset": {"type": ["integer", "null"], "minimum": 0, "default": None},
                        "limit": {"type": ["integer", "null"], "minimum": 0, "default": None},
                    },
                    "required": ["file_path"],
                    "additionalProperties": False,
                },
                self._read,
            ),
            Tool.from_schema(
                "Write",
                (
                    "Atomically write UTF-8 text to a file confined to the configured file roots. "
                    "The direct parent must already exist; parent directories are never created."
                ),
                {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Absolute path or path relative to cwd."},
                        "content": {"type": "string"},
                    },
                    "required": ["file_path", "content"],
                    "additionalProperties": False,
                },
                self._write,
            ),
            Tool.from_schema(
                "Edit",
                (
                    "Atomically edit UTF-8 text in a file confined to the configured file roots. "
                    "old_string must match exactly once unless replace_all is true."
                ),
                {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Absolute path or path relative to cwd."},
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                        "replace_all": {"type": "boolean", "default": False},
                    },
                    "required": ["file_path", "old_string", "new_string"],
                    "additionalProperties": False,
                },
                self._edit,
            ),
        ]
        if self._enable_bash:
            tools.append(
                Tool.from_schema(
                    "Bash",
                    (
                        "Run a shell command asynchronously from the configured cwd and return its "
                        "exit code, stdout, and stderr. Output is bounded. On Windows, timeouts terminate "
                        "the kernel-tracked job tree; on POSIX, they terminate the process group plus "
                        "descendants discoverable by parent PID at cleanup time. Bash is NOT "
                        "path-confined by file_roots: once a command "
                        "runs, it has the same filesystem and other access as the host process."
                    ),
                    {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string"},
                            "timeout": {
                                "type": ["number", "null"],
                                "exclusiveMinimum": 0,
                                "default": None,
                                "description": "Optional timeout in seconds.",
                            },
                        },
                        "required": ["command"],
                        "additionalProperties": False,
                    },
                    self._bash,
                )
            )
        return tools

    def _requested_path(self, file_path: str) -> Path:
        path = Path(file_path)
        return path if path.is_absolute() else self._cwd / path

    def _contained(self, path: Path) -> bool:
        return any(path == root or root in path.parents for root in self._file_roots)

    def _relative_to_root(self, file_path: str, operation: str) -> tuple[Path, tuple[str, ...]]:
        """Map a lexical request to its root without resolving attacker-controlled links."""
        requested = Path(os.path.abspath(self._requested_path(file_path)))
        matches: list[tuple[Path, tuple[str, ...]]] = []
        for root in self._file_roots:
            try:
                relative = requested.relative_to(root)
            except ValueError:
                continue
            matches.append((root, relative.parts))
        if not matches:
            raise ValueError(f"{operation}: path is outside the configured file roots: {file_path}")
        return max(matches, key=lambda item: len(item[0].parts))

    def _open_posix_parent(self, file_path: str, operation: str) -> tuple[int, str, Path]:
        root, parts = self._relative_to_root(file_path, operation)
        if not parts:
            raise ValueError(f"{operation}: path is not a file: {file_path}")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW  # type: ignore[attr-defined]
        descriptor = os.dup(self._posix_root_fds[root])
        try:
            for component in parts[:-1]:
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
        except OSError as exc:
            is_link = False
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                with contextlib.suppress(OSError):
                    is_link = stat.S_ISLNK(os.stat(component, dir_fd=descriptor, follow_symlinks=False).st_mode)
            os.close(descriptor)
            if is_link:
                raise ValueError(
                    f"{operation}: path is outside the configured roots or contains a symlink: {file_path}"
                ) from exc
            if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
                raise ValueError(f"{operation}: parent directory does not exist: {Path(file_path).parent}") from exc
            if exc.errno == errno.ELOOP:
                raise ValueError(
                    f"{operation}: path is outside the configured roots or contains a symlink: {file_path}"
                ) from exc
            raise ValueError(f"{operation}: could not open parent directory: {exc}") from exc
        return descriptor, parts[-1], root.joinpath(*parts)

    def _open_existing(self, file_path: str, operation: str) -> tuple[int, Path, int | None, str | None]:
        """Open and validate a file, returning its fd and POSIX parent context."""
        if os.name != "nt":
            parent_fd, name, display_path = self._open_posix_parent(file_path, operation)
            try:
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)  # type: ignore[attr-defined]
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    os.close(descriptor)
                    raise ValueError(f"{operation}: path is not a file: {file_path}")
            except OSError as exc:
                os.close(parent_fd)
                if exc.errno == errno.ENOENT:
                    raise ValueError(f"{operation}: file does not exist: {file_path}") from exc
                if exc.errno == errno.ELOOP:
                    raise ValueError(
                        f"{operation}: path is outside the configured roots or contains a symlink: {file_path}"
                    ) from exc
                raise ValueError(f"{operation}: could not open {file_path}: {exc}") from exc
            return descriptor, display_path, parent_fd, name

        requested = self._requested_path(file_path)
        try:
            descriptor = os.open(requested, os.O_RDONLY | os.O_BINARY)  # type: ignore[attr-defined]
        except OSError as exc:
            raise ValueError(f"{operation}: file does not exist: {file_path}") from exc
        try:
            actual = _path_from_windows_fd(descriptor)
            if not self._contained(actual):
                raise ValueError(f"{operation}: path is outside the configured file roots: {file_path}")
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError(f"{operation}: path is not a file: {file_path}")
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor, actual, None, None

    def _write_target(self, file_path: str, operation: str) -> Path:
        requested = self._requested_path(file_path)
        if requested.exists() or requested.is_symlink():
            try:
                target = requested.resolve(strict=True)
            except OSError as exc:
                raise ValueError(f"{operation}: target cannot be resolved: {file_path}") from exc
            if not self._contained(target):
                raise ValueError(f"{operation}: path is outside the configured file roots: {file_path}")
            if not target.is_file():
                raise ValueError(f"{operation}: path is not a file: {file_path}")
            return target

        try:
            parent = requested.parent.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"{operation}: parent directory does not exist: {requested.parent}") from exc
        if not self._contained(parent):
            raise ValueError(f"{operation}: parent is outside the configured file roots: {requested.parent}")
        if not parent.is_dir():
            raise ValueError(f"{operation}: parent is not a directory: {requested.parent}")
        return parent / requested.name

    def _read(self, file_path: str, offset: int | None = None, limit: int | None = None) -> str:
        """Read a zero-based line slice while bounding captured bytes."""
        if offset is not None and offset < 0:
            raise ValueError("Read: offset must be non-negative")
        if limit is not None and limit < 0:
            raise ValueError("Read: limit must be non-negative")
        descriptor, _path, parent_fd, _name = self._open_existing(file_path, "Read")
        start = offset or 0
        if limit == 0:
            os.close(descriptor)
            if parent_fd is not None:
                os.close(parent_fd)
            return ""

        selected = bytearray()
        current_line = 0
        selected_lines = 0
        decoder = codecs.getincrementaldecoder("utf-8")()
        truncated = False

        try:
            with os.fdopen(descriptor, "rb") as stream:
                while limit is None or selected_lines < limit:
                    chunk = stream.read(64 * 1024)
                    if not chunk:
                        decoder.decode(b"", final=True)
                        break
                    decoder.decode(chunk)
                    cursor = 0
                    while cursor < len(chunk):
                        newline = chunk.find(b"\n", cursor)
                        end = len(chunk) if newline < 0 else newline + 1
                        if current_line >= start:
                            remaining = self._max_read_bytes + 1 - len(selected)
                            selected.extend(chunk[cursor:end][:remaining])
                            if len(selected) > self._max_read_bytes:
                                truncated = True
                                break
                        if newline < 0:
                            break
                        if current_line >= start:
                            selected_lines += 1
                            if limit is not None and selected_lines >= limit:
                                break
                        current_line += 1
                        cursor = end
                    if truncated or (limit is not None and selected_lines >= limit):
                        break
        except UnicodeDecodeError as exc:
            raise ValueError(f"Read: file is not valid UTF-8: {file_path}") from exc
        except OSError as exc:
            raise ValueError(f"Read: could not read {file_path}: {exc}") from exc
        finally:
            if parent_fd is not None:
                os.close(parent_fd)

        if truncated:
            del selected[self._max_read_bytes :]
            while selected:
                try:
                    text = selected.decode("utf-8")
                    break
                except UnicodeDecodeError as exc:
                    if exc.end != len(selected):
                        raise ValueError(f"Read: file is not valid UTF-8: {file_path}") from exc
                    selected.pop()
            else:
                text = ""
            return text + _TRUNCATION_NOTICE.format(size=self._max_read_bytes)
        return selected.decode("utf-8")

    def _write(self, file_path: str, content: str) -> str:
        with self._mutation_lock:
            if os.name == "nt":
                target = self._write_target(file_path, "Write")
                _atomic_write_windows(target, content, self._file_roots, "Write")
            else:
                parent_fd, name, target = self._open_posix_parent(file_path, "Write")
                try:
                    try:
                        target_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        mode = None
                    else:
                        if not stat.S_ISREG(target_stat.st_mode):
                            raise ValueError(f"Write: path is not a file: {file_path}")
                        mode = stat.S_IMODE(target_stat.st_mode)
                    _atomic_write_posix(parent_fd, name, content, mode)
                finally:
                    os.close(parent_fd)
        return f"Wrote {len(content.encode('utf-8'))} bytes to {target}"

    def _edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
        with self._mutation_lock:
            descriptor, path, parent_fd, name = self._open_existing(file_path, "Edit")
            opened_stat = os.fstat(descriptor)
            try:
                with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                    content = stream.read()
            except UnicodeDecodeError as exc:
                if parent_fd is not None:
                    os.close(parent_fd)
                raise ValueError(f"Edit: file is not valid UTF-8: {file_path}") from exc
            except OSError as exc:
                if parent_fd is not None:
                    os.close(parent_fd)
                raise ValueError(f"Edit: could not read {file_path}: {exc}") from exc

            matches = content.count(old_string)
            if not old_string:
                if parent_fd is not None:
                    os.close(parent_fd)
                raise ValueError("Edit: old_string must not be empty; file was not changed")
            if matches == 0:
                if parent_fd is not None:
                    os.close(parent_fd)
                raise ValueError("Edit: old_string was not found; file was not changed")
            if matches > 1 and not replace_all:
                if parent_fd is not None:
                    os.close(parent_fd)
                raise ValueError(
                    f"Edit: old_string matched {matches} times; set replace_all=true to replace every match"
                )
            updated = content.replace(old_string, new_string, -1 if replace_all else 1)
            if os.name == "nt":
                check_fd, write_path, _unused_parent, _unused_name = self._open_existing(file_path, "Edit")
                try:
                    if os.path.samestat(opened_stat, os.fstat(check_fd)) is False:
                        raise ValueError("Edit: path changed while it was being edited; file was not changed")
                finally:
                    os.close(check_fd)
                _atomic_write_windows(write_path, updated, self._file_roots, "Edit")
            else:
                assert parent_fd is not None and name is not None
                try:
                    current_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    if not os.path.samestat(opened_stat, current_stat):
                        raise ValueError("Edit: path changed while it was being edited; file was not changed")
                    _atomic_write_posix(parent_fd, name, updated, stat.S_IMODE(opened_stat.st_mode))
                finally:
                    os.close(parent_fd)
        return f"Replaced {matches if replace_all else 1} occurrence(s) in {path}"

    async def _bash(self, command: str, timeout: float | None = None) -> dict[str, Any]:
        """Run a shell command; this is not path-confined by ``file_roots``."""
        if timeout is not None and timeout <= 0:
            raise ValueError("Bash: timeout must be greater than zero")

        windows_job = _WindowsJob() if os.name == "nt" else None
        try:
            if windows_job is not None:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-c",
                    _WINDOWS_SHELL_LAUNCHER,
                    command,
                    cwd=self._cwd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                process = await asyncio.create_subprocess_shell(
                    command,
                    cwd=self._cwd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
        except OSError as exc:
            if windows_job is not None:
                windows_job.close()
            raise ValueError(f"Bash: could not start command: {exc}") from exc
        if windows_job is not None:
            try:
                windows_job.assign(process)
            except OSError:
                process.kill()
                await process.wait()
                windows_job.close()
                raise
            assert process.stdin is not None
            process.stdin.write(b"1")
            await process.stdin.drain()
            process.stdin.close()

        captured = bytearray()
        stdout = bytearray()
        stderr = bytearray()
        truncated = False

        async def _drain(stream: asyncio.StreamReader, destination: bytearray) -> None:
            nonlocal truncated
            while chunk := await stream.read(64 * 1024):
                remaining = self._max_output_bytes - len(captured)
                if remaining > 0:
                    kept = chunk[:remaining]
                    captured.extend(kept)
                    destination.extend(kept)
                if len(chunk) > remaining:
                    truncated = True

        assert process.stdout is not None
        assert process.stderr is not None
        readers = [
            asyncio.create_task(_drain(process.stdout, stdout)),
            asyncio.create_task(_drain(process.stderr, stderr)),
        ]
        try:
            if timeout is None:
                await process.wait()
            else:
                await asyncio.wait_for(process.wait(), timeout)
            if windows_job is not None:
                windows_job.close()
            await asyncio.gather(*readers)
        except TimeoutError as exc:
            await _finish_termination(process, readers, windows_job)
            if os.name == "nt":
                cleanup = "its process tree was terminated"
            else:
                cleanup = "its process group and discovered descendants were terminated"
            raise TimeoutError(f"Bash: command timed out after {timeout}s and {cleanup}") from exc
        except BaseException:
            await _finish_termination(process, readers, windows_job)
            raise

        decoded_stdout, decoded_stderr, decode_truncated = _decode_bounded_output(
            stdout, stderr, self._max_output_bytes
        )
        return {
            "exit_code": process.returncode,
            "stdout": decoded_stdout,
            "stderr": decoded_stderr,
            "truncated": truncated or decode_truncated,
        }


def _atomic_write_posix(parent_fd: int, name: str, content: str, mode: int | None) -> None:
    """Atomically replace a leaf relative to an already verified POSIX directory."""
    temporary_name = f".{name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW  # type: ignore[attr-defined]
    descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary_name, mode, dir_fd=parent_fd, follow_symlinks=False)
        os.replace(temporary_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary_name, dir_fd=parent_fd)
        raise


def _atomic_write_windows(target: Path, content: str, roots: tuple[Path, ...], operation: str) -> None:
    """Atomically replace on Windows after validating the opened temp handle.

    Python does not expose descriptor-relative create/rename on Windows. The
    temp file is verified through its handle before data is written, but the
    final ``os.replace`` necessarily names the parent again.
    """
    mode: int | None = None
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        pass

    _validate_windows_parent(target.parent, roots, operation)
    descriptor = -1
    temporary: Path | None = None
    for _attempt in range(_WINDOWS_TEMP_CREATE_ATTEMPTS):
        candidate = target.parent / f".{target.name}.{secrets.token_hex(8)}.tmp"
        try:
            descriptor = os.open(
                candidate,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_BINARY,  # type: ignore[attr-defined]
                0o600,
            )
        except FileExistsError:
            continue
        except OSError as exc:
            raise ValueError(f"{operation}: could not create a temporary file in the verified parent") from exc
        temporary = candidate
        break
    else:
        raise ValueError(
            f"{operation}: could not create a unique temporary file after {_WINDOWS_TEMP_CREATE_ATTEMPTS} attempts"
        )

    try:
        actual_temporary = _path_from_windows_fd(descriptor)
        if not any(actual_temporary == root or root in actual_temporary.parents for root in roots):
            raise ValueError(f"{operation}: temporary file resolved outside the configured file roots")
        stream_descriptor = descriptor
        descriptor = -1
        with os.fdopen(stream_descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, target)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def _validate_windows_parent(parent: Path, roots: tuple[Path, ...], operation: str) -> None:
    """Reject a redirected or reparse-point parent before attempting temp creation."""
    descriptor = _open_windows_directory_fd(parent)
    try:
        parent_stat = os.fstat(descriptor)
        reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        attributes = getattr(parent_stat, "st_file_attributes", 0)
        if attributes & reparse_attribute:
            raise ValueError(f"{operation}: parent directory is a reparse point: {parent}")
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise ValueError(f"{operation}: parent is not a directory: {parent}")
        actual_parent = _path_from_windows_fd(descriptor)
        if actual_parent != parent or not any(actual_parent == root or root in actual_parent.parents for root in roots):
            raise ValueError(f"{operation}: parent directory resolved outside its expected location: {parent}")
    finally:
        os.close(descriptor)


def _open_windows_directory_fd(path: Path) -> int:
    """Open a directory itself, without following a final-component reparse point."""
    if os.name != "nt":
        raise OSError("Windows directory-handle verification is unavailable")
    import msvcrt
    from ctypes import wintypes

    file_read_attributes = 0x0080
    share_read_write_delete = 0x0001 | 0x0002 | 0x0004
    open_existing = 3
    backup_semantics = 0x02000000
    open_reparse_point = 0x00200000

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        file_read_attributes,
        share_read_write_delete,
        None,
        open_existing,
        backup_semantics | open_reparse_point,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)  # type: ignore[attr-defined]
    except BaseException:
        kernel32.CloseHandle(handle)
        raise


def _path_from_windows_fd(descriptor: int) -> Path:
    """Return the kernel-resolved path for an open Windows descriptor."""
    if os.name != "nt":
        raise OSError("Windows handle-path verification is unavailable")
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.GetFinalPathNameByHandleW
    function.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    function.restype = wintypes.DWORD
    handle = msvcrt.get_osfhandle(descriptor)
    needed = function(handle, None, 0, 0)
    if not needed:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_unicode_buffer(needed + 1)
    written = function(handle, buffer, len(buffer), 0)
    if not written or written >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    path = buffer.value
    if path.startswith("\\\\?\\UNC\\"):
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    return Path(path)


async def _finish_termination(
    process: asyncio.subprocess.Process,
    readers: list[asyncio.Task[None]],
    windows_job: _WindowsJob | None,
) -> None:
    """Terminate a subprocess tree and finish draining its already-piped output."""
    termination = asyncio.create_task(_terminate_process_tree(process, windows_job))
    try:
        await asyncio.shield(termination)
    except asyncio.CancelledError:
        await termination
    await asyncio.gather(*readers, return_exceptions=True)


async def _terminate_process_tree(process: asyncio.subprocess.Process, windows_job: _WindowsJob | None) -> None:
    if os.name == "nt":
        if windows_job is not None:
            windows_job.terminate()
            windows_job.close()
        if process.returncode is None:
            process.kill()
    else:
        descendants = _posix_descendants(process.pid)
        for pid in reversed(descendants):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGTERM)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)  # type: ignore[attr-defined]
        try:
            await asyncio.wait_for(process.wait(), 1.0)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)  # type: ignore[attr-defined]
        for pid in reversed(descendants):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
    await process.wait()


def _posix_descendants(root_pid: int) -> list[int]:
    """Snapshot descendants by PPID, including children outside the process group."""
    relationships: dict[int, list[int]] = {}
    proc = Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                # The comm field may contain spaces and parentheses; everything
                # after its final ')' starts with state then PPID.
                fields = (entry / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
                parent = int(fields[1])
            except (OSError, ValueError, IndexError):
                continue
            relationships.setdefault(parent, []).append(int(entry.name))
    else:
        try:
            result = subprocess.run(
                ["ps", "-A", "-o", "pid=", "-o", "ppid="],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        for line in result.stdout.splitlines():
            try:
                pid_text, parent_text = line.split()
                relationships.setdefault(int(parent_text), []).append(int(pid_text))
            except ValueError:
                continue

    descendants: list[int] = []
    pending = list(relationships.get(root_pid, ()))
    while pending:
        pid = pending.pop()
        descendants.append(pid)
        pending.extend(relationships.get(pid, ()))
    return descendants


def _decode_bounded_output(stdout: bytearray, stderr: bytearray, maximum: int) -> tuple[str, str, bool]:
    """Decode replacement-tolerantly while bounding the returned UTF-8 bytes."""
    remaining = maximum
    results: list[str] = []
    truncated = False
    for captured in (stdout, stderr):
        text = captured.decode("utf-8", errors="replace")
        encoded = text.encode("utf-8")
        if len(encoded) > remaining:
            text = encoded[:remaining].decode("utf-8", errors="ignore")
            truncated = True
        results.append(text)
        remaining -= len(text.encode("utf-8"))
    return results[0], results[1], truncated


class _WindowsJob:
    """Minimal Windows Job Object wrapper for kernel-enforced tree cleanup."""

    def __init__(self) -> None:
        self._handle: int | None = None
        if os.name != "nt":
            return

        from ctypes import wintypes

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())

        information = ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(information), ctypes.sizeof(information)):
            error = ctypes.WinError(ctypes.get_last_error())
            kernel32.CloseHandle(handle)
            raise error
        self._handle = handle

    def assign(self, process: asyncio.subprocess.Process) -> None:
        if self._handle is None:
            return
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        popen = process._transport.get_extra_info("subprocess")  # type: ignore[attr-defined]
        if not kernel32.AssignProcessToJobObject(self._handle, int(popen._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def terminate(self) -> None:
        if self._handle is None:
            return
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        if not kernel32.TerminateJobObject(self._handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self._handle is None:
            return
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle(self._handle)
        self._handle = None


__all__ = ["WorkspaceTools"]
