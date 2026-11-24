"""Offline acceptance tests for the MCP code policy boundary."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from lazybridge import Tool
from lazybridge.engines.sessions import SessionRegistry
from lazybridge.ext.approval import ApprovalQueue
from lazybridge.policy import PolicyError, TaskGrant, resolve

from _code_fakes import install
from lazytools.code_bridge import _launch, _registry, _store, _task, cli
from lazytools.code_bridge._mcp import CodeBridgeTools
from lazytools.connectors.code_support import (
    CodeSessionTools,
    CodeWriteTools,
    claude_consultant,
    claude_reviewer,
    codex_consultant,
    codex_native_reviewer,
    codex_reviewer,
)
from lazytools.connectors.code_support._policy import (
    CODE_ACCESS,
    CodeTaskContext,
    local_launch,
    native_client,
    task_scope,
)
from lazytools.connectors.code_support._writer import CodeWriteBlocked
from lazytools.mcp_server import build_server
from lazytools.mcp_server._code_scope import configured_task_resolver
from lazytools.mcp_server.server import http_app
from lazytools.projects.task_service import ProjectTaskService
from mcp_test_utils import connected_client, protocol_field

pytest.importorskip("mcp")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    for engine in ("CODEX", "CLAUDE"):
        monkeypatch.setenv(f"LAZYTOOLS_CODE_BRIDGE_{engine}_RELEASE_REPORT", str(tmp_path / "test-report"))
    monkeypatch.delenv("LAZYTOOLS_CODE_BRIDGE_PROFILES", raising=False)
    monkeypatch.setenv("LAZYTOOLS_PROJECTS_STORE_DB", str(tmp_path / "projects.sqlite"))
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_DB", str(tmp_path / "bridge.sqlite"))
    monkeypatch.setenv("LAZYBRIDGE_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    return repo


@pytest.fixture
def qualified(monkeypatch):
    """Stand in for release evidence. The adapter still uses the real compilers."""
    compile_launch = _launch._compile

    def compile_fixture(engine, policy, report):
        compiled, _ = compile_launch(engine, policy, None)
        if report is None:
            return compiled, None
        assert policy.network == "off" and not policy.tools
        return {**compiled, "eligible": True, "ineligibility_reasons": []}, "fixture-receipt"

    monkeypatch.setattr(_launch, "_compile", compile_fixture)


def context(repo, profile="read", classes=("read",), **kwargs):
    policy = resolve(profile, TaskGrant(repo, {"tool_classes": classes}, "off", "local"))
    return CodeTaskContext(
        "p/task", "worker", datetime.now(UTC) + timedelta(minutes=5), policy, frozenset(CODE_ACCESS), **kwargs,
    )


def result_error(result):
    assert protocol_field(result, "is_error", "isError")
    return result.content[0].text


async def test_nested_writer_keeps_scratch_under_parent_and_is_discoverable(workspace, qualified, monkeypatch):
    script = install(monkeypatch)
    nested = workspace / "packages" / "app"
    nested.mkdir(parents=True)
    task = context(workspace, "edit-and-run", ("read", "edit"))
    writer = CodeWriteTools(base_dir=str(nested), claude=False, codex=True)
    server = build_server([writer], read_only=False, code_task_resolver=lambda _: task)

    async with connected_client(server) as client:
        assert {tool.name for tool in (await client.list_tools()).tools} == {"codex_write"}
        args = {"task": "Make a scoped edit"}
        approval = result_error(await client.call_tool("codex_write", args))
        ticket_id = approval.split("approval_id=")[1].split()[0]
        assert cli.main(["approve", ticket_id]) == 0
        result = await client.call_tool("codex_write", {**args, "approval_id": ticket_id})
        assert not protocol_field(result, "is_error", "isError")

    with task_scope(task):
        launch = local_launch("codex_write", "codex", nested)
    scratch = task.policy.scratch_folder / "packages" / "app"
    assert launch.policy.folder == nested
    assert launch.policy.scratch_folder == scratch
    assert script.kwargs["cwd"] == str(nested)
    roots = launch.compiled["config_overrides"]["sandbox_workspace_write"]["writable_roots"]
    assert roots == [str(nested), str(scratch)]


async def test_real_sdk_rechecks_list_and_call_on_an_existing_connection(workspace, qualified, monkeypatch):
    script = install(monkeypatch)
    writer = CodeWriteTools(base_dir=str(workspace), claude=False, codex=True, require_confirmation=False)
    reviews = [codex_consultant(root=str(workspace)), codex_native_reviewer(root=str(workspace))]
    listing = CodeSessionTools(root=str(workspace), allow_mutate=True)
    fake_reader = Tool(lambda: pytest.fail("unclassified operation was called"), name="sounds_read_only", policy_grant="read")
    valid = context(workspace)
    active = valid
    server = build_server(
        [writer, listing, fake_reader, *reviews], read_only=False, code_task_resolver=lambda _: active,
    )
    async with connected_client(server) as client:
        by_name = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert set(by_name) == {"codex_ask", "code_sessions_list"}
        assert protocol_field(by_name["codex_ask"].annotations, "read_only_hint", "readOnlyHint") is False
        assert protocol_field(by_name["code_sessions_list"].annotations, "read_only_hint", "readOnlyHint") is True
        for name in ("codex_write", "codex_review_changes", "sounds_read_only", "code_sessions_forget"):
            result_error(await client.call_tool(name, {}))
        ok = await client.call_tool("codex_ask", {"question": "Explain this API"})
        assert not protocol_field(ok, "is_error", "isError")
        assert ok.content[0].text.endswith("done")
        assert not protocol_field(await client.call_tool("code_sessions_list", {}), "is_error", "isError")
        stale = replace(valid, expires_at=datetime.now(UTC) - timedelta(seconds=1))
        mismatched = replace(valid, policy=replace(valid.policy, revision="obsolete"))
        sibling = workspace.with_name("sibling")
        sibling.mkdir()
        for invalid in (stale, mismatched, context(sibling)):
            active = invalid
            assert (await client.list_tools()).tools == []
            result_error(await client.call_tool("codex_ask", {"question": "Reuse the previous listing"}))
        active = valid
        monkeypatch.delenv("LAZYTOOLS_CODE_BRIDGE_CODEX_RELEASE_REPORT")
        assert {tool.name for tool in (await client.list_tools()).tools} == {"code_sessions_list"}
        result_error(await client.call_tool("codex_ask", {"question": "Reuse prior runtime evidence"}))
    assert script.prompts == ["Explain this API"]


@pytest.mark.parametrize("factory", [codex_consultant, claude_consultant, codex_reviewer, claude_reviewer])
async def test_constructed_read_tool_checks_scope_and_records_policy(workspace, qualified, monkeypatch, caplog, factory):
    script = install(monkeypatch)
    tool = factory(root=str(workspace.parent))
    args = {"question" if tool.name.endswith("_ask") else "task": "Explain"}
    task = context(workspace)
    caplog.set_level("INFO", logger="lazytools.connectors.code_support._policy")
    with task_scope(task):
        with pytest.raises(PolicyError, match="outside"):
            await tool.func(repo_path=str(workspace.parent), **args)
        with pytest.raises(PolicyError, match="binding changed"):
            id_arg = "thread_id" if tool.name.startswith("codex") else "session_id"
            await tool.run(repo_path=str(workspace), **{id_arg: "old-unbound-session"}, **args)
        assert (await tool.run(repo_path=str(workspace), session_name="review", **args)).endswith("done")
        assert (await tool.run(repo_path=str(workspace), session_name="review", **args)).endswith("done")
    assert script.counter == 1
    assert any(record.launch_mode == "governed" and record.policy_digest == task.policy.digest for record in caplog.records)


@pytest.mark.parametrize("engine", ["codex", "claude"])
async def test_governed_writer_uses_single_registry_and_one_shot_bound_approval(workspace, qualified, monkeypatch, engine):
    script = install(monkeypatch)
    writer = CodeWriteTools(base_dir=str(workspace), claude=True, codex=True, require_confirmation=False)
    writer.confirm_write()
    operation = "codex_write" if engine == "codex" else "claude_code_write"
    tool = next(tool for tool in writer.as_tools() if tool.name == operation)
    task = context(workspace, "edit-and-run", ("read", "edit", "shell"))
    with task_scope(context(workspace)), pytest.raises(PolicyError):
        await tool.func("Direct edit")
    with task_scope(task):
        with pytest.raises(CodeWriteBlocked, match="approval_id=") as exc:
            await tool.run(task="Edit this file", effort="high")
        ticket_id = str(exc.value).split("approval_id=")[1].split()[0]
    assert not script.prompts
    assert cli.main(["approve", ticket_id]) == 0
    queue = ApprovalQueue(_registry.registry_store())
    ticket = queue.get_ticket(ticket_id)
    assert ticket.request.action == operation and ticket.task_id == task.task_id
    assert ticket.request.payload["policy_digest"] == resolve(
        "edit-and-run", TaskGrant(workspace, {"tool_classes": ["read", "edit"]}, "off", "local"),
    ).digest
    for changed in (replace(task, actor="another-worker"), replace(task, attempt=2)):
        with task_scope(changed), pytest.raises(CodeWriteBlocked, match="does not match"):
            await tool.run(task="Edit this file", effort="high", approval_id=ticket_id)
    with task_scope(task):
        with pytest.raises(CodeWriteBlocked, match="does not match"):
            await tool.run(task="Change the prompt", effort="high", approval_id=ticket_id)
        out = await tool.run(task="Edit this file", effort="high", approval_id=ticket_id)
        assert out["result"].endswith("done")
        with pytest.raises(CodeWriteBlocked, match="already used"):
            await tool.run(task="Edit this file", effort="high", approval_id=ticket_id)
    assert script.prompts == ["Edit this file"]
    assert queue.get_ticket(ticket_id).consumed_at is not None
    assert queue.get_ticket(ticket_id).effect_outcome.succeeded
    assert _store.build_approval_queue(_store.build_store()).get_ticket(ticket_id) is None
    if engine == "claude":
        assert "Bash" not in script.kwargs["config"].claude.extra_tools
        assert script.kwargs["web"] is False


@pytest.mark.parametrize(("engine", "operation", "handle_kind"), [
    ("codex", "codex_write", "thread_id"),
    ("claude", "claude_code_write", "session_id"),
])
async def test_governed_writer_can_resume_native_session_after_exception(
    workspace, qualified, monkeypatch, engine, operation, handle_kind,
):
    script = install(monkeypatch)
    registry = SessionRegistry(workspace.parent / "sessions.json")
    writer = CodeWriteTools(base_dir=str(workspace), codex=True, session_registry=registry)
    tool = next(tool for tool in writer.as_tools() if tool.name == operation)
    task = context(workspace, "edit-and-run", ("read", "edit"))
    args = {"task": "Edit this file", "session_name": "writer"}

    with task_scope(task), pytest.raises(CodeWriteBlocked, match="approval_id=") as exc:
        await tool.run(**args)
    ticket_id = str(exc.value).split("approval_id=")[1].split()[0]
    assert cli.main(["approve", ticket_id]) == 0
    script.mode = "raise"
    with task_scope(task):
        failure = await tool.run(**args, approval_id=ticket_id)

    handle = script.last.handle
    assert isinstance(failure, str)
    assert handle and f"{handle_kind}={handle}" in failure and "engine blew up" in failure
    assert registry.resolve(engine, workspace, "writer") == handle
    ticket = ApprovalQueue(_registry.registry_store()).get_ticket(ticket_id)
    assert ticket.consumed_at is not None and ticket.effect_outcome is None

    script.mode = "ok"
    follow_up = {**args, handle_kind: handle}
    with task_scope(task), pytest.raises(CodeWriteBlocked, match="approval_id=") as exc:
        await tool.run(**follow_up)
    retry_ticket_id = str(exc.value).split("approval_id=")[1].split()[0]
    assert cli.main(["approve", retry_ticket_id]) == 0
    with task_scope(task):
        result = await tool.run(**follow_up, approval_id=retry_ticket_id)
    assert f"{handle_kind}={handle}" in result["result"]
    assert script.counter == 1


def test_credentials_and_egress_are_narrowed_at_the_native_boundary(workspace, qualified, monkeypatch):
    from types import SimpleNamespace

    from lazybridge.engines.claude_code.sdk_client import AgentSdkClient
    from lazybridge.engines.codex.policy_launch import PolicyLaunch

    monkeypatch.setenv("GH_TOKEN", "fake-publish-credential")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-key")
    monkeypatch.setenv("HTTPS_PROXY", "http://egress.invalid")
    monkeypatch.setattr(AgentSdkClient, "_sdk_options", staticmethod(lambda _: replaceable_options()))
    task = replace(
        context(workspace), codex_api_key="scoped-key", codex_credential_id=uuid4(),
        claude_api_key="scoped-claude", claude_credential_id=uuid4(),
    )
    with task_scope(task):
        codex = local_launch("codex_ask", "codex", workspace, web=True)
        claude = local_launch("claude_ask", "claude", workspace, web=True)
        assert codex.compiled["config_overrides"]["web_search"] == "disabled"
        assert not codex.compiled["config_overrides"]["sandbox_workspace_write"]["network_access"]
        assert "WebFetch" in claude.compiled["settings"]["permissions"]["deny"]
        client = native_client("codex", codex)["client"]
        launch = PolicyLaunch(codex.policy, codex.compiled, "offline-fixture")
        with launch.home(None, False, None, client._policy_api_key, client.policy_credential_id) as (_, env):
            assert env["OPENAI_API_KEY"] == "scoped-key"
            assert {"GH_TOKEN", "ANTHROPIC_API_KEY", "HTTPS_PROXY"}.isdisjoint(env)
        sdk_options = native_client("claude", claude)["client"]._sdk_options(SimpleNamespace())
        assert sdk_options.env["ANTHROPIC_API_KEY"] == "scoped-claude"
        assert all(not sdk_options.env[name] for name in ("GH_TOKEN", "OPENAI_API_KEY", "HTTPS_PROXY"))


def replaceable_options():
    from dataclasses import make_dataclass

    return make_dataclass("NativeOptions", [("env", dict)])({})


async def test_streamable_http_requires_current_bearer_for_each_call(workspace, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    monkeypatch.setenv("TEST_ROW9_BEARER", "first")
    cfg = {
        "code_task_context": {
            "task_id": "p/task", "actor": "worker",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            "folder": str(workspace), "profile": "read", "tool_classes": ["read"],
            "operations": ["code_sessions_list"], "http_token_env": "TEST_ROW9_BEARER",
        },
    }
    tool = CodeSessionTools(root=str(workspace), session_registry=SessionRegistry())
    server = build_server([tool], code_task_resolver=configured_task_resolver(cfg, http=True))
    app = http_app(server, json_response=True, allowed_hosts=["testserver"])
    headers = {"Accept": "application/json, text/event-stream", "Authorization": "Bearer first"}
    async with app.router.lifespan_context(app), AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver", headers=headers,
    ) as client:
        async def request(method, params=None):
            response = await client.post("/mcp/", json={
                "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {},
            })
            assert response.status_code == 200, response.text
            return response

        init = await request("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "offline-test", "version": "1"},
        })
        client.headers["MCP-Session-Id"] = init.headers["MCP-Session-Id"]
        client.headers["MCP-Protocol-Version"] = init.json()["result"]["protocolVersion"]
        await client.post("/mcp/", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = (await request("tools/list")).json()["result"]["tools"]
        assert [row["name"] for row in listed] == ["code_sessions_list"]
        params = {"name": "code_sessions_list", "arguments": {}}
        called = (await request("tools/call", params)).json()["result"]
        assert json.loads(called["content"][0]["text"])["total"] == 0
        monkeypatch.setenv("TEST_ROW9_BEARER", "rotated")
        assert (await request("tools/list")).json()["result"]["tools"] == []
        denied = (await request("tools/call", params)).json()["result"]
        assert denied["isError"] is True
        client.headers["Authorization"] = "Bearer rotated"
        assert (await request("tools/list")).json()["result"]["tools"]


def test_bridge_scopes_queries_and_blocks_cloud_before_dispatch(workspace, monkeypatch):
    bridge = CodeBridgeTools(root=str(workspace.parent))
    store = _store.build_store()
    jobs = _store.build_job_registry(store)
    jobs.write("matching-job", "Test", tool_name="codex", status="done")
    _store.write_meta(store, "matching-job", {
        "cwd": str(workspace), "task_id": "p/task", "task_attempt": 1, "task_executor": "worker",
    })
    calls = []

    def invoke(argv, timeout):
        calls.append(argv)
        return 0, '{"status": "done"}', ""

    monkeypatch.setattr(bridge, "_invoke", invoke)
    with task_scope(context(workspace)):
        with pytest.raises(PolicyError, match="egress off"):
            bridge.code_bridge_submit(
                "Review", profile="read", engine="codex", network="on", task_id="p/task", claim_token="token",
            )
        assert bridge.code_bridge_status("matching")["status"] == "done"
    with task_scope(context(workspace, attempt=2)), pytest.raises(PolicyError, match="does not match"):
        bridge.code_bridge_result("matching-job")
    assert calls == [["status", "matching-job", "--json"]]


def test_scoped_bridge_submit_binds_policy_and_strips_ambient_credentials(workspace, qualified, monkeypatch):
    bridge = CodeBridgeTools(root=str(workspace.parent))
    task = replace(
        context(workspace), codex_api_key="bridge-codex-key", codex_credential_id=uuid4(),
        claude_api_key="unrelated-provider-key", claude_credential_id=uuid4(),
    )
    calls = []
    launched = []

    def claim(self, principal, task_id, token, *, state):
        assert principal.actor == "worker" and principal.projects == ("p",)
        assert (task_id, token, state) == ("p/task", "claim-fence", "intent")
        return {
            "engine_family": "codex", "attempt": 1, "requested_profile": "read",
            "lease_expires_at": task.expires_at.timestamp() - 10,
        }

    def run(argv, **kwargs):
        launched.append(argv)
        calls.append(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, '{"status": "running"}', "")

    monkeypatch.setenv("GH_TOKEN", "must-not-cross-to-child")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-cross-to-child")
    monkeypatch.setattr(ProjectTaskService, "local_claim", claim)
    monkeypatch.setattr(subprocess, "run", run)
    with task_scope(task):
        result = bridge.code_bridge_submit("Inspect", profile="read", task_id="p/task", claim_token="claim-fence")
    assert result["ok"] and result["placement"]["where"] == "local"
    assert {"GH_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"}.isdisjoint(calls[0])
    assert calls[0][_task.MCP_CODEX_KEY_ENV] == "bridge-codex-key"
    assert calls[0][_task.MCP_POLICY_DIGEST_ENV] == task.policy.digest
    argv = launched[0]
    assert argv[argv.index("--launch-mode") + 1] == "governed"
    assert argv[argv.index("--network") + 1] == "off"
