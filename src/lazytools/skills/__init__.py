"""Skills for LazyTools agents.

Two flavours live here:

* **Documentation skills** (``doc_skills``) — index local documentation folders
  into a portable BM25 bundle and expose it as a tool. Stdlib only.
* **Analyst skills** (``analyst``) — specialist agents (domain tools + a
  tailored system prompt = a skill) that share a blackboard, plus three
  orchestrators (deterministic Plan, blackboard, replan) that compose them.
  These build on ``lazybridge`` agents.
* **Council skills** (``council``) — spontaneous multi-agent deliberation via
  ``AgentPool``, including a DeepSeek + Claude Code current-news preset.
"""

from __future__ import annotations

from lazytools.skills.analyst import (
    FINANCIALS,
    MARKET_DATA,
    REGIME,
    REPORT,
    SKILLS,
    STATS,
    AnalystConfig,
    Blackboard,
    Skill,
    blackboard_orchestrator,
    build_specialists,
    plan_orchestrator,
    replan_orchestrator,
    roster,
)
from lazytools.skills.council import (
    CouncilResult,
    WizengAImot,
    deepseek_claude_news_council,
    standard_council,
)
from lazytools.skills.doc_skills import (
    DocChunk,
    SkillManifest,
    build_skill,
    query_skill,
    skill_builder_tools,
    skill_pipeline,
    skill_tools,
)
from lazytools.skills.stats_agents import (
    regime_analyst,
    regression_analyst,
    stats_supervisor,
    volatility_correlation_analyst,
)
from lazytools.skills.stats_report import (
    REGRESSION,
    STATS_REPORT,
    STATS_REPORT_SKILLS,
    VOL_CORR,
    build_stats_report_specialists,
    stats_report_pipeline,
)

__all__ = [
    # documentation skills
    "DocChunk",
    "SkillManifest",
    "build_skill",
    "query_skill",
    "skill_builder_tools",
    "skill_pipeline",
    "skill_tools",
    # spontaneous councils
    "CouncilResult",
    "WizengAImot",
    "standard_council",
    "deepseek_claude_news_council",
    # analyst skills
    "Skill",
    "Blackboard",
    "AnalystConfig",
    "SKILLS",
    "MARKET_DATA",
    "FINANCIALS",
    "STATS",
    "REGIME",
    "REPORT",
    "roster",
    "build_specialists",
    "plan_orchestrator",
    "blackboard_orchestrator",
    "replan_orchestrator",
    # focused statistical specialists + supervisor (agent-as-tool)
    "volatility_correlation_analyst",
    "regime_analyst",
    "regression_analyst",
    "stats_supervisor",
    # charted statistical report (blackboard pipeline)
    "VOL_CORR",
    "REGRESSION",
    "STATS_REPORT",
    "STATS_REPORT_SKILLS",
    "build_stats_report_specialists",
    "stats_report_pipeline",
]
