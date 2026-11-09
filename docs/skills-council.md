# WizengAImot

Difficult choice? Summon the WizengAImot. It brings independent perspectives
into a discussion, challenges assumptions, and records the recommendation and
remaining dissent. Choose the live Claude Code skill for interactive work, or
the Python council for scripts and pipelines.

## Claude Code skill

Install the two bundled skills with LazyBridge 1.8.7 or newer:

```bash
python -m lazytools.skill install --to claude
```

Use `--to codex` or `--to all` to install to those assistant homes; omit
`--to` to use the detected homes. `--dest PATH` installs LazyTools at PATH and
WizengAImot in the adjacent `wizengaimot` directory. Configure the LazyTools
MCP server with `codex_ask` and `claude_ask` as described in the
[MCP server setup](mcp-server.md). In Claude Code invoke `/wizengaimot` and
provide the work to debate. The user can reassign the default Codex and Claude
seats and their effort or models.

The orchestrator drafts the work, then both seats independently critique it in
parallel. They cross-examine each other, with alternating speaker order in later
rounds. By default the discussion has at most three rounds. The orchestrator
returns the final artifact and a **verbale** recording each round's proposals,
decisions, overrides and surviving dissent. If a seat fails, the skill retries
once with reconstructed context and records any reduced participation.
The draft and discussion go to both external engines: do not include secrets.

## Python process

The Python council uses LazyBridge agents for research, opening positions, and
an unscripted AgentPool discussion. Configure the provider credentials needed by
the preset before running:

```python
from lazytools.skills import standard_council

council = standard_council("Should we enter this market?", quorum=0.7)
result = council.run()
print(result.text())
print("Quorum:", result.quorum_reached, "Winning motion:", result.motion)
```

During discussion, the moderator puts a recommendation to a vote with the tool
`propose_motion(text)`, which returns an ID such as `motion:1`; members suggest
or amend motions by routing to the moderator. Members use
`cast_vote(motion_id, aligned, confidence, rationale)` to vote on that current
motion. For example, a member might call `cast_vote("motion:1", True, 0.9,
"The evidence supports a pilot")`. A new motion makes earlier votes irrelevant
to quorum, and stale votes are rejected. The moderator can call `check_quorum()`
and `close_discussion(summary)`; closing is refused until the fraction of all
members aligned on the current motion reaches the threshold. No motion means no
quorum. Two limits keep a run bounded: `max_depth` caps how long one chain of
routes can get, and `max_exchanges` caps the total number of routed exchanges
(default `max(8, 2 * max_rounds * members)`). When the budget is used up,
further routes are refused and members are told to vote. When the free debate
ends without quorum, a closing vote runs: the moderator states the motion that
best captures the debate and every member votes on it once.

`CouncilResult` provides `question`, `quorum_reached`, `motion` (winning text
or `None`), `votes`, `transcript`, and `synthesis`; `text()` returns the
synthesis. The latest votes may include votes on earlier motions, identified by
`motion_id`. Custom members, a moderator, a synthesiser, and knowledge can be
provided through `WizengAImot`.

| | Skill | Python process |
| --- | --- | --- |
| Orchestration | Claude Code session | LazyBridge AgentPool |
| When to use | Interactive review of a draft or decision | Scripts, applications, and pipelines |
| Output | Final artifact and verbale | `CouncilResult` with synthesis, votes, and transcript |

## API reference

::: lazytools.skills.council.WizengAImot

::: lazytools.skills.council.CouncilResult

::: lazytools.skills.council.standard_council

::: lazytools.skills.council.deepseek_claude_news_council
