"""Test helpers for LazyTools connectors."""

from __future__ import annotations

from lazytools.testing.fake_clients import (
    FakeEdgarClient,
    FakeGmailService,
    FakeMarketDataAdapter,
    FakeOutlookService,
    FakeTelegramService,
)

__all__ = [
    "FakeEdgarClient",
    "FakeGmailService",
    "FakeMarketDataAdapter",
    "FakeOutlookService",
    "FakeTelegramService",
]
