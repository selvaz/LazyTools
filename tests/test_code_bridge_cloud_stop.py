from __future__ import annotations

import io
import json
import subprocess
import textwrap
import urllib.error

from code_bridge_test_helpers import write_python_executable
from lazytools.code_bridge import _cloud, _cloud_stop, _store
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
    codex.mkdir()
    (codex / "auth.json").write_text(json.dumps({"tokens": {"access_token": token}}))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    return token


def _fake_claude(monkeypatch, tmp_path):
    exe = write_python_executable(
        tmp_path,
        "claude",
        textwrap.dedent(
            """
            import json, os, sys
            with open(os.environ["CLAUDE_LOG"], "w", encoding="utf-8") as fh:
                json.dump({"argv": sys.argv[1:], "stdin": sys.stdin.read()}, fh)
            if os.environ.get("CLAUDE_LOGIN_NEEDED"):
                print("Authentication expired; run claude login. token=do-not-print-this", file=sys.stderr)
                raise SystemExit(1)
            print(json.dumps({"ok": True}))
            """
        ),
    )
    log = tmp_path / "claude.json"
    monkeypatch.setenv("CLAUDE_BIN", str(exe))
    monkeypatch.setenv("CLAUDE_LOG", str(log))
    return log


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


def test_claude_stop_uses_cli_with_closed_stdin(monkeypatch, tmp_path):
    log = _fake_claude(monkeypatch, tmp_path)
    result = _cloud_stop.stop("claude", "cse_abcdef")
    assert result["status"] == "stop requested"
    assert json.loads(log.read_text(encoding="utf-8")) == {
        "argv": [
            "-p",
            "Stop all work now. Do not make further changes, do not push, do not open PRs.",
            "--cloud",
            "cse_abcdef",
            "--output-format",
            "json",
        ],
        "stdin": "",
    }


def test_claude_stop_reports_login_needed_without_error_detail(monkeypatch, tmp_path):
    _fake_claude(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_LOGIN_NEEDED", "1")
    try:
        _cloud_stop.stop("claude", "cse_abcdef")
    except Exception as exc:
        assert str(exc) == "claude: login needed"
        assert "do-not-print-this" not in str(exc)


def test_codex_cancel_timeout_is_unconfirmed_not_failed(monkeypatch, tmp_path):
    _credentials(monkeypatch, tmp_path)
    replies = iter([
        {"current_assistant_turn": {"turn_status": "running"}},
        {},
        {"current_assistant_turn": {"turn_status": "running"}},
    ])
    clock = iter([0, 61])
    monkeypatch.setattr(_cloud_stop.time, "monotonic", lambda: next(clock))

    result = _cloud_stop.stop(
        "codex", "task_abc", opener=lambda request, timeout: Response(next(replies)), sleep=lambda _: None
    )
    assert result["status"] == "stop requested, not confirmed"


def test_credential_paths_only_names_codex(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert _cloud_stop.credential_paths() == {"codex": tmp_path / "auth.json"}


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


def test_codex_running_uses_shared_binary_resolver(monkeypatch):
    calls = []
    monkeypatch.setattr(_cloud, "_codex_argv", lambda: ["node", "resolved-codex.js"])

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, '[{"id": "task_abc", "status": "running"}]', "")

    monkeypatch.setattr(_cloud_stop.subprocess, "run", run)

    assert _cloud_stop.codex_running() == ["task_abc"]
    assert calls[0][0] == ["node", "resolved-codex.js", "cloud", "list", "--json"]


def test_codex_running_follows_cursor_to_second_page(monkeypatch):
    calls = []
    pages = iter(
        [
            {"tasks": [{"id": "task_first", "status": "running"}], "cursor": "page-two"},
            {
                "tasks": [
                    {"id": "task_first", "status": "running"},
                    {"id": "task_second", "status": "running"},
                ]
            },
        ]
    )
    monkeypatch.setattr(_cloud, "_codex_argv", lambda: ["codex-resolved"])

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(next(pages)), "")

    monkeypatch.setattr(_cloud_stop.subprocess, "run", run)

    assert _cloud_stop.codex_running() == ["task_first", "task_second"]
    assert calls == [
        ["codex-resolved", "cloud", "list", "--json"],
        ["codex-resolved", "cloud", "list", "--json", "--cursor", "page-two"],
    ]


def test_unconfirmed_stop_keeps_bridge_job_active(monkeypatch, tmp_path, capsys):
    db = tmp_path / "store.sqlite"
    store = _store.build_store(db)
    registry = _store.build_job_registry(store)
    _cloud._write(
        store,
        registry,
        "job-active",
        {"engine": "codex", "cloud_handle": "task_abc", "objective": "test"},
        status="running",
    )
    monkeypatch.setattr(
        _cloud_stop,
        "stop",
        lambda *args: {
            "provider": "codex",
            "handle": "task_abc",
            "status": "stop requested, not confirmed",
        },
    )

    code = main(["cloud-stop", "job-active", "--json", "--db", str(db)])

    assert code == 0
    assert json.loads(capsys.readouterr().out)[0]["status"] == "stop requested, not confirmed"
    assert [job["job_id"] for job in _cloud.active_jobs(store)] == ["job-active"]
