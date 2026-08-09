"""Tests for spontaneous council configuration without external model calls."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from lazybridge import Agent, LLMEngine, Tool

from lazytools.skills.council import (
    WizengAImot,
    deepseek_claude_news_council,
    standard_council,
)


def _agent(name: str) -> Agent:
    return Agent(engine=LLMEngine("medium", provider="deepseek"), name=name)


def _install_fake_crawler(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub lazycrawler so the news preset builds without a real crawler."""

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

    lazycrawler = types.ModuleType("lazycrawler")
    lazycrawler.CrawlerDB = CrawlerDB
    lazycrawler.CrawlerTools = CrawlerTools
    lazycrawler.DBConfig = Config
    lazycrawler.LLMConfig = Config
    crawler_config = types.ModuleType("lazycrawler.config")
    crawler_config.resolve_news_db_path = lambda value: value
    monkeypatch.setitem(sys.modules, "lazycrawler", lazycrawler)
    monkeypatch.setitem(sys.modules, "lazycrawler.config", crawler_config)


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


def test_council_reasoning_toggles_default_moderator_and_synthesiser() -> None:
    off = WizengAImot("question")
    assert off._default_moderator().engine.thinking is False
    assert off._default_synthesiser().engine.thinking is False

    on = WizengAImot("question", reasoning=True)
    assert on._default_moderator().engine.thinking is True
    assert on._default_synthesiser().engine.thinking is True


def test_council_reasoning_round_trips_through_save_and_preset(tmp_path: Path) -> None:
    path = tmp_path / "council.json"
    WizengAImot("question", reasoning=True).save(path)
    assert WizengAImot.preset(path).reasoning is True


def test_standard_council_reasoning_propagates_to_members_and_moderator() -> None:
    council = standard_council("question", reasoning=True)
    assert all(member.engine.thinking is True for member in council._members)
    assert council.reasoning is True
    assert council._default_moderator().engine.thinking is True


def test_council_default_memory_summarizer_is_cheap_non_reasoning() -> None:
    summarizer = WizengAImot("question")._default_memory_summarizer()
    assert summarizer.engine.model == "super_cheap"
    assert summarizer.engine.provider == "deepseek"
    assert summarizer.engine.thinking is False
    # Small hard cap: each compression call is self-contained and must
    # not accumulate history across unrelated calls.
    assert summarizer.memory.strategy == "none"
    assert summarizer.memory.max_turns == 4


def test_council_custom_memory_summarizer_is_stored() -> None:
    custom = _agent("custom_summarizer")
    council = WizengAImot("question", memory_summarizer=custom)
    assert council._memory_summarizer is custom


def test_news_preset_reasoning_rejects_unknown_level() -> None:
    with pytest.raises(ValueError, match="reasoning"):
        deepseek_claude_news_council("question", reasoning="ultra")


def test_news_preset_reasoning_maps_to_each_engine_vocabulary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    try:
        from lazybridge import ClaudeCodeEngine  # noqa: F401
    except ImportError:
        pytest.skip("requires LazyBridge 1.0.2+ ClaudeCodeEngine")

    _install_fake_crawler(monkeypatch)
    news_db = tmp_path / "news_low.db"
    news_db.touch()

    council = deepseek_claude_news_council("What changed?", news_db=news_db, reasoning="low")

    debater, claude_debater = council._members[3], council._members[4]
    assert debater.engine.thinking is False
    assert claude_debater.engine.reasoning_effort == "low"
    assert claude_debater.engine.thinking == {"type": "disabled"}
    assert council._moderator.engine.thinking is False
    assert council._synthesiser.engine.reasoning_effort == "low"
    # the two fast analysts are unaffected by `reasoning`, by design
    assert council._members[0].engine.thinking is False
    assert council._members[2].engine.thinking == {"type": "disabled"}


def test_news_preset_wires_reasoning_and_shared_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Build the preset with a fake crawler; never contact DeepSeek or Claude."""

    try:
        from lazybridge import ClaudeCodeEngine  # noqa: F401
    except ImportError:
        pytest.skip("requires LazyBridge 1.0.2+ ClaudeCodeEngine")

    _install_fake_crawler(monkeypatch)
    news_db = tmp_path / "news.db"
    news_db.touch()

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
