"""Canonical LazyBridge agent for evidence-backed financial investigation.

The agent is deliberately small: source-specific retrieval tools can be added
later, while the durable ``Store`` already gives every investigation a shared
memory.  The model may interpret documents and explain uncertainty, but every
fact or note it keeps must pass through an explicit, auditable tool.
"""

from __future__ import annotations

import json
from typing import Any, Literal


_PREFIX = "issuer:"

_SYSTEM = """You are a financial-data investigation specialist.

Your task is to find and interpret accounting evidence for a named issuer,
globally and across inconsistent filing formats. You are not a generic chat
assistant and you are not a rating agency. Never invent a number, period,
currency, accounting standard, source, page, or interpretation.

For internet research, use Codex's own account-level native web-search and
web-fetch capabilities when they are available. Do not call a named external
search tool and do not use LazyCrawler. Do not treat a search snippet as
evidence: open the source and save its exact URL and locator in the dossier. If
native web access is unavailable, say so and leave the affected item unresolved;
never silently substitute another search provider.

Separate every result into:
- evidence: what the source literally contains;
- interpretation: what the evidence appears to mean;
- calculation: arithmetic derived from accepted evidence;
- decision: accepted, rejected, or ambiguous.

Use prior issuer memory as a hypothesis, never as proof. Re-validate a prior
mapping when the filing, taxonomy, period, entity perimeter, or presentation
changes. If two readings remain plausible, save both candidates and mark the
metric ambiguous. A precise-looking unsupported value is a failure.

Before new research, read the issuer playbook. It contains only observed
exceptions and their validation rules from prior filings; apply a rule only
when its stated conditions still hold. When a new exception is resolved, save
the observed pattern, decision rule, evidence references and the condition that
would require a fresh check. Do not put issuer names or historical examples in
your instructions: the playbook is the only source of issuer-specific memory.

At the end of useful work, save (1) structured evidence or candidate facts and
(2) a research note explaining sources searched, difficulties, alternatives,
resolution steps, unresolved issues, and what should be checked next time. The
note is a summary of the investigation, not a substitute for evidence.

Use the shared blackboard for short-lived work handles such as the current
issuer, filing key, pending metric, evidence bundle key, and next action. Do not
put bulk documents or long prose on the blackboard.
"""

_BASELINE_SYSTEM = """You are a financial filing researcher.

Use Codex's native web-search and web-fetch capabilities to find the requested
reported accounting value. Use a primary filing when available. Return only the
metric, reported value, unit/currency, reporting period, exact source URL, and
page/table/section locator. If evidence is insufficient, say unresolved.
"""


def _key(issuer: str, kind: str, identifier: str) -> str:
    return f"{_PREFIX}{issuer}:{kind}:{identifier}"


def _json_object(value: str, label: str) -> dict[str, Any] | str:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        return f"{label} must be valid JSON: {exc.msg}"
    if not isinstance(parsed, dict):
        return f"{label} must be a JSON object"
    return parsed


class FinancialInvestigatorMemory:
    """Explicit Store-backed tools for an evidence dossier."""

    _is_lazy_tool_provider = True

    def __init__(self, store: Any, *, agent_id: str = "financial-investigator") -> None:
        self.store = store
        self.agent_id = agent_id

    def blackboard_tools(self) -> list[Any]:
        """Return the standard LazyTools blackboard over this same Store."""
        from lazytools.skills.analyst import Blackboard

        return Blackboard(self.store).as_tools()

    def as_tools(self) -> list[Any]:
        from lazybridge import Tool

        def list_issuer_memory(issuer: str) -> str:
            """List stored dossier keys for an issuer; does not return bulk data."""
            prefix = f"{_PREFIX}{issuer}:"
            keys = [key for key in self.store.keys() if key.startswith(prefix)]
            return json.dumps({"issuer": issuer, "keys": keys}, ensure_ascii=False)

        def read_issuer_memory(key: str) -> str:
            """Read one exact dossier entry by its full Store key."""
            if not key.startswith(_PREFIX):
                return "memory key must start with 'issuer:'"
            value = self.store.read(key, None)
            if value is None:
                return json.dumps({"found": False, "key": key})
            return json.dumps({"found": True, "key": key, "value": value}, ensure_ascii=False)

        def read_issuer_playbook(issuer: str) -> str:
            """Read reusable, issuer-specific resolution rules before research."""
            prefix = f"{_PREFIX}{issuer}:resolution:"
            entries = [
                {"key": key, "value": self.store.read(key)}
                for key in self.store.keys() if key.startswith(prefix)
            ]
            return json.dumps({"issuer": issuer, "resolutions": entries}, ensure_ascii=False)

        def save_evidence(issuer: str, evidence_id: str, evidence_json: str) -> str:
            """Save one source-backed evidence record as a JSON object.

            Include source, locator, period, unit/currency when applicable, the
            literal value or text, and a concise interpretation status.
            """
            payload = _json_object(evidence_json, "evidence_json")
            if isinstance(payload, str):
                return payload
            required = {"source", "locator", "content"}
            missing = sorted(required - payload.keys())
            if missing:
                return f"evidence_json missing required fields: {', '.join(missing)}"
            key = _key(issuer, "evidence", evidence_id)
            self.store.write(key, payload, agent_id=self.agent_id)
            return f"saved evidence: {key}"

        def save_candidate_fact(issuer: str, metric: str, fact_json: str) -> str:
            """Save an interpreted candidate fact, including its evidence refs."""
            payload = _json_object(fact_json, "fact_json")
            if isinstance(payload, str):
                return payload
            required = {"status", "value", "evidence_refs", "interpretation"}
            missing = sorted(required - payload.keys())
            if missing:
                return f"fact_json missing required fields: {', '.join(missing)}"
            if payload["status"] not in {"accepted", "rejected", "ambiguous"}:
                return "fact_json.status must be accepted, rejected, or ambiguous"
            if not isinstance(payload["evidence_refs"], list) or not payload["evidence_refs"]:
                return "fact_json.evidence_refs must be a non-empty list"
            key = _key(issuer, "fact", metric)
            self.store.write(key, payload, agent_id=self.agent_id)
            return f"saved candidate fact: {key}"

        def save_research_note(issuer: str, investigation_id: str, note_json: str) -> str:
            """Save the human-readable investigation summary and open issues."""
            payload = _json_object(note_json, "note_json")
            if isinstance(payload, str):
                return payload
            required = {"sources_searched", "difficulties", "resolutions", "unresolved"}
            missing = sorted(required - payload.keys())
            if missing:
                return f"note_json missing required fields: {', '.join(missing)}"
            key = _key(issuer, "note", investigation_id)
            self.store.write(key, payload, agent_id=self.agent_id)
            return f"saved research note: {key}"

        def save_resolution(issuer: str, resolution_id: str, resolution_json: str) -> str:
            """Save one reusable exception rule, backed by evidence already stored.

            The record describes a pattern, not a one-off answer: when it
            applies, the rule to follow, how it was verified, and when it must
            be reconsidered for a changed presentation.
            """
            payload = _json_object(resolution_json, "resolution_json")
            if isinstance(payload, str):
                return payload
            required = {"observed_pattern", "applies_when", "decision_rule", "verification",
                        "evidence_refs", "recheck_when"}
            missing = sorted(required - payload.keys())
            if missing:
                return f"resolution_json missing required fields: {', '.join(missing)}"
            refs = payload["evidence_refs"]
            if not isinstance(refs, list) or not refs:
                return "resolution_json.evidence_refs must be a non-empty list"
            unknown = [ref for ref in refs if self.store.read(str(ref), None) is None]
            if unknown:
                return "resolution_json references evidence that is not stored: " + ", ".join(unknown)
            key = _key(issuer, "resolution", resolution_id)
            self.store.write(key, payload, agent_id=self.agent_id)
            return f"saved resolution: {key}"

        return [
            Tool.wrap(list_issuer_memory, name="list_issuer_memory"),
            Tool.wrap(read_issuer_memory, name="read_issuer_memory"),
            Tool.wrap(read_issuer_playbook, name="read_issuer_playbook"),
            Tool.wrap(save_evidence, name="save_evidence"),
            Tool.wrap(save_candidate_fact, name="save_candidate_fact"),
            Tool.wrap(save_research_note, name="save_research_note"),
            Tool.wrap(save_resolution, name="save_resolution"),
        ]


def financial_investigator(
    model: str = "gpt-5.6-terra",
    *,
    store: Any,
    engine: Any = None,
    name: str = "financial-investigator",
    reasoning_effort: str = "medium",
    web_search_mode: Literal["disabled", "cached", "indexed", "live"] = "live",
    agent_id: str = "financial-investigator",
    session: Any = None,
    tools: list[Any] | None = None,
) -> Any:
    """Build the canonical investigator over a durable or in-memory Store.

    ``store`` is required so callers make persistence explicit.  Pass
    ``Store(db="financial-investigator.sqlite")`` for cross-run memory.  Extra
    retrieval tools can be supplied through ``tools``; memory tools are always
    included.
    """
    from lazybridge import Agent, CodexEngine, CodexPolicy, CodingAgentConfig, Session

    if engine is None:
        engine = CodexEngine(
            model,
            system=_SYSTEM,
            reasoning_effort=reasoning_effort,
            config=CodingAgentConfig(
                codex=CodexPolicy(
                    web_search=web_search_mode,
                    preapprove_dynamic_tools=True,
                )
            ),
        )
    memory = FinancialInvestigatorMemory(store, agent_id=agent_id)
    session = session or Session()
    return Agent(
        engine=engine,
        tools=[
            *memory.blackboard_tools(),
            *memory.as_tools(),
            *(tools or []),
        ],
        name=name,
        session=session,
    )


def financial_investigator_baseline(
    model: str = "gpt-5.6-terra",
    *,
    reasoning_effort: str = "medium",
    web_search_mode: Literal["disabled", "cached", "indexed", "live"] = "live",
    session: Any = None,
) -> Any:
    """Build the no-tool baseline used before enabling dossier capabilities."""
    from lazybridge import Agent, CodexEngine, CodexPolicy, CodingAgentConfig, Session

    return Agent(
        engine=CodexEngine(
            model,
            system=_BASELINE_SYSTEM,
            reasoning_effort=reasoning_effort,
            config=CodingAgentConfig(codex=CodexPolicy(web_search=web_search_mode)),
        ),
        name="financial-investigator-baseline",
        session=session or Session(),
    )


__all__ = ["FinancialInvestigatorMemory", "financial_investigator", "financial_investigator_baseline"]
