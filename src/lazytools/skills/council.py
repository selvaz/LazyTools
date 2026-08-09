"""WizengAImot — a spontaneous AI council built on LazyBridge.

The council prepares shared research and initial opinions, then opens an
unscripted AgentPool discussion. Members decide whom to challenge, when to
revise their position, and when to update their vote. The moderator may close
the discussion once quorum is reached; AgentPool's depth limit is only a
recursion safety brake.

Quick start::

    from lazytools.skills import standard_council

    result = standard_council("Should we expand to the US?").run()
    print(result.text())

Custom council::

    from lazybridge import Agent, LLMEngine
    from lazytools.skills import WizengAImot

    optimist = Agent(
        engine=LLMEngine(
            "medium", provider="anthropic", system="Find opportunities."
        ),
        name="optimist",
    )
    critic = Agent(
        engine=LLMEngine(
            "medium", provider="openai", system="Find risks."
        ),
        name="critic",
    )

    result = WizengAImot("Should we expand to the US?").add(optimist).add(critic).run()
    print(result.text())
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lazybridge import (
    Agent,
    AgentPool,
    LLMEngine,
    Memory,
    NativeTool,
    Session,
    Store,
    Tool,
    conclude,
)


@dataclass
class CouncilResult:
    question: str
    quorum_reached: bool
    votes: list[dict]
    transcript: list[dict]
    synthesis: str

    def text(self) -> str:
        return self.synthesis

    def __repr__(self) -> str:
        status = "✓ quorum" if self.quorum_reached else "✗ no quorum"
        return f"CouncilResult({status}, q={self.question[:50]!r})"


class WizengAImot:
    """Run a free-form council of pre-built LazyBridge agents.

    ``max_rounds`` is retained for compatibility, but it does not schedule
    rounds. Unless ``max_depth`` is supplied, it only helps derive a generous
    AgentPool recursion limit. The conversation itself has no speaker order.
    """

    def __init__(
        self,
        question: str = "",
        *,
        quorum: float = 0.7,
        max_rounds: int = 3,
        max_depth: int | None = None,
        synthesiser: Agent | None = None,
        moderator: Agent | None = None,
        reasoning: bool = False,
        memory_summarizer: Agent | None = None,
        session: Session | None = None,
    ) -> None:
        """``reasoning`` enables extended thinking on the *default* moderator
        and synthesiser only (ignored once ``moderator=``/``synthesiser=``
        supply your own agents — configure their engines directly instead).

        ``memory_summarizer`` compresses the free-form debate as it runs
        (every debater's memory and the moderator's memory use
        ``Memory(strategy="summary", summarizer=...)``). A long, unscripted
        debate has no fixed number of turns, so leaving it uncompressed
        risks the final synthesis call exceeding the model's context
        window. Defaults to a cheap, non-reasoning DeepSeek agent.
        """
        if not 0.0 < quorum <= 1.0:
            raise ValueError("quorum must be greater than 0 and at most 1.")
        if max_rounds < 1:
            raise ValueError("max_rounds must be at least 1.")
        if max_depth is not None and max_depth < 1:
            raise ValueError("max_depth must be at least 1.")

        self.question = question
        self.quorum = quorum
        self.max_rounds = max_rounds
        self.max_depth = max_depth
        self.reasoning = reasoning
        self.session = session
        self._members: list[Agent] = []
        self._researchers: list[Agent] = []
        self._kbs: list[dict] = []
        self._synthesiser = synthesiser
        self._moderator = moderator
        self._memory_summarizer = memory_summarizer

    def add(self, member: Agent, *, researcher: Agent | None = None) -> WizengAImot:
        """Add a member and, optionally, a separate research agent."""
        if not member.name or not str(member.name).strip():
            raise ValueError("Council members require an explicit non-empty name.")
        if member.name == "moderator":
            raise ValueError("'moderator' is reserved for the council moderator.")
        if any(existing.name == member.name for existing in self._members):
            raise ValueError(f"Duplicate council member name: {member.name!r}.")
        self._members.append(member)
        self._researchers.append(researcher or member)
        return self

    def knowledge(
        self,
        path: str | list[str],
        *,
        name: str = "knowledge",
        description: str = "Council knowledge base.",
        mode: str = "skill",
        output_root: str = "/tmp/wizengaimot_skills",
        rebuild: bool = False,
        extensions: str = "txt,md,pdf,docx,html",
        max_chars: int = 20_000,
    ) -> WizengAImot:
        """Attach grounded knowledge to every participant."""
        if mode not in {"skill", "static"}:
            raise ValueError("mode must be 'skill' or 'static'.")
        if not name.strip():
            raise ValueError("knowledge name must not be empty.")
        if max_chars < 1:
            raise ValueError("max_chars must be at least 1.")
        paths = [path] if isinstance(path, str) else list(path)
        if not paths:
            raise ValueError("knowledge path must contain at least one item.")
        self._kbs.append(
            {
                "paths": paths,
                "name": name,
                "description": description,
                "mode": mode,
                "output_root": output_root,
                "rebuild": rebuild,
                "extensions": extensions,
                "max_chars": max_chars,
            }
        )
        return self

    def __call__(self, question: str | None = None) -> CouncilResult:
        return self.run(question)

    @classmethod
    def preset(cls, path: str | Path, question: str = "") -> WizengAImot:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            question=question or data.get("question", ""),
            **{
                key: data[key]
                for key in ("quorum", "max_rounds", "max_depth", "reasoning")
                if key in data
            },
        )

    def save(self, path: str | Path) -> None:
        data = {
            "question": self.question,
            "quorum": self.quorum,
            "max_rounds": self.max_rounds,
            "max_depth": self.max_depth,
            "reasoning": self.reasoning,
        }
        Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")

    @staticmethod
    def _slug(value: str) -> str:
        value = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
        return re.sub(r"-+", "-", value).strip("-") or "skill"

    def _prepare_knowledge(self) -> tuple[list[Tool], str]:
        if not self._kbs:
            return [], ""

        try:
            from lazytools.documents import read_folder_docs
            from lazytools.skills import build_skill, skill_tools
        except ImportError as exc:
            raise ImportError(
                "Knowledge support requires the LazyTools lazytoolkit package "
                "(plus its 'docs' extra for PDF/DOCX/HTML)."
            ) from exc

        tools: list[Tool] = []
        context_sections: list[str] = []
        for kb in self._kbs:
            if kb["mode"] == "skill":
                supported = {
                    ".md", ".mdx", ".txt", ".rst", ".adoc",
                    ".py", ".json", ".yaml", ".yml", ".toml",
                }
                requested = {
                    f".{ext.strip().lstrip('.').lower()}"
                    for ext in kb["extensions"].split(",")
                    if ext.strip()
                }
                indexable = sorted(requested & supported)
                if not indexable:
                    raise ValueError(
                        "skill mode needs a text-indexable extension from "
                        f"{sorted(supported)}; use mode='static' for PDF/DOCX/HTML."
                    )

                skill_dir = Path(kb["output_root"]) / self._slug(kb["name"])
                manifest_path = skill_dir / "manifest.json"
                needs_rebuild = kb["rebuild"] or not manifest_path.exists()
                if not needs_rebuild:
                    # Same name/output_root can still mean different content —
                    # rebuild if the cached bundle doesn't match what was asked
                    # for, instead of silently grounding on stale documents.
                    try:
                        cached = json.loads(manifest_path.read_text(encoding="utf-8"))
                        cached_paths = set(cached.get("source_dirs", []))
                        cached_extensions = set(cached.get("extensions", []))
                        requested_paths = {
                            str(Path(p).expanduser().resolve()) for p in kb["paths"]
                        }
                        needs_rebuild = (
                            cached_paths != requested_paths
                            or cached_extensions != set(indexable)
                        )
                    except (OSError, ValueError):
                        needs_rebuild = True
                if needs_rebuild:
                    metadata = build_skill(
                        kb["paths"],
                        kb["name"],
                        kb["output_root"],
                        kb["description"],
                        include_extensions=indexable,
                        overwrite=True,
                    )
                    skill_dir = Path(metadata["skill_dir"])
                tools.append(
                    skill_tools(
                        skill_dir=str(skill_dir),
                        name=self._slug(kb["name"]).replace("-", "_"),
                        description=kb["description"],
                    )[0]
                )
                continue

            texts = [
                read_folder_docs(path, extensions=kb["extensions"])
                for path in kb["paths"]
            ]
            combined = "\n\n".join(texts)[: kb["max_chars"]]
            context_sections.append(f"## {kb['name']}\n\n{combined}")

        return tools, "\n\n".join(context_sections)

    def _derive(self, agent: Agent, *, tools: list[Any] | None = None, **overrides) -> Agent:
        if self.session is not None and "session" not in overrides:
            overrides["session"] = self.session
        return agent.derive(tools=tools, **overrides)

    @staticmethod
    def _result_text(result, stage: str) -> str:
        if result.error is not None:
            raise RuntimeError(f"{stage} failed: {result.error.type}: {result.error.message}")
        text = result.text().strip()
        if not text:
            raise RuntimeError(f"{stage} returned an empty result.")
        return text

    def _default_memory_summarizer(self) -> Agent:
        """Cheap, non-reasoning DeepSeek agent used to compress debate memory.

        Called repeatedly (once per compression event) and shared across
        every debater's Memory and the moderator's, so its own memory is
        capped to 0 retained turns: each call must be stateless, or one
        member's compression request would leak into another's, or the
        moderator's, as unrelated prior "conversation" history.
        """
        return Agent(
            engine=LLMEngine(
                "super_cheap",
                provider="deepseek",
                thinking=False,
                system="Summarize conversations concisely.",
                max_turns=1,
            ),
            memory=Memory(strategy="none", max_turns=0),
            name="council_memory_summarizer",
        )

    def _default_moderator(self) -> Agent:
        return Agent(
            engine=LLMEngine(
                "top",
                provider="deepseek",
                system=(
                    "You moderate a spontaneous council discussion. Listen for real "
                    "convergence and material dissent. Check the vote state, invite the "
                    "most relevant next voice, and close only when quorum is genuine."
                ),
                max_turns=5,
                max_tool_calls_per_turn=1,
                thinking=self.reasoning,
            ),
            name="moderator",
            session=self.session,
        )

    def _default_synthesiser(self) -> Agent:
        return Agent(
            engine=LLMEngine(
                "medium",
                provider="anthropic",
                system=(
                    "You are a council scribe. Produce a decision-ready report with: "
                    "Council Recommendation, Key Findings, Agreement, Disagreement, "
                    "Recommendation, Quorum, and Confidence. Do not claim unanimity "
                    "when dissent remains."
                ),
                max_turns=3,
                thinking=self.reasoning,
            ),
            name="synth",
            session=self.session,
        )

    def run(self, question: str | None = None) -> CouncilResult:
        q = self.question if question is None else question
        if not q.strip():
            raise ValueError("question is required.")
        if len(self._members) < 2:
            raise ValueError("Need at least 2 council members.")

        kb_tools, kb_context = self._prepare_knowledge()
        members = [self._derive(agent, tools=kb_tools) for agent in self._members]
        researchers = [self._derive(agent, tools=kb_tools) for agent in self._researchers]
        knowledge = f"\n\nGROUNDED KNOWLEDGE:\n{kb_context}" if kb_context else ""

        research_result = Agent.parallel(
            *researchers, name="research", session=self.session
        )(
            f"QUESTION: {q}\n\nResearch independently. Distinguish evidence, "
            f"assumptions, and uncertainty.{knowledge}"
        )
        research = self._result_text(research_result, "research")

        opinions_result = Agent.parallel(
            *members, name="opinions", session=self.session
        )(
            f"QUESTION: {q}\n\nState your opening position for the council. "
            f"Be candid about uncertainty and what could change your mind.\n\n"
            f"RESEARCH:\n{research}{knowledge}"
        )
        opinions = self._result_text(opinions_result, "opinions")

        vote_store = Store()
        member_names = [member.name for member in members]
        memory_summarizer = self._memory_summarizer or self._default_memory_summarizer()

        # Single, non-duplicated record of the debate, visible to every
        # participant via sources=[...] (a live, re-read-every-call view —
        # see Memory's "shared use" mode). Bounded the same way private
        # memories are: Memory.text() only ever renders a running summary
        # plus the last 5 exchanges, regardless of how long the debate
        # runs, so sharing it doesn't reopen the unbounded-growth problem.
        # Debaters no longer need to recap each other from memory — they
        # can see the real thing — which is what the previous per-agent-
        # private-memory design forced them to do, at real token cost.
        history = Memory(strategy="summary", summarizer=memory_summarizer)
        transcript_log: list[dict] = []

        def latest_votes() -> dict[str, dict]:
            latest: dict[str, dict] = {}
            for _, value in vote_store.items(prefix="vote:"):
                if not isinstance(value, dict) or value.get("member") not in member_names:
                    continue
                previous = latest.get(value["member"])
                if previous is None or value["timestamp"] > previous["timestamp"]:
                    latest[value["member"]] = value
            return latest

        def quorum_status() -> tuple[bool, dict[str, dict]]:
            votes = latest_votes()
            aligned = sum(bool(v["aligned"]) for v in votes.values())
            return aligned / len(members) >= self.quorum, votes

        def check_quorum() -> str:
            """Inspect the latest council votes and report whether quorum exists."""
            reached, votes = quorum_status()
            aligned = sum(bool(v["aligned"]) for v in votes.values())
            lines = [
                f"{v['member']}: {'aligned' if v['aligned'] else 'not aligned'} "
                f"(confidence={v['confidence']:.2f}) — {v['rationale']}"
                for v in votes.values()
            ]
            detail = "\n".join(lines) if lines else "No votes recorded."
            return (
                f"{aligned}/{len(members)} members aligned; {len(votes)} voted; "
                f"threshold={self.quorum:.0%}; "
                f"{'QUORUM REACHED' if reached else 'quorum not reached'}.\n{detail}"
            )

        def close_discussion(summary: str) -> str:
            """Close the council only when the latest votes satisfy quorum."""
            reached, _ = quorum_status()
            if not reached:
                return "Cannot close: quorum is not reached. Invite more discussion or votes."
            return conclude(summary)

        depth_limit = self.max_depth or max(10, self.max_rounds * len(members) + 5)
        pool = AgentPool(max_depth=depth_limit)
        roster = ", ".join(member_names)
        protocol = (
            "COUNCIL PROTOCOL — This is an unscripted discussion, not ordered rounds. "
            f"Participants: {roster}; moderator. Use route(agent_name, task) to invite "
            "the person whose perspective is most relevant next. Challenge assumptions, "
            "respond to what others actually said, and revisit your view freely. Use "
            "cast_vote whenever your position changes or becomes clearer. aligned=true "
            "means you can endorse the emerging recommendation without a material "
            "reservation. Route to the moderator when the council may be ready to close. "
            "Only the moderator can close the discussion. Do not follow a fixed order. "
            "DEBATE HISTORY below shows the exchanges so far and stays up to date "
            "automatically — do not restate or summarize it yourself; read it and "
            "respond directly to the latest points."
        )

        memories = [
            Memory(strategy="summary", summarizer=memory_summarizer) for _ in members
        ]
        debaters: list[Agent] = []

        def make_route_tool(caller_name: str) -> Tool:
            async def route(agent_name: str, task: str) -> str:
                """Delegate task to the named specialist agent and return its answer.

                The exchange is recorded to the shared debate history so every
                participant can see it without you needing to recap it yourself.
                """
                response = await pool.route(agent_name, task)
                history.add(f"{caller_name} -> {agent_name}: {task}", f"{agent_name}: {response}")
                transcript_log.append({"member": agent_name, "text": response})
                return response

            return Tool.wrap(route, name="route")

        def make_vote_tool(member_name: str) -> Tool:
            def cast_vote(
                aligned: bool,
                confidence: float,
                rationale: str,
                recommendation: str,
            ) -> str:
                """Record or update your current vote on the emerging recommendation."""
                if not 0.0 <= confidence <= 1.0:
                    return "Vote rejected: confidence must be between 0.0 and 1.0."
                if not rationale.strip() or not recommendation.strip():
                    return "Vote rejected: rationale and recommendation are required."
                timestamp = time.time_ns()
                vote_store.write(
                    f"vote:{member_name}:{timestamp}",
                    {
                        "member": member_name,
                        "aligned": aligned,
                        "confidence": confidence,
                        "rationale": rationale.strip(),
                        "recommendation": recommendation.strip(),
                        "timestamp": timestamp,
                    },
                )
                return f"Vote updated for {member_name}: aligned={aligned}."

            return Tool.wrap(cast_vote, name="cast_vote")

        for member, memory in zip(members, memories, strict=True):
            debaters.append(
                self._derive(
                    member,
                    tools=[make_route_tool(member.name), make_vote_tool(member.name)],
                    memory=memory,
                    sources=[*member.sources, protocol, history],
                )
            )

        moderator_base = self._moderator or self._default_moderator()
        moderator = self._derive(
            moderator_base,
            tools=[
                *kb_tools,
                make_route_tool("moderator"),
                Tool.wrap(check_quorum, name="check_quorum"),
                Tool.wrap(close_discussion, name="close_discussion"),
            ],
            name="moderator",
            memory=Memory(strategy="summary", summarizer=memory_summarizer),
            sources=[*moderator_base.sources, protocol, history],
        )
        pool.register(*debaters, moderator)

        opening_result = debaters[0](
            f"QUESTION: {q}\n\nSHARED RESEARCH:\n{research}\n\n"
            f"OPENING POSITIONS:\n{opinions}\n\n"
            "Open the council discussion naturally. Address the most consequential "
            "claim, update your vote when ready, and route to whoever should respond next."
        )
        discussion_outcome = self._result_text(opening_result, "discussion")
        history.add("(council opens the debate)", f"{debaters[0].name}: {discussion_outcome}")
        transcript_log.insert(0, {"member": debaters[0].name, "text": discussion_outcome})

        # transcript_log was appended to in real time as route() calls
        # resolved, so it's already in true chronological order — unlike
        # reconstructing it after the fact from each agent's own memory,
        # which only reflects that agent's own turn order, not the debate's.
        transcript = transcript_log

        quorum_reached, latest = quorum_status()
        votes = []
        for member_name in member_names:
            if member_name not in latest:
                continue
            vote = dict(latest[member_name])
            vote.pop("timestamp", None)
            votes.append(vote)

        vote_report = "\n".join(
            f"- {vote['member']}: {'aligned' if vote['aligned'] else 'not aligned'} "
            f"(confidence {vote['confidence']:.2f}) — {vote['rationale']}\n"
            f"  Recommendation: {vote['recommendation']}"
            for vote in votes
        ) or "No structured votes were recorded."
        transcript_report = "\n".join(
            f"- {turn['member']}: {turn['text']}" for turn in transcript
        )

        synth_base = self._synthesiser or self._default_synthesiser()
        synthesiser = self._derive(synth_base, tools=kb_tools, output=str, name="synth")
        synthesis_result = synthesiser(
            f"QUESTION: {q}\n\nRESEARCH:\n{research}\n\n"
            f"OPENING POSITIONS:\n{opinions}\n\n"
            f"DISCUSSION OUTCOME:\n{discussion_outcome}\n\n"
            f"LATEST VOTES:\n{vote_report}\n\nTRANSCRIPT:\n{transcript_report}\n\n"
            f"QUORUM: {'reached' if quorum_reached else 'not reached'} "
            f"(threshold {self.quorum:.0%}). Write the final council report.{knowledge}"
        )
        synthesis = self._result_text(synthesis_result, "synthesis")

        return CouncilResult(
            question=q,
            quorum_reached=quorum_reached,
            votes=votes,
            transcript=transcript,
            synthesis=synthesis,
        )


def standard_council(
    question: str = "", *, reasoning: bool = False, **kwargs
) -> WizengAImot:
    """Return a ready-made four-member, multi-provider council.

    ``reasoning`` enables extended thinking on the four debating members
    (not their researchers, which stay fast/cheap) and on the default
    moderator and synthesiser.
    """
    gpt_search = Agent(
        engine=LLMEngine(
            "super_cheap",
            provider="openai",
            system="Search the web and return a concise, sourced summary.",
            max_turns=4,
        ),
        native_tools=[NativeTool.WEB_SEARCH],
        name="gpt_web_search",
    ).as_tool("web_search", description="Search the web via OpenAI.")

    def make(
        name: str,
        council_provider: str,
        research_provider: str,
        research_tier: str,
        system: str,
        *,
        search: bool = True,
        extra_tools: list[Any] | None = None,
    ) -> tuple[Agent, Agent]:
        member = Agent(
            engine=LLMEngine(
                "medium",
                provider=council_provider,
                system=system,
                max_turns=6,
                thinking=reasoning,
            ),
            name=name,
        )
        researcher = Agent(
            engine=LLMEngine(
                research_tier,
                provider=research_provider,
                system=f"Research assistant for {name}.",
                max_turns=8,
            ),
            tools=extra_tools or [],
            native_tools=[NativeTool.WEB_SEARCH] if search else None,
            name=f"{name}_researcher",
        )
        return member, researcher

    pairs = [
        make(
            "optimist", "anthropic", "anthropic", "cheap",
            "Optimistic strategist. Find opportunities without hiding risks.",
        ),
        make(
            "devil_advocate", "openai", "openai", "super_cheap",
            "Devil's advocate. Surface risks and weak assumptions.",
        ),
        make(
            "analyst", "google", "google", "cheap",
            "Data-driven analyst. Ground conclusions in evidence.",
        ),
        make(
            "visionary", "deepseek", "openai", "super_cheap",
            "Visionary. Identify long-term and second-order implications.",
            search=False,
            extra_tools=[gpt_search],
        ),
    ]

    council = WizengAImot(question, reasoning=reasoning, **kwargs)
    for member, researcher in pairs:
        council.add(member, researcher=researcher)
    return council


_REASONING_LEVELS = ("low", "medium", "high", "xhigh", "max")


def _reasoning_settings(level: str) -> tuple[bool | str, str, str]:
    """Map one external reasoning level to each engine's own vocabulary.

    Returns ``(deepseek_thinking, claude_reasoning_effort, claude_thinking)``.
    DeepSeek's ``thinking`` only distinguishes off/on here (no graduated
    scale is used elsewhere in this codebase); Claude's ``reasoning_effort``
    passes the level straight through, since ``ClaudeCodeEngine`` supports
    the full scale natively.
    """
    if level not in _REASONING_LEVELS:
        raise ValueError(f"reasoning must be one of {_REASONING_LEVELS}, got {level!r}.")
    deepseek_thinking: bool | str = False if level == "low" else "max"
    claude_thinking = "disabled" if level == "low" else "adaptive"
    return deepseek_thinking, level, claude_thinking


def deepseek_claude_news_council(
    question: str = "",
    *,
    news_db: str | Path | None = None,
    reasoning: str = "medium",
    **kwargs,
) -> WizengAImot:
    """Build the DeepSeek + Claude Code subscription council.

    Composition:

    - two DeepSeek V4 Flash analysts with reasoning disabled;
    - one Claude Haiku analyst via Claude Agent SDK with reasoning disabled;
    - one DeepSeek V4 Flash debater with maximum reasoning;
    - one Claude Sonnet debater via Claude Agent SDK with medium reasoning.

    Every member, moderator, and synthesiser receives the same LazyCrawler
    tools backed by the same current-news database. Claude uses the local
    Claude Code/Claude.ai subscription login, never ``ANTHROPIC_API_KEY``.

    ``reasoning`` (one of ``"low"``, ``"medium"``, ``"high"``, ``"xhigh"``,
    ``"max"``) controls the debater, moderator, and synthesiser only — the
    two fast evidence/risk analysts always run with reasoning disabled,
    by design.

    Requires current LazyBridge with the ``claude-code`` extra, LazyTools with
    the ``web`` extra, an authenticated Claude Code CLI, ``DEEPSEEK_API_KEY``,
    and either ``news_db=...`` or ``LAZYCRAWLER_NEWS_DB``.
    """
    deepseek_thinking, claude_reasoning_effort, claude_thinking = _reasoning_settings(
        reasoning
    )

    try:
        from lazybridge import ClaudeCodeEngine
        from lazycrawler import CrawlerDB, CrawlerTools, DBConfig, LLMConfig
        from lazycrawler.config import resolve_news_db_path

        from lazytools.connectors.web import WebTools
    except ImportError as exc:
        raise ImportError(
            "This preset requires current lazybridge[claude-code] and "
            "lazytoolkit[web] with LazyCrawler."
        ) from exc

    resolved_news_db = resolve_news_db_path(str(news_db) if news_db else None)
    if not resolved_news_db:
        raise FileNotFoundError(
            "Current news database is not configured. Pass news_db=... or set "
            "LAZYCRAWLER_NEWS_DB to its absolute path."
        )
    news_path = Path(resolved_news_db).expanduser().resolve()
    if not news_path.is_file():
        raise FileNotFoundError(f"Current news database not found: {news_path}")

    crawler = CrawlerTools(
        db=CrawlerDB(DBConfig(db_path=str(news_path))),
        llm_cfg=LLMConfig(model="deepseek-v4-flash"),
        content="smart",
        links="pure",
    )
    shared_tools = WebTools(provider=crawler).as_tools()

    def deepseek_agent(
        name: str,
        system: str,
        *,
        thinking: bool | str,
        max_turns: int = 8,
    ) -> Agent:
        return Agent(
            engine=LLMEngine(
                "deepseek-v4-flash",
                provider="deepseek",
                # str thinking values ("max", "adaptive", ...) need a LazyBridge
                # release beyond the 1.0.1 currently on PyPI; LLMEngine.thinking
                # is still typed `bool` there. Same floor issue as ClaudeCodeEngine.
                thinking=thinking,  # type: ignore[arg-type]
                system=system,
                max_turns=max_turns,
            ),
            tools=shared_tools,
            name=name,
        )

    deepseek_evidence = deepseek_agent(
        "deepseek_evidence_analyst",
        "Evidence analyst. Establish facts, dates, sources, and uncertainty.",
        thinking=False,
    )
    deepseek_risk = deepseek_agent(
        "deepseek_risk_analyst",
        "Risk analyst. Test assumptions, downside cases, and missing evidence.",
        thinking=False,
    )
    claude_haiku = Agent(
        engine=ClaudeCodeEngine(
            model="haiku",
            reasoning_effort=None,
            thinking="disabled",
            web=True,
            system=(
                "Fast evidence analyst. Use the shared crawler and news database; "
                "extract relevant facts without extended reasoning."
            ),
            max_turns=8,
        ),
        tools=shared_tools,
        name="claude_haiku_analyst",
    )
    deepseek_debater = deepseek_agent(
        "deepseek_max_debater",
        "Senior debater. Integrate the evidence, challenge weak claims, and revise openly.",
        thinking=deepseek_thinking,
        max_turns=12,
    )
    claude_debater = Agent(
        engine=ClaudeCodeEngine(
            model="sonnet",
            reasoning_effort=claude_reasoning_effort,
            thinking=claude_thinking,
            web=True,
            system=(
                "Senior final-stage debater. Integrate the full discussion, challenge "
                "remaining uncertainty, and seek a defensible recommendation."
            ),
            max_turns=12,
        ),
        tools=shared_tools,
        name="claude_sonnet_debater",
    )

    moderator = deepseek_agent(
        "news_council_moderator",
        "Neutral moderator. Preserve dissent, check genuine convergence, and avoid premature closure.",
        thinking=deepseek_thinking,
        max_turns=10,
    )
    synthesiser = Agent(
        engine=ClaudeCodeEngine(
            model="sonnet",
            reasoning_effort=claude_reasoning_effort,
            thinking=claude_thinking,
            web=True,
            system=(
                "Council scribe. Produce a sourced, decision-ready synthesis that "
                "distinguishes facts, interpretations, agreement, and dissent."
            ),
            max_turns=10,
        ),
        tools=shared_tools,
        name="news_council_synth",
    )

    council = WizengAImot(
        question,
        moderator=moderator,
        synthesiser=synthesiser,
        **kwargs,
    )
    for member in (
        deepseek_evidence,
        deepseek_risk,
        claude_haiku,
        deepseek_debater,
        claude_debater,
    ):
        council.add(member)
    return council
