from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lazytools.mcp_server.providers import PROVIDER_FACTORIES
from lazytools.mcp_server.studio import StudioTools


class _StudioHandler(BaseHTTPRequestHandler):
    token = "operator-secret"

    def log_message(self, format, *args):
        pass

    def _send(self, payload):
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        assert self.headers["Authorization"] == f"Bearer {self.token}"
        if self.path == "/api/mcp-tools":
            self._send(
                [
                    {
                        "name": "studio_echo_demo",
                        "description": "Echo through Studio.",
                        "input_schema": {"type": "object"},
                    }
                ]
            )
        else:
            self._send({"status": "succeeded", "output": "finished", "run_id": "run-1"})

    def do_POST(self):
        assert self.headers["Authorization"] == f"Bearer {self.token}"
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert body == {"task": "hello"}
        if self.path == "/api/mcp-tools/studio_echo_demo/call":
            self._send({"status": "succeeded", "output": "echo: hello", "run_id": "run-1"})


@pytest.fixture
def studio(monkeypatch, tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StudioHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    token_file = tmp_path / "operator-token"
    token_file.write_text(_StudioHandler.token)
    monkeypatch.setenv("LAZYSTUDIO_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("LAZYSTUDIO_TOKEN_FILE", str(token_file))
    yield server
    server.shutdown()
    thread.join()


def test_lists_and_calls_published_tool(studio):
    provider = StudioTools(allow_run=True)
    tools = {tool.name: tool for tool in provider.as_tools()}
    assert set(tools) == {"studio_echo_demo", "studio_run_status"}
    assert tools["studio_echo_demo"].definition().description == "Echo through Studio."
    assert tools["studio_echo_demo"].run_sync(task="hello") == "echo: hello"
    assert tools["studio_run_status"].run_sync(run_id="run-1") == "finished"


def test_running_result(studio, monkeypatch):
    provider = StudioTools(allow_run=True)
    monkeypatch.setattr(
        provider,
        "_request",
        lambda *args, **kwargs: {"status": "running", "run_id": "run-2"},
    )
    tool = next(tool for tool in provider.as_tools() if tool.name == "studio_echo_demo")
    assert tool.run_sync(task="hello") == "still running, run_id=run-2"


def test_missing_token_exposes_status_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("LAZYSTUDIO_TOKEN_FILE", str(tmp_path / "missing"))
    provider = StudioTools(allow_run=True)
    tools = {tool.name: tool for tool in provider.as_tools()}
    assert set(tools) == {"studio_run_status", "studio_status"}
    assert "operator token not found" in tools["studio_status"].run_sync()


def test_studio_down_exposes_status_tools(monkeypatch, tmp_path):
    token_file = tmp_path / "operator-token"
    token_file.write_text("do-not-print-this")
    monkeypatch.setenv("LAZYSTUDIO_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("LAZYSTUDIO_URL", "http://127.0.0.1:1")
    provider = StudioTools(allow_run=True)
    tools = {tool.name: tool for tool in provider.as_tools()}
    assert set(tools) == {"studio_run_status", "studio_status"}
    assert "do-not-print-this" not in tools["studio_status"].run_sync()


def test_runs_are_unsafe_gated(studio):
    provider = StudioTools(allow_run=False)
    assert [tool.name for tool in provider.as_tools()] == ["studio_run_status"]


def test_studio_provider_is_registered():
    assert "studio" in PROVIDER_FACTORIES
