# Human-in-the-loop

Put a **human** inside an agent pipeline — as an approval gate, a form, or an
interactive REPL — without leaving the `Agent` abstraction. LazyBridge ships two
human engines and two sugar factories that return a plain `Agent`:

- **[Human approval](human.md)** — `HumanEngine` / `human_agent`. A *single-turn*
  step: present a task to a person, collect their response (free text or a
  Pydantic form), return it as an `Envelope`. Terminal or web UI.
- **[Supervisor REPL](supervisor.md)** — `SupervisorEngine` / `supervisor_agent`.
  An *interactive* REPL the human drives: approve/continue, retry a named
  sub-agent with feedback, call tools, read the store — with an audit trail.

> **Ships in `lazybridge.ext.hil`.** Both are **Engine** classes you pass via
> `Agent(engine=…)` (like `LLMEngine` or `Plan`); the `human_agent(...)` /
> `supervisor_agent(...)` factories are the one-liner sugar and return an
> `Agent`. HiL lives in `ext` rather than core because it's a workflow pattern,
> not a primitive every agent needs. Import from `lazybridge.ext.hil`.

## When to use which

| You need… | Use |
|---|---|
| A single approval / form / clarification turn | **[`HumanEngine` / `human_agent`](human.md)** |
| An interactive session: tools, agent-retry, store access, audit trail | **[`SupervisorEngine` / `supervisor_agent`](supervisor.md)** (the REPL) |
| Only to *check* an LLM's output (no human) | `verify=` on a normal agent — **not** HiL |
| Unattended automation with a human fallback | Either engine **with `timeout=` + `default=`** set |

## They behave as agents

`human_agent(...)` and `supervisor_agent(...)` return a real `Agent` — callable,
composable, and usable as a step or a tool:

```python
from lazybridge import Agent, Plan, Step
from lazybridge.ext.hil import human_agent

ask_city = human_agent(name="ask_city")          # terminal UI by default
pipeline = Agent(engine=Plan(Step(gather),
                             Step(ask_city, task="What city is the user in?"),
                             Step(finalise)),
                 name="address_normaliser")
```

Everything you already know — `chain`, `parallel`, `Plan`, routing — still works
around a human engine; it's just another engine.

## See also

- [Human approval](human.md) — `HumanEngine` reference.
- [Supervisor REPL](supervisor.md) — `SupervisorEngine` reference.
- [Core tools overview](../core-tools.md) — where HiL sits in the stack.
- [Safety](../safety.md) — guards and confirmation gates for outbound tools.
