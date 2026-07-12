"""Skills for LazyTools agents.

Two flavours live here:

* **Documentation skills** (``doc_skills``) — index local documentation folders
  into a portable BM25 bundle and expose it as a tool. Stdlib only.
* **Analyst skills** (``analyst``) — specialist agents (domain tools + a
  tailored system prompt = a skill) that share a blackboard, plus three
  orchestrators (deterministic Plan, blackboard, replan) that compose them.
  These build on ``lazybridge`` agents.
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
from lazytools.skills.doc_skills import (
    DocChunk,
    SkillManifest,
    build_skill,
    query_skill,
    skill_builder_tools,
    skill_pipeline,
    skill_tools,
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
]
