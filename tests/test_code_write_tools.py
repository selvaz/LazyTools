"""CodeWriteTools — the gated, sandboxed write path for the coding CLIs.

The contract under test (mirrors the Gmail send tools):

* write tools exist only if the developer constructs the provider;
* every call's cwd must resolve inside ``base_dir`` (escape → blocked,
  and a blocked cwd must NOT burn a confirmation grant);
* with ``require_confirmation=True`` (default) each call consumes exactly
  one ``confirm_write()`` grant — one approval can never authorize a flood;
* grants can be scope-bound to a task id;
* successful output is labelled ``content_is_untrusted``.

All subprocess calls are mocked — no real CLIs are required.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from lazytools.connectors.code_support import CodeWriteBlocked, CodeWriteTools
from lazytools.safety.context import active_scope


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    p = MagicMock()
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


def _claude_payload(result: str = "done") -> str:
    return json.dumps({"subtype": "success", "result": result})


def _writer(tmp_path, **kwargs) -> CodeWriteTools:
    return CodeWriteTools(base_dir=str(tmp_path), **kwargs)


# ─── construction ─────────────────────────────────────────────────────────────


def test_base_dir_is_mandatory_and_must_exist(tmp_path):
    with pytest.raises(TypeError):
        CodeWriteTools()  # type: ignore[call-arg]  — no ungated default
    with pytest.raises(ValueError, match="not an existing directory"):
        CodeWriteTools(base_dir=str(tmp_path / "nope"))


def test_default_tool_surface_is_claude_only(tmp_path):
    names = [t.name for t in _writer(tmp_path).as_tools()]
    assert names == ["claude_code_write"]

    both = _writer(tmp_path, codex=True)
    assert [t.name for t in both.as_tools()] == ["claude_code_write", "codex_write"]


# ─── confirmation gate ────────────────────────────────────────────────────────


async def test_write_blocked_without_confirmation(tmp_path):
    writer = _writer(tmp_path)
    with pytest.raises(CodeWriteBlocked, match="no outstanding write confirmation"):
        await writer._claude_write("edit the file")


async def test_one_grant_authorizes_exactly_one_write(tmp_path):
    writer = _writer(tmp_path)
    writer.confirm_write()
    with patch("subprocess.run", return_value=_proc(stdout=_claude_payload())):
        out = await writer._claude_write("edit the file")
    assert out == {"result": "done", "content_is_untrusted": True}

    # The grant is spent — a second call must block (no flood after one OK).
    with pytest.raises(CodeWriteBlocked):
        await writer._claude_write("edit it again")


async def test_scope_bound_grant_not_spendable_outside_its_task(tmp_path):
    writer = _writer(tmp_path)
    writer.confirm_write(task_id="task-A")

    # No active scope → the task-bound grant must not match.
    with pytest.raises(CodeWriteBlocked):
        await writer._claude_write("edit")

    token = active_scope.set("task-A")
    try:
        with patch("subprocess.run", return_value=_proc(stdout=_claude_payload())):
            out = await writer._claude_write("edit")
    finally:
        active_scope.reset(token)
    assert out["result"] == "done"


async def test_require_confirmation_false_skips_gate_but_keeps_sandbox(tmp_path):
    writer = _writer(tmp_path, require_confirmation=False)
    with patch("subprocess.run", return_value=_proc(stdout=_claude_payload())):
        out = await writer._claude_write("edit")  # no grant needed
    assert out["result"] == "done"
    with pytest.raises(CodeWriteBlocked, match="outside base_dir"):
        await writer._claude_write("edit", cwd="../outside")


# ─── base_dir sandbox ─────────────────────────────────────────────────────────


async def test_cwd_defaults_to_base_dir_and_subdirs_allowed(tmp_path):
    (tmp_path / "pkg").mkdir()
    writer = _writer(tmp_path, require_confirmation=False)
    with patch("subprocess.run", return_value=_proc(stdout=_claude_payload())) as mock_run:
        await writer._claude_write("edit")
        assert mock_run.call_args.kwargs["cwd"] == str(tmp_path)
        await writer._claude_write("edit", cwd="pkg")
        assert mock_run.call_args.kwargs["cwd"] == str(tmp_path / "pkg")


async def test_cwd_escape_blocked_and_does_not_burn_grant(tmp_path):
    writer = _writer(tmp_path)
    writer.confirm_write()

    with pytest.raises(CodeWriteBlocked, match="outside base_dir"):
        await writer._claude_write("edit", cwd="../..")
    with pytest.raises(CodeWriteBlocked, match="outside base_dir"):
        await writer._claude_write("edit", cwd="/etc")

    # The escape attempts must not have consumed the grant.
    with patch("subprocess.run", return_value=_proc(stdout=_claude_payload())):
        out = await writer._claude_write("edit")
    assert out["result"] == "done"


async def test_missing_subdir_inside_sandbox_blocked(tmp_path):
    writer = _writer(tmp_path, require_confirmation=False)
    with pytest.raises(CodeWriteBlocked, match="not a directory"):
        await writer._claude_write("edit", cwd="does-not-exist")


# ─── CLI flag wiring ──────────────────────────────────────────────────────────


async def test_claude_write_uses_accept_edits_flags(tmp_path):
    writer = _writer(tmp_path, require_confirmation=False)
    with patch("subprocess.run", return_value=_proc(stdout=_claude_payload())) as mock_run:
        await writer._claude_write("edit")
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "claude"
    assert "acceptEdits" in cmd
    assert "Write" in " ".join(cmd)


async def test_codex_write_uses_workspace_write_and_keeps_git_rail(tmp_path):
    writer = _writer(tmp_path, codex=True, require_confirmation=False)
    with (
        # Mocked at resolve_codex_bin()'s defining module, not at
        # lazybridge.engines.codex directly — that submodule is absent on
        # the declared/verified minimum lazybridge version (see
        # test_code_review_tool.py's compatibility fixture), where patching
        # it would raise ModuleNotFoundError before this test body runs.
        patch(
            "lazytools.connectors.code_support._codex.resolve_codex_bin",
            return_value="/resolved/codex",
        ),
        patch("subprocess.run", return_value=_proc(stdout="done")) as mock_run,
    ):
        out = await writer._codex_write("edit")
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "/resolved/codex"
    # `codex exec` has no `--full-auto`/`-a` flag on current CLI builds
    # (verified live against `codex exec --help`; passing it is a hard
    # argument-parsing error). `-c approval_policy=never` pins the same
    # non-interactive guarantee `--full-auto` used to (verified live,
    # including a task that runs a shell command).
    assert "--full-auto" not in cmd
    assert "workspace-write" in cmd
    assert "approval_policy=never" in cmd
    # Writes keep git as the recovery rail by default.
    assert "--skip-git-repo-check" not in cmd
    assert out == {"result": "done", "content_is_untrusted": True}


async def test_connector_error_strings_pass_through_unlabelled(tmp_path):
    writer = _writer(tmp_path, require_confirmation=False)
    with patch("subprocess.run", return_value=_proc(returncode=1, stderr="boom")):
        out = await writer._claude_write("edit")
    assert isinstance(out, str) and out.startswith("[claude_code]")


# ─── collaboration integration ────────────────────────────────────────────────


def test_collaboration_defaults_to_three_readonly_sessions():
    from lazytools.connectors.code_support import build_cli_collaboration

    pipeline = build_cli_collaboration()
    step_names = [s.name for s in pipeline.engine.steps]
    assert step_names == ["claude_analyst", "codex_analyst", "synthesizer"]  # no executor


def test_collaboration_execute_requires_base_dir_or_writer(tmp_path):
    from lazytools.connectors.code_support import build_cli_collaboration

    with pytest.raises(ValueError, match=r"requires base_dir= .*or writer="):
        build_cli_collaboration(execute=True)

    pipeline = build_cli_collaboration(execute=True, base_dir=str(tmp_path))
    step_names = [s.name for s in pipeline.engine.steps]
    assert step_names == ["claude_analyst", "codex_analyst", "synthesizer", "executor"]


def test_collaboration_gated_execution_via_caller_owned_writer(tmp_path):
    """Codex review (#32): a gate-enabled writer must be caller-owned, so the
    human holds the confirm_write() handle while the pipeline runs."""
    from lazytools.connectors.code_support import build_cli_collaboration

    writer = CodeWriteTools(base_dir=str(tmp_path))  # gate ON by default
    pipeline = build_cli_collaboration(execute=True, writer=writer)
    assert [s.name for s in pipeline.engine.steps][-1] == "executor"

    # The handle works: a grant issued on the caller's instance is the one
    # the executor's tool consumes.
    writer.confirm_write()
    assert writer._gate.consume("write") is True

    # Mutually exclusive / read-only argument validation.
    with pytest.raises(ValueError, match="not both"):
        build_cli_collaboration(execute=True, writer=writer, base_dir=str(tmp_path))
    with pytest.raises(ValueError, match="only apply with execute=True"):
        build_cli_collaboration(base_dir=str(tmp_path))
