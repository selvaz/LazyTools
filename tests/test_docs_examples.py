"""Execute the refreshed Markdown examples with fake engines and local evidence.

The snippets are extracted from the actual docs, not copies. No coding runtime,
quota endpoint, package installation, MCP stdio loop or external DB is used.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from packaging.requirements import Requirement

import _code_bridge_fakes as bridge_fakes
import _code_fakes as code_fakes
from lazytools.code_bridge import _jobs, _routing, _store, cli
from lazytools.projects.admission import TelemetryReading, WindowReading

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("docs_audit", ROOT / "tools" / "audit_docs.py")
audit_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit_module)
blocks = audit_module.blocks


def python_blocks(name):
    return [code for lang, code in blocks(ROOT / name) if lang == "python"]


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LAZYBRIDGE_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_DB", str(tmp_path / "bridge.sqlite"))
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_POLL_SECONDS", "0.01")
    monkeypatch.setenv("CODEX_BIN", "mock-codex")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    # Reject accidentally reaching a real runtime even if an injection breaks.
    original = subprocess.Popen

    class GuardedPopen(original):
        def __init__(self, args, *pargs, **kwargs):
            allowed = str(args[0]).lower() == "git" or (
                args[0] == sys.executable and args[1:3] == ["-m", "unittest"]
            )
            if not allowed:
                raise AssertionError(f"Unmocked process: {args}")
            super().__init__(args, *pargs, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)
    return tmp_path


def execute(code, namespace=None):
    namespace = {} if namespace is None else namespace
    exec(compile(code, "<actual Markdown snippet>", "exec"), namespace)
    return namespace


def test_code_support_python_snippets(isolated, monkeypatch):
    from lazytools.connectors.code_support import _claude_code, _codex
    from lazytools.connectors.mcp import MCP
    from test_mcp import FakeTransport

    script = code_fakes.install(monkeypatch)
    deadlines = []
    discoveries = []
    subprocess.run(["git", "init", str(isolated)], check=True, capture_output=True)

    def fake_stdio(name, **kwargs):
        names = ["codex", "codex-reply"] if name == "codex" else ["Read", "Glob", "Grep", "Write", "Bash"]
        transport = FakeTransport([
            {"name": item, "description": "Mock runtime primitive", "inputSchema": {"type": "object", "properties": {}}}
            for item in names
        ])
        discoveries.append(transport)
        return MCP.from_transport(name, transport, allow=kwargs.get("allow"), deny=kwargs.get("deny"))

    monkeypatch.setattr(MCP, "stdio", staticmethod(fake_stdio))

    def mock_cli(*args, **kwargs):
        deadlines.append(kwargs["timeout"])
        return "Mock CLI read result"

    monkeypatch.setattr(_claude_code, "_run_claude", mock_cli)
    monkeypatch.setattr(_codex, "_run_codex", mock_cli)
    for name in ("index", "claude-code", "codex"):
        for code in python_blocks(f"docs/code-support/{name}.md"):
            before = len(script.engines)
            namespace = execute(code)
            if "async def continue_review" in code:
                resumed = script.engines[before:]
                assert len(resumed) >= 2 and len({engine.handle for engine in resumed}) == 1
            for helper in ("read_with_claude", "read_with_codex"):
                if helper in namespace:
                    assert namespace[helper]("Read deployment.py")["content_is_untrusted"] is True
            if "reviewer_for" in namespace:
                assert namespace["reviewer_for"](str(isolated)).name == "codex_code_review"
    assert deadlines == [3600.0, 3600.0]
    assert {engine.kind for engine in script.engines} == {"claude", "codex"}
    assert len(discoveries) == 4
    assert all(transport.connected and transport.closed and not transport.calls for transport in discoveries)
    assert any(engine.kwargs.get("session_alias") == "codex-implementation" for engine in script.engines)


def test_projects_and_gantt_snippets(isolated, monkeypatch):
    namespace = {}
    for code in python_blocks("docs/projects.md"):
        execute(code, namespace)
    assert (isolated / "gantt.html").read_text(encoding="utf-8").startswith("<!doctype html>")
    from lazytools.projects import gantt

    assert gantt.main(["--store", "projects.sqlite", "--out", "gantt.html", "--all-statuses", "--text"]) == 0


def test_bridge_worked_flow_from_markdown(isolated, monkeypatch):
    code = python_blocks("docs/code-bridge.md")
    execute(code[0])  # Actual setup, Git baseline and four independent boundaries.
    execute(code[1])  # Pure route + recommend with supplied readings.
    project_ns = execute(python_blocks("docs/projects.md")[0])  # Contract before delegation.
    script = bridge_fakes.install(monkeypatch)
    original_run = bridge_fakes.FakeAgent.run

    async def write_or_review(self, prompt):
        answer = await original_run(self, prompt)
        if answer.ok and self.engine.kind == "claude":
            Path(self.engine.kwargs["cwd"], "deployment.py").write_text(
                "def deployment_allowed(day):\n    return day.weekday() < 5\n", encoding="utf-8"
            )
        return answer

    monkeypatch.setattr(bridge_fakes.FakeAgent, "run", write_or_review)
    script.request_approval = True
    script.approval_overrides = {"arguments": {"command": "git add deployment.py"}}
    script.text = "No Friday-to-Monday boundary defects in the fixture change."

    def readings():
        now = datetime.now(UTC)
        return {
            "codex": TelemetryReading("codex", "mock", now, (WindowReading("codex/10080m", 10, 10080),)),
            "claude_code": TelemetryReading("claude_code", "mock", now,
                                            (WindowReading("weekly/all models", 10, 10080),)),
        }

    monkeypatch.setattr(_routing, "read_readings", readings)
    threads = []
    outcomes = []

    def spawn(argv, log_path):
        args = cli.build_parser().parse_args(argv[3:])

        def worker():
            try:
                outcomes.append(_jobs.run_job(
                    engine_name=args.engine, cwd=args.cwd, task=args.task,
                    session_name=args.session, model=args.model, effort=args.effort,
                    root=args.root, db_path=Path(args.db) if args.db else None,
                    job_id=args.job_id, timeout=args.timeout,
                    routing=json.loads(args.routing_record) if args.routing_record else None,
                ))
            except BaseException as exc:
                outcomes.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        threads.append(thread)
        return os.getpid()  # No OS worker; real job/queue/store/lock machinery.

    monkeypatch.setattr(cli, "_spawn_detached", spawn)

    def command(argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(argv)
        assert rc == 0, (argv, out.getvalue())
        return out.getvalue()

    def approve_pending():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            pending = json.loads(command(["pending", "--json"]))
            if pending:
                command(["approve", pending[0]["approval_id"]])
                return
            if not threads[-1].is_alive():
                assert outcomes[-1].status == "done"
                return
            time.sleep(0.01)
        raise AssertionError("Mock engine never filed its real approval ticket")

    ids = {}
    try:
        for lang, snippet in blocks(ROOT / "docs/code-bridge.md"):
            if lang != "sh":
                continue
            for line in snippet.splitlines():
                argv = shlex.split(line)
                if not argv:
                    continue
                if argv[0] == "lazytools-code-bridge":
                    args = [ids.get(arg, arg) for arg in argv[1:]]
                    if args[0] in ("pending", "wait"):
                        approve_pending() if threads[-1].is_alive() else None
                    output = command(args)
                    if args[0] == "run":
                        key = "REVIEW_JOB_ID" if "--review-of" in args else "WRITER_JOB_ID"
                        ids[key] = json.loads(output)["job_id"]
                    if args[0] == "wait":
                        threads[-1].join(timeout=10)
                        assert not threads[-1].is_alive()
                elif argv[0] == "python":
                    subprocess.run([sys.executable, *argv[1:]], check=True, capture_output=True)
                elif argv[0] == "git":
                    subprocess.run(argv, check=True, capture_output=True)
                else:
                    raise AssertionError(f"Unhandled tutorial line: {line}")
        assert len(outcomes) == 2 and all(outcome.status == "done" for outcome in outcomes)
        assert [engine.kind for engine in script.engines] == ["claude", "codex"]
        assert script.engines[0].kwargs["request_timeout"] == 120
        assert script.engines[1].kwargs["model"] == "gpt-6.1-sol"
        # Execute the docs' evidence recorder against the mock writer's real diff/tests.
        execute(python_blocks("docs/projects.md")[2], project_ns)
        accepted = project_ns["record_checked_attempt"](
            project_ns["store"], project_ns["contract"].contract_id,
            isolated / "toy-tandem", ids["WRITER_JOB_ID"], script.text
        )
        assert accepted.status == "accepted"
        command(["cancel", ids["WRITER_JOB_ID"], "--reason", "Stop this attempt"])
        script.mode = "error"
        script.request_approval = False
        failed = _jobs.run_job(engine_name="claude", cwd=str(isolated / "toy-tandem"), task="timeout mock",
                               root=str(isolated), db_path=isolated / "bridge.sqlite", timeout=0.1)
        assert failed.status == "failed" and "workspace:" in failed.error
        assert "1 uncommitted path(s)" in failed.error
    finally:
        for ticket in _store.build_approval_queue(_store.build_store()).list_pending_tickets():
            cli.main(["reject", ticket.approval_id, "--reason", "Fixture cleanup"])
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive()


def test_mcp_configuration_and_tools(isolated, monkeypatch):
    import shutil

    from lazytools.connectors.code_support import codex_consultant
    from lazytools.mcp_server import build_server, default_providers, expand_tools, providers
    from lazytools.mcp_server.__main__ import _parse_args

    code_fakes.install(monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: "mock-runtime")
    monkeypatch.setattr(providers, "_consultant_toolset", lambda *args: [])
    ids = ["projects", "code_review", "claude_review", "code_write", "code_sessions"]
    conf = {"code_root": str(isolated), "projects_store_db": str(isolated / "projects.sqlite")}
    monkeypatch.setenv("LAZYTOOLS_PROJECTS_TOOLS", "full")
    tools = expand_tools(default_providers(ids, allow_write=True, data_source=conf), read_only=False)
    expected = {"codex_code_review", "codex_write", "claude_code_review", "claude_ask", "claude_code_write",
                "code_sessions_list", "code_sessions_bind", "code_sessions_rename", "code_sessions_forget"}
    assert expected <= tools.keys()
    assert build_server(list(tools.values()), read_only=False) is not None
    assert codex_consultant(root=str(isolated)).name == "codex_ask"
    for name in ("docs/mcp-server.md", "docs/index.md", "docs/code-support/index.md", "README.md"):
        for lang, code in blocks(ROOT / name):
            if lang == "json":
                payload = json.loads(code)
                if "mcpServers" in payload:
                    for server in payload["mcpServers"].values():
                        args = _parse_args(server.get("args", []))
                        assert not args.providers or set(args.providers) <= providers.PROVIDER_FACTORIES.keys()
            if lang in ("sh", "bash"):
                for line in code.splitlines():
                    if line.startswith("lazytools-mcp"):
                        assert _parse_args(shlex.split(line)[1:]) is not None
    # Execute the modified programmatic block without touching a data service.
    monkeypatch.setattr("lazytools.mcp_server.server.expand_tools", lambda *args, **kwargs: {})
    programmatic = [c for c in python_blocks("docs/mcp-server.md") if "name=\"lazytools-finance\"" in c]
    assert len(programmatic) == 1
    execute(programmatic[0])


def test_changed_install_snippets_and_tree_flags(isolated, monkeypatch):
    # The portfolio wrappers live behind the optional ``fin`` extra (lazyfin);
    # CI installs the core test extras only.
    pytest.importorskip("lazyfin")
    import tomllib

    extras = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["optional-dependencies"]
    for name in ("README.md", "docs/index.md", "docs/mcp-server.md", "docs/code-support/index.md",
                 "docs/skills-council.md"):
        for lang, code in blocks(ROOT / name):
            if lang not in ("sh", "bash"):
                continue
            for line in code.splitlines():
                if line.startswith("G="):
                    git_url = shlex.split(line)[0][2:]
                if "pip install" in line and not line.lstrip().startswith("#"):
                    argv = shlex.split(line.split("#", 1)[0])
                    requirement = Requirement(argv[-1].replace("$G", git_url) if "$G" in argv[-1] else argv[-1])
                    if requirement.name == "lazytoolkit":
                        assert requirement.url.endswith("@v0.10.1")
                        assert requirement.extras <= extras.keys()
    # Mock only the optional portfolio dependency, preserving the real constructor.
    module = ModuleType("lazyportfolio")
    for name in ("HierarchicalV2Backtester", "HierarchicalV2Estimator", "MarketDataHubOptimizationBackend", "V2Model"):
        setattr(module, name, SimpleNamespace)
    for name in ("delete_model", "list_saved_models", "mode_from_config", "read_model", "resolve_store_path", "write_model"):
        setattr(module, name, lambda *args, **kwargs: None)
    calendar = ModuleType("lazyportfolio.calendar")
    calendar._annualization_factor = calendar._resample_simple_returns = lambda *args: None

    class MockModel:
        @staticmethod
        def from_config(config):
            return SimpleNamespace(reference_currency=config["currency"], config=config)

    class MockBackend:
        def load_returns(self, instruments, **kwargs):
            assert instruments == ["ticker:SPY", "ticker:TLT"]
            assert kwargs == {"start": "2018-01-01", "end": "2026-01-01", "frequency": "D", "currency": "USD"}
            return SimpleNamespace(returns=object(), metadata={"source": "mock-hub", "n_rows": 1000})

    class MockBacktester:
        def run(self, model, returns, **kwargs):
            assert model.config["nodes"][0]["goal"]["objective"] == "min_risk"
            assert kwargs == {"mode": "flat", "train_size": 104, "estimation_frequency": "W",
                              "rebalance_frequency": "M", "transaction_cost_bps": 10.0}
            return SimpleNamespace(folds=[object()], metrics={"FINAL": {"cagr": 0.01}, "B0": {"cagr": 0.02}},
                                   transaction_cost_paid={"FINAL": 0.001})

    module.V2Model = MockModel
    module.MarketDataHubOptimizationBackend = MockBackend
    module.HierarchicalV2Backtester = MockBacktester
    monkeypatch.setitem(sys.modules, "lazyportfolio", module)
    monkeypatch.setitem(sys.modules, "lazyportfolio.calendar", calendar)
    for name in ("README.md", "docs/portfolio-optimization.md"):
        for code in python_blocks(name):
            if "flat = PortfolioOptimizationTools()" in code:
                namespace = execute(code)
                if "flat" in namespace:
                    assert len(namespace["flat"].as_tools()) == 3
            if "tools = PortfolioTreeTools(" in code:
                namespace = execute(code)
                assert len(namespace["tools"].as_tools()) == 7


def test_restored_tree_json_matches_current_model():
    V2Model = pytest.importorskip("lazyportfolio").V2Model

    configs = [json.loads(code) for lang, code in blocks(ROOT / "docs/portfolio-optimization.md") if lang == "json"]
    assert len(configs) == 1
    model = V2Model.from_config(configs[0])
    assert model.reference_currency == "USD"
    assert model.root.id == "root" and len(model.root.children) == 2


def test_every_page_import_audit():
    try:
        import lazyfin  # noqa: F401
        optional_missing = ()
    except ImportError:
        # Pages documenting the optional ``fin`` extra cannot be imported
        # without it; every other finding still fails.
        optional_missing = ("requires the lazyfin package",)
    for path in [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]:
        _, findings = audit_module.audit(path)
        findings = [f for f in findings if not any(m in str(f) for m in optional_missing)]
        assert not findings, (path, findings)


def test_touched_python_blocks_compile():
    for name in ("docs/code-bridge.md", "docs/projects.md", "docs/code-support/index.md",
                 "docs/code-support/claude-code.md", "docs/code-support/codex.md"):
        for code in python_blocks(name):
            ast.parse(code)
