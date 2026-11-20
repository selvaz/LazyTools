from __future__ import annotations

from dataclasses import dataclass

import pytest

from lazytools.mcp_server.catalog import PROVIDER_GROUPS, tool_metadata
from lazytools.mcp_server.providers import PROVIDER_FACTORIES


@dataclass
class _Tool:
    name: str


def test_every_registered_provider_has_complete_catalog_metadata() -> None:
    assert PROVIDER_GROUPS.keys() == PROVIDER_FACTORIES.keys()
    for provider_id, entry in PROVIDER_GROUPS.items():
        assert entry.group
        assert entry.group_order >= 0
        assert entry.provider_label
        assert entry.origin_repo
        assert entry.description.endswith("."), provider_id


@pytest.mark.parametrize("provider_id", PROVIDER_FACTORIES)
def test_every_provider_tool_gets_an_access_value(provider_id: str) -> None:
    metadata = tool_metadata(provider_id, _Tool(f"{provider_id}_representative_tool"))
    assert metadata["access"] in {"read", "write"}
    assert set(metadata) == {"group", "group_order", "provider_label", "origin_repo", "access"}


@pytest.mark.parametrize(
    ("provider_id", "tool_name", "access"),
    [
        ("datahub", "datahub_get_series", "read"),
        ("datahub", "datahub_ensure_price_history", "write"),
        ("regimes", "regime_get_summary", "read"),
        ("regimes", "regime_generate_plots", "write"),
        ("code_review", "codex_ask", "write"),
        ("code_sessions", "code_sessions_list", "write"),
        ("studio", "studio_run_status", "read"),
        ("studio", "published_agent", "write"),
    ],
)
def test_tool_access_uses_existing_write_gates(provider_id: str, tool_name: str, access: str) -> None:
    assert tool_metadata(provider_id, tool_name)["access"] == access


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(KeyError):
        tool_metadata("not_registered", "anything")
