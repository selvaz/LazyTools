"""Local documentation skill runtime.

Index local documentation folders into a portable skill bundle (BM25), then
expose the bundle as a tool or pipeline any agent can call. No extra
dependencies beyond the standard library.
"""

from __future__ import annotations

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
    "DocChunk",
    "SkillManifest",
    "build_skill",
    "query_skill",
    "skill_builder_tools",
    "skill_pipeline",
    "skill_tools",
]
