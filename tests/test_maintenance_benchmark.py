"""No-credential tests for the paired LazyBridge Plan and native writer tools."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from lazybridge import Envelope, Tool

from lazytools.maintenance import CheckSpec, ImprovementRequest, RepoSpec, Workspace
from lazytools.maintenance.benchmark import PairedMaintenanceBenchmark
from lazytools.maintenance.improvement import _existing_tools


@pytest.fixture
def pair(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    workspace = Workspace((RepoSpec("repo", root),), tmp_path / "state")
    objective = "Fix a deterministic regression and add a boundary test"
    request = ImprovementRequest(
        repo="repo", objective=objective, allowed_paths=("api.py",),
        approved_local_proposal=True,
        writer_model="sonnet", reviewer_model="gpt-6-terra",
        checks=(CheckSpec("py", "repo", ("python", "--version")),),
    )

    class StubCoordinator:
        context_mode = "repomap"
        task_query = objective

        def __init__(self):
            self.workspace = workspace
            self.calls = 0

        def execute(self):
            self.calls += 1
            return {
                "sha": {"repo": "f" * 40},
                "run_id": "scan-a",
                "report_json": str(workspace.state_dir / "scan.json"),
                "context_mode": "repomap",
                "task_context": {"repo": {"overview": "Same overview", "search": "Same symbols"}},
            }

    return StubCoordinator(), request


@pytest.mark.parametrize("order", ["baseline_first", "mapped_first"])
def test_same_scan_same_models_only_writer_tool_access_changes(pair, monkeypatch, order):
    import lazytools.maintenance.benchmark as module

    coordinator, request = pair
    captured = []

    class StubImprover:
        def __init__(self, workspace):
            assert workspace == coordinator.workspace

        def run(self, report, policy, **kwargs):
            assert report["run_id"] == "scan-a"
            assert report["context_mode"] == "repomap"
            assert report["task_context"]["repo"]["search"] == "Same symbols"
            assert policy.objective == request.objective
            assert policy.writer_model == request.writer_model
            assert policy.reviewer_model == request.reviewer_model
            captured.append(policy.writer_repomap)
            return {
                "status": "proposal_ready_for_human_review",
                "report_json": "stub-local-proposal",
                "rounds": [{
                    "verification": {"status": "passed_selected_checks"},
                    "writer_usage": {
                        "source": "lazybridge_envelope",
                        "input_tokens": 40, "output_tokens": 18,
                    },
                }],
            }

    monkeypatch.setattr(module, "MaintenanceImprover", StubImprover)
    benchmark = PairedMaintenanceBenchmark(coordinator, request, order=order)
    steps = [step.name for step in benchmark.build_agent().engine.steps]
    expected = ["bench_scan", "bench_baseline", "bench_repomap", "bench_report"]
    if order == "mapped_first":
        expected[1:3] = ["bench_repomap", "bench_baseline"]
    assert steps == expected

    result = benchmark.run()
    assert coordinator.calls == 1
    assert captured == ([False, True] if order == "baseline_first" else [True, False])
    assert result["coordinator"]["context_mode"] == "repomap"
    assert result["arms"]["baseline"]["configured_writer_tools"] == []
    assert result["arms"]["repomap"]["configured_writer_tools"] == [
        "codemap_overview", "codemap_search", "codemap_context", "codemap_impact"
    ]
    assert result["arms"]["baseline"]["status"] == result["arms"]["repomap"]["status"]
    assert result["arms"]["baseline"]["writer_usage"][0]["input_tokens"] == 40
    assert (coordinator.workspace.state_dir / "benchmarks" / result["case_id"] / "pair.json").is_file()


def test_benchmark_fails_closed_without_pins_checks_or_authorization(pair):
    coordinator, request = pair
    with pytest.raises(ValueError, match="SAME RepoMap"):
        coordinator.context_mode = "git"
        PairedMaintenanceBenchmark(coordinator, request)
    coordinator.context_mode = "repomap"
    with pytest.raises(ValueError, match="task_query"):
        coordinator.task_query = "Other task"
        PairedMaintenanceBenchmark(coordinator, request)
    coordinator.task_query = request.objective
    with pytest.raises(ValueError, match="model"):
        PairedMaintenanceBenchmark(coordinator, replace(request, writer_model=None))
    with pytest.raises(ValueError, match="acceptance tests"):
        PairedMaintenanceBenchmark(coordinator, replace(request, checks=()))
    with pytest.raises(PermissionError, match="approval"):
        PairedMaintenanceBenchmark(
            coordinator, replace(request, approved_local_proposal=False)
        ).run()
    assert coordinator.calls == 0


@pytest.mark.parametrize("mapped", [False, True])
def test_existing_writer_provider_gets_readonly_tools_on_clone_only(tmp_path, monkeypatch, mapped):
    import lazytools.codemap as codemap
    import lazytools.connectors.code_support as code_support

    repo = tmp_path / "source"
    clone = tmp_path / "private"
    other = tmp_path / "dependency"
    for path in (repo, clone, other):
        path.mkdir()
    workspace = Workspace(
        (RepoSpec("repo", repo), RepoSpec("dependency", other)),
        tmp_path / "state",
    )
    request = ImprovementRequest(
        "repo", "Fix a deterministic regression and add boundary tests",
        ("api.py",), writer_repomap=mapped,
    )
    observed = {"maps": [], "writers": []}

    class FakeMap:
        def __init__(self, roots, *, use_embeddings):
            assert use_embeddings is False
            observed["maps"].append(dict(roots))

        def as_tools(self):
            return [Tool.wrap(lambda query: query, name="codemap_search")]

    class FakeWriter:
        def __init__(self, **kwargs):
            observed["writers"].append(kwargs)

        def as_tools(self):
            return [SimpleNamespace(name="claude_code_write")]

    monkeypatch.setattr(codemap, "RepoMapTools", FakeMap)
    monkeypatch.setattr(code_support, "CodeWriteTools", FakeWriter)
    monkeypatch.setattr(code_support, "codex_reviewer", lambda **kw: "reviewer")
    writer, reviewer = _existing_tools(clone, request, workspace)
    assert writer.name == "claude_code_write"
    assert reviewer == "reviewer"
    opts = observed["writers"][0]
    assert len(opts["application_tools"]) == (1 if mapped else 0)
    assert opts["claude_bash"] is False
    assert observed["maps"] == (
        [{"repo": clone, "dependency": other}] if mapped else []
    )


@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_native_coding_agent_receives_additional_lazybridge_tools(tmp_path, monkeypatch, engine):
    """No Claude/Codex authentication: test only Python tool registration."""
    import lazybridge

    import lazytools.connectors.code_support._writer as module

    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    names = []
    extra = Tool.wrap(lambda query: "mapped:" + query, name="codemap_search")

    class FakeAgent:
        def __init__(self, engine, *, name, tools):
            names.append((name, [item.name for item in tools]))

        async def run(self, task):
            return Envelope(payload="done")

    monkeypatch.setattr(lazybridge, "Agent", FakeAgent)
    if engine == "codex":
        monkeypatch.setattr(
            module, "_make_codex_engine", lambda **kwargs: SimpleNamespace(thread_id=None)
        )
    else:
        monkeypatch.setattr(
            module, "_make_claude_engine", lambda **kwargs: SimpleNamespace(session_id=None)
        )
    provider = module.CodeWriteTools(
        base_dir=str(root), claude=engine == "claude", codex=engine == "codex",
        require_confirmation=False, claude_bash=False,
        application_tools=(extra,),
    )
    if engine == "codex":
        result = asyncio.run(provider._codex_write("fix bug", cwd=str(root)))
    else:
        result = asyncio.run(provider._claude_write("fix bug", cwd=str(root)))
    assert isinstance(result, dict)
    assert result["usage"]["source"] == "lazybridge_envelope"
    assert names == [(f"{engine}_code_write" if engine == "claude" else "codex_write",
                      ["codemap_search"])]
