from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from lazytools.mcp_server.catalog import PROVIDER_GROUPS, tool_metadata
from lazytools.mcp_server.providers import PROVIDER_FACTORIES
from lazytools.mcp_server.server import expand_tools, is_unsafe_tool_name


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
        ("dispatch", "dispatch_status", "read"),
        ("dispatch", "dispatch_start", "write"),
        ("dispatch", "dispatch_stop", "write"),
        ("studio", "studio_run_status", "read"),
        ("studio", "published_agent", "write"),
        ("projects", "projects_create", "write"),
        ("projects", "projects_review_plan", "write"),
        ("projects", "projects_promote", "write"),
        ("projects", "projects_set_owner", "write"),
        ("projects", "projects_set_deadline", "write"),
        ("projects", "projects_add_note", "write"),
        ("projects", "projects_revise_plan", "write"),
        ("projects", "projects_schedule_task", "write"),
        ("projects", "projects_open_contract", "write"),
        ("fin", "portfolio_optimizer_run", "read"),
        ("fin", "portfolio_optimizer_backtest", "read"),
        ("web", "web_search", "write"),
    ],
)
def test_tool_access_uses_existing_write_gates(provider_id: str, tool_name: str, access: str) -> None:
    assert tool_metadata(provider_id, tool_name)["access"] == access


def test_web_fetch_access_tracks_interactive_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LAZYTOOLS_WEB_CACHE_DB", raising=False)
    assert tool_metadata("web", "web_crawl", web_cache_db="none")["access"] == "read"


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(KeyError):
        tool_metadata("not_registered", "anything")


def test_public_unsafe_name_helper() -> None:
    assert is_unsafe_tool_name("projects_close_task")
    assert not is_unsafe_tool_name("projects_get")


# These factories require operator credentials or a configured external runtime,
# so an offline catalog test cannot instantiate their real tool surfaces. Keep
# their code-facing provider ids explicit so a rename cannot silently evade the
# enumeration below.
ONLINE_PROVIDER_IDS = frozenset(
    {
        "calendar_agent",
        "claude_review",
        "code_bridge",
        "code_review",
        "code_sessions",
        "code_write",
        "gmail",
        "optimizer_agent",
        "outlook",
        "report_agent",
        "stats_agents",
        "telegram",
    }
)


def _real_tool_names(provider_id: str, allow_write: bool, tmp_path: Path) -> set[str]:
    data_source = {
        "path": ":memory:",
        "regime_db_path": ":memory:",
        "projects_store_db": ":memory:",
        "ceo_state_db": ":memory:",
        "tree_store_db": str(tmp_path) + "/trees.sqlite",
        "reports_dir": str(tmp_path) + "/reports",
    }
    built = PROVIDER_FACTORIES[provider_id](allow_write, data_source=data_source)
    providers = list(built) if isinstance(built, (list, tuple)) else [built]
    return set(expand_tools(providers, read_only=False))


@pytest.mark.parametrize("provider_id", sorted(PROVIDER_FACTORIES.keys() - ONLINE_PROVIDER_IDS))
def test_allow_write_only_real_tools_are_catalogued_as_writes(provider_id: str, tmp_path: Path) -> None:
    """The provider gate, rather than naming convention, is the source of truth."""

    try:
        read_names = _real_tool_names(provider_id, False, tmp_path)
        write_names = _real_tool_names(provider_id, True, tmp_path)
    except (ImportError, RuntimeError) as exc:
        pytest.skip(f"optional offline provider dependency unavailable: {exc}")
    gated_names = write_names - read_names
    assert all(tool_metadata(provider_id, name)["access"] == "write" for name in gated_names)


def test_online_provider_ids_are_real_code_names() -> None:
    assert ONLINE_PROVIDER_IDS.issubset(PROVIDER_FACTORIES)
