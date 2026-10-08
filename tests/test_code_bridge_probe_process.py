"""Windows Job Object ownership/lifecycle with a fake kernel; launches nothing."""

from __future__ import annotations

import ctypes

import pytest

from lazytools.code_bridge import _probe_process as probe


class Kernel:
    def __init__(self):
        self.calls = []
        self.fail_assignment = False
        self.fail_limits = False

    def CreateJobObjectW(self, *args):
        self.calls.append(("create",))
        return 123

    def SetInformationJobObject(self, job, kind, data, size):
        flags = ctypes.cast(data, ctypes.POINTER(probe._ExtendedLimits)).contents.BasicLimitInformation.LimitFlags
        self.calls.append(("limits", kind, flags, size))
        return not self.fail_limits

    def OpenProcess(self, rights, inherit, pid):
        self.calls.append(("process", rights, inherit, pid))
        return 234

    def AssignProcessToJobObject(self, job, process):
        self.calls.append(("assign", job, process))
        return not self.fail_assignment

    def CreateToolhelp32Snapshot(self, kind, pid):
        self.calls.append(("snapshot", kind, pid))
        return 345

    def Thread32First(self, snapshot, pointer):
        entry = ctypes.cast(pointer, ctypes.POINTER(probe._ThreadEntry)).contents
        entry.th32OwnerProcessID, entry.th32ThreadID = 4321, 5678
        return True

    def OpenThread(self, rights, inherit, thread_id):
        self.calls.append(("thread", rights, inherit, thread_id))
        return 456

    def ResumeThread(self, thread):
        self.calls.append(("resume", thread))
        return 1

    def CloseHandle(self, handle):
        self.calls.append(("close", handle))
        return True


@pytest.fixture
def kernel(monkeypatch):
    kernel = Kernel()
    monkeypatch.setattr(probe, "_kernel32", lambda: kernel)
    monkeypatch.setattr(probe, "_error", lambda operation: OSError(operation))
    return kernel


def test_job_is_kill_on_close_and_assignment_precedes_resume(kernel):
    job = probe.WindowsProbeJob()
    job.attach_and_resume(4321)
    assert kernel.calls[1] == ("limits", 9, 0x2000, ctypes.sizeof(probe._ExtendedLimits))
    assert kernel.calls.index(("assign", 123, 234)) < kernel.calls.index(("resume", 456))
    assert ("process", 0x0101, False, 4321) in kernel.calls
    assert ("thread", 2, False, 5678) in kernel.calls
    assert ("close", 234) in kernel.calls and ("close", 345) in kernel.calls and ("close", 456) in kernel.calls
    job.close()
    job.close()
    assert kernel.calls.count(("close", 123)) == 1


def test_failed_assignment_never_resumes_child_and_closes_process_handle(kernel):
    kernel.fail_assignment = True
    job = probe.WindowsProbeJob()
    with pytest.raises(OSError, match="AssignProcessToJobObject"):
        job.attach_and_resume(4321)
    job.close()
    assert not any(call[0] == "resume" for call in kernel.calls)
    assert ("close", 234) in kernel.calls and ("close", 123) in kernel.calls


def test_failed_limits_close_the_job_before_any_probe_spawn(kernel):
    kernel.fail_limits = True
    with pytest.raises(OSError, match="SetInformationJobObject"):
        probe.WindowsProbeJob()
    assert ("close", 123) in kernel.calls
