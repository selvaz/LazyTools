"""Tests for spontaneous council configuration without external model calls."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from lazybridge import Agent, LLMEngine, Tool

from lazytools.skills.council import WizengAImot, deepseek_claude_news_council


def _agent(name: str) -> Agent:
    return Agent(engine=LLMEngine("medium", provider="deepseek"), name=name)


def test_council_requires_named_unique_members() -> None:
    council = WizengAImot("question")
    council.add(_agent("analyst"))

    with pytest.raises(ValueError, match="Duplicate"):
        council.add(_agent("analyst"))
    with pytest.raises(ValueError, match="reserved"):
        council.add(_agent("moderator"))


def test_council_validates_quorum_and_depth() -> None:
    with pytest.raises(ValueError, match="quorum"):
        WizengAImot(quorum=0)
    with pytest.raises(ValueError, match="max_rounds"):
        WizengAImot(max_rounds=0)
    with pytest.raises(ValueError, match="max_depth"):
        WizengAImot(max_depth=0)


def test_news_preset_wires_reasoning_and_shared_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Build the preset with a fake crawler; never contact DeepSeek or Claude."""

    try:
        from lazybridge import ClaudeCodeEngine  # noqa: F401
    except ImportError:
        pytest.skip("requires LazyBridge 1.0.2+ ClaudeCodeEngine")

    class Config:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class CrawlerDB:
        def __init__(self, config):
            self.config = config

    class CrawlerTools:
        _is_lazy_tool_provider = True

        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def as_tools(self):
            return [
                Tool.wrap(
                    lambda query: f"news:{query}",
                    name="web_search",
                    description="Search the current news database.",
                )
            ]

    news_db = tmp_path / "news.db"
    news_db.touch()

    lazycrawler = types.ModuleType("lazycrawler")
    lazycrawler.CrawlerDB = CrawlerDB
    lazycrawler.CrawlerTools = CrawlerTools
    lazycrawler.DBConfig = Config
    lazycrawler.LLMConfig = Config
    crawler_config = types.ModuleType("lazycrawler.config")
    crawler_config.resolve_news_db_path = lambda value: value
    monkeypatch.setitem(sys.modules, "lazycrawler", lazycrawler)
    monkeypatch.setitem(sys.modules, "lazycrawler.config", crawler_config)

    council = deepseek_claude_news_council("What changed?", news_db=news_db)

    assert [member.name for member in council._members] == [
        "deepseek_evidence_analyst",
        "deepseek_risk_analyst",
        "claude_haiku_analyst",
        "deepseek_max_debater",
        "claude_sonnet_debater",
    ]
    assert council._members[0].engine.thinking is False
    assert council._members[1].engine.thinking is False
    assert council._members[2].engine.model == "haiku"
    assert council._members[2].engine.thinking == {"type": "disabled"}
    assert council._members[2].engine.web is True
    assert council._members[3].engine.thinking == "max"
    assert council._members[4].engine.reasoning_effort == "medium"
    assert council._members[4].engine.thinking == {"type": "adaptive"}
    assert council._members[4].engine.web is True
    assert council._moderator.engine.thinking == "max"
    assert council._synthesiser.engine.reasoning_effort == "medium"
    assert all("web_search" in member._tool_map for member in council._members)
    assert "web_search" in council._moderator._tool_map
    assert "web_search" in council._synthesiser._tool_map
