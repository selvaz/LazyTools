"""Detached jobs must run with a hidden console that their children inherit."""
from __future__ import annotations

import os
import subprocess

import pytest

from lazytools.code_bridge import cli


@pytest.mark.skipif(os.name != "nt", reason="Windows creation flags")
def test_detached_job_uses_hidden_console_not_detached_process(monkeypatch, tmp_path):
    seen: list[int] = []

    class _Proc:
        pid = 4242

    def fake_popen(argv, creationflags=0, **kwargs):
        seen.append(creationflags)
        return _Proc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert cli._spawn_detached(["python", "-c", "pass"], tmp_path / "job.log") == 4242
    flags = seen[0]
    assert flags & 0x08000000, "CREATE_NO_WINDOW must be set"
    assert not flags & 0x00000008, "DETACHED_PROCESS gives children visible consoles"
    assert flags & 0x00000200, "new process group keeps Ctrl+C from reaching the job"
