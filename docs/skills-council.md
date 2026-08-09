# Spontaneous AI councils

`lazytools.skills.council` provides a free-form multi-agent council built on
LazyBridge `AgentPool`. It is intended for questions where several independent
perspectives should challenge one another before a recommendation is written.

Unlike a fixed debate pipeline, the council does not schedule speakers or
manufacture rounds. Members choose whom to engage next, may update their vote
at any point, and invite the moderator when they believe the discussion can
close. Only the moderator receives the closing tool, and that tool refuses to
close until the configured quorum is actually present.

## Generic council

```python
from lazybridge import Agent, LLMEngine
from lazytools.skills import WizengAImot

optimist = Agent(
    name="optimist",
    engine=LLMEngine(
        "medium",
        provider="anthropic",
        system="Find opportunities without concealing risks.",
    ),
)
critic = Agent(
    name="critic",
    engine=LLMEngine(
        "medium",
        provider="deepseek",
        system="Test assumptions and surface material downside.",
    ),
)

result = (
    WizengAImot("Should we enter this market?", quorum=0.7)
    .add(optimist)
    .add(critic)
    .run()
)
print(result.text())
```

Research and opening positions use `Agent.parallel`. The discussion itself is
an `AgentPool`: participants call `route(agent_name, task)` based on the
conversation rather than a predetermined order.

## Reasoning level

Every member's engine is your own `Agent`, so you control its reasoning
directly (`LLMEngine(thinking=...)`, `ClaudeCodeEngine(reasoning_effort=...)`).
For the *default* moderator and synthesiser — used whenever you don't pass
`moderator=`/`synthesiser=` yourself — `WizengAImot(..., reasoning=True)`
enables extended thinking on both. It's ignored once you supply your own
moderator/synthesiser agents; configure their engines directly instead.

`standard_council(..., reasoning=True)` forwards the same switch to its four
built-in members (not their researchers, which stay fast/cheap) and to the
default moderator/synthesiser.

`deepseek_claude_news_council(..., reasoning="high")` takes a graduated level
— `"low"`, `"medium"` (default), `"high"`, `"xhigh"`, or `"max"` — applied to
the debater, moderator, and synthesiser only; the two fast evidence/risk
analysts always run with reasoning disabled, by design. DeepSeek's `thinking`
only distinguishes off (`"low"`) from on (everything else); Claude's
`reasoning_effort` receives the level directly.

## DeepSeek + Claude subscription news council

The current-news preset combines API-backed DeepSeek models with Claude Code
agents authenticated through a Claude.ai subscription:

```python
from lazytools.skills import deepseek_claude_news_council

council = deepseek_claude_news_council(
    "How could the latest geopolitical developments affect European energy?",
    news_db="/data/news.db",
)
result = council.run()
```

The roster is:

| Role | Engine | Reasoning |
| --- | --- | --- |
| Evidence analyst | DeepSeek V4 Flash | disabled |
| Risk analyst | DeepSeek V4 Flash | disabled |
| Fast analyst | Claude Code Haiku | disabled |
| Senior debater | DeepSeek V4 Flash | `max` |
| Senior debater | Claude Code Sonnet | `medium`, adaptive thinking |
| Moderator | DeepSeek V4 Flash | `max` |
| Synthesiser | Claude Code Sonnet | `medium`, adaptive thinking |

The debater/moderator/synthesiser rows reflect the default `reasoning="medium"`
— pass `reasoning="low"`/`"high"`/`"xhigh"`/`"max"` to `deepseek_claude_news_council`
to change all four at once; the two analyst rows never change (see
[Reasoning level](#reasoning-level)).

Every participant receives the same LazyCrawler tool set backed by the same
SQLite news database. Claude Code agents additionally have native
`WebSearch`/`WebFetch` enabled.

### Requirements

Install current LazyBridge with Claude Code support and LazyTools with web
support:

```bash
pip install "lazybridge[claude-code]"
pip install "lazytoolkit[web] @ git+https://github.com/selvaz/LazyTools.git"
```

!!! warning "ClaudeCodeEngine release status"

    `ClaudeCodeEngine` is merged to LazyBridge `main` but not yet in a
    tagged PyPI release as of this writing (the latest tag, `1.0.2`,
    predates it). Until a release ships, install LazyBridge from source —
    e.g. `pip install "lazybridge[claude-code] @ git+https://github.com/selvaz/LazyBridge.git@main"`
    — or pin to whichever tag first includes it once released.

Configure DeepSeek normally:

```bash
export DEEPSEEK_API_KEY="..."
```

For Claude, do **not** set `ANTHROPIC_API_KEY` when subscription billing is
intended. Install Claude Code, run `claude`, and choose the Claude App / Claude.ai
subscription login. `ClaudeCodeEngine` then reuses that local login through the
Claude Agent SDK.

Set the news database path or pass it explicitly:

```bash
export LAZYCRAWLER_NEWS_DB="/absolute/path/news.db"
```

The preset fails loudly when the database is missing; it never replaces the
requested current-news source with an empty in-memory cache.

!!! note "Haiku availability"

    Claude Code exposes the `haiku` alias through the Agent SDK, but actual
    availability depends on the installed Claude Code version and subscription.
    Validate it with the target account before production use.

## Debate memory

A free-form debate has no fixed number of turns — `route()` hops until the
moderator closes it. Left uncompressed, that can push the final synthesis
call past the model's context window on a long debate. Every debater's
memory and the moderator's memory therefore run
`Memory(strategy="summary", summarizer=...)`, compressing older turns once
past 10 while keeping the last 10 verbatim. The summarizer defaults to a
cheap, non-reasoning DeepSeek agent (`WizengAImot._default_memory_summarizer`);
pass your own with `memory_summarizer=`:

```python
from lazybridge import Agent, LLMEngine

fast_summarizer = Agent(
    engine=LLMEngine("super_cheap", provider="deepseek", thinking=False),
    name="summarizer",
)
council = WizengAImot(question, memory_summarizer=fast_summarizer)
```

## Knowledge bases

Use `knowledge(..., mode="static")` for direct document context or
`mode="skill"` for a BM25 documentation bundle:

```python
council.knowledge("./briefing", name="briefing", mode="skill")
```

Static mode supports text, PDF, DOCX, and HTML through LazyTools document
readers. Skill mode indexes text-oriented formats and exposes the resulting
retriever to every participant.

## Result contract

`CouncilResult` contains:

- `synthesis`: final decision-ready report;
- `quorum_reached`: whether the latest votes meet the configured threshold;
- `votes`: latest structured vote from each member that voted;
- `transcript`: assistant contributions captured from council memories;
- `question`: the original question.

The recursion `max_depth` is a safety brake, not a debate schedule. The legacy
`max_rounds` option is retained only to derive a default depth when
`max_depth` is omitted.

## API reference

::: lazytools.skills.council.WizengAImot

::: lazytools.skills.council.CouncilResult

::: lazytools.skills.council.standard_council

::: lazytools.skills.council.deepseek_claude_news_council
