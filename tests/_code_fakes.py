"""Shared fakes for the engine-backed code tools (reviewers, consultants, writers).

No Codex / Claude Code process is ever started. The engines are replaced at the
``_make_codex_engine`` / ``_make_claude_engine`` seams and ``lazybridge.Agent``
by a scripted stand-in. The fake engine mimics what the real ones do with a
``SessionRegistry`` (resume the id a known alias names, bind the alias after the
turn, explicit id wins and rebinds) so the tools' alias handling is tested end to
end against a REAL registry file in ``tmp_path`` -- never ``~/.lazybridge``.
"""

from __future__ import annotations

from typing import Any

import pytest

_SEAMS = (
    ("lazytools.connectors.code_support._review", "_make_codex_engine", "codex"),
    ("lazytools.connectors.code_support._writer", "_make_codex_engine", "codex"),
    ("lazytools.connectors.code_support._claude_review", "_make_claude_engine", "claude"),
    ("lazytools.connectors.code_support._writer", "_make_claude_engine", "claude"),
)


class Script:
    """What the next fake turn does, and what the fakes saw."""

    def __init__(self) -> None:
        self.mode = "ok"  # "ok" | "error" | "raise"
        self.text = "done"
        self.engines: list[FakeEngine] = []
        self.prompts: list[str] = []
        self.counter = 0

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
        """What a real engine does once the turn has run: open an id if needed, bind the alias."""
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
        self.engine.finish_turn()
        if script.mode == "raise":
            raise RuntimeError("engine blew up")
        if script.mode == "error":
            return Envelope.error_envelope(RuntimeError("turn failed"))
        return Envelope(task=prompt, payload=script.text)


def install(monkeypatch: pytest.MonkeyPatch) -> Script:
    """Patch every engine seam and ``lazybridge.Agent``; return the script to drive/inspect."""
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
