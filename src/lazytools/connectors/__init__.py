"""Connector clients and tool providers for external services.

Each connector bridges an agent to an external service or protocol — Gmail,
Telegram, MCP servers, and remote tool gateways. Import the one you need
directly (``from lazytools.connectors.gmail import GmailTools``); this package
intentionally performs no eager imports so installing the toolkit never pulls a
connector's optional dependencies until it is used.
"""
