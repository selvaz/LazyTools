"""Specialist agent factories: construction contract only (no live LLM calls).

Both factories mirror ``connectors/fin/agents.py``'s shape — a plain function
taking an already-built ``engine`` plus a caller-supplied ``tools`` list,
returning a ``lazybridge.Agent``. Constructing an ``LLMEngine`` never touches
the network (verified: its ``__init__`` only stores parameters), so these
tests exercise the real classes without any live call or API key.
"""

from __future__ import annotations

import pytest

pytest.importorskip("lazybridge")

from lazybridge import LLMEngine

from lazytools.connectors.fin.optimizer_agent import (
    OPTIMIZER_SPECIALIST_SYSTEM,
    optimizer_specialist,
)
from lazytools.report.agents import REPORT_SPECIALIST_SYSTEM, report_specialist


def test_optimizer_specialist_returns_a_named_agent() -> None:
    engine = LLMEngine("deepseek-v4-flash", system=OPTIMIZER_SPECIALIST_SYSTEM)
    agent = optimizer_specialist(engine, tools=[])
    assert agent._is_lazy_agent is True
    assert agent.name == "portfolio-optimizer-specialist"
    assert agent.description


def test_optimizer_specialist_accepts_a_custom_name() -> None:
    engine = LLMEngine("deepseek-v4-flash", system=OPTIMIZER_SPECIALIST_SYSTEM)
    agent = optimizer_specialist(engine, tools=[], name="my-optimizer")
    assert agent.name == "my-optimizer"


def test_report_specialist_returns_a_named_agent() -> None:
    engine = LLMEngine("deepseek-v4-flash", system=REPORT_SPECIALIST_SYSTEM)
    agent = report_specialist(engine, tools=[])
    assert agent._is_lazy_agent is True
    assert agent.name == "report-specialist"
    assert agent.description


def test_report_specialist_accepts_a_custom_name() -> None:
    engine = LLMEngine("deepseek-v4-flash", system=REPORT_SPECIALIST_SYSTEM)
    agent = report_specialist(engine, tools=[], name="my-reporter")
    assert agent.name == "my-reporter"


def test_system_prompts_are_non_trivial_strings() -> None:
    assert len(OPTIMIZER_SPECIALIST_SYSTEM) > 200
    assert len(REPORT_SPECIALIST_SYSTEM) > 200
