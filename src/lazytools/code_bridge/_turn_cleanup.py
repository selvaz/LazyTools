"""Own async subprocesses and callbacks on the worker's private event loop."""

from __future__ import annotations

import asyncio
from typing import Any


class TurnCleanup:
    """No continuation may start while a previous turn can still run callbacks.

    Coding transports (including the Claude SDK's AnyIO asyncio backend)
    spawn through the loop's public subprocess methods. Observe only this
    worker's loop, retain the actual transports, and verify return codes.
    Never enumerate or kill unrelated machine processes.
    """

    def __init__(self, *, timeout: float = 5.0) -> None:
        self.timeout = timeout
        self.transports: list[asyncio.SubprocessTransport] = []
        self.error: str | None = None
        self.originals: dict[str, Any] = {}

    async def __aenter__(self) -> TurnCleanup:
        self.loop = asyncio.get_running_loop()
        self.baseline = asyncio.all_tasks()
        for name in ("subprocess_exec", "subprocess_shell"):
            original = getattr(self.loop, name)
            self.originals[name] = original

            async def spawn(*args: Any, _original: Any = original, **kwargs: Any) -> Any:
                transport, protocol = await _original(*args, **kwargs)
                self.transports.append(transport)
                return transport, protocol

            setattr(self.loop, name, spawn)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        deadline = self.loop.time() + self.timeout
        try:
            while True:
                live = [process for process in self.transports if process.get_returncode() is None]
                for process in live:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                tasks = asyncio.all_tasks() - self.baseline - {asyncio.current_task()}
                for task in tasks:
                    task.cancel()
                if not live and not tasks:
                    return
                if self.loop.time() >= deadline:
                    self.error = f"turn cleanup incomplete: {len(live)} live subprocess(es), {len(tasks)} callback task(s)"
                    return
                if tasks:
                    done, _pending = await asyncio.wait(tasks, timeout=min(0.05, max(0, deadline - self.loop.time())))
                    await asyncio.gather(*done, return_exceptions=True)
                else:
                    await asyncio.sleep(0.01)
        finally:
            for name, original in self.originals.items():
                setattr(self.loop, name, original)
            for process in self.transports:
                if process.get_returncode() is not None:
                    process.close()
