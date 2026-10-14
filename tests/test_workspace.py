"""WorkspaceTools confinement, atomicity, bounds, and process cleanup."""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

import lazytools.workspace as workspace_module
from lazytools.workspace import WorkspaceTools


def _tools(provider: WorkspaceTools):
    return {tool.name: tool for tool in provider.as_tools()}


@pytest.mark.asyncio
async def test_read_write_edit_absolute_and_cwd_relative(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    provider = WorkspaceTools([root], root)
    tools = _tools(provider)

    await tools["Write"].run(file_path="notes.txt", content="one\ntwo\nthree\n")
    await tools["Write"].run(file_path=str(root / "absolute.txt"), content="absolute")
    assert (root / "absolute.txt").read_text(encoding="utf-8") == "absolute"
    assert await tools["Read"].run(file_path=str(root / "notes.txt"), offset=1, limit=1) == "two\n"

    await tools["Edit"].run(file_path=str(root / "notes.txt"), old_string="two", new_string="TWO", replace_all=False)
    await tools["Edit"].run(file_path="notes.txt", old_string="three", new_string="THREE", replace_all=False)
    assert await tools["Read"].run(file_path="notes.txt") == "one\nTWO\nTHREE\n"


@pytest.mark.asyncio
async def test_dot_dot_traversal_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    tools = _tools(WorkspaceTools([root], root))

    with pytest.raises(ValueError, match="outside"):
        await tools["Read"].run(file_path="../outside.txt")
    with pytest.raises(ValueError, match="outside"):
        await tools["Write"].run(file_path="../outside.txt", content="changed")
    assert outside.read_text(encoding="utf-8") == "secret"


def _make_directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError as symlink_error:
        if os.name != "nt":
            pytest.skip(f"directory symlinks are unavailable: {symlink_error}")
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            pytest.skip(
                "neither a directory symlink nor a junction can be created in this environment: "
                f"{symlink_error}; {result.stderr.strip() or result.stdout.strip()}"
            )


def _remove_directory_link(link: Path) -> None:
    if os.name == "nt":
        os.rmdir(link)
    else:
        link.unlink()


def _swap_directory_for_link(directory: Path, parked: Path, outside: Path) -> None:
    directory.rename(parked)
    _make_directory_link(directory, outside)


def _restore_swapped_directory(directory: Path, parked: Path) -> None:
    if directory.exists() or directory.is_symlink():
        _remove_directory_link(directory)
    parked.rename(directory)


@pytest.mark.asyncio
async def test_link_to_outside_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    _make_directory_link(root / "escape", outside)
    tools = _tools(WorkspaceTools([root], root))

    with pytest.raises(ValueError, match="outside"):
        await tools["Read"].run(file_path="escape/secret.txt")
    with pytest.raises(ValueError, match="outside"):
        await tools["Write"].run(file_path="escape/new.txt", content="no")
    assert not (outside / "new.txt").exists()


@pytest.mark.asyncio
async def test_read_uses_opened_file_not_path_checked_before_ancestor_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    directory = root / "d"
    parked = root / "d-parked"
    outside = tmp_path / "outside"
    directory.mkdir(parents=True)
    outside.mkdir()
    target = directory / "target.txt"
    target.write_text("inside", encoding="utf-8")
    (outside / "target.txt").write_text("outside-secret", encoding="utf-8")
    read = _tools(WorkspaceTools([root], root))["Read"]

    original_open = Path.open
    swapped = False

    def swap_after_old_confinement_check(path: Path, *args, **kwargs):
        nonlocal swapped
        if not swapped and path == target:
            _swap_directory_for_link(directory, parked, outside)
            swapped = True
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", swap_after_old_confinement_check)
    try:
        result = await read.run(file_path="d/target.txt")
        assert result == "inside"
    finally:
        if swapped:
            _restore_swapped_directory(directory, parked)


@pytest.mark.parametrize("operation", ["Write", "Edit"])
@pytest.mark.skipif(os.name != "nt", reason="exercises Windows parent-handle validation")
@pytest.mark.asyncio
async def test_mutation_does_not_replace_outside_target_after_ancestor_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    root = tmp_path / "root"
    directory = root / "d"
    parked = root / "d-parked"
    outside = tmp_path / "outside"
    directory.mkdir(parents=True)
    outside.mkdir()
    (directory / "target.txt").write_text("inside", encoding="utf-8")
    outside_target = outside / "target.txt"
    outside_target.write_text("outside", encoding="utf-8")
    tool = _tools(WorkspaceTools([root], root))[operation]

    original_validate_parent = workspace_module._validate_windows_parent
    swapped = False

    def swap_before_parent_validation(parent: Path, roots: tuple[Path, ...], checked_operation: str) -> None:
        nonlocal swapped
        if not swapped:
            _swap_directory_for_link(directory, parked, outside)
            swapped = True
        original_validate_parent(parent, roots, checked_operation)

    monkeypatch.setattr(workspace_module, "_validate_windows_parent", swap_before_parent_validation)
    try:
        if operation == "Write":
            arguments = {"file_path": "d/target.txt", "content": "changed"}
        else:
            arguments = {"file_path": "d/target.txt", "old_string": "inside", "new_string": "changed"}
        with pytest.raises(ValueError, match=r"parent directory (?:is a reparse point|resolved outside)"):
            await asyncio.wait_for(tool.run(**arguments), timeout=10)
        assert swapped
        assert outside_target.read_text(encoding="utf-8") == "outside"
    finally:
        if swapped:
            _restore_swapped_directory(directory, parked)


@pytest.mark.skipif(os.name != "nt", reason="exercises the Windows bounded temp-create loop")
def test_atomic_write_windows_bounds_temp_name_collision_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0

    def collide(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise FileExistsError("simulated collision-shaped create failure")

    monkeypatch.setattr(workspace_module, "_validate_windows_parent", lambda *args: None)
    monkeypatch.setattr(workspace_module.os, "open", collide)

    with pytest.raises(ValueError, match=r"unique temporary file after 8 attempts"):
        workspace_module._atomic_write_windows(tmp_path / "target.txt", "changed", (tmp_path,), "Write")
    assert attempts == workspace_module._WINDOWS_TEMP_CREATE_ATTEMPTS == 8


@pytest.mark.asyncio
async def test_write_requires_an_existing_in_root_parent(tmp_path: Path) -> None:
    root = tmp_path / "root"
    child = root / "child"
    outside = tmp_path / "outside"
    child.mkdir(parents=True)
    outside.mkdir()
    tools = _tools(WorkspaceTools([root], root))

    await tools["Write"].run(file_path="child/new.txt", content="created")
    assert (child / "new.txt").read_text(encoding="utf-8") == "created"
    with pytest.raises(ValueError, match="parent directory does not exist"):
        await tools["Write"].run(file_path="missing/new.txt", content="no")
    with pytest.raises(ValueError, match="outside"):
        await tools["Write"].run(file_path=str(outside / "new.txt"), content="no")


@pytest.mark.asyncio
async def test_edit_match_rules_do_not_touch_file_on_error(tmp_path: Path) -> None:
    path = tmp_path / "repeated.txt"
    path.write_text("same same", encoding="utf-8")
    edit = _tools(WorkspaceTools([tmp_path], tmp_path))["Edit"]

    with pytest.raises(ValueError, match="not found"):
        await edit.run(file_path="repeated.txt", old_string="absent", new_string="x")
    assert path.read_text(encoding="utf-8") == "same same"
    with pytest.raises(ValueError, match="matched 2 times"):
        await edit.run(file_path="repeated.txt", old_string="same", new_string="x")
    assert path.read_text(encoding="utf-8") == "same same"

    await edit.run(file_path="repeated.txt", old_string="same", new_string="x", replace_all=True)
    assert path.read_text(encoding="utf-8") == "x x"


@pytest.mark.parametrize("preexisting", [False, True])
@pytest.mark.asyncio
async def test_failed_atomic_replace_preserves_original_or_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preexisting: bool
) -> None:
    path = tmp_path / "target.txt"
    if preexisting:
        path.write_text("original", encoding="utf-8")
    write = _tools(WorkspaceTools([tmp_path], tmp_path))["Write"]

    real_fdopen = os.fdopen

    class PartialWriter:
        def __init__(self, stream) -> None:
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            self.stream.close()

        def write(self, content: str) -> None:
            self.stream.write(content[:3])
            self.stream.flush()
            raise OSError("simulated crash during temp-file write")

    def fail_mid_write(descriptor, *args, **kwargs):
        return PartialWriter(real_fdopen(descriptor, *args, **kwargs))

    monkeypatch.setattr(os, "fdopen", fail_mid_write)
    with pytest.raises(OSError, match="simulated crash"):
        await write.run(file_path="target.txt", content="replacement")

    if preexisting:
        assert path.read_text(encoding="utf-8") == "original"
    else:
        assert not path.exists()
    assert not list(tmp_path.glob(".target.txt.*.tmp"))


@pytest.mark.asyncio
async def test_read_bounds_and_truncation_are_explicit(tmp_path: Path) -> None:
    path = tmp_path / "large.txt"
    path.write_bytes(b"first\nsecond\nthird\n")
    read = _tools(WorkspaceTools([tmp_path], tmp_path, max_read_bytes=8))["Read"]

    assert await read.run(file_path="large.txt", offset=1, limit=1) == "second\n"
    truncated = await read.run(file_path="large.txt")
    assert truncated.startswith("first\nse")
    assert "truncated after 8 bytes" in truncated


def test_bash_is_only_exposed_when_enabled(tmp_path: Path) -> None:
    assert set(_tools(WorkspaceTools([tmp_path], tmp_path))) == {"Read", "Write", "Edit"}
    assert set(_tools(WorkspaceTools([tmp_path], tmp_path, enable_bash=True))) == {"Read", "Write", "Edit", "Bash"}


@pytest.mark.asyncio
async def test_bash_timeout_kills_real_child_process(tmp_path: Path) -> None:
    marker = tmp_path / "child-survived"
    started = tmp_path / "child-started"
    child = tmp_path / "child.py"
    parent = tmp_path / "parent.py"
    child.write_text(
        "import pathlib, sys, time\ntime.sleep(4)\npathlib.Path(sys.argv[1]).write_text('alive')\n",
        encoding="utf-8",
    )
    parent.write_text(
        "import pathlib, subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
        "pathlib.Path(sys.argv[3]).write_text('started')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    argv = [sys.executable, str(parent), str(child), str(marker), str(started)]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    bash = _tools(WorkspaceTools([tmp_path], tmp_path, enable_bash=True))["Bash"]

    # 2s, not 0.5s: on Windows this command chain is 2-3 nested cold
    # interpreter starts (the launcher, cmd.exe, parent.py) before the
    # parent even reaches its own Popen call -- 0.5s made the test fail
    # on "parent never got that far" rather than exercising the kill path
    # it exists to test.
    with pytest.raises(TimeoutError, match="terminated"):
        await bash.run(command=command, timeout=2.0)
    assert started.exists(), "the parent did not reach the child-spawn point, so the test was inconclusive"
    await asyncio.sleep(4.0)
    assert not marker.exists(), "the timed-out command's child process survived"


@pytest.mark.skipif(os.name == "nt", reason="setsid is POSIX-only; Windows uses a Job Object")
@pytest.mark.asyncio
async def test_bash_timeout_kills_descendant_that_calls_setsid(tmp_path: Path) -> None:
    marker = tmp_path / "detached-child-survived"
    started = tmp_path / "detached-child-started"
    child = tmp_path / "detached.py"
    parent = tmp_path / "parent.py"
    child.write_text(
        "import os, pathlib, sys, time\nos.setsid()\ntime.sleep(3)\npathlib.Path(sys.argv[1]).write_text('alive')\n",
        encoding="utf-8",
    )
    parent.write_text(
        "import pathlib, subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
        "pathlib.Path(sys.argv[3]).write_text('started')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    command = shlex.join([sys.executable, str(parent), str(child), str(marker), str(started)])
    bash = _tools(WorkspaceTools([tmp_path], tmp_path, enable_bash=True))["Bash"]

    with pytest.raises(TimeoutError, match="discovered descendants were terminated"):
        await bash.run(command=command, timeout=1.5)
    assert started.exists(), "the detached child was never spawned, so the test was inconclusive"
    await asyncio.sleep(3.0)
    assert not marker.exists(), "the setsid descendant survived timeout cleanup"


@pytest.mark.asyncio
async def test_bash_caps_output_while_draining(tmp_path: Path) -> None:
    argv = [sys.executable, "-c", "import sys; sys.stdout.write('x' * 100000); sys.stderr.write('y' * 100000)"]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    bash = _tools(WorkspaceTools([tmp_path], tmp_path, enable_bash=True, max_output_bytes=1024))["Bash"]

    result = await bash.run(command=command)
    assert result["exit_code"] == 0
    assert len(result["stdout"].encode()) + len(result["stderr"].encode()) == 1024
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_bash_caps_returned_utf8_after_replacement_decoding(tmp_path: Path) -> None:
    argv = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff' * 10)"]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    bash = _tools(WorkspaceTools([tmp_path], tmp_path, enable_bash=True, max_output_bytes=10))["Bash"]

    result = await bash.run(command=command)
    assert result["stdout"] == "\ufffd" * 3
    assert len(result["stdout"].encode("utf-8")) + len(result["stderr"].encode("utf-8")) <= 10
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_concurrent_writes_are_whole_file_last_writer_wins(tmp_path: Path) -> None:
    write = _tools(WorkspaceTools([tmp_path], tmp_path))["Write"]
    first = "A" * 500_000
    second = "B" * 500_000

    await asyncio.gather(
        write.run(file_path="shared.txt", content=first),
        write.run(file_path="shared.txt", content=second),
    )
    assert (tmp_path / "shared.txt").read_text(encoding="utf-8") in {first, second}
