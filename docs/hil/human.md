# Human approval (`HumanEngine`)

`HumanEngine` turns a person into a **single-turn** agent step: it presents the
task to a human and returns their response as an `Envelope`. Use it as an
approval gate, a clarification prompt, or a structured form. `human_agent(...)`
is the sugar that builds an `Agent` around it.

> Part of [Human-in-the-loop](index.md). Ships in `lazybridge.ext.hil`.

## Signature

```python
from lazybridge import Agent
from lazybridge.ext.hil import HumanEngine, human_agent

# Engine — pass to Agent(engine=...)
HumanEngine(
    *,
    timeout=None,                  # float | None — seconds to wait before falling back
    ui="terminal",                 # "terminal" | "web" | custom prompt() object
    default=None,                  # str | None — value returned on timeout
)

# Sugar — returns a ready Agent(engine=HumanEngine(...))
human_agent(
    *,
    timeout=None,
    ui="terminal",
    default=None,
    **agent_kwargs,                # name=, session=, description=, …
) -> Agent
```

## Parameters

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `timeout` | `float \| None` | `None` | Seconds to wait for human input before falling back to `default`. `None` = wait indefinitely. |
| `ui` | `"terminal" \| "web" \| object` | `"terminal"` | Where to prompt. `"terminal"` uses `input()`; `"web"` spins up a localhost form server; or pass any object implementing `async def prompt(self, task, *, tools, output_type) -> str`. |
| `default` | `str \| None` | `None` | Value returned if `timeout` elapses (enables unattended fallback). |
| `**agent_kwargs` | — | — | (`human_agent` only) forwarded to `Agent(...)`: `name`, `session`, `description`, … |

## Synopsis

`HumanEngine` implements the same engine protocol as `LLMEngine`, so it's a
drop-in `Agent(engine=…)`. It runs **one** turn:

- With a plain `output_type` it returns the human's free-text response.
- With `output=<PydanticModel>` the terminal prompts **field by field** (and the
  web UI renders a form), re-prompting until the input validates.
- It emits the same event types as `LLMEngine`, so sessions / tracing stay
  transparent (each input is a `HIL_DECISION` event).

## Example

```python
from lazybridge import Agent, Plan, Step
from lazybridge.ext.hil import human_agent

# Approval gate inside a pipeline (terminal UI by default).
ask_city = human_agent(name="ask_city")
pipeline = Agent(
    engine=Plan(
        Step(gather),
        Step(ask_city, task="What city is the user in?"),
        Step(finalise),
    ),
    name="address_normaliser",
)
result = pipeline("Normalise this contact record")

# Web UI entry point (renders a form on localhost).
ask = human_agent(ui="web", name="ask")

# Unattended fallback: auto-approve after 30s.
gate = human_agent(timeout=30.0, default="approve", name="gate")
```

## When to use it

- **Approval gates** before an irreversible or high-stakes action.
- **Structured form entry** — collect typed fields via a Pydantic `output`.
- **Clarification turns** in an otherwise automated pipeline.
- **Unattended pipelines with a fallback** — set `timeout=` + `default=`.

## When NOT to use it

- **The human needs tools, retry, or store access** → use the interactive
  [Supervisor REPL](supervisor.md).
- **Blocking on a person isn't appropriate** (high-throughput automation).
- **You only want to *check* an LLM's output** → that's `verify=`, not HiL.

## Security & safety

- **A human turn is a real gate.** Treat `default=` carefully — on timeout it
  becomes the answer, so a permissive default can auto-approve actions.
- **Downstream guards still apply.** Approving here doesn't bypass a tool's
  allow-list or [confirmation gate](../safety.md); those fire when the tool runs.
- **Web UI is localhost.** `ui="web"` serves a form on the local machine; don't
  expose it publicly without your own auth in front.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Pipeline hangs waiting for input | No `timeout` in an unattended context | Set `timeout=` + `default=`, or use scripted input in tests. |
| Returns `default` immediately | `timeout` too low or `default` mis-set | Raise `timeout`; verify `default` is the intended fallback. |
| Re-prompts repeatedly | `output=<PydanticModel>` input keeps failing validation | Enter values matching the model's field types. |
| Need tool calls during the turn | `HumanEngine` is single-turn | Switch to [`SupervisorEngine`](supervisor.md). |

## Pitfalls

- **Single turn only.** No tools, no retry loop — that's the supervisor's job.
- **`default` on timeout is the answer.** Choose it as if a human typed it.
- **Web UI persists across calls** within a run; the server stays up between
  `prompt()` calls.

## See also

- [Supervisor REPL](supervisor.md) — the interactive, tool-capable engine.
- [Human-in-the-loop overview](index.md) — choosing between the two engines.
- [Safety](../safety.md) — allow-lists and confirmation gates.
