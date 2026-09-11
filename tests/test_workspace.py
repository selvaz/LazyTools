"""WorkspaceTools confinement, atomicity, bounds, and process cleanup."""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

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
    with pytest.raises(TimeoutError, match="process tree was terminated"):
        await bash.run(command=command, timeout=2.0)
    assert started.exists(), "the parent did not reach the child-spawn point, so the test was inconclusive"
    await asyncio.sleep(4.0)
    assert not marker.exists(), "the timed-out command's child process survived"


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
async def test_concurrent_writes_are_whole_file_last_writer_wins(tmp_path: Path) -> None:
    write = _tools(WorkspaceTools([tmp_path], tmp_path))["Write"]
    first = "A" * 500_000
    second = "B" * 500_000

    await asyncio.gather(
        write.run(file_path="shared.txt", content=first),
        write.run(file_path="shared.txt", content=second),
    )
    assert (tmp_path / "shared.txt").read_text(encoding="utf-8") in {first, second}
