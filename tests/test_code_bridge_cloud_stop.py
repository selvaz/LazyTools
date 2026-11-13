from __future__ import annotations

import io
import json
import urllib.error

from lazytools.code_bridge import _cloud_stop
from lazytools.code_bridge.cli import main


class Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return self.payload


def _credentials(monkeypatch, tmp_path, token="super-secret-token-value"):
    codex = tmp_path / "codex"
    claude = tmp_path / "claude"
    codex.mkdir(); claude.mkdir()
    (codex / "auth.json").write_text(json.dumps({"tokens": {"access_token": token}}))
    (claude / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": token}}))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    return token


def test_codex_cancel_url_method_and_poll(monkeypatch, tmp_path):
    token = _credentials(monkeypatch, tmp_path)
    calls = []
    replies = iter([{"current_assistant_turn": {"turn_status": "running"}}, {},
                    {"current_assistant_turn": {"turn_status": "failed"}}])

    def opener(request, timeout):
        calls.append((request.method, request.full_url, request.headers))
        return Response(next(replies))

    result = _cloud_stop.stop("codex", "task_abc", opener=opener, sleep=lambda _: None)
    assert result["status"] == "stopped"
    assert calls[1][:2] == ("POST", "https://chatgpt.com/backend-api/wham/tasks/task_abc/cancel")
    assert calls[1][2]["Authorization"] == f"Bearer {token}"


def test_already_finished_does_not_cancel(monkeypatch, tmp_path):
    _credentials(monkeypatch, tmp_path)
    calls = []
    def opener(request, timeout):
        calls.append(request.method)
        return Response({"current_assistant_turn": {"turn_status": "completed"}})
    assert _cloud_stop.stop("codex", "task_done", opener=opener)["status"] == "already finished"
    assert calls == ["GET"]


def test_claude_archive_is_reported_honestly(monkeypatch, tmp_path):
    _credentials(monkeypatch, tmp_path)
    calls = []
    def opener(request, timeout):
        calls.append((request.method, request.full_url))
        return Response({})
    result = _cloud_stop.stop("claude", "cse_abcdef", opener=opener)
    assert result["status"] == "archive requested"
    assert calls == [("POST", "https://api.anthropic.com/v1/code/sessions/cse_abcdef/archive")]


def test_missing_or_expired_token_says_login_needed_without_leaking(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))
    try:
        _cloud_stop.stop("codex", "task_abc")
    except Exception as exc:
        assert str(exc) == "codex: login needed"
    token = _credentials(monkeypatch, tmp_path, "secret-that-must-never-appear")
    def expired(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, token, {}, io.BytesIO(token.encode()))
    try:
        _cloud_stop.stop("codex", "task_abc", opener=expired)
    except Exception as exc:
        assert str(exc) == "login needed"
        assert token not in str(exc)


def test_cli_dry_run_json_never_calls_stop(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(_cloud_stop, "stop", lambda *a, **k: (_ for _ in ()).throw(AssertionError("HTTP called")))
    code = main(["cloud-stop", "https://chatgpt.com/codex/tasks/task_abc", "--dry-run", "--json",
                 "--db", str(tmp_path / "store.sqlite")])
    output = capsys.readouterr()
    assert code == 0 and not output.err
    assert json.loads(output.out) == [{"handle": "task_abc", "provider": "codex", "status": "would stop"}]


def test_all_running_dry_run_uses_codex_account_listing(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(_cloud_stop, "codex_running", lambda: ["task_external"])
    monkeypatch.setattr(_cloud_stop, "stop", lambda *a, **k: (_ for _ in ()).throw(AssertionError("HTTP called")))
    code = main(["cloud-stop", "--all-running", "--dry-run", "--json",
                 "--db", str(tmp_path / "store.sqlite")])
    assert code == 0
    assert json.loads(capsys.readouterr().out) == [
        {"handle": "task_external", "provider": "codex", "status": "would stop"}
    ]
