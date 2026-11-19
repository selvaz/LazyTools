# WizengAImot council skill

WizengAImot is a multi-LLM council. Several models deliberate before an answer
ships, and one orchestrator decides. LazyTools ships it in two forms.

## As a Claude Code skill

A bundled agent skill (`SKILL.md`) that turns Claude Code into the
orchestrator of a round-based discussion with two external participants:
Codex (`codex_ask`) and another Claude (`claude_ask`), reached through the
LazyTools MCP server.

- Round 1: both participants comment blind, then each cross-examines the other.
- Later rounds swap who speaks first.
- Every change of position must be labelled; the orchestrator may overrule
  both participants but must record each override in the minutes.

Install:

```bash
python -c "from lazytools.skills.agent_skills import install_agent_skill; install_agent_skill('wizengaimot')"
```

Add `overwrite=True` to update an existing copy. Then run `/wizengaimot` in
Claude Code. It needs `lazytools-mcp --allow-unsafe code_review claude_review`
registered in Claude Code: without `--allow-unsafe` the `codex_ask` and
`claude_ask` tools are not exposed. They start Codex and Claude Code with read
access to the repository you point them at.

## As a Python process

`lazytools.skills.council.standard_council()` builds a four-member,
multi-provider council on LazyBridge agents (Anthropic, OpenAI, Google and DeepSeek
members by default; any LazyBridge provider works). See
[skills-council.md](skills-council.md).

The Python council is free-form rather than round-based. Its `max_exchanges`
argument bounds all routed exchanges. Only its moderator can call
`propose_motion`; members call `cast_vote` with that motion's identifier, and
`CouncilResult.motion` exposes the current or winning motion. These Python API
controls are distinct from the Claude Code skill's `max_rounds` protocol.
