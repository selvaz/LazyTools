"""Real discovery/probe/audit code with fake App Server streams and Claude runner."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections import deque
from dataclasses import replace
from pathlib import Path

import pytest

from lazytools.code_bridge import _models, _probe_process, cli
from lazytools.routing.catalogue import DEFAULT_PATH, load_tiers


def codex_data():
    return [
        {
            "id": model, "isDefault": model == "gpt-6.1-sol", "hidden": False,
            "supportedReasoningEfforts": [
                {"reasoningEffort": effort}
                for effort in (("low", "medium", "high", "xhigh", "max") if model.endswith("-luna") else ("low", "medium", "high", "xhigh", "max", "ultra"))
            ],
            "defaultReasoningEffort": "medium",
        }
        for model in ("gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-6-luna", "gpt-5.6-luna")
    ]


class FakeServer:
    def __init__(self, lines, *, hang=False):
        self.lines = deque(line if isinstance(line, bytes) else (json.dumps(line) + "\n").encode() for line in lines)
        self.messages = []
        self.hang = hang
        self.stdin = self.stdout = self
        self.killed = self.waited = False

    def write(self, data):
        self.messages.append(json.loads(data))

    async def drain(self):
        pass

    async def readline(self):
        if self.lines:
            return self.lines.popleft()
        if self.hang:
            await asyncio.Event().wait()
        return b""

    def kill(self):
        self.killed = True

    async def wait(self):
        self.waited = True
        return 0


def install_server(monkeypatch, lines, *, hang=False):
    server = FakeServer(lines, hang=hang)

    async def spawn(*argv, **kwargs):
        assert argv == ("fake-codex", "app-server")
        assert kwargs["stdin"] == asyncio.subprocess.PIPE
        assert kwargs["stderr"] == asyncio.subprocess.DEVNULL
        return server

    monkeypatch.setattr(_models.asyncio, "create_subprocess_exec", spawn)
    return server


@pytest.mark.asyncio
async def test_app_server_handshake_reads_models_without_thread_or_turn(monkeypatch):
    server = install_server(monkeypatch, [
        b"not JSON\n", [], {"method": "notification"}, {"id": 1, "result": {}},
        {"id": 2, "result": {"data": codex_data(), "nextCursor": None}},
    ])
    models = await _models.fetch_codex_models(executable="fake-codex")
    assert len(models) == 7 and models[0].is_default is True
    assert models[0].default_effort == "medium"
    assert "ultra" not in models[-1].efforts and "ultra" in models[0].efforts
    assert [message["method"] for message in server.messages] == ["initialize", "initialized", "model/list"]
    assert server.messages[0]["params"]["capabilities"] == {"experimentalApi": True}
    assert server.messages[1] == {"method": "initialized", "params": {}}
    assert server.messages[2] == {"method": "model/list", "id": 2, "params": {}}
    assert server.killed and server.waited


@pytest.mark.asyncio
async def test_app_server_follows_model_pages_and_keeps_hidden_metadata(monkeypatch):
    data = codex_data()
    data[1]["hidden"] = True
    server = install_server(monkeypatch, [
        {"id": 1, "result": {}},
        {"id": 2, "result": {"data": data[:2], "nextCursor": "next"}},
        {"id": 3, "result": {"data": data[2:], "nextCursor": None}},
    ])
    models = await _models.fetch_codex_models(executable="fake-codex")
    assert len(models) == 7 and models[1].hidden is True
    assert server.messages[-1] == {"method": "model/list", "id": 3, "params": {"cursor": "next"}}


@pytest.mark.parametrize("lines,match", [
    ([{"id": 1, "error": {"message": "initialize refused"}}], "initialize refused"),
    ([{"id": 1, "result": {}}, {"id": 2, "error": {"message": "method not found"}}], "method not found"),
    ([{"id": 1, "result": {}}, {"id": 2, "result": None}], "no result"),
    ([{"id": 1, "result": {}}, {"id": 2, "result": {}}], "no data"),
    ([{"id": 1, "result": {}}, {"id": 2, "result": {"data": []}}], "no models"),
    ([{"id": 1, "result": {}}, {"id": 2, "result": {"data": [{}]}}], "without an id"),
    ([{"id": 1, "result": {}}, {"id": 2, "result": {"data": [{"id": "broken"}]}}], "no usable efforts"),
    ([{"id": 1, "result": {}}, {"id": 2, "result": {"data": [codex_data()[0]] * 2}}], "duplicate model"),
    ([{"id": 1, "result": {}}], "closed the stream"),
])
@pytest.mark.asyncio
async def test_app_server_refusals_and_bad_payloads_reap_child(monkeypatch, lines, match):
    server = install_server(monkeypatch, lines)
    with pytest.raises(RuntimeError, match=match):
        await _models.fetch_codex_models(executable="fake-codex")
    assert server.killed and server.waited


@pytest.mark.asyncio
async def test_app_server_timeout_reaps_child(monkeypatch):
    server = install_server(monkeypatch, [], hang=True)
    with pytest.raises(RuntimeError, match=r"within 0\.01s"):
        await _models.fetch_codex_models(executable="fake-codex", timeout=0.01)
    assert server.killed and server.waited


@pytest.fixture
def discovery(monkeypatch):
    models = _models._parse_codex({"data": codex_data()})
    calls = []

    async def fetch():
        return models

    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[:3] == ["fake-claude", "-p", "Reply with just: ok"]
        assert argv[argv.index("--max-turns") + 1] == "1"
        assert argv[argv.index("--output-format") + 1] == "json"
        assert argv[argv.index("--tools") + 1] == ""
        assert "--no-session-persistence" in argv
        assert "--strict-mcp-config" in argv and "--safe-mode" in argv
        assert argv[argv.index("--setting-sources") + 1] == ""
        assert kwargs["timeout"] == 60
        assert list(Path(kwargs["cwd"]).iterdir()) == []
        model = argv[argv.index("--model") + 1]
        resolved = {"sonnet": "claude-sonnet-5", "opus": "claude-opus-5-5"}.get(model, model)
        warning = f'[claude-code:unrecognized_model] {{"model":"{model}"}}' if model == "claude-sonnet-5-5" else ""
        return subprocess.CompletedProcess(argv, 0, json.dumps({"result": "ok", "is_error": False, "modelUsage": {resolved: {}}}), warning)

    monkeypatch.setattr(_models, "fetch_codex_models", fetch)
    monkeypatch.setattr(_models, "_claude_executable", lambda: "fake-claude")
    monkeypatch.setattr(_models, "_run_claude", run)
    monkeypatch.setattr(_models, "load_default_tiers", lambda **kwargs: load_tiers(DEFAULT_PATH, **kwargs))
    return models, calls


def test_models_without_opt_in_spends_no_claude_quota(discovery, capsys):
    assert cli.main(["models"]) == 0
    out = capsys.readouterr().out
    assert "Claude not probed" in out and "no model-list endpoint" in out
    assert "gpt-6.1-sol" in out and "Audit: 0 mismatch(es), 0 error(s)." in out
    assert discovery[1] == []


def test_models_probes_unique_catalogue_models_and_aliases_and_retains_warning(discovery, capsys):
    assert cli.main(["models", "--probe-claude", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mismatches"] == [] and payload["errors"] == []
    assert payload["probe_claude"] is True
    rows = {row["model"]: row for row in payload["models"]}
    assert len(rows) == 11
    assert rows["sonnet"]["answered_models"] == ["claude-sonnet-5"]
    assert rows["opus"]["answered_models"] == ["claude-opus-5-5"]
    assert rows["claude-sonnet-5-5"]["available"] is True
    assert "unrecognized_model" in rows["claude-sonnet-5-5"]["warnings"][0]
    assert rows["claude-sonnet-5-5"]["efforts"] is None
    assert rows["gpt-6.1-sol"]["is_default"] is True
    assert rows["gpt-6.1-sol"]["default_effort"] == "medium"
    assert "ultra" not in rows["gpt-6-luna"]["efforts"]
    assert [call[call.index("--model") + 1] for call in discovery[1]] == ["claude-sonnet-5-5", "claude-opus-5-5", "sonnet", "opus"]


def test_models_human_table_shows_alias_resolution_and_warning(discovery, capsys):
    assert cli.main(["models", "--probe-claude"]) == 0
    out = capsys.readouterr().out
    assert "consumes a small amount of quota" in out
    assert "answered model" in out and "default" in out and "efforts" in out
    assert "claude-sonnet-5" in out and "unrecognized_model" in out
    assert "0 mismatch(es)" in out


def test_audit_reports_every_catalogue_and_policy_mismatch(discovery, tmp_path, capsys):
    models, _ = discovery
    models[:] = [model for model in models if model.model != "gpt-5.6-terra"]
    models[0] = replace(models[0], efforts=("low", "medium", "high", "max", "ultra", "new-effort"))
    models.append(_models.ModelInfo("codex", "gpt-new", True, ("high",)))
    path = tmp_path / "tiers.toml"
    text = DEFAULT_PATH.read_text().replace("gpt-6.1-sol", "gpt-6-luna").replace('effort = "medium"', 'effort = "ultra"')
    text = text.replace("gpt-6-astra", "gpt-missing")
    path.write_text(text)
    assert cli.main(["models", "--tiers", str(path), "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    mismatches = payload["mismatches"]
    assert any("gpt-new" in item and "missing from the policy" in item for item in mismatches)
    assert any("gpt-5.6-terra" in item and "not offered" in item for item in mismatches)
    assert any("gpt-6.1-sol" in item and "xhigh" in item and "not supported" in item for item in mismatches)
    assert any("new-effort" in item and "missing from the policy" in item for item in mismatches)
    assert any("basic.steps[0]" in item and "ultra" in item and "gpt-6-luna" in item for item in mismatches)
    assert sum("model 'gpt-missing' is not offered" in item for item in mismatches) == 3
    assert any("default policy rejects effort 'ultra'" in item for item in mismatches)
    assert next(row for row in payload["models"] if row["model"] == "gpt-missing")["available"] is False


def test_default_policy_luna_effort_drift_is_reported(discovery, capsys):
    models, _ = discovery
    for index, model in enumerate(models):
        if model.model == "gpt-6-luna":
            models[index] = replace(model, efforts=(*model.efforts, "ultra"))
    assert cli.main(["models", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["mismatches"] == ["policy: Codex model 'gpt-6-luna' offers effort 'ultra', missing from the policy"]


def test_claude_unavailable_and_wrong_concrete_answer_are_mismatches(discovery, monkeypatch, capsys):
    def run(argv, **kwargs):
        model = argv[argv.index("--model") + 1]
        if model == "claude-sonnet-5-5":
            return subprocess.CompletedProcess(argv, 1, json.dumps({"is_error": True, "result": "model unavailable"}), "")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"modelUsage": {"claude-opus-old": {}}}), "")

    monkeypatch.setattr(_models, "_run_claude", run)
    assert cli.main(["models", "--probe-claude", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert any("claude-sonnet-5-5' is unavailable" in item for item in payload["mismatches"])
    assert any("claude-opus-5-5' answered as claude-opus-old" in item for item in payload["mismatches"])
    assert "model unavailable" in payload["errors"][0]


@pytest.mark.parametrize("problem", ["exit", "invalid_json", "no_usage", "timeout", "missing_cli"])
def test_failed_claude_probe_never_claims_availability(tmp_path, monkeypatch, problem):
    monkeypatch.setattr(_models, "_claude_executable", lambda: "fake")

    def run(argv, **kwargs):
        if problem == "timeout":
            raise subprocess.TimeoutExpired(argv, 60)
        if problem == "missing_cli":
            raise FileNotFoundError("missing executable")
        stdout = "noise" if problem == "invalid_json" else json.dumps({"result": "failure", "modelUsage": {} if problem == "no_usage" else {"sonnet": {}}})
        return subprocess.CompletedProcess(argv, 1 if problem == "exit" else 0, stdout, "")

    monkeypatch.setattr(_models, "_run_claude", run)
    info = _models.probe_claude_model("sonnet", cwd=tmp_path)
    assert info.available is False and info.error


def test_unreadable_codex_is_an_error_not_a_list_of_missing_models(discovery, monkeypatch, capsys):
    async def fetch():
        raise RuntimeError("offline")

    monkeypatch.setattr(_models, "fetch_codex_models", fetch)
    assert cli.main(["models", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["mismatches"] == []
    assert payload["errors"] == ["Codex discovery failed: offline"]
    assert all(row["available"] is None for row in payload["models"])


def test_bad_catalogue_reports_json_error_before_discovery(discovery, tmp_path, capsys):
    path = tmp_path / "tiers.toml"
    path.write_text("broken")
    assert cli.main(["models", "--tiers", str(path), "--probe-claude", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert "not valid TOML" in payload["errors"][0]
    assert set(payload) == {"models", "probe_claude", "mismatches", "errors"}
    assert payload["probe_claude"] is True
    assert discovery[1] == []


def test_models_help_explains_opt_in_cost(capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["models", "--help"])
    assert caught.value.code == 0
    out = capsys.readouterr().out
    assert "Opt in" in out and "quota" in out and "--probe-claude" in out


class FakeProbe:
    def __init__(self, *, hung=False):
        self.pid = 4321
        self.returncode = None
        self.hung = hung
        self.communications = []
        self.waits = []
        self.killed = False

    def communicate(self, *, timeout):
        self.communications.append(timeout)
        if self.hung:
            raise subprocess.TimeoutExpired("fake probe", timeout)
        self.returncode = 0
        return '{"modelUsage":{"sonnet":{}}}', "diagnostic"

    def kill(self):
        self.killed = True

    def wait(self, *, timeout):
        self.waits.append(timeout)
        if self.hung:
            raise subprocess.TimeoutExpired("fake probe", timeout)
        return 0


class FakeProbeJob:
    def __init__(self):
        self.attached = None
        self.closed = False
        self.descendant_alive = True

    def attach_and_resume(self, pid):
        self.attached = pid

    def close(self):
        self.closed = True
        self.descendant_alive = False


def test_probe_runner_uses_pipes_and_a_deadline(tmp_path, monkeypatch):
    child = FakeProbe()
    job = FakeProbeJob()
    monkeypatch.setattr(_probe_process, "WindowsProbeJob", lambda: job)
    seen = {}

    def popen(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        return child

    monkeypatch.setattr(_models.subprocess, "Popen", popen)
    result = _models._run_claude(["fake"], cwd=tmp_path, timeout=12)
    assert result.returncode == 0 and result.stderr == "diagnostic"
    assert child.communications == [12]
    assert seen["stdin"] == subprocess.DEVNULL
    assert seen["stdout"] == seen["stderr"] == subprocess.PIPE
    assert seen["start_new_session"] == (not _models._PROBE_WINDOWS)


@pytest.mark.parametrize("root_exited", [False, True])
def test_windows_probe_timeout_owns_descendants_even_after_root_exit(tmp_path, monkeypatch, root_exited):
    child = FakeProbe(hung=True)
    child.returncode = 0 if root_exited else None
    job = FakeProbeJob()
    calls = []

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return child

    monkeypatch.setattr(_models, "_PROBE_WINDOWS", True)
    monkeypatch.setattr(_probe_process, "WindowsProbeJob", lambda: job)
    monkeypatch.setattr(_models.subprocess, "Popen", popen)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        _models._run_claude(["fake"], cwd=tmp_path, timeout=0.01)
    assert caught.value.timeout == 0.01
    assert child.killed
    assert len(calls) == 1  # Cleanup never depends on taskkill finding an exited root.
    assert calls[0][1]["creationflags"] & 0x00000004  # CREATE_SUSPENDED
    assert job.attached == 4321 and job.closed and not job.descendant_alive
    assert child.communications == [0.01, _models._PROBE_CLEANUP_SECONDS]
    assert child.waits == [_models._PROBE_CLEANUP_SECONDS]


def test_posix_timeout_kills_the_new_process_group(tmp_path, monkeypatch):
    child = FakeProbe(hung=True)
    groups = []
    monkeypatch.setattr(_models, "_PROBE_WINDOWS", False)
    monkeypatch.setattr(_models.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(_models.subprocess, "Popen", lambda *a, **kw: child)
    monkeypatch.setattr(_models.os, "killpg", lambda *args: groups.append(args), raising=False)
    with pytest.raises(subprocess.TimeoutExpired):
        _models._run_claude(["fake"], cwd=tmp_path, timeout=1)
    assert groups == [(4321, _models.signal.SIGKILL)] and child.killed
    assert all(timeout is not None for timeout in child.communications)
