from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lazytools.mcp_server.providers import PROVIDER_FACTORIES
from lazytools.mcp_server.studio import StudioTools


class _StudioHandler(BaseHTTPRequestHandler):
    token = "operator-secret"
    requests = []
    caller_headers: list = []

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
        self.requests.append(("GET", self.path, self.headers["Authorization"], None))
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
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append(("POST", self.path, self.headers["Authorization"], body))
        self.caller_headers.append(self.headers.get("X-LazyStudio-Caller-Execution"))
        if self.path == "/api/mcp-tools/studio_echo_demo/call":
            if body["task"] == "running":
                self._send({"status": "running", "run_id": "run-2"})
            else:
                self._send({"status": "succeeded", "output": "echo: hello", "run_id": "run-1"})


@pytest.fixture
def studio(monkeypatch, tmp_path):
    _StudioHandler.requests = []
    _StudioHandler.caller_headers = []
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
    assert all(request[2] == f"Bearer {_StudioHandler.token}" for request in _StudioHandler.requests)
    assert _StudioHandler.requests[1][3] == {"task": "hello", "wait_seconds": 60}


def test_running_result(studio):
    provider = StudioTools(allow_run=True)
    tool = next(tool for tool in provider.as_tools() if tool.name == "studio_echo_demo")
    assert tool.run_sync(task="running") == "still running, run_id=run-2"
    assert _StudioHandler.requests[-1][3] == {"task": "running", "wait_seconds": 60}


def test_missing_token_exposes_status_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("LAZYSTUDIO_TOKEN_FILE", str(tmp_path / "missing"))
    provider = StudioTools(allow_run=True)
    tools = {tool.name: tool for tool in provider.as_tools()}
    assert set(tools) == {"studio_run_status", "studio_status"}
    assert "operator token not found" in tools["studio_status"].run_sync()
    assert "operator token not found" in tools["studio_run_status"].run_sync(run_id="run-1")


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


@pytest.mark.parametrize("code", [400, 404, 409])
def test_http_errors_include_server_message(monkeypatch, tmp_path, code):
    from io import BytesIO
    from urllib.error import HTTPError

    token_file = tmp_path / "token"
    token_file.write_text("secret")
    monkeypatch.setenv("LAZYSTUDIO_TOKEN_FILE", str(token_file))

    def fail(*args, **kwargs):
        payload = BytesIO(json.dumps({"message": "server says no"}).encode())
        raise HTTPError("http://127.0.0.1", code, "error", {}, payload)

    monkeypatch.setattr("lazytools.mcp_server.studio.urlopen", fail)
    provider = StudioTools()
    assert "server says no" in provider.problem
    assert f"HTTP {code}" in provider.problem


def test_timeout_is_clear_and_uses_wait_timeout(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("secret")
    monkeypatch.setenv("LAZYSTUDIO_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("LAZYSTUDIO_WAIT_SECONDS", "6000")
    observed = []

    def timeout(request, timeout):
        observed.append(timeout)
        raise TimeoutError()

    monkeypatch.setattr("lazytools.mcp_server.studio.urlopen", timeout)
    provider = StudioTools()
    assert observed == [615]
    assert "timed out after 615 seconds" in provider.problem


def test_malformed_result_and_failed_result():
    with pytest.raises(ValueError, match="invalid result"):
        StudioTools._result({"status": "succeeded", "run_id": "run-1"})
    assert StudioTools._result({"status": "failed", "error": "boom", "run_id": "run-2"}) == (
        "failed: boom (run_id=run-2)"
    )


def test_remote_url_does_not_receive_token(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("do-not-send")
    monkeypatch.setenv("LAZYSTUDIO_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("LAZYSTUDIO_URL", "https://studio.example.com")
    provider = StudioTools()
    assert "refusing to send" in provider.problem


def test_invalid_reserved_and_duplicate_names_are_skipped(studio, caplog):
    provider = StudioTools(allow_run=True)
    provider.published.extend(
        [
            {"name": "studio_status"},
            {"name": "Bad name"},
            {"name": "studio_echo_demo"},
        ]
    )
    assert [tool.name for tool in provider.as_tools()] == ["studio_run_status", "studio_echo_demo"]
    assert "Skipping invalid, reserved, or duplicate" in caplog.text


def test_call_sends_caller_execution_header_only_when_env_is_set(studio, monkeypatch):
    monkeypatch.delenv("LAZYSTUDIO_CALLER_EXECUTION", raising=False)
    tool = {t.name: t for t in StudioTools(allow_run=True).as_tools()}["studio_echo_demo"]
    tool.run_sync(task="hello")
    monkeypatch.setenv("LAZYSTUDIO_CALLER_EXECUTION", "abc123DEF-_")
    tool.run_sync(task="hello")
    monkeypatch.setenv("LAZYSTUDIO_CALLER_EXECUTION", "bad value\r\nX-Injected: 1")
    tool.run_sync(task="hello")
    assert _StudioHandler.caller_headers == [None, "abc123DEF-_", None]
