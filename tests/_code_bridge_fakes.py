"""Shared fakes for ``lazytools.code_bridge`` tests.

No real Codex / Claude Code process is ever started. Engines are replaced at
the ``_make_codex_engine`` / ``_make_claude_engine`` seams in
``lazytools.code_bridge._engines``, and ``lazybridge.Agent`` by a scripted
stand-in -- the same technique ``tests/_code_fakes.py`` uses for
``lazytools.connectors.code_support``.

When ``Script.request_approval`` is set, the fake agent's ``run()`` actually
calls the engine's configured ``approval_gate`` with one synthesized
``ApprovalRequest`` before finishing the turn -- this is what lets the
approval-ticket tests (filed -> approve -> job continues; reject -> job
fails; TTL expiry -> job fails) exercise the REAL ``TieredGate`` /
``StoreApprovalChannel`` / ``ApprovalQueue`` stack end to end, with only the
underlying engine faked out.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_SEAMS = (
    ("lazytools.code_bridge._engines", "_make_codex_engine", "codex"),
    ("lazytools.code_bridge._engines", "_make_claude_engine", "claude"),
)


class Script:
    def __init__(self) -> None:
        self.mode = "ok"  # "ok" | "error" | "raise"
        self.text = "done"
        self.reported_model: str | None = None
        #: Exception type/message "raise" mode uses -- lets a test simulate
        #: Ctrl+C / task cancellation, not just an ordinary engine error.
        self.exception_cls: type[BaseException] = RuntimeError
        self.exception_message = "engine blew up"
        self.engines: list[FakeEngine] = []
        self.prompts: list[str] = []
        self.counter = 0
        #: When true, run() asks the configured approval_gate once before
        #: finishing the turn, and fails the turn if the gate does not allow.
        self.request_approval = False
        #: Override the synthesized ApprovalRequest's fields, e.g.
        #: {"arguments": {"command": "git push origin main"}}.
        self.approval_overrides: dict[str, Any] = {}

    @property
    def last(self) -> FakeEngine:
        return self.engines[-1]

    @property
    def kwargs(self) -> dict[str, Any]:
        return self.last.kwargs


class FakeEngine:
    def __init__(self, kind: str, script: Script, kwargs: dict[str, Any]) -> None:
        self.kind = kind
        self.script = script
        self.kwargs = kwargs
        self.handle_attr = "thread_id" if kind == "codex" else "session_id"
        explicit = kwargs.get(self.handle_attr)
        alias = kwargs.get("session_alias")
        registry = kwargs.get("session_registry")
        known = registry.resolve(kind, kwargs["cwd"], alias) if alias and registry is not None else None
        self._set(explicit or known)

    def _set(self, value: str | None) -> None:
        setattr(self, self.handle_attr, value)

    @property
    def handle(self) -> str | None:
        return getattr(self, self.handle_attr)

    def finish_turn(self) -> None:
        if self.handle is None:
            self.script.counter += 1
            self._set(f"{self.kind}-id-{self.script.counter}")
        alias = self.kwargs.get("session_alias")
        registry = self.kwargs.get("session_registry")
        if alias and registry is not None and self.handle:
            registry.bind(
                self.kind,
                self.kwargs["cwd"],
                alias,
                self.handle,
                model=self.kwargs.get("model"),
                effort=self.kwargs.get("reasoning_effort"),
            )


class FakeAgent:
    script: Script

    def __init__(self, engine: FakeEngine, name: str | None = None, tools: list[Any] | None = None) -> None:
        self.engine, self.name, self.tools = engine, name, list(tools or [])

    async def run(self, prompt: str) -> Any:
        from lazybridge import Envelope

        script = type(self).script
        script.prompts.append(prompt)

        if script.request_approval:
            from lazybridge.engines.coding import ApprovalRequest

            config = self.engine.kwargs.get("config")
            gate = config.approval_gate if config is not None else None
            fields: dict[str, Any] = {
                "provider": "codex" if self.engine.kind == "codex" else "claude-code",
                "kind": "command",
                "name": "shell",
                "arguments": {"command": "rm -rf scratch"},
                "cwd": self.engine.kwargs.get("cwd"),
            }
            fields.update(script.approval_overrides)
            request = ApprovalRequest(**fields)
            decision = await gate(request)
            self.engine.finish_turn()
            if decision.action not in ("allow", "allow_session"):
                return Envelope.error_envelope(RuntimeError(f"denied: {decision.message}"))
            if script.mode == "raise":
                raise script.exception_cls(script.exception_message)
            if script.mode == "error":
                return Envelope.error_envelope(RuntimeError("turn failed"))
            return Envelope(task=prompt, payload=script.text)

        self.engine.finish_turn()
        if script.mode == "raise":
            raise script.exception_cls(script.exception_message)
        if script.mode == "error":
            return Envelope.error_envelope(RuntimeError("turn failed"))
        metadata = {"model": script.reported_model, "model_resolved": bool(script.reported_model)}
        return Envelope(task=prompt, payload=script.text, metadata=metadata)


def install(monkeypatch: pytest.MonkeyPatch) -> Script:
    """Patch both engine seams and ``lazybridge.Agent``; return the script to drive/inspect."""
    import importlib

    import lazybridge

    script = Script()

    for module_name, attr, kind in _SEAMS:
        module = importlib.import_module(module_name)

        def factory(_kind: str = kind, **kwargs: Any) -> FakeEngine:
            engine = FakeEngine(_kind, script, kwargs)
            script.engines.append(engine)
            return engine

        monkeypatch.setattr(module, attr, factory)

    agent_cls = type("BoundFakeAgent", (FakeAgent,), {"script": script})
    monkeypatch.setattr(lazybridge, "Agent", agent_cls)
    return script


# Emulates `gh repo set-default --view`: print the remote marked gh-resolved, else nothing.
FAKE_GH = r'''
import subprocess, sys
if sys.argv[1:] != ["repo", "set-default", "--view"]:
    sys.exit(2)
def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True).stdout.split()
resolved = git("config", "--get-regexp", r"^remote\..*\.gh-resolved$")
if not resolved:
    print("X No default remote repository has been set.", file=sys.stderr)
    sys.exit(0)
key, value = resolved[:2]
if value == "base":
    url = git("remote", "get-url", key.split(".")[1])[0]
    value = "/".join(url.removesuffix(".git").replace(":", "/").split("/")[-2:])
print(value)
'''


def install_fake_gh(directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Put a ``gh`` that only answers ``repo set-default --view`` first on PATH."""
    directory.mkdir()
    script = directory / "fake_gh.py"
    script.write_text(FAKE_GH, encoding="utf-8")
    if os.name == "nt":
        (directory / "gh.cmd").write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        launcher = directory / "gh"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")
