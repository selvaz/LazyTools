from __future__ import annotations

import json

from lazybridge import Store

from lazytools.skills.financial_investigator import (
    FinancialInvestigatorMemory,
    financial_investigator,
    financial_investigator_baseline,
)


def _tools(store: Store):
    return {tool.name: tool for tool in FinancialInvestigatorMemory(store).as_tools()}


def test_memory_tools_save_structured_evidence_fact_and_note() -> None:
    store = Store()
    tools = _tools(store)

    assert "saved evidence" in tools["save_evidence"].run_sync(
        issuer="ACME",
        evidence_id="filing-2024-revenue",
        evidence_json=json.dumps({"source": "annual-report", "locator": "p42", "content": "Revenue 100"}),
    )
    assert "saved candidate fact" in tools["save_candidate_fact"].run_sync(
        issuer="ACME",
        metric="revenue",
        fact_json=json.dumps(
            {"status": "accepted", "value": 100, "evidence_refs": ["issuer:ACME:evidence:filing-2024-revenue"], "interpretation": "reported revenue"}
        ),
    )
    assert "saved research note" in tools["save_research_note"].run_sync(
        issuer="ACME",
        investigation_id="2024",
        note_json=json.dumps({"sources_searched": ["annual-report"], "difficulties": [], "resolutions": [], "unresolved": []}),
    )
    keys = json.loads(tools["list_issuer_memory"].run_sync(issuer="ACME"))["keys"]
    assert len(keys) == 3
    assert json.loads(tools["read_issuer_memory"].run_sync(key="issuer:ACME:fact:revenue"))["value"]["status"] == "accepted"


def test_a_resolution_becomes_the_issuer_playbook_only_with_saved_evidence() -> None:
    tools = _tools(Store())
    evidence = "issuer:ACME:evidence:income-statement"
    tools["save_evidence"].run_sync(
        issuer="ACME", evidence_id="income-statement",
        evidence_json=json.dumps({"source": "annual-report", "locator": "p42", "content": "statement"}),
    )
    result = tools["save_resolution"].run_sync(
        issuer="ACME", resolution_id="operating-income",
        resolution_json=json.dumps({
            "observed_pattern": "no operating-income subtotal",
            "applies_when": "the income statement presents gross profit and total operating expenses",
            "decision_rule": "derive operating income from those two lines",
            "verification": "the derived subtotal reconciles within statement rounding",
            "evidence_refs": [evidence],
            "recheck_when": "the statement layout or taxonomy changes",
        }),
    )
    assert "saved resolution" in result
    playbook = json.loads(tools["read_issuer_playbook"].run_sync(issuer="ACME"))
    assert playbook["resolutions"][0]["value"]["decision_rule"].startswith("derive")


def test_a_resolution_cannot_cite_evidence_that_was_never_saved() -> None:
    tools = _tools(Store())
    result = tools["save_resolution"].run_sync(
        issuer="ACME", resolution_id="missing-proof",
        resolution_json=json.dumps({
            "observed_pattern": "x", "applies_when": "x", "decision_rule": "x",
            "verification": "x", "evidence_refs": ["issuer:ACME:evidence:missing"],
            "recheck_when": "x",
        }),
    )
    assert "not stored" in result


def test_memory_tools_fail_closed_on_incomplete_or_invalid_payload() -> None:
    tools = _tools(Store())
    assert "valid JSON" in tools["save_evidence"].run_sync(issuer="ACME", evidence_id="x", evidence_json="not-json")
    assert "missing required" in tools["save_evidence"].run_sync(issuer="ACME", evidence_id="x", evidence_json="{}")
    assert "must be accepted" in tools["save_candidate_fact"].run_sync(
        issuer="ACME",
        metric="revenue",
        fact_json=json.dumps({"status": "guess", "value": 1, "evidence_refs": ["x"], "interpretation": "x"}),
    )


def test_agent_builder_has_canonical_lazybridge_shape() -> None:
    class StubEngine:
        pass

    agent = financial_investigator(engine=StubEngine(), store=Store())
    assert agent.name == "financial-investigator"
    assert agent.session is not None
    assert {
        "bb_put",
        "bb_get",
        "bb_list",
        "list_issuer_memory",
        "read_issuer_memory",
        "read_issuer_playbook",
        "save_evidence",
        "save_candidate_fact",
        "save_research_note",
        "save_resolution",
    } <= set(agent._tool_map)


def test_agent_builder_accepts_a_shared_session() -> None:
    from lazybridge import Session

    class StubEngine:
        pass

    session = Session()
    agent = financial_investigator(engine=StubEngine(), store=Store(), session=session)
    assert agent.session is session


def test_baseline_agent_has_a_session_but_no_dynamic_tools_or_structured_output() -> None:
    agent = financial_investigator_baseline()
    assert agent.name == "financial-investigator-baseline"
    assert agent.session is not None
    assert agent._tool_map == {}
    assert agent.output is str


def test_blackboard_and_dossier_share_the_same_store() -> None:
    store = Store()
    memory = FinancialInvestigatorMemory(store)
    tools = {tool.name: tool for tool in memory.blackboard_tools()}
    tools["bb_put"].run_sync(key="current_issuer", value="ACME")
    assert store.read("current_issuer") == "ACME"


def test_investigator_instructions_keep_issuer_specific_exceptions_in_the_playbook() -> None:
    from lazytools.skills.financial_investigator import _SYSTEM

    assert "issuer-specific memory" in _SYSTEM
    assert "web__run" not in _SYSTEM
    assert not any(name in _SYSTEM.casefold() for name in ("nike", "walmart", "nvidia", "deere"))
