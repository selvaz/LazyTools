"""Per-call ``model`` / ``effort`` / ``session_name`` on the review and consult tools,
and the ``code_sessions_*`` management provider.

The engines are faked (see ``_code_fakes``) but the alias registry is a REAL
``SessionRegistry`` file under ``tmp_path``, so resume / bind / rebind / rename /
forget are exercised against LazyBridge's own rules. Nothing touches
``~/.lazybridge``.
"""

from __future__ import annotations

import pytest

from _code_fakes import Script, install

pytest.importorskip("lazybridge")

from lazybridge.engines.sessions import SessionRegistry, normalize_scope

from lazytools.connectors.code_support import (
    CodeSessionTools,
    ReviewNotPerformed,
    claude_consultant,
    claude_reviewer,
    codex_consultant,
    codex_native_reviewer,
    codex_reviewer,
)


@pytest.fixture(autouse=True)
def _fake_codex_bin(monkeypatch):
    """Make ``codex_executable()`` resolve without a Codex install."""
    monkeypatch.setenv("CODEX_BIN", "codex-not-really-here")


@pytest.fixture
def script(monkeypatch) -> Script:
    return install(monkeypatch)


@pytest.fixture
def registry(tmp_path_factory) -> SessionRegistry:
    return SessionRegistry(tmp_path_factory.mktemp("registry") / "sessions.json")


@pytest.fixture
def root(tmp_path):
    base = tmp_path / "code-root"
    (base / "repo-a").mkdir(parents=True)
    (base / "repo-b").mkdir()
    return base


def _scope(path) -> str:
    return normalize_scope(path)


# ─── model / effort passthrough and validation ───────────────────────────────


class TestModelAndEffort:
    async def test_codex_review_passes_model_and_effort_per_call(self, root, script, registry):
        tool = codex_reviewer(root=str(root), session_registry=registry)
        await tool.run(task="look", model="gpt-x", effort="xhigh")
        assert script.kwargs["model"] == "gpt-x"
        assert script.kwargs["reasoning_effort"] == "xhigh"

    async def test_codex_call_overrides_the_factory_default(self, root, script, registry):
        tool = codex_consultant(root=str(root), model="base", effort="low", session_registry=registry)
        await tool.run(question="?")
        assert (script.kwargs["model"], script.kwargs["reasoning_effort"]) == ("base", "low")
        await tool.run(question="?", model="big", effort="max")
        assert (script.kwargs["model"], script.kwargs["reasoning_effort"]) == ("big", "max")

    async def test_defaults_are_the_providers_own(self, root, script, registry):
        await codex_consultant(root=str(root), session_registry=registry).run(question="?")
        assert script.kwargs["model"] is None
        assert script.kwargs["reasoning_effort"] is None

    async def test_native_review_takes_model_and_effort(self, root, script, registry):
        tool = codex_native_reviewer(root=str(root), session_registry=registry)
        await tool.run(scope="uncommitted", model="gpt-x", effort="medium")
        assert script.kwargs["model"] == "gpt-x"
        assert script.kwargs["reasoning_effort"] == "medium"
        assert script.kwargs["review_target"] == {"type": "uncommittedChanges"}

    async def test_claude_review_and_ask_take_effort(self, root, script, registry):
        await claude_reviewer(root=str(root), session_registry=registry).run(task="x", model="opus", effort="high")
        assert (script.kwargs["model"], script.kwargs["reasoning_effort"]) == ("opus", "high")

        await claude_consultant(root=str(root), session_registry=registry).run(
            question="?", effort="max", thinking="adaptive"
        )
        assert script.kwargs["reasoning_effort"] == "max"
        assert script.kwargs["thinking"] == "adaptive"

    @pytest.mark.parametrize("effort", ["hgih", "ultra", "persistent"])
    async def test_a_bad_codex_effort_names_the_allowed_values(self, effort, root, script, registry):
        tool = codex_reviewer(root=str(root), session_registry=registry)
        with pytest.raises(ValueError, match="use one of: none, minimal, low, medium, high, xhigh, max"):
            await tool.run(task="x", effort=effort)
        assert script.engines == []

    @pytest.mark.parametrize("effort", ["hgih", "none", "minimal"])
    async def test_a_bad_claude_effort_names_the_allowed_values(self, effort, root, script, registry):
        tool = claude_consultant(root=str(root), session_registry=registry)
        with pytest.raises(ValueError, match="use one of: low, medium, high, xhigh, max"):
            await tool.run(question="x", effort=effort)
        assert script.engines == []

    async def test_blank_strings_mean_not_given(self, root, script, registry):
        tool = codex_consultant(root=str(root), model="base", session_registry=registry)
        await tool.run(question="?", model="", effort="  ", session_name="")
        assert script.kwargs["model"] == "base"
        assert script.kwargs["reasoning_effort"] is None
        assert script.kwargs["session_alias"] is None


# ─── session_name ────────────────────────────────────────────────────────────


class TestCodexSessionNames:
    async def test_a_new_name_opens_a_thread_and_is_remembered(self, root, script, registry):
        tool = codex_consultant(root=str(root), session_registry=registry)

        out = await tool.run(question="remember PINEAPPLE", session_name="notes")

        assert script.kwargs["session_alias"] == "notes"
        assert script.kwargs["session_registry"] is registry
        assert script.kwargs["persist_thread"] is True
        header = out.splitlines()[0]
        assert "thread_id=.#codex-id-1" in header and "session_name=notes" in header
        assert registry.resolve("codex", root, "notes") == "codex-id-1"

    async def test_the_same_name_resumes_that_thread(self, root, script, registry):
        tool = codex_consultant(root=str(root), session_registry=registry)
        await tool.run(question="remember PINEAPPLE", session_name="notes")

        out = await tool.run(question="what word?", session_name="notes")

        assert script.last.handle == "codex-id-1"
        assert "thread_id=.#codex-id-1" in out.splitlines()[0]

    async def test_no_name_keeps_the_old_header(self, root, script, registry):
        out = await codex_consultant(root=str(root), session_registry=registry).run(question="?")
        assert "session_name" not in out.splitlines()[0]
        assert script.kwargs["session_alias"] is None

    async def test_an_explicit_thread_id_wins_and_rebinds(self, root, script, registry):
        tool = codex_consultant(root=str(root), session_registry=registry)
        await tool.run(question="one", session_name="notes")

        out = await tool.run(question="two", session_name="notes", thread_id=".#native-42")

        assert script.kwargs["thread_id"] == "native-42"
        assert "thread_id=.#native-42" in out.splitlines()[0]
        assert registry.resolve("codex", root, "notes") == "native-42"

    async def test_names_are_per_repository(self, root, script, registry):
        tool = codex_consultant(root=str(root), session_registry=registry)
        await tool.run(question="a", repo_path="repo-a", session_name="notes")
        await tool.run(question="b", repo_path="repo-b", session_name="notes")

        assert registry.resolve("codex", root / "repo-a", "notes") != registry.resolve(
            "codex", root / "repo-b", "notes"
        )

    async def test_the_reviewer_and_native_review_take_names_too(self, root, script, registry):
        review = await codex_reviewer(root=str(root), session_registry=registry).run(task="x", session_name="rev")
        assert "session_name=rev" in review.splitlines()[0]

        native = await codex_native_reviewer(root=str(root), session_registry=registry).run(
            scope="uncommitted", session_name="nat"
        )
        assert "session_name=nat" in native.splitlines()[0]
        assert {r["name"] for r in registry.entries("codex")} == {"rev", "nat"}

    async def test_a_bad_name_is_refused_with_the_rule(self, root, script, registry):
        tool = codex_consultant(root=str(root), session_registry=registry)
        with pytest.raises(ValueError, match="invalid session alias"):
            await tool.run(question="?", session_name="2 bad")
        assert script.engines == []

    async def test_a_failed_turn_keeps_handle_and_name(self, root, script, registry):
        script.mode = "error"
        tool = codex_reviewer(root=str(root), session_registry=registry)

        with pytest.raises(ReviewNotPerformed) as excinfo:
            await tool.run(task="review", session_name="notes")

        exc = excinfo.value
        assert exc.handle == ".#codex-id-1"
        assert exc.session_name == "notes"
        assert "session_name=notes" in str(exc) and "thread_id=.#codex-id-1" in str(exc)
        # The name was bound even though the turn failed: it can be used to go and look.
        assert registry.resolve("codex", root, "notes") == "codex-id-1"


class TestClaudeSessionNames:
    async def test_a_new_name_opens_a_session_and_the_same_name_resumes_it(self, root, script, registry):
        tool = claude_consultant(root=str(root), session_registry=registry)

        first = await tool.run(question="remember PINEAPPLE", session_name="notes")
        assert script.kwargs["session_alias"] == "notes"
        header = first.splitlines()[0]
        assert "session_id=.#claude-id-1" in header and "session_name=notes" in header

        second = await tool.run(question="what word?", session_name="notes")
        assert script.last.handle == "claude-id-1"
        assert "session_id=.#claude-id-1" in second.splitlines()[0]

    async def test_an_explicit_session_id_wins_and_rebinds(self, root, script, registry):
        tool = claude_reviewer(root=str(root), session_registry=registry)
        await tool.run(task="one", session_name="notes")

        await tool.run(task="two", session_name="notes", session_id=".#sess-9")

        assert script.kwargs["session_id"] == "sess-9"
        assert registry.resolve("claude", root, "notes") == "sess-9"

    async def test_codex_and_claude_names_do_not_collide(self, root, script, registry):
        await codex_consultant(root=str(root), session_registry=registry).run(question="?", session_name="same")
        await claude_consultant(root=str(root), session_registry=registry).run(question="?", session_name="same")

        assert registry.resolve("codex", root, "same") == "codex-id-1"
        assert registry.resolve("claude", root, "same") == "claude-id-2"

    async def test_a_failed_turn_keeps_handle_and_name(self, root, script, registry):
        script.mode = "error"
        tool = claude_reviewer(root=str(root), session_registry=registry)

        with pytest.raises(ReviewNotPerformed) as excinfo:
            await tool.run(task="review", session_name="notes")

        assert excinfo.value.session_name == "notes"
        assert excinfo.value.handle == ".#claude-id-1"
        assert "session_name=notes" in str(excinfo.value)


# ─── code_sessions_* management provider ─────────────────────────────────────


def _tools(provider: CodeSessionTools) -> dict:
    return {t.name: t for t in provider.as_tools()}


class TestCodeSessionTools:
    def test_read_only_provider_exposes_only_the_list(self, root, registry):
        assert set(_tools(CodeSessionTools(root=str(root), session_registry=registry))) == {"code_sessions_list"}

    def test_mutators_appear_only_when_allowed(self, root, registry):
        tools = _tools(CodeSessionTools(root=str(root), allow_mutate=True, session_registry=registry))
        assert set(tools) == {
            "code_sessions_list",
            "code_sessions_bind",
            "code_sessions_rename",
            "code_sessions_forget",
        }

    def test_direct_mutator_calls_are_refused_without_allow(self, root, registry):
        provider = CodeSessionTools(root=str(root), session_registry=registry)
        with pytest.raises(PermissionError, match="read-only"):
            provider.code_sessions_bind("codex", "x", "id-1")
        with pytest.raises(PermissionError):
            provider.code_sessions_rename("codex", "x", "y")
        with pytest.raises(PermissionError):
            provider.code_sessions_forget("codex", "x")
        assert registry.entries() == []

    def test_list_rows_carry_the_documented_fields(self, root, registry):
        registry.bind("codex", root / "repo-a", "notes", "thr-1", model="gpt-x", effort="high")
        provider = CodeSessionTools(root=str(root), session_registry=registry)

        (row,) = provider.code_sessions_list()

        assert set(row) == {"name", "kind", "native_id", "scope", "model", "effort", "updated_at"}
        assert (row["name"], row["kind"], row["native_id"]) == ("notes", "codex", "thr-1")
        assert (row["model"], row["effort"]) == ("gpt-x", "high")
        assert row["scope"] == _scope(root / "repo-a")
        assert row["updated_at"]

    def test_list_filters_by_kind_and_repo(self, root, registry):
        registry.bind("codex", root / "repo-a", "one", "t-1")
        registry.bind("claude", root / "repo-a", "two", "s-1")
        registry.bind("codex", root / "repo-b", "three", "t-2")
        provider = CodeSessionTools(root=str(root), session_registry=registry)

        assert {r["name"] for r in provider.code_sessions_list()} == {"one", "two", "three"}
        assert {r["name"] for r in provider.code_sessions_list(kind="codex")} == {"one", "three"}
        assert {r["name"] for r in provider.code_sessions_list(repo_path="repo-a")} == {"one", "two"}
        assert [r["name"] for r in provider.code_sessions_list(kind="claude", repo_path="repo-a")] == ["two"]
        with pytest.raises(ValueError, match="kind must be one of"):
            provider.code_sessions_list(kind="gemini")

    def test_list_without_repo_path_hides_scopes_outside_the_root(self, root, tmp_path, registry):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        registry.bind("codex", outside, "secret-repo-name", "t-9")
        registry.bind("codex", root / "repo-a", "mine", "t-1")

        rows = CodeSessionTools(root=str(root), session_registry=registry).code_sessions_list()

        assert [r["name"] for r in rows] == ["mine"]

    def test_repo_path_is_confined_to_the_root(self, root, registry):
        provider = CodeSessionTools(root=str(root), allow_mutate=True, session_registry=registry)
        for call in (
            lambda: provider.code_sessions_list(repo_path=".."),
            lambda: provider.code_sessions_bind("codex", "x", "id", repo_path=".."),
            lambda: provider.code_sessions_rename("codex", "x", "y", repo_path="../.."),
            lambda: provider.code_sessions_forget("codex", "x", repo_path=str(root.parent)),
        ):
            with pytest.raises(ValueError, match="outside the allowed root"):
                call()

    def test_bind_rename_forget_roundtrip(self, root, registry):
        provider = CodeSessionTools(root=str(root), allow_mutate=True, session_registry=registry)

        bound = provider.code_sessions_bind("claude", "notes", "sess-1", repo_path="repo-a")
        assert bound["bound"] is True
        assert registry.resolve("claude", root / "repo-a", "notes") == "sess-1"

        # Re-binding an existing name re-points it.
        provider.code_sessions_bind("claude", "notes", "sess-2", repo_path="repo-a")
        assert registry.resolve("claude", root / "repo-a", "notes") == "sess-2"

        assert provider.code_sessions_rename("claude", "notes", "plan", repo_path="repo-a")["renamed"] is True
        assert registry.resolve("claude", root / "repo-a", "notes") is None
        assert registry.resolve("claude", root / "repo-a", "plan") == "sess-2"

        assert provider.code_sessions_forget("claude", "plan", repo_path="repo-a")["forgotten"] is True
        assert provider.code_sessions_forget("claude", "plan", repo_path="repo-a")["forgotten"] is False
        assert registry.entries() == []

    def test_rename_errors_are_value_errors(self, root, registry):
        provider = CodeSessionTools(root=str(root), allow_mutate=True, session_registry=registry)
        provider.code_sessions_bind("codex", "a", "t-1")
        provider.code_sessions_bind("codex", "b", "t-2")

        with pytest.raises(ValueError):
            provider.code_sessions_rename("codex", "missing", "c")
        with pytest.raises(ValueError):
            provider.code_sessions_rename("codex", "a", "b")  # target exists

    def test_bad_names_and_kinds_are_refused(self, root, registry):
        provider = CodeSessionTools(root=str(root), allow_mutate=True, session_registry=registry)
        with pytest.raises(ValueError, match="invalid session alias"):
            provider.code_sessions_bind("codex", "9bad", "t-1")
        with pytest.raises(ValueError, match="kind is required"):
            provider.code_sessions_bind("", "ok", "t-1")
        with pytest.raises(ValueError, match="kind must be one of"):
            provider.code_sessions_forget("gemini", "ok")

    async def test_a_name_bound_by_hand_is_resumed_by_a_tool(self, root, script, registry):
        provider = CodeSessionTools(root=str(root), allow_mutate=True, session_registry=registry)
        provider.code_sessions_bind("codex", "adopted", "thread-from-header")

        await codex_consultant(root=str(root), session_registry=registry).run(question="?", session_name="adopted")

        assert script.last.handle == "thread-from-header"

    def test_default_registry_honours_the_env_var(self, root, tmp_path, monkeypatch):
        """No ``session_registry`` argument: LazyBridge's default (env-overridable) is used."""
        path = tmp_path / "from-env" / "sessions.json"
        monkeypatch.setenv("LAZYBRIDGE_SESSIONS_FILE", str(path))
        import lazybridge.engines.sessions as sessions

        monkeypatch.setattr(sessions, "_default_registry", None, raising=False)
        provider = CodeSessionTools(root=str(root), allow_mutate=True)

        provider.code_sessions_bind("codex", "x", "t-1")

        assert path.exists()


class TestCodeSessionsProvider:
    def test_requires_write_mode(self):
        from lazytools.mcp_server.providers import PROVIDER_FACTORIES

        with pytest.raises(RuntimeError, match="opt-in"):
            PROVIDER_FACTORIES["code_sessions"](allow_write=False)

    def test_write_mode_serves_list_and_mutators(self, tmp_path, monkeypatch):
        from lazytools.mcp_server.providers import default_providers
        from lazytools.mcp_server.server import expand_tools

        monkeypatch.setenv("LAZYTOOLS_CODE_ROOT", str(tmp_path))
        providers = default_providers(["code_sessions"], allow_write=True)

        names = set(expand_tools(providers, read_only=False))
        assert names == {
            "code_sessions_list",
            "code_sessions_bind",
            "code_sessions_rename",
            "code_sessions_forget",
        }

    def test_read_only_mode_serves_nothing(self, tmp_path, monkeypatch):
        """Read-only guard: the mutators match UNSAFE_TOOL_PATTERNS, and the provider is opt-in."""
        from lazytools.mcp_server.providers import default_providers
        from lazytools.mcp_server.server import UNSAFE_TOOL_PATTERNS, expand_tools

        for name in ("code_sessions_bind", "code_sessions_rename", "code_sessions_forget"):
            assert any(p in name for p in UNSAFE_TOOL_PATTERNS), name

        monkeypatch.setenv("LAZYTOOLS_CODE_ROOT", str(tmp_path))
        assert default_providers(["code_sessions"], allow_write=False) == []
        providers = default_providers(["code_sessions"], allow_write=True)
        assert set(expand_tools(providers, read_only=True)) <= {"code_sessions_list"}
