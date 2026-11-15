---
name: wizengaimot
description: |
  Run a Wizengaimot: a structured, round-based discussion between this
  Claude Code session (the orchestrator) and two senior external agents —
  Codex (codex_ask) and another Claude (claude_ask) — to pressure-test a
  piece of work before finalizing it. Use when the user invokes
  /wizengaimot, or asks to "discuss with the other agents", "convene the
  council", "ask the other two what they think", or wants a
  deliverable (code, implementation plan, design, analysis, answer)
  debated by multiple high-level models before it ships. Same spirit as the
  multi-provider Python council in lazytools.skills.council, orchestrated
  live from Claude Code over the LazyTools MCP consultants.
---

# Wizengaimot — structured three-agent deliberation

You are the **orchestrator**. The two **participants** are reached through the
LazyTools MCP server:

| Seat | Tool | Default configuration |
|---|---|---|
| A | `codex_ask` | the server's default Codex model, `effort="medium"` |
| B | `claude_ask` | `model="opus"` |

The user can reassign either seat's model/effort per invocation ("this time
Codex high and Sonnet") — pass the overrides on every call for that discussion.
If the live tool schema has no `model`/`effort`/`thinking` parameters, the MCP
server is older than LazyTools v0.10 or hasn't been restarted since: proceed with
the server defaults and tell the user why the override was skipped.

## Prerequisites

- The LazyTools MCP server (`lazytools-mcp`) registered in Claude Code, with the
  `codex_ask` and `claude_ask` tools available (a logged-in Codex CLI and
  Claude Code CLI on this machine).
- Install this skill with
  `python -c "from lazytools.skills.agent_skills import install_agent_skill; install_agent_skill('wizengaimot')"`
  (writes `~/.claude/skills/wizengaimot/SKILL.md`), then invoke `/wizengaimot`.
- Without Claude Code, the same idea runs as plain Python:
  `from lazytools.skills.council import standard_council` (a multi-provider
  council on LazyBridge agents; see docs/skills-council.md).

## The protocol

**Step 0 — Draft.** Produce version 1 of the artifact yourself: the code, the
implementation plan, the analysis, or the answer the user asked for. This is
what goes to discussion — never send the participants a bare open question
when the protocol calls for a position to attack.

Each round has a **first speaker** and a **second speaker**. In round 1 the
first speaker is A (Codex) unless the user says otherwise; **the roles swap
every round** — the structure gives the first speaker both the opening and
the closing word, and alternation is what keeps that advantage from
compounding across the discussion.

**Round 1 — independent opinions, then cross-examination (4 calls):**

1. **You** put the artifact up for discussion.
2. **Blind comments, in parallel**: send A and B the artifact in the SAME
   message batch (both MCP calls in one go — they run concurrently, halving
   the wait). Each gets the artifact and nothing of the other: concrete
   proposed changes, each with its motivation. No motivation, no proposal.
   The independence is the point — two anchored critiques are worth less
   than one honest one.
3. **First speaker cross-examines**: send the first speaker (same thread)
   the second speaker's comments verbatim; ask for an explicit position on
   each proposal (agree / disagree / extend, and why).
4. **Second speaker cross-examines**: send the second speaker (same thread)
   the first speaker's comments **and** its step-3 positions; same request,
   plus replies where the first speaker disagreed.
5. **You decide** — see "Close of round" below.

**Rounds ≥ 2 — the standard exchange (3 calls, roles swapped):**

1. **You** put the enriched artifact version up for discussion.
2. **First speaker comments**: proposed changes with motivations, in light of
   what its thread already knows.
3. **Second speaker comments**: gets the new version and the first speaker's
   comments verbatim; own proposals plus explicit positions on each of the
   first speaker's.
4. **First speaker replies**: gets the second speaker's comments; replies to
   the positions — especially the disagreements.
5. **You decide** — see below.

**Close of round — three outcomes:**

- **Continue** — real disagreements or open questions remain and another
  round looks profitable: integrate what you accept into a new artifact
  version, note what you reject and why, swap the speaker roles, and start
  the next round.
- **Converge** — you have enough: write the final version. Done.
- **Stall** — `max_rounds` reached without convergence: write the final
  version anyway, deciding the contested points yourself, and record the
  surviving disagreements in the report. This is a partial success, not a
  failure — say so plainly.

`max_rounds` defaults to **3**; honor a user-supplied value. The counter
exists because marginal returns fall fast — do not talk yourself into a
fourth round "just to be sure" unless the user raised the cap.

## Rules of engagement

- **Open every participant call with the committee charter.** Include this
  framing (adapt wording, keep the substance) in EVERY prompt to A and B:
  *"You are a full and equal member of this deliberation committee — not an
  assistant reviewing someone's work. Maintain an independent view. Agree
  only when genuinely convinced: conceding to be agreeable corrupts the
  process and is worse than useless. When you disagree, say so explicitly
  and argue it — sustained, reasoned dissent is a contribution, not a
  failure. When you change position, label it explicitly ('I concede X
  because Y'); when you keep it, label that too ('I hold on X because Y').
  Absorbing the other's point silently into an 'extension' without
  acknowledging the change is not allowed."*
  The last clause matters in both directions: it catches the seat that
  concedes too easily AND the seat that never admits a concession while
  quietly incorporating the other's arguments.
- **Threads are the memory.** Capture `thread_id=` (A) and `session_id=` (B)
  from the first reply and pass them back on *every* subsequent call of the
  discussion, across rounds. A thread belongs to the repository it was opened
  on. Losing a handle means paying a cold re-read — don't.
- **Every call self-contained.** Participants share none of your conversation
  context. Each message states: what is being built and for whom, the current
  artifact version in full (or a diff against what that same thread already
  saw), what the other participant said, and what you want back. Quote the
  other's arguments faithfully — the debate only works if positions travel
  undistorted. When a comment set is too long to quote whole, compress it
  but keep its numbered structure and every claim's substance, and label the
  relay "summary, faithful" so the recipient knows it is your compression,
  not the original text.
- **Demand motivations.** A proposal without a reason is noise; say so in the
  prompt and discard unmotivated proposals when integrating.
- **You are a participant too.** Integrate on the merits, not by majority:
  two agents agreeing does not make them right, and you may overrule both —
  with a recorded reason. You are also judge and party (you wrote the draft
  you are defending), so every overrule of a position BOTH participants
  shared must appear in the report under an explicit **"Orchestrator
  override"** marker — the user eyeballs exactly those first.
- **Evidence.** Both participants can read the repository and the web
  themselves (Codex: native web search + LazyCrawler dynamic tools; Claude:
  WebSearch/WebFetch + LazyCrawler tools). When a point of fact should be
  settled on *shared* sources, fetch the material yourself and paste it into
  both prompts instead of letting each agent find its own version of reality.
- **repo_path.** Point both tools at the repository the discussion is about;
  for non-code topics use the workspace root and say the repo is incidental.

## Output — always both pieces

1. **The final artifact** — the thing itself, ready to use.
2. **The report (minutes)** — per round: each participant's substantive
   proposals, which were accepted/rejected/deferred and why, and how the
   artifact changed. Mark every **Orchestrator override** (a rejection of a
   position both participants shared) as such. End with the outcome
   (converged / stalled after N rounds) and, on a stall, the precise points
   of disagreement and the rationale for the orchestrator's tie-break. Keep
   it honest: dissent that survived is information, not embarrassment.

## Operational notes

- Each `codex_ask`/`claude_ask` call takes **minutes**. Expect a full round
  to take 5–15 minutes; keep the user informed between steps.
- The LazyTools MCP server takes ~16 s to start; a missing tool at session
  start is usually `MCP_TIMEOUT`, not a fault.
- One focused request per call. If a round's step needs two unrelated
  questions answered, that is a sign the artifact section should be split,
  not the call.
