"""Tests for the Codex-backed reviewer (``codex_reviewer`` / provider ``code_review``).

No real ``codex`` process is ever launched: ``CODEX_BIN`` satisfies the
fail-fast lookup, and the engine/agent pair is faked so the prompt assembly,
path confinement and MCP gating are asserted deterministically. The live path
is exercised by hand (see docs/code-support/codex.md).
"""

from __future__ import annotations

import importlib

import pytest

from lazytools.connectors.code_support import (
    CODE_CONSULTANT_SYSTEM,
    CODE_REVIEWER_SYSTEM,
    claude_consultant,
    claude_reviewer,
    codex_consultant,
    codex_native_reviewer,
    codex_reviewer,
)
from lazytools.connectors.code_support._review import _confine_paths, _resolve_repo, _scope_block

pytest.importorskip("lazybridge")


@pytest.fixture(autouse=True)
def _coding_engines(monkeypatch):
    """Stand in for the coding engines when the installed lazybridge lacks them.

    ``engines.codex`` / ``engines.claude_code`` are unreleased, so a lazybridge
    from PyPI does not have them and every test here would skip — leaving the
    two modules they cover unmeasured. Nothing in this file exercises a real
    engine anyway (the fixtures below replace them), so a stub keeps the tests
    meaningful wherever they run. Delete once lazybridge ships the engines.
    """
    import sys
    import types

    def stub(name: str, **attrs):
        try:
            importlib.import_module(name)
        except ImportError:
            module = types.ModuleType(name)
            module.__dict__.update(attrs)
            module.__lazytools_stub__ = True  # ...so a test needing the real thing can skip
            monkeypatch.setitem(sys.modules, name, module)

    class _Engine:
        def __init__(self, **kwargs):
            self.thread_id = kwargs.get("thread_id")
            self.session_id = kwargs.get("session_id")

    class _Config:
        @classmethod
        def reviewer(cls):
            return cls()

    stub("lazybridge.engines.codex", CodexEngine=_Engine, codex_executable=lambda: "codex")
    stub("lazybridge.engines.claude_code", ClaudeCodeEngine=_Engine)
    stub("lazybridge.engines.coding", CodingAgentConfig=_Config)


@pytest.fixture(autouse=True)
def _fake_codex_bin(monkeypatch):
    """Make ``codex_executable()`` resolve without a Codex install."""
    monkeypatch.setenv("CODEX_BIN", "codex-not-really-here")


# ─── path confinement ────────────────────────────────────────────────


class TestResolveRepo:
    def test_defaults_to_root(self, tmp_path):
        assert _resolve_repo(None, tmp_path) == tmp_path.resolve()

    def test_relative_path_resolves_under_root(self, tmp_path):
        (tmp_path / "repo").mkdir()
        assert _resolve_repo("repo", tmp_path) == (tmp_path / "repo").resolve()

    def test_absolute_path_inside_root_is_allowed(self, tmp_path):
        (tmp_path / "repo").mkdir()
        assert _resolve_repo(str(tmp_path / "repo"), tmp_path) == (tmp_path / "repo").resolve()

    def test_escape_via_parent_is_refused(self, tmp_path):
        (tmp_path / "root").mkdir()
        with pytest.raises(ValueError, match="outside the allowed root"):
            _resolve_repo("../elsewhere", tmp_path / "root")

    def test_absolute_path_outside_root_is_refused(self, tmp_path):
        (tmp_path / "root").mkdir()
        (tmp_path / "other").mkdir()
        with pytest.raises(ValueError, match="outside the allowed root"):
            _resolve_repo(str(tmp_path / "other"), tmp_path / "root")

    def test_missing_directory_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="not an existing directory"):
            _resolve_repo("nope", tmp_path)


class TestScopeBlock:
    def test_empty_without_hints(self):
        assert _scope_block(None, []) == ""

    def test_diff_ref_names_the_git_commands(self):
        # `git diff <ref>...HEAD` alone shows only *committed* work: a branch
        # whose changes are staged or unstaged would review as empty.
        block = _scope_block("main", [])
        assert "git diff main...HEAD" in block
        assert "git diff --cached" in block
        assert "git status --short" in block

    @pytest.mark.parametrize("ref", ["main; rm -rf /", "main && cat secrets", "`whoami`", "--upload-pack=x"])
    def test_shell_metacharacters_in_a_ref_are_refused(self, ref):
        # The ref is interpolated into a command the reviewer will run.
        with pytest.raises(ValueError, match="not a plain git ref"):
            _scope_block(ref, [])

    @pytest.mark.parametrize("ref", ["main", "HEAD~1", "origin/feat/x", "v1.0.2", "HEAD@{2}", "main^"])
    def test_ordinary_refs_are_accepted(self, ref):
        assert ref in _scope_block(ref, [])

    def test_paths_are_listed(self):
        assert "src/a.py, src/b.py" in _scope_block(None, ["src/a.py", "src/b.py"])


class TestConfinePaths:
    """``repo_path`` pins the *cwd*; a read-only sandbox still reads anywhere,
    so the structured path list has to be confined too."""

    def test_none_and_empty_entries(self, tmp_path):
        assert _confine_paths(None, tmp_path) == []
        assert _confine_paths(" , ", tmp_path) == []

    def test_relative_entries_are_normalised(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").touch()
        assert _confine_paths("src/a.py, src", tmp_path) == ["src/a.py", "src"]

    def test_absolute_entry_outside_is_refused(self, tmp_path):
        (tmp_path / "repo").mkdir()
        secret = tmp_path / "secret.env"
        secret.touch()
        with pytest.raises(ValueError, match="outside the reviewed directory"):
            _confine_paths(str(secret), tmp_path / "repo")

    def test_traversal_entry_is_refused(self, tmp_path):
        (tmp_path / "repo").mkdir()
        with pytest.raises(ValueError, match="outside the reviewed directory"):
            _confine_paths("src/a.py, ../../secrets", tmp_path / "repo")


# ─── the tool itself ─────────────────────────────────────────────────


class _FakeEngine:
    """Records construction kwargs; never spawns anything."""

    last_kwargs: dict = {}

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs
        # What CodexEngine does after a durable run: the id it used, ready to
        # be reported back to the caller.
        self.thread_id = kwargs.get("thread_id") or "thread-new"


class _FakeAgent:
    last_prompt: str | None = None

    last_tools: list = []

    def __init__(self, engine, name=None, tools=None):
        self.engine, self.name = engine, name
        type(self).last_tools = list(tools or [])

    async def run(self, prompt):
        from lazybridge import Envelope

        type(self).last_prompt = prompt
        return Envelope(task=prompt, payload="No bugs found.")


@pytest.fixture
def faked(monkeypatch):
    """Swap the engine/agent the factory imports for fakes."""
    import lazybridge
    import lazybridge.engines.codex as codex_mod

    monkeypatch.setattr(codex_mod, "CodexEngine", _FakeEngine)
    monkeypatch.setattr(lazybridge, "Agent", _FakeAgent)
    return _FakeEngine, _FakeAgent


class TestCodexReviewer:
    def test_tool_name_and_schema(self, tmp_path):
        tool = codex_reviewer(root=str(tmp_path))
        assert tool.name == "codex_code_review"
        params = tool.definition().parameters
        assert set(params["properties"]) == {"task", "repo_path", "diff_ref", "paths", "thread_id"}
        assert params["required"] == ["task"]

    @pytest.mark.parametrize("timeout", [0, -1.0, float("inf"), float("nan")])
    def test_rejects_a_non_positive_or_infinite_timeout(self, timeout, tmp_path):
        # The MCP provider validates its env var; the direct API must not be
        # the lax way in.
        with pytest.raises(ValueError, match="positive finite"):
            codex_reviewer(root=str(tmp_path), timeout=timeout)

    def test_missing_codex_cli_raises(self, tmp_path, monkeypatch):
        # Exercises the real resolver, so it cannot run against the stub.
        import lazybridge.engines.codex as codex_mod

        if getattr(codex_mod, "__lazytools_stub__", False):
            pytest.skip("needs the real lazybridge.engines.codex resolver")
        # No CODEX_BIN, nothing on PATH, no desktop install directory.
        monkeypatch.delenv("CODEX_BIN", raising=False)
        monkeypatch.setattr("shutil.which", lambda _name: None)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
        with pytest.raises(FileNotFoundError, match="codex"):
            codex_reviewer(root=str(tmp_path))

    @pytest.mark.asyncio
    async def test_runs_in_the_requested_repo(self, tmp_path, faked):
        (tmp_path / "repo").mkdir()
        tool = codex_reviewer(root=str(tmp_path), model="gpt-x", effort="high", timeout=300.0)

        out = await tool.run(task="review the parser", repo_path="repo")

        assert "No bugs found." in out
        kwargs = _FakeEngine.last_kwargs
        assert kwargs["cwd"] == str((tmp_path / "repo").resolve())
        assert kwargs["model"] == "gpt-x"
        assert kwargs["reasoning_effort"] == "high"
        assert kwargs["request_timeout"] == 300.0
        assert kwargs["stream_idle_timeout"] == 200.0
        assert _FakeAgent.last_prompt == "review the parser"

    @pytest.mark.asyncio
    async def test_scope_hints_reach_the_prompt(self, tmp_path, faked):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").touch()
        tool = codex_reviewer(root=str(tmp_path))

        await tool.run(task="check the diff", diff_ref="main", paths="src/a.py")

        assert "git diff main...HEAD" in _FakeAgent.last_prompt
        assert "src/a.py" in _FakeAgent.last_prompt

    @pytest.mark.asyncio
    async def test_the_thread_id_is_reported_for_follow_ups(self, tmp_path, faked):
        tool = codex_reviewer(root=str(tmp_path))

        out = await tool.run(task="first look")

        # "<repo>#<id>" — the repo half is what makes a stale handle catchable.
        assert "thread_id=.#thread-new" in out.splitlines()[0]
        assert _FakeEngine.last_kwargs["persist_thread"] is True
        assert _FakeEngine.last_kwargs["thread_id"] is None

    @pytest.mark.asyncio
    async def test_a_supplied_thread_id_continues_that_conversation(self, tmp_path, faked):
        tool = codex_reviewer(root=str(tmp_path))

        out = await tool.run(task="and the retry path?", thread_id=".#thread-42")

        assert _FakeEngine.last_kwargs["thread_id"] == "thread-42"
        assert "thread_id=.#thread-42" in out.splitlines()[0]

    @pytest.mark.asyncio
    async def test_the_thread_id_survives_a_failed_turn(self, tmp_path, monkeypatch, faked):
        # The id matters most on failure: an interrupted turn is exactly what
        # someone needs to go and inspect.
        async def failing(self, prompt):
            from lazybridge import Envelope

            return Envelope.error_envelope(RuntimeError("outcome unknown"))

        monkeypatch.setattr(_FakeAgent, "run", failing)
        tool = codex_reviewer(root=str(tmp_path))

        out = await tool.run(task="review", thread_id=".#thread-42")

        assert "thread_id=.#thread-42" in out and "outcome unknown" in out

    @pytest.mark.asyncio
    async def test_a_relative_root_is_pinned_at_build_time(self, tmp_path, monkeypatch, faked):
        # Otherwise the boundary follows the process cwd: build under A, chdir
        # to B, and the "same" root now means B/repos.
        (tmp_path / "a" / "repos").mkdir(parents=True)
        (tmp_path / "b" / "repos").mkdir(parents=True)
        monkeypatch.chdir(tmp_path / "a")
        tool = codex_reviewer(root="repos")

        monkeypatch.chdir(tmp_path / "b")
        await tool.run(task="review")

        assert _FakeEngine.last_kwargs["cwd"] == str((tmp_path / "a" / "repos").resolve())

    @pytest.mark.asyncio
    async def test_paths_cannot_point_outside_the_repo(self, tmp_path, faked):
        (tmp_path / "repo").mkdir()
        secret = tmp_path / "secret.env"
        secret.touch()
        tool = codex_reviewer(root=str(tmp_path))

        with pytest.raises(ValueError, match="outside the reviewed directory"):
            await tool.run(task="summarise", repo_path="repo", paths=str(secret))

    @pytest.mark.asyncio
    async def test_engine_failure_is_returned_not_raised(self, tmp_path, monkeypatch, faked):
        async def failing(self, prompt):
            from lazybridge import Envelope

            return Envelope.error_envelope(RuntimeError("codex exploded"))

        monkeypatch.setattr(_FakeAgent, "run", failing)
        tool = codex_reviewer(root=str(tmp_path))

        out = await tool.run(task="anything")

        assert out.startswith("[codex_code_review] failed")
        assert "codex exploded" in out

    @pytest.mark.asyncio
    async def test_escaping_repo_path_is_refused(self, tmp_path, faked):
        (tmp_path / "root").mkdir()
        tool = codex_reviewer(root=str(tmp_path / "root"))

        with pytest.raises(ValueError, match="outside the allowed root"):
            await tool.run(task="peek", repo_path="..")


# ─── MCP wiring ──────────────────────────────────────────────────────


class TestCodeReviewProvider:
    def test_opt_in_only(self):
        from lazytools.mcp_server.providers import PROVIDER_FACTORIES

        with pytest.raises(RuntimeError, match="opt-in"):
            PROVIDER_FACTORIES["code_review"](allow_write=False)

    def test_write_mode_serves_the_three_codex_tools(self, tmp_path, monkeypatch):
        from lazytools.mcp_server.providers import default_providers
        from lazytools.mcp_server.server import expand_tools

        monkeypatch.setenv("LAZYTOOLS_CODE_ROOT", str(tmp_path))
        providers = default_providers(["code_review"], allow_write=True)

        assert set(expand_tools(providers, read_only=False)) == {
            "codex_code_review",
            "codex_ask",
            "codex_review_changes",
        }
        # ...and the name guard keeps all of them off the read-only surface.
        assert expand_tools(providers, read_only=True) == {}

    def test_absent_without_allow_write(self):
        from lazytools.mcp_server.providers import default_providers

        assert default_providers(["code_review"], allow_write=False) == []

    def test_rejects_a_non_numeric_timeout(self, monkeypatch):
        from lazytools.mcp_server.providers import PROVIDER_FACTORIES

        monkeypatch.setenv("LAZYTOOLS_CODE_REVIEW_TIMEOUT", "soon")
        with pytest.raises(RuntimeError, match="not a number"):
            PROVIDER_FACTORIES["code_review"](allow_write=True)

    @pytest.mark.parametrize("value", ["-1", "0", "inf", "nan"])
    def test_rejects_a_non_positive_or_infinite_timeout(self, value, monkeypatch):
        # A negative value builds a provider whose every call dies inside
        # CodexEngine; `inf` silently removes the deadline it advertises.
        from lazytools.mcp_server.providers import PROVIDER_FACTORIES

        monkeypatch.setenv("LAZYTOOLS_CODE_REVIEW_TIMEOUT", value)
        with pytest.raises(RuntimeError, match="positive finite"):
            PROVIDER_FACTORIES["code_review"](allow_write=True)


# ─── the consulting tool ─────────────────────────────────────────────


class TestCodexConsultant:
    """``codex_ask``: same engine, different role — a question, not a review."""

    def test_tool_name_and_schema(self, tmp_path):
        tool = codex_consultant(root=str(tmp_path))
        assert tool.name == "codex_ask"
        params = tool.definition().parameters
        assert set(params["properties"]) == {"question", "repo_path", "thread_id", "model", "effort"}
        assert params["required"] == ["question"]

    @pytest.mark.asyncio
    async def test_per_call_model_and_effort_override_the_defaults(self, tmp_path, faked):
        tool = codex_consultant(root=str(tmp_path), model="gpt-base", effort="low")

        await tool.run(question="?")
        assert _FakeEngine.last_kwargs["model"] == "gpt-base"
        assert _FakeEngine.last_kwargs["reasoning_effort"] == "low"

        await tool.run(question="?", model="gpt-big", effort="high")
        assert _FakeEngine.last_kwargs["model"] == "gpt-big"
        assert _FakeEngine.last_kwargs["reasoning_effort"] == "high"

    @pytest.mark.asyncio
    async def test_extra_tools_reach_the_agent(self, tmp_path, faked):
        # A consultant may need to read the world — the reviewer never gets
        # these, so the factory takes them explicitly.
        def probe() -> str:
            """Sentinel tool."""
            return "probed"

        tool = codex_consultant(root=str(tmp_path), tools=[probe])

        await tool.run(question="?")

        assert _FakeAgent.last_tools == [probe]

    def test_it_does_not_use_the_reviewer_instructions(self, tmp_path):
        # The reviewer prompt turns every question into a findings list, which
        # is the wrong shape for "should I do X" — that is the whole reason
        # this second tool exists.
        assert CODE_CONSULTANT_SYSTEM != CODE_REVIEWER_SYSTEM
        codex_consultant(root=str(tmp_path))  # constructs with its own default

    @pytest.mark.asyncio
    async def test_asking_runs_in_the_repo_and_threads(self, tmp_path, faked):
        (tmp_path / "repo").mkdir()
        tool = codex_consultant(root=str(tmp_path))

        out = await tool.run(question="does resume keep tools?", repo_path="repo", thread_id="t-1")

        assert _FakeEngine.last_kwargs["cwd"] == str((tmp_path / "repo").resolve())
        assert _FakeEngine.last_kwargs["system"] == CODE_CONSULTANT_SYSTEM
        assert _FakeEngine.last_kwargs["thread_id"] == "t-1"
        assert _FakeAgent.last_prompt == "does resume keep tools?"
        assert out.startswith("[codex_ask]")

    @pytest.mark.asyncio
    async def test_repo_path_is_confined(self, tmp_path, faked):
        (tmp_path / "root").mkdir()
        tool = codex_consultant(root=str(tmp_path / "root"))

        with pytest.raises(ValueError, match="outside the allowed root"):
            await tool.run(question="peek", repo_path="..")


class TestThreadHandles:
    """A handle names its repository: resuming a thread opened elsewhere would
    splice that repository's transcript into this answer — a leak that arrives
    through Codex' memory, which path confinement says nothing about. Found by
    Codex reviewing this module."""

    @pytest.mark.asyncio
    async def test_the_handle_names_the_repository(self, tmp_path, faked):
        (tmp_path / "repo-a").mkdir()
        tool = codex_reviewer(root=str(tmp_path))

        out = await tool.run(task="look", repo_path="repo-a")

        assert "thread_id=repo-a#thread-new" in out

    @pytest.mark.asyncio
    async def test_a_handle_from_another_repo_is_refused(self, tmp_path, faked):
        (tmp_path / "repo-a").mkdir()
        (tmp_path / "repo-b").mkdir()
        tool = codex_reviewer(root=str(tmp_path))

        with pytest.raises(ValueError, match="belongs to 'repo-a'"):
            await tool.run(task="look", repo_path="repo-b", thread_id="repo-a#abc-123")

    @pytest.mark.asyncio
    async def test_a_matching_handle_resumes_the_bare_id(self, tmp_path, faked):
        (tmp_path / "repo-a").mkdir()
        tool = codex_reviewer(root=str(tmp_path))

        await tool.run(task="look", repo_path="repo-a", thread_id="repo-a#abc-123")

        assert _FakeEngine.last_kwargs["thread_id"] == "abc-123"

    @pytest.mark.asyncio
    async def test_both_tools_share_the_check(self, tmp_path, faked):
        (tmp_path / "repo-a").mkdir()
        (tmp_path / "repo-b").mkdir()
        ask = codex_consultant(root=str(tmp_path))

        with pytest.raises(ValueError, match="belongs to 'repo-a'"):
            await ask.run(question="?", repo_path="repo-b", thread_id="repo-a#abc-123")

    @pytest.mark.asyncio
    async def test_a_malformed_handle_is_refused(self, tmp_path, faked):
        tool = codex_reviewer(root=str(tmp_path))

        with pytest.raises(ValueError, match="malformed"):
            await tool.run(task="look", thread_id="repo-a#")


class TestNativeReviewTool:
    """``codex_review_changes``: Codex' own harness, typed target, no prompt."""

    def test_tool_name_and_schema(self, tmp_path):
        tool = codex_native_reviewer(root=str(tmp_path))
        assert tool.name == "codex_review_changes"
        assert set(tool.definition().parameters["properties"]) == {"repo_path", "scope", "ref"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("scope", "ref", "expected"),
        [
            ("uncommitted", None, {"type": "uncommittedChanges"}),
            ("branch", "main", {"type": "baseBranch", "branch": "main"}),
            ("commit", "f75a1ce8", {"type": "commit", "sha": "f75a1ce8"}),
        ],
    )
    async def test_each_scope_maps_to_its_review_target(self, scope, ref, expected, tmp_path, faked):
        tool = codex_native_reviewer(root=str(tmp_path))

        await tool.run(scope=scope, ref=ref)

        assert _FakeEngine.last_kwargs["review_target"] == expected

    @pytest.mark.asyncio
    async def test_an_unknown_scope_is_refused(self, tmp_path, faked):
        tool = codex_native_reviewer(root=str(tmp_path))

        with pytest.raises(ValueError, match="scope must be one of"):
            await tool.run(scope="everything")

    @pytest.mark.asyncio
    async def test_a_scope_needing_a_ref_says_so(self, tmp_path, faked):
        tool = codex_native_reviewer(root=str(tmp_path))

        with pytest.raises(ValueError, match="needs a ref"):
            await tool.run(scope="branch")

    @pytest.mark.asyncio
    async def test_the_ref_is_still_validated(self, tmp_path, faked):
        # It reaches a protocol field rather than a shell line here, but the
        # same rule keeps it a ref and not an argument.
        tool = codex_native_reviewer(root=str(tmp_path))

        with pytest.raises(ValueError, match="not a plain git ref"):
            await tool.run(scope="branch", ref="main; rm -rf /")

    @pytest.mark.asyncio
    async def test_the_review_thread_is_returned_for_follow_ups(self, tmp_path, faked):
        # The point of running it inline on a durable thread: codex_ask can
        # then interrogate the findings without re-running the review.
        tool = codex_native_reviewer(root=str(tmp_path))

        out = await tool.run(scope="uncommitted")

        assert "thread_id=.#thread-new" in out


# ─── the Claude Code twins ───────────────────────────────────────────


@pytest.fixture
def faked_claude(monkeypatch):
    """Swap the Claude engine/agent the factory imports for fakes."""
    import lazybridge
    import lazybridge.engines.claude_code as cc

    monkeypatch.setattr(cc, "ClaudeCodeEngine", _FakeClaudeEngine)
    monkeypatch.setattr(lazybridge, "Agent", _FakeAgent)
    return _FakeClaudeEngine, _FakeAgent


class _FakeClaudeEngine:
    last_kwargs: dict = {}

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs
        self.session_id = kwargs.get("session_id") or "sess-new"


class TestClaudeTools:
    """The mirror of the Codex tools: same arguments, same handle protocol."""

    def test_names_and_schemas_mirror_the_codex_ones(self, tmp_path):
        review = claude_reviewer(root=str(tmp_path))
        ask = claude_consultant(root=str(tmp_path))

        assert review.name == "claude_code_review"
        assert set(review.definition().parameters["properties"]) == {
            "task",
            "repo_path",
            "diff_ref",
            "paths",
            "session_id",
        }
        assert ask.name == "claude_ask"
        assert set(ask.definition().parameters["properties"]) == {
            "question",
            "repo_path",
            "session_id",
            "model",
            "thinking",
        }

    @pytest.mark.parametrize("timeout", [0, -1.0, float("inf"), float("nan")])
    def test_rejects_a_non_positive_or_infinite_timeout(self, timeout, tmp_path):
        with pytest.raises(ValueError, match="positive finite"):
            claude_reviewer(root=str(tmp_path), timeout=timeout)

    @pytest.mark.asyncio
    async def test_runs_read_only_in_the_requested_repo(self, tmp_path, faked_claude):
        (tmp_path / "repo").mkdir()
        tool = claude_reviewer(root=str(tmp_path), model="opus")

        out = await tool.run(task="review the parser", repo_path="repo")

        kwargs = _FakeClaudeEngine.last_kwargs
        assert kwargs["cwd"] == str((tmp_path / "repo").resolve())
        assert kwargs["file_roots"] == [str((tmp_path / "repo").resolve())]
        assert kwargs["model"] == "opus"
        assert kwargs["web"] is False  # a review has no business off-machine
        assert kwargs["persist_session"] is True
        assert "session_id=repo#sess-new" in out

    @pytest.mark.asyncio
    async def test_the_reviewer_gets_read_only_git_tools(self, tmp_path, faked_claude):
        # The engine grants no shell, so git has to arrive as tools.
        (tmp_path / "repo").mkdir()
        tool = claude_reviewer(root=str(tmp_path))

        await tool.run(task="review", repo_path="repo")

        assert {t.__name__ for t in _FakeAgent.last_tools} == {"git_diff", "git_status"}

    @pytest.mark.asyncio
    async def test_handles_are_scoped_to_their_repository(self, tmp_path, faked_claude):
        (tmp_path / "repo-a").mkdir()
        (tmp_path / "repo-b").mkdir()
        tool = claude_reviewer(root=str(tmp_path))

        with pytest.raises(ValueError, match="belongs to 'repo-a'"):
            await tool.run(task="look", repo_path="repo-b", session_id="repo-a#sess-1")

    @pytest.mark.asyncio
    async def test_a_matching_handle_resumes_the_bare_id(self, tmp_path, faked_claude):
        (tmp_path / "repo-a").mkdir()
        tool = claude_consultant(root=str(tmp_path))

        await tool.run(question="?", repo_path="repo-a", session_id="repo-a#sess-1")

        assert _FakeClaudeEngine.last_kwargs["session_id"] == "sess-1"

    @pytest.mark.asyncio
    async def test_the_consultant_gets_the_web_where_the_reviewer_does_not(self, tmp_path, faked_claude):
        ask = claude_consultant(root=str(tmp_path))
        await ask.run(question="?")
        assert _FakeClaudeEngine.last_kwargs["web"] is True

        review = claude_reviewer(root=str(tmp_path))
        await review.run(task="look")
        assert _FakeClaudeEngine.last_kwargs["web"] is False

    @pytest.mark.asyncio
    async def test_per_call_model_and_thinking_override_the_defaults(self, tmp_path, faked_claude):
        tool = claude_consultant(root=str(tmp_path), model="sonnet", thinking="disabled")

        await tool.run(question="?")
        assert _FakeClaudeEngine.last_kwargs["model"] == "sonnet"
        assert _FakeClaudeEngine.last_kwargs["thinking"] == "disabled"

        await tool.run(question="?", model="opus", thinking="adaptive")
        assert _FakeClaudeEngine.last_kwargs["model"] == "opus"
        assert _FakeClaudeEngine.last_kwargs["thinking"] == "adaptive"

    @pytest.mark.asyncio
    async def test_extra_tools_ride_alongside_the_git_tools(self, tmp_path, faked_claude):
        def probe() -> str:
            """Sentinel tool."""
            return "probed"

        tool = claude_consultant(root=str(tmp_path), tools=[probe])

        await tool.run(question="?")

        names = [getattr(t, "__name__", None) for t in _FakeAgent.last_tools]
        assert names == ["git_diff", "git_status", "probe"]


class TestClaudeReviewProvider:
    def test_opt_in_only(self):
        from lazytools.mcp_server.providers import PROVIDER_FACTORIES

        with pytest.raises(RuntimeError, match="opt-in"):
            PROVIDER_FACTORIES["claude_review"](allow_write=False)

    def test_write_mode_serves_both_tools(self, tmp_path, monkeypatch):
        from lazytools.mcp_server.providers import default_providers
        from lazytools.mcp_server.server import expand_tools

        # The provider skips itself without the CLI (asserted separately); this
        # test is about the surface it serves when the CLI is there.
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
        monkeypatch.setenv("LAZYTOOLS_CODE_ROOT", str(tmp_path))
        providers = default_providers(["claude_review"], allow_write=True)

        assert set(expand_tools(providers, read_only=False)) == {"claude_code_review", "claude_ask"}
        # ...and the name guard keeps them off the read-only surface, same as
        # the Codex tools: an LLM-driven tool is not part of it.
        assert expand_tools(providers, read_only=True) == {}

    def test_skipped_without_the_claude_cli(self, monkeypatch):
        from lazytools.mcp_server.providers import PROVIDER_FACTORIES

        monkeypatch.setattr("shutil.which", lambda _name: None)
        with pytest.raises(RuntimeError, match="claude CLI not found"):
            PROVIDER_FACTORIES["claude_review"](allow_write=True)


class TestHandleHygiene:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["repo#abc-123):", "repo#abc 123", "repo#", "repo#abc#"])
    async def test_a_handle_with_stray_characters_is_refused(self, bad, tmp_path, faked):
        # Copied out of a failure line, a handle keeps its trailing "):" —
        # which the server rejects with an opaque parse error about a
        # character the caller never typed.
        (tmp_path / "repo").mkdir()
        tool = codex_reviewer(root=str(tmp_path))

        with pytest.raises(ValueError, match=r"malformed|belongs to"):
            await tool.run(task="x", repo_path="repo", thread_id=bad)
