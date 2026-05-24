"""LazyTools — reusable tool providers, connector clients, and safety wrappers.

Built on top of `lazybridge`. Import what you need directly so installing the
toolkit never pulls a connector's optional dependencies until it is used::

    from lazytools.connectors.gmail import GmailTools, GmailClient
    from lazytools.connectors.telegram import TelegramTools
    from lazytools.connectors.mcp import MCP
    from lazytools.safety import Allowlist, ConfirmationGate, ActionBlocked
    from lazytools.documents import read_docs_tools
    from lazytools.skills import build_skill, skill_tools

This top-level module performs no eager heavy imports by design.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
