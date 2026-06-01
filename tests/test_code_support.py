"""Tests for the CLI-mode code-support tools (claude_code, codex, check_clis_available).

All subprocess calls are mocked — no real CLIs are required.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

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
        assert result == "3 .py files found"
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--output-format" in cmd
        assert "json" in cmd
        assert "--allowedTools" in cmd

    def test_write_mode_flags(self):
        payload = json.dumps({"subtype": "success", "result": "done"})
        with patch("subprocess.run", return_value=_proc(stdout=payload)) as mock_run:
            claude_code("add docstring", mode="write")
        cmd = mock_run.call_args[0][0]
        assert "--permission-mode" in cmd
        assert "acceptEdits" in cmd
        assert "Write" in " ".join(cmd)

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
        assert result == "raw text output"

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
    def test_read_mode_returns_result(self):
        with patch("subprocess.run", return_value=_proc(stdout="function list: foo, bar")) as mock_run:
            result = codex("list functions in main.py", mode="read")
        assert result == "function list: foo, bar"
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "codex"
        assert "exec" in cmd
        assert "-s" in cmd
        assert "read-only" in cmd
        # `codex exec` has no `-a` approval flag — it must not be emitted.
        assert "-a" not in cmd

    def test_write_mode_uses_full_auto(self):
        with patch("subprocess.run", return_value=_proc(stdout="done")) as mock_run:
            codex("add type hints", mode="write")
        cmd = mock_run.call_args[0][0]
        assert "--full-auto" in cmd
        assert "workspace-write" in cmd
        # -a on-failure must NOT appear: it hangs waiting for stdin
        cmd_str = " ".join(cmd)
        assert "on-failure" not in cmd_str

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

    def test_cli_not_found_returns_error(self):
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            result = codex("task")
        assert "[codex]" in result
        assert "not found" in result

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

    def test_invalid_mode_returns_error(self):
        result = codex("task", mode="badmode")
        assert "[codex]" in result
        assert "invalid mode" in result


# ─── check_clis_available ─────────────────────────────────────────────────────


class TestCheckClisAvailable:
    def test_both_found(self):
        with patch("shutil.which", return_value="/usr/local/bin/claude"):
            result = check_clis_available()
        assert result["claude"] is True
        assert result["codex"] is True

    def test_none_found(self):
        with patch("shutil.which", return_value=None):
            result = check_clis_available()
        assert result["claude"] is False
        assert result["codex"] is False

    def test_only_claude_found(self):
        def _which(name: str) -> str | None:
            return "/usr/bin/claude" if name == "claude" else None

        with patch("shutil.which", side_effect=_which):
            result = check_clis_available()
        assert result["claude"] is True
        assert result["codex"] is False
