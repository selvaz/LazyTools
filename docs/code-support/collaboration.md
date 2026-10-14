# Claude Code + Codex collaboration

LazyTools connects writing, independent review, durable projects and quota
decisions. Start with the [worked toy-repo bridge flow](../code-bridge.md#claude-writes-codex-reviews):
Claude writes the weekday deployment guard, Codex reviews, code executes the
boundary tests, and the operator inspects the evidence. Reverse the engines
by choosing Codex as writer; tier-based `--review-of` then requires Claude.

| Work | Integration | Evidence |
| --- | --- | --- |
| Long write / review | Bridge `run --detach`, `--session`, `--tier`, `--review-of` | Jobs, routing, native session, result, failure workspace snapshot |
| Approval | Bridge `pending`, `approve`, `reject` | Durable tickets, actor, reason, expiry |
| Short turn | Engine-backed [MCP tools](index.md) | Reply header, findings or error, session alias |
| Shared project | [Projects](../projects.md) on a configured Store | Plan, claims, contracts, checks, review, decision, notes |
| Schedule | `python -m lazytools.projects.gantt` | English HTML/SVG and text computed from recorded dates |
| Quota | Bridge `route`; Python `route()` / `recommend()` | Rung, exclusions, windows, reservations, admission reason |

Tier-based bridge `--review-of` excludes the producing engine and refuses
with `human_review_required` if no opposite reviewer is eligible. MCP callers
choose their reviewer themselves. The bridge runs a write-capable job even
for review prompts; MCP reviewers provide a read-only surface when required.

Checks and acceptance are separate steps. Reviewer prose is not a test result.
Timeout does not mean no files changed: inspect workspace evidence, Git diff,
and checks before continuing. Project verification has evidence gates and
an injectable caller authorization policy. Neither a successful job nor the
last output of an exhausted LazyBridge `verify=` loop implies acceptance.

Native conversations do not migrate. Use separate writer/reviewer aliases;
share the task, diff and project records. The bridge locks one job per Git
repository: wait for the writer to finish before reviewing in that checkout.

## Existing CLI collaboration builder

`build_cli_collaboration()` remains available for the older read/plan workflow:
Claude analyses, Codex critiques, a provider-model synthesizer plans and an
executor produces the response. It uses CLI subprocess wrappers and separate
provider engines, so it is not a detached bridge or an enforced acceptance
gate. See its full signature in the [reference](../reference.md).

**Benchmark in progress.** Functional tests establish mechanisms; no
comparative quality, time, quota-efficiency or subscription-savings result is
published here. Tier ladders are deterministic policy, not a measured ranking.
