"""Test helpers for LazyTools connectors."""

from __future__ import annotations

from lazytools.testing.fake_clients import (
    FakeDataHubBackend,
    FakeEdgarClient,
    FakeGmailService,
    FakeMarketDataAdapter,
    FakeOutlookService,
    FakeTelegramService,
)

__all__ = [
    "FakeDataHubBackend",
    "FakeEdgarClient",
    "FakeGmailService",
    "FakeMarketDataAdapter",
    "FakeOutlookService",
    "FakeTelegramService",
]
