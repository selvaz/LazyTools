"""CodeWriteTools — the gated, sandboxed write path for the coding agents.

The contract under test (mirrors the Gmail send tools):

* write tools exist only if the developer constructs the provider;
* every call's cwd must resolve inside ``base_dir`` (escape → blocked,
  and a blocked cwd must NOT burn a confirmation grant);
* with ``require_confirmation=True`` (default) each call consumes exactly
  one ``confirm_write()`` grant — one approval can never authorize a flood;
* grants can be scope-bound to a task id;
* successful output is labelled ``content_is_untrusted``;
* the writers run on the LazyBridge engines, with per-call ``model`` /
  ``effort`` / ``session_name`` (a renamable alias in a ``SessionRegistry``).

No engine is ever started: the ``_make_*_engine`` seams and ``lazybridge.Agent``
are faked (see ``_code_fakes``), and the alias registry lives in ``tmp_path``.
"""

from __future__ import annotations

import os
import sys

import pytest

from _code_fakes import Script, install

pytest.importorskip("lazybridge")

from lazybridge.engines.sessions import SessionRegistry

from lazytools.connectors.code_support import CodeWriteBlocked, CodeWriteTools
from lazytools.connectors.code_support._writer import (
    DEFAULT_CLAUDE_WRITE_MAX_TURNS,
    _claude_writer_gate,
    _inside_git_repo,
)
from lazytools.safety.context import active_scope


@pytest.fixture
def script(monkeypatch) -> Script:
    return install(monkeypatch)


@pytest.fixture
def registry(tmp_path_factory) -> SessionRegistry:
    return SessionRegistry(tmp_path_factory.mktemp("registry") / "sessions.json")


@pytest.fixture
def repo(tmp_path):
    """A sandbox that is also a git repository (the Codex writer insists on one)."""
    root = tmp_path / "sandbox"
    root.mkdir()
    (root / ".git").mkdir()
    return root


def _writer(root, registry=None, **kwargs) -> CodeWriteTools:
    return CodeWriteTools(base_dir=str(root), session_registry=registry, **kwargs)


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


def test_tool_schemas_expose_model_effort_and_session_name(tmp_path):
    tools = {t.name: t for t in _writer(tmp_path, codex=True).as_tools()}

    claude = set(tools["claude_code_write"].definition().parameters["properties"])
    assert claude == {"task", "cwd", "session_id", "model", "effort", "session_name"}

    codex = set(tools["codex_write"].definition().parameters["properties"])
    assert codex == {"task", "cwd", "thread_id", "model", "effort", "session_name"}
    # Breaking change: the "continue the last thread" flag is gone — name the session instead.
    assert "resume_last" not in codex


# ─── confirmation gate ────────────────────────────────────────────────────────


async def test_write_blocked_without_confirmation(tmp_path, script):
    writer = _writer(tmp_path)
    with pytest.raises(CodeWriteBlocked, match="no outstanding write confirmation"):
        await writer._claude_write("edit the file")
    assert script.engines == []  # nothing was launched


async def test_codex_write_blocked_without_confirmation(repo, script):
    writer = _writer(repo, codex=True)
    with pytest.raises(CodeWriteBlocked, match="no outstanding write confirmation"):
        await writer._codex_write("edit the file")
    assert script.engines == []


async def test_one_grant_authorizes_exactly_one_write(tmp_path, script, registry):
    writer = _writer(tmp_path, registry)
    writer.confirm_write()
    out = await writer._claude_write("edit the file")
    assert isinstance(out, dict) and out["content_is_untrusted"] is True
    assert out["result"].endswith("\n\ndone")

    # The grant is spent — a second call must block (no flood after one OK).
    with pytest.raises(CodeWriteBlocked):
        await writer._claude_write("edit it again")


async def test_scope_bound_grant_not_spendable_outside_its_task(tmp_path, script, registry):
    writer = _writer(tmp_path, registry)
    writer.confirm_write(task_id="task-A")

    # No active scope → the task-bound grant must not match.
    with pytest.raises(CodeWriteBlocked):
        await writer._claude_write("edit")

    token = active_scope.set("task-A")
    try:
        out = await writer._claude_write("edit")
    finally:
        active_scope.reset(token)
    assert out["result"].endswith("done")


async def test_require_confirmation_false_skips_gate_but_keeps_sandbox(tmp_path, script, registry):
    writer = _writer(tmp_path, registry, require_confirmation=False)
    out = await writer._claude_write("edit")  # no grant needed
    assert out["result"].endswith("done")
    with pytest.raises(CodeWriteBlocked, match="outside base_dir"):
        await writer._claude_write("edit", cwd="../outside")


# ─── base_dir sandbox ─────────────────────────────────────────────────────────


async def test_cwd_defaults_to_base_dir_and_subdirs_allowed(tmp_path, script, registry):
    (tmp_path / "pkg").mkdir()
    writer = _writer(tmp_path, registry, require_confirmation=False)
    await writer._claude_write("edit")
    assert script.kwargs["cwd"] == str(tmp_path.resolve())
    await writer._claude_write("edit", cwd="pkg")
    assert script.kwargs["cwd"] == str((tmp_path / "pkg").resolve())


async def test_cwd_escape_blocked_and_does_not_burn_grant(tmp_path, script, registry):
    writer = _writer(tmp_path, registry)
    writer.confirm_write()

    with pytest.raises(CodeWriteBlocked, match="outside base_dir"):
        await writer._claude_write("edit", cwd="../..")
    with pytest.raises(CodeWriteBlocked, match="outside base_dir"):
        await writer._claude_write("edit", cwd="/etc")

    # The escape attempts must not have consumed the grant.
    out = await writer._claude_write("edit")
    assert out["result"].endswith("done")


async def test_missing_subdir_inside_sandbox_blocked(tmp_path, script):
    writer = _writer(tmp_path, require_confirmation=False)
    with pytest.raises(CodeWriteBlocked, match="not a directory"):
        await writer._claude_write("edit", cwd="does-not-exist")


async def test_bad_arguments_do_not_burn_the_grant(tmp_path, script, registry):
    """A typo in effort / session_name must fail BEFORE the one-shot approval is spent."""
    writer = _writer(tmp_path, registry)
    writer.confirm_write()

    with pytest.raises(ValueError, match="effort 'hgih' is not valid for Claude Code"):
        await writer._claude_write("edit", effort="hgih")
    with pytest.raises(ValueError, match="invalid session alias"):
        await writer._claude_write("edit", session_name="1 bad name")

    out = await writer._claude_write("edit")  # the grant is still there
    assert out["result"].endswith("done")


# ─── claude_code_write on the engine ──────────────────────────────────────────


async def test_claude_write_engine_configuration(tmp_path, script, registry):
    writer = _writer(tmp_path, registry, require_confirmation=False, timeout=900.0)
    await writer._claude_write("edit", model="opus", effort="high")

    kw = script.kwargs
    root = str(tmp_path.resolve())
    assert kw["cwd"] == root and kw["file_roots"] == [root]
    assert kw["model"] == "opus"
    assert kw["reasoning_effort"] == "high"
    assert kw["web"] is False
    assert kw["persist_session"] is True
    assert kw["max_turns"] == DEFAULT_CLAUDE_WRITE_MAX_TURNS
    assert kw["max_retries"] == 0  # a retry would replay a write that may have landed
    assert kw["request_timeout"] == 900.0
    assert kw["stream_idle_timeout"] == 600.0
    assert kw["session_registry"] is registry

    policy = kw["config"].claude
    assert policy.permission_mode == "acceptEdits"
    assert set(policy.extra_tools) == {"Write", "Edit", "Bash"}
    assert kw["config"].approval_gate is _claude_writer_gate
    assert script.prompts == ["edit"]


async def test_claude_model_is_left_to_the_engine_when_not_given(tmp_path, script, registry):
    writer = _writer(tmp_path, registry, require_confirmation=False)
    await writer._claude_write("edit")
    assert "model" not in script.kwargs
    assert script.kwargs["reasoning_effort"] is None


async def test_claude_bash_can_be_dropped(tmp_path, script, registry):
    writer = _writer(tmp_path, registry, require_confirmation=False, claude_bash=False)
    await writer._claude_write("edit")

    config = script.kwargs["config"]
    assert set(config.claude.extra_tools) == {"Write", "Edit"}
    assert config.approval_gate is None


def test_the_claude_gate_allows_only_the_old_tool_surface():
    from lazybridge.engines.coding import ApprovalRequest

    def allowed(name: str) -> bool:
        decision = _claude_writer_gate(ApprovalRequest(provider="claude-code", kind="tool", name=name))
        return decision.action == "allow"

    for name in ("Bash", "Write", "Edit", "Read", "Glob", "Grep"):
        assert allowed(name), name
    for name in ("WebFetch", "Task", "mcp__something"):
        assert not allowed(name), name


@pytest.mark.parametrize("effort", ["ultra", "persistent"])
async def test_unknown_effort_is_a_clear_value_error(effort, tmp_path, script):
    writer = _writer(tmp_path, require_confirmation=False)
    with pytest.raises(ValueError, match="use one of: low, medium, high, xhigh, max"):
        await writer._claude_write("edit", effort=effort)


async def test_blank_arguments_mean_default(tmp_path, script, registry):
    # MCP clients that fill in every parameter send "" for "not given".
    writer = _writer(tmp_path, registry, require_confirmation=False)
    await writer._claude_write("edit", model="  ", effort="", session_name="", session_id="")
    assert "model" not in script.kwargs
    assert script.kwargs["reasoning_effort"] is None
    assert script.kwargs["session_alias"] is None
    assert script.kwargs["session_id"] is None


# ─── session_name: resume / bind / rebind ─────────────────────────────────────


async def test_session_name_opens_then_resumes_the_same_claude_session(tmp_path, script, registry):
    writer = _writer(tmp_path, registry, require_confirmation=False)

    first = await writer._claude_write("remember PINEAPPLE", session_name="notes")
    assert script.kwargs["session_alias"] == "notes"
    assert script.kwargs["session_id"] is None
    header = first["result"].splitlines()[0]
    assert "session_id=claude-id-1" in header and "session_name=notes" in header
    assert registry.resolve("claude", str(tmp_path.resolve()), "notes") == "claude-id-1"

    second = await writer._claude_write("what word?", session_name="notes")
    assert script.last.handle == "claude-id-1"  # the alias resumed the bound session
    assert "session_id=claude-id-1" in second["result"].splitlines()[0]


async def test_an_explicit_session_id_wins_and_rebinds_the_alias(tmp_path, script, registry):
    writer = _writer(tmp_path, registry, require_confirmation=False)
    await writer._claude_write("one", session_name="notes")

    out = await writer._claude_write("two", session_name="notes", session_id="native-777")

    assert script.kwargs["session_id"] == "native-777"
    assert "session_id=native-777" in out["result"].splitlines()[0]
    assert registry.resolve("claude", str(tmp_path.resolve()), "notes") == "native-777"


async def test_aliases_are_scoped_to_the_working_directory(tmp_path, script, registry):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    writer = _writer(tmp_path, registry, require_confirmation=False)

    await writer._claude_write("x", cwd="a", session_name="notes")
    await writer._claude_write("y", cwd="b", session_name="notes")

    # Same name, two repositories, two different sessions.
    assert script.engines[0].handle != script.engines[1].handle


async def test_the_alias_is_bound_with_model_and_effort(tmp_path, script, registry):
    writer = _writer(tmp_path, registry, require_confirmation=False)
    await writer._claude_write("x", session_name="notes", model="opus", effort="max")

    (row,) = registry.entries("claude")
    assert (row["name"], row["model"], row["effort"]) == ("notes", "opus", "max")


# ─── failure paths keep the handle and the name ───────────────────────────────


async def test_engine_error_keeps_handle_and_name(tmp_path, script, registry):
    script.mode = "error"
    writer = _writer(tmp_path, registry, require_confirmation=False)

    out = await writer._claude_write("edit", session_name="notes")

    assert isinstance(out, str) and out.startswith("[claude_code] failed in ")
    assert "session_id=claude-id-1" in out and "session_name=notes" in out
    assert "turn failed" in out


async def test_a_raised_engine_exception_keeps_handle_and_name(tmp_path, script, registry):
    script.mode = "raise"
    writer = _writer(tmp_path, registry, require_confirmation=False)

    out = await writer._claude_write("edit", session_name="notes", session_id="native-1")

    assert out.startswith("[claude_code] failed in ")
    assert "session_id=native-1" in out and "session_name=notes" in out
    assert "RuntimeError: engine blew up" in out


async def test_failure_without_a_session_name_is_still_labelled(tmp_path, script, registry):
    script.mode = "error"
    writer = _writer(tmp_path, registry, require_confirmation=False)
    out = await writer._claude_write("edit")
    assert out.startswith("[claude_code]")
    assert "session_name" not in out


# ─── codex_write on the engine ────────────────────────────────────────────────


async def test_codex_write_engine_configuration(repo, script, registry):
    writer = _writer(repo, registry, codex=True, require_confirmation=False, timeout=600.0)
    out = await writer._codex_write("edit", model="gpt-x", effort="high")

    kw = script.kwargs
    assert kw["cwd"] == str(repo.resolve())
    assert kw["model"] == "gpt-x"
    assert kw["reasoning_effort"] == "high"
    assert kw["persist_thread"] is True
    assert kw["max_retries"] == 0
    assert kw["request_timeout"] == 600.0
    assert kw["stream_idle_timeout"] == 400.0
    assert kw["session_registry"] is registry
    policy = kw["config"].codex
    assert policy.sandbox == "workspace-write"
    assert policy.approval_policy == "never"
    # Same result shape as the old CLI writer: a labelled dict.
    assert out["content_is_untrusted"] is True
    assert out["result"].endswith("\n\ndone")
    assert out["result"].startswith("[codex] ")
    assert "thread_id=codex-id-1" in out["result"].splitlines()[0]


async def test_codex_write_keeps_git_as_the_recovery_rail(tmp_path, script, registry):
    plain = tmp_path / "plain"
    plain.mkdir()
    writer = _writer(plain, registry, codex=True, require_confirmation=False)

    out = await writer._codex_write("edit")

    assert isinstance(out, str) and out.startswith("[codex] refusing to write")
    assert "git init" in out
    assert script.engines == []  # refused before anything was launched


async def test_codex_git_check_can_be_waived_for_throwaway_dirs(tmp_path, script, registry):
    plain = tmp_path / "plain"
    plain.mkdir()
    writer = _writer(plain, registry, codex=True, require_confirmation=False, codex_skip_git_check=True)

    out = await writer._codex_write("edit")

    assert out["content_is_untrusted"] is True


def test_git_check_accepts_a_subdirectory_and_a_worktree_file(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "pkg" / "deep").mkdir(parents=True)
    assert _inside_git_repo(tmp_path / "pkg" / "deep")

    wt = tmp_path / "other" / "wt"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /somewhere\n")  # a linked worktree has a .git FILE
    assert _inside_git_repo(wt)


async def test_codex_session_name_opens_then_resumes_the_same_thread(repo, script, registry):
    writer = _writer(repo, registry, codex=True, require_confirmation=False)

    first = await writer._codex_write("remember PINEAPPLE", session_name="notes")
    header = first["result"].splitlines()[0]
    assert "thread_id=codex-id-1" in header and "session_name=notes" in header

    second = await writer._codex_write("what word?", session_name="notes")
    assert script.last.handle == "codex-id-1"
    assert "thread_id=codex-id-1" in second["result"].splitlines()[0]


async def test_codex_explicit_thread_id_wins_and_rebinds(repo, script, registry):
    writer = _writer(repo, registry, codex=True, require_confirmation=False)
    await writer._codex_write("one", session_name="notes")

    await writer._codex_write("two", session_name="notes", thread_id="native-9")

    assert script.kwargs["thread_id"] == "native-9"
    assert registry.resolve("codex", str(repo.resolve()), "notes") == "native-9"


async def test_codex_failure_keeps_handle_and_name(repo, script, registry):
    script.mode = "error"
    writer = _writer(repo, registry, codex=True, require_confirmation=False)

    out = await writer._codex_write("edit", session_name="notes")

    assert out.startswith("[codex] failed in ")
    assert "thread_id=codex-id-1" in out and "session_name=notes" in out


async def test_codex_raised_exception_keeps_handle_and_name(repo, script, registry):
    script.mode = "raise"
    writer = _writer(repo, registry, codex=True, require_confirmation=False)

    out = await writer._codex_write("edit", session_name="notes", thread_id="t-5")

    assert out.startswith("[codex] failed in ")
    assert "thread_id=t-5" in out and "session_name=notes" in out


async def test_codex_bad_effort_is_refused_before_the_grant(repo, script, registry):
    writer = _writer(repo, registry, codex=True)
    writer.confirm_write()

    with pytest.raises(ValueError, match="effort 'hgih' is not valid for Codex; use one of: none, minimal, low"):
        await writer._codex_write("edit", effort="hgih")

    out = await writer._codex_write("edit")
    assert out["content_is_untrusted"] is True


# ─── Windows: the engines need the LONG path as cwd ───────────────────────────


@pytest.mark.skipif(sys.platform != "win32", reason="8.3 short names exist only on Windows")
async def test_windows_8dot3_cwd_reaches_the_engine_as_the_long_path(tmp_path, script, registry):
    """Both writers break on an 8.3 short cwd (``C:\\Users\\ADMINI~1``); the sandbox
    check resolves it, and the engine must be handed the resolved long path."""
    import ctypes

    long_dir = tmp_path / "a-directory-with-a-long-name"
    long_dir.mkdir()
    (long_dir / ".git").mkdir()

    buf = ctypes.create_unicode_buffer(1024)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(long_dir), buf, 1024) or buf.value == str(long_dir):
        pytest.skip("8.3 short names are disabled on this volume")
    short = buf.value
    assert "~" in short

    writer = CodeWriteTools(
        base_dir=short, claude=True, codex=True, require_confirmation=False, session_registry=registry
    )
    await writer._claude_write("edit")
    claude_cwd = script.kwargs["cwd"]
    await writer._codex_write("edit")
    codex_cwd = script.kwargs["cwd"]

    for cwd in (claude_cwd, codex_cwd):
        assert "~" not in cwd
        assert os.path.normcase(cwd) == os.path.normcase(str(long_dir.resolve()))


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
