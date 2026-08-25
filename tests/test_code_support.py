"""Tests for the CLI-mode code-support tools (claude_code, codex, check_clis_available).

All subprocess calls are mocked — no real CLIs are required.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from lazytools.connectors.code_support import check_clis_available, claude_code, codex

# ─── helpers ──────────────────────────────────────────────────────────────────


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    p = MagicMock()
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


# ─── claude_code ──────────────────────────────────────────────────────────────


class TestClaudeCode:
    def test_read_mode_returns_result(self):
        payload = json.dumps({"subtype": "success", "result": "3 .py files found"})
        with patch("subprocess.run", return_value=_proc(stdout=payload)) as mock_run:
            result = claude_code("count .py files", mode="read")
        assert result == {"result": "3 .py files found", "content_is_untrusted": True}
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--output-format" in cmd
        assert "json" in cmd
        assert "--allowedTools" in cmd

    def test_write_mode_rejected_points_at_writer(self):
        # Write capability is NOT reachable through the plain function: the
        # LLM controls arguments, so mode='write' must not be an argument.
        result = claude_code("add docstring", mode="write")
        assert isinstance(result, str)
        assert "invalid mode" in result
        assert "CodeWriteTools" in result

    def test_plan_mode_flags(self):
        payload = json.dumps({"subtype": "success", "result": "plan ready"})
        with patch("subprocess.run", return_value=_proc(stdout=payload)) as mock_run:
            claude_code("plan refactor", mode="plan")
        cmd = mock_run.call_args[0][0]
        assert "--permission-mode" in cmd
        assert "plan" in cmd

    def test_session_id_adds_resume_flag(self):
        payload = json.dumps({"subtype": "success", "result": "resumed"})
        with patch("subprocess.run", return_value=_proc(stdout=payload)) as mock_run:
            claude_code("continue", mode="read", session_id="abc123")
        cmd = mock_run.call_args[0][0]
        assert "--resume" in cmd
        assert "abc123" in cmd

    def test_cli_not_found_returns_error(self):
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            result = claude_code("task")
        assert "[claude_code]" in result
        assert "not found" in result

    def test_timeout_returns_error(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 300)):
            result = claude_code("task", timeout=300.0)
        assert "[claude_code]" in result
        assert "timeout" in result

    def test_nonzero_exit_returns_error(self):
        with patch("subprocess.run", return_value=_proc(returncode=1, stderr="auth failed")):
            result = claude_code("task")
        assert "[claude_code]" in result
        assert "exit 1" in result

    def test_error_subtype_propagated(self):
        payload = json.dumps({"subtype": "error", "result": "model overloaded"})
        with patch("subprocess.run", return_value=_proc(stdout=payload)):
            result = claude_code("task")
        assert "model overloaded" in result

    def test_non_json_output_returned_raw(self):
        with patch("subprocess.run", return_value=_proc(stdout="raw text output\n")):
            result = claude_code("task")
        assert result == {"result": "raw text output", "content_is_untrusted": True}

    def test_invalid_mode_returns_error(self):
        result = claude_code("task", mode="badmode")
        assert "[claude_code]" in result
        assert "invalid mode" in result

    def test_does_not_override_oauth_env(self):
        # Auth is left to the CLI: we must not synthesize/override env, so
        # subprocess.run is called without an explicit env= kwarg (inherits).
        payload = json.dumps({"subtype": "success", "result": "ok"})
        with patch("subprocess.run", return_value=_proc(stdout=payload)) as mock_run:
            claude_code("task")
        assert "env" not in mock_run.call_args.kwargs


# ─── codex ────────────────────────────────────────────────────────────────────


class TestCodex:
    #: resolve_codex_bin() is mocked at its defining module, not at
    #: lazybridge.engines.codex directly — that submodule is absent on
    #: lazybridge==1.2.0 (the declared/verified minimum, see
    #: test_code_review_tool.py's compatibility fixture), where patching it
    #: would raise ModuleNotFoundError before the test body even runs.
    #: resolve_codex_bin() itself already handles that absence (falls back to
    #: shutil.which), so mocking it directly also exercises less incidental
    #: surface per test.
    @pytest.fixture(autouse=True)
    def _fake_codex_executable(self):
        with patch(
            "lazytools.connectors.code_support._codex.resolve_codex_bin",
            return_value="/resolved/codex",
        ):
            yield

    def test_read_mode_returns_result(self):
        with patch("subprocess.run", return_value=_proc(stdout="function list: foo, bar")) as mock_run:
            result = codex("list functions in main.py")
        assert result == {"result": "function list: foo, bar", "content_is_untrusted": True}
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/resolved/codex"
        assert "exec" in cmd
        assert "-s" in cmd
        assert "read-only" in cmd
        # `codex exec` has no `-a` approval flag — it must not be emitted.
        assert "-a" not in cmd

    def test_codex_has_no_write_mode_parameter(self):
        # Write capability is NOT reachable through the plain function.
        import inspect

        assert "mode" not in inspect.signature(codex).parameters

    def test_skip_git_check_added_by_default(self):
        with patch("subprocess.run", return_value=_proc(stdout="ok")) as mock_run:
            codex("task")
        cmd = mock_run.call_args[0][0]
        assert "--skip-git-repo-check" in cmd

    def test_skip_git_check_omittable(self):
        with patch("subprocess.run", return_value=_proc(stdout="ok")) as mock_run:
            codex("task", skip_git_check=False)
        cmd = mock_run.call_args[0][0]
        assert "--skip-git-repo-check" not in cmd

    def test_resume_last_flag(self):
        with patch("subprocess.run", return_value=_proc(stdout="resumed")) as mock_run:
            codex("continue analysis", resume_last=True)
        cmd = mock_run.call_args[0][0]
        assert "resume" in cmd
        assert "--last" in cmd

    def test_resume_last_places_exec_flags_before_the_resume_subcommand(self):
        # Found live: `-s`/`-c`/`--skip-git-repo-check` are `codex exec`'s
        # OWN flags, not `resume`'s -- once `resume` is on the command line
        # the CLI parses everything after it with `resume`'s own (much
        # smaller) grammar, so a flag placed after `resume --last <task>`
        # is rejected immediately ("unexpected argument '-s'"). The
        # pre-fix code appended every flag AFTER `resume --last <task>`;
        # this asserts the actual argv ORDER, which the older
        # presence-only assertions above would not have caught.
        with patch("subprocess.run", return_value=_proc(stdout="resumed")) as mock_run:
            codex("continue analysis", resume_last=True)
        cmd = mock_run.call_args[0][0]
        resume_index = cmd.index("resume")
        for flag in ("-s", "read-only", "--skip-git-repo-check"):
            assert cmd.index(flag) < resume_index, f"{flag!r} must appear before 'resume', got {cmd!r}"

    def test_resume_last_appends_task_after_last_flag(self):
        with patch("subprocess.run", return_value=_proc(stdout="resumed")) as mock_run:
            codex("continue analysis", resume_last=True)
        cmd = mock_run.call_args[0][0]
        assert cmd[cmd.index("--last") + 1] == "continue analysis"

    def test_subprocess_stdin_is_devnull(self):
        # Found live: without this, `codex exec` inherits the MCP server's
        # own stdin. Nothing is ever there to answer a prompt a given CLI
        # build might emit (auth, a config migration notice, a trust
        # dialog) -- the subprocess blocks on a read that never completes,
        # burning the full timeout with zero output, indistinguishable
        # from the process being slow. This is what actually explained a
        # `codex_write` call hanging for the full 300s on a completely
        # trivial task with an otherwise-healthy CLI install.
        with patch("subprocess.run", return_value=_proc(stdout="ok")) as mock_run:
            codex("task")
        assert mock_run.call_args.kwargs["stdin"] == subprocess.DEVNULL

    def test_executable_not_found_returns_error(self):
        # resolve_codex_bin() itself finds nothing (no PATH, no CODEX_BIN, no
        # desktop-app install dir) — subprocess.run is never even attempted.
        with patch("lazytools.connectors.code_support._codex.resolve_codex_bin", return_value=None):
            result = codex("task")
        assert "[codex]" in result
        assert "not found" in result

    def test_resolved_binary_missing_at_exec_returns_error(self):
        # resolve_codex_bin() resolved a path, but the subprocess call itself
        # still fails (e.g. removed after resolution, permissions).
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            result = codex("task")
        assert "[codex]" in result
        assert "could not be executed" in result

    def test_timeout_returns_error(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 300)):
            result = codex("task", timeout=300.0)
        assert "[codex]" in result
        assert "timeout" in result

    def test_nonzero_exit_returns_error(self):
        with patch("subprocess.run", return_value=_proc(returncode=1, stderr="api error")):
            result = codex("task")
        assert "[codex]" in result
        assert "exit 1" in result


# ─── check_clis_available ─────────────────────────────────────────────────────


class TestCheckClisAvailable:
    # codex's own availability goes through resolve_codex_bin() — mocked at
    # its defining module (see TestCodex's fixture for why, not at
    # lazybridge.engines.codex directly) so these stay deterministic
    # regardless of whether this machine has Codex installed.
    def test_both_found(self):
        with (
            patch("shutil.which", return_value="/usr/local/bin/claude"),
            patch("lazytools.connectors.code_support.resolve_codex_bin", return_value="/resolved/codex"),
        ):
            result = check_clis_available()
        assert result["claude"] is True
        assert result["codex"] is True

    def test_none_found(self):
        with (
            patch("shutil.which", return_value=None),
            patch("lazytools.connectors.code_support.resolve_codex_bin", return_value=None),
        ):
            result = check_clis_available()
        assert result["claude"] is False
        assert result["codex"] is False

    def test_only_claude_found(self):
        def _which(name: str) -> str | None:
            return "/usr/bin/claude" if name == "claude" else None

        with (
            patch("shutil.which", side_effect=_which),
            patch("lazytools.connectors.code_support.resolve_codex_bin", return_value=None),
        ):
            result = check_clis_available()
        assert result["claude"] is True
        assert result["codex"] is False
