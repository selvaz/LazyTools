"""Asynchronous CLI bridge to Codex / Claude Code, for a Claude Code session.

``codex_write`` / ``claude_code_write`` (``lazytools.connectors.code_support``)
are synchronous MCP tool calls: the delegating Claude Code session blocks
until the job finishes, and a long job can outrun the MCP transport's own
timeout -- looking failed while the work keeps running underneath.

This package is the asynchronous alternative: a console script
(``lazytools-code-bridge``, or ``python -m lazytools.code_bridge``) that
Claude Code launches via its Bash tool with ``run_in_background`` so it is
never blocked, and is notified automatically when the process exits. A
permission request the engine escalates mid-job ("ask" tier) files a ticket
in a durable queue instead of blocking on a human at a terminal; the
launching Claude Code session polls ``pending``, relays it to the user in
chat, and answers with ``approve``/``reject``.

See ``docs/code-bridge.md`` for the full workflow and ``cli.py`` for the
command surface. Reuses the SAME code-session registry, cwd confinement,
and engine construction style as
``lazytools.connectors.code_support``/``lazytools.mcp_server.providers``
(see ``_engines.py``'s own docstring for exactly what is shared vs. new).
"""

from __future__ import annotations

__all__: list[str] = []
