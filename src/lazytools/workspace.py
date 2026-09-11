"""Confined workspace file tools and an opt-in shell tool."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import ctypes
import os
import signal
import stat
import sys
import tempfile
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


class WorkspaceTools:
    """A LazyBridge tool provider for file work and optional shell commands.

    ``Read``, ``Write``, and ``Edit`` are confined to ``file_roots``. ``Bash``
    is exposed only when explicitly enabled and is intentionally not confined
    to those roots; authorization belongs to the engine's approval gate.
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
        self._cwd = Path(cwd).resolve()
        self._enable_bash = enable_bash
        self._max_read_bytes = max_read_bytes
        self._max_output_bytes = max_output_bytes
        self._mutation_lock = threading.Lock()

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
                        "exit code, stdout, and stderr. Output is bounded and timeouts terminate the "
                        "spawned process tree. Bash is NOT path-confined by file_roots: once a command "
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

    def _existing_file(self, file_path: str, operation: str) -> Path:
        requested = self._requested_path(file_path)
        try:
            resolved = requested.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"{operation}: file does not exist: {file_path}") from exc
        if not self._contained(resolved):
            raise ValueError(f"{operation}: path is outside the configured file roots: {file_path}")
        if not resolved.is_file():
            raise ValueError(f"{operation}: path is not a file: {file_path}")
        return resolved

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
        path = self._existing_file(file_path, "Read")
        start = offset or 0
        if limit == 0:
            return ""

        selected = bytearray()
        current_line = 0
        selected_lines = 0
        decoder = codecs.getincrementaldecoder("utf-8")()
        truncated = False

        try:
            with path.open("rb") as stream:
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
        target = self._write_target(file_path, "Write")
        with self._mutation_lock:
            target = self._write_target(file_path, "Write")
            _atomic_write(target, content)
        return f"Wrote {len(content.encode('utf-8'))} bytes to {target}"

    def _edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
        path = self._existing_file(file_path, "Edit")
        with self._mutation_lock:
            path = self._existing_file(file_path, "Edit")
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"Edit: file is not valid UTF-8: {file_path}") from exc
            except OSError as exc:
                raise ValueError(f"Edit: could not read {file_path}: {exc}") from exc

            matches = content.count(old_string)
            if not old_string:
                raise ValueError("Edit: old_string must not be empty; file was not changed")
            if matches == 0:
                raise ValueError("Edit: old_string was not found; file was not changed")
            if matches > 1 and not replace_all:
                raise ValueError(
                    f"Edit: old_string matched {matches} times; set replace_all=true to replace every match"
                )
            updated = content.replace(old_string, new_string, -1 if replace_all else 1)
            write_path = self._existing_file(file_path, "Edit")
            if write_path != path:
                raise ValueError("Edit: path changed while it was being edited; file was not changed")
            _atomic_write(write_path, updated)
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
            raise TimeoutError(f"Bash: command timed out after {timeout}s and its process tree was terminated") from exc
        except BaseException:
            await _finish_termination(process, readers, windows_job)
            raise

        return {
            "exit_code": process.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "truncated": truncated,
        }


def _atomic_write(target: Path, content: str) -> None:
    """Replace ``target`` only after a complete same-directory temp write."""
    mode: int | None = None
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        pass

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


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
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)  # type: ignore[attr-defined]
        try:
            await asyncio.wait_for(process.wait(), 1.0)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)  # type: ignore[attr-defined]
    await process.wait()


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
