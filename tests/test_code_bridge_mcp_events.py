"""Approval tickets sent to MCP clients as notifications (SPEC R4.2), on either SDK generation."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

pytest.importorskip("mcp")

import anyio
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from lazytools.code_bridge import _store
from lazytools.mcp_server import __main__ as entrypoint
from lazytools.mcp_server import _approval_notifications
from lazytools.mcp_server.providers import PROVIDER_FACTORIES
from lazytools.mcp_server.server import build_server


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A temporary job store with one running job, polled quickly."""
    db = tmp_path / "jobs.sqlite"
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_DB", str(db))
    monkeypatch.setenv("LAZYTOOLS_PROJECTS_STORE_DB", str(tmp_path / "projects.sqlite"))
    monkeypatch.setattr(_approval_notifications, "POLL_INTERVAL", 0.01)
    store = _store.build_store(db)
    _store.build_job_registry(store).write("running-job", "task", tool_name="codex", status="running")
    _store.write_meta(store, "running-job", {"job_id": "running-job", "engine": "codex", "where": "local", "cwd": "."})
    return store


def _server(*, read_only: bool = False, **kwargs: Any) -> Any:
    fleet = PROVIDER_FACTORIES["fleet"](not read_only)
    return build_server([fleet], read_only=read_only, notify_approvals=True, **kwargs)


def _count_reads(monkeypatch) -> list[str]:
    """Record the cursor of every read of the event source."""
    from lazytools.code_bridge import _events

    reads: list[str] = []
    poll = _events.poll

    def counted(cursor: str = "", **kwargs: Any) -> Any:
        reads.append(cursor)
        return poll(cursor, **kwargs)

    monkeypatch.setattr(_events, "poll", counted)
    return reads


@asynccontextmanager
async def _session(server: Any, received: list[Any]):
    """A handshake client over memory streams; leaving it closes the transport, as a client exit does."""

    async def on_log(params: Any) -> None:
        received.append(params)

    with anyio.fail_after(30):  # a loop that outlives its session would keep ``server.run`` from returning
        async with (
            create_client_server_memory_streams() as (client_streams, server_streams),
            anyio.create_task_group() as tasks,
        ):
            tasks.start_soon(server.run, *server_streams, server.create_initialization_options())
            async with ClientSession(*client_streams, logging_callback=on_log) as client:
                yield client, await client.initialize()
            await client_streams[1].aclose()


async def _until(condition) -> None:
    with anyio.fail_after(20):
        while not condition():
            await anyio.sleep(0.01)


async def _more_reads(reads: list[str]) -> None:
    target = len(reads) + 3
    await _until(lambda: len(reads) >= target)


def _ticket(store: Any) -> str:
    queue = _store.build_approval_queue(store)
    return queue.create_ticket(task_id="running-job", prompt="Allow git push?").approval_id


async def test_new_ticket_is_sent_once_and_the_loop_stops_with_the_session(store, monkeypatch):
    reads = _count_reads(monkeypatch)
    received: list[Any] = []
    async with _session(_server(), received) as (_client, initialized):
        assert initialized.capabilities.logging is not None
        await _until(lambda: len(reads) >= 2)  # the stream has started "from now"
        assert received == []

        ticket = _ticket(store)
        await _until(lambda: received)
        await _more_reads(reads)  # the ticket stays pending and must not be sent again

        assert len(received) == 1
        assert received[0].level == "info"
        data = received[0].data
        assert (data["kind"], data["job_id"], data["approval"]["approval_id"]) == ("approval", "running-job", ticket)

    stopped = len(reads)
    await anyio.sleep(0.1)
    assert len(reads) == stopped


async def test_only_approval_events_are_sent(store, monkeypatch):
    reads = _count_reads(monkeypatch)
    received: list[Any] = []
    async with _session(_server(), received):
        await _until(lambda: reads)
        _store.build_job_registry(store).write("quick-job", "task", tool_name="codex", status="done")
        _store.write_meta(store, "quick-job", {"job_id": "quick-job", "engine": "codex", "where": "local", "cwd": "."})
        await _more_reads(reads)
        assert received == []


async def test_repeated_initialized_does_not_start_a_second_loop(store, monkeypatch):
    from mcp import types

    reads = _count_reads(monkeypatch)
    received: list[Any] = []
    async with _session(_server(), received) as (client, _initialized):
        await _until(lambda: reads)
        notification: Any = types.InitializedNotification(method="notifications/initialized")
        if hasattr(types.ClientNotification, "model_validate"):  # SDK 1 wraps it in a root model
            notification = types.ClientNotification(notification)
        await client.send_notification(notification)
        await _more_reads(reads)
        _ticket(store)
        await _until(lambda: received)
        await _more_reads(reads)
        assert len(received) == 1
        assert reads.count("") == 1


@pytest.mark.parametrize("kwargs", [
    {"read_only": True},
    {"code_task_resolver": lambda _request: None},
])
async def test_no_loop_when_read_only_or_task_scoped(store, monkeypatch, kwargs):
    reads = _count_reads(monkeypatch)
    received: list[Any] = []
    _ticket(store)
    async with _session(_server(**kwargs), received) as (client, initialized):
        await client.send_ping()
        await anyio.sleep(0.2)
        assert initialized.capabilities.logging is None
    assert reads == [] and received == []


@pytest.mark.parametrize(("main", "argv", "expected"), [
    ("orchestration_main", ["fleet", "--allow-unsafe"], True),
    ("orchestration_main", ["fleet"], False),
    ("main", ["fleet", "--allow-unsafe"], False),
])
def test_only_the_writable_orchestration_server_notifies(store, monkeypatch, main, argv, expected):
    for name in ("LAZYTOOLS_MCP_PROVIDERS", "LAZYTOOLS_MCP_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    registered: list[Any] = []
    monkeypatch.setattr(_approval_notifications, "register", lambda *args, **kwargs: registered.append(args))

    async def serve(_server: Any) -> None:
        return None

    monkeypatch.setattr(entrypoint, "serve_stdio", serve)
    getattr(entrypoint, main)(argv)
    assert bool(registered) is expected
