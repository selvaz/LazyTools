# Safety

Dangerous tools (`gmail_send`, `telegram_send_message`, …) are gated by two
**independent, composable** primitives in `lazytools.safety`. A tool may use
either or both.

## `Allowlist`

A case-insensitive target allow-list.

- `Allowlist(None)` — no allow-list configured → permits everything.
- `Allowlist([])` — denies everything.
- `Allowlist(["a@x.com", 42])` — permits those targets (string-normalized,
  case-insensitive).

## `ConfirmationGate`

One-shot, target-bound confirmation grants — **not** a sticky boolean.

- Each `grant(target)` / `grant_any()` authorizes **exactly one** action and is
  consumed on use, so an approved single message can never silently authorize a
  flood.
- A target-bound grant is preferred over an any-target grant.
- A grant may be bound to a **scope** (`grant(target, scope=...)`). A scoped
  grant is only consumable when the same scope is supplied at `consume` time —
  and never when no scope is supplied. In LazyPulse the scope is the running
  task id (read from `current_scope()`), so under concurrency an approval issued
  for one task can never be spent by another.
- There is no process-global mutable approval state — grants live on the tool
  instance.

```python
from lazytools.safety import Allowlist, ConfirmationGate

gate = ConfirmationGate(enabled=True)
gate.grant("alice@x.com")            # authorizes one send to alice
gate.consume("alice@x.com")          # -> True (spent)
gate.consume("alice@x.com")          # -> False (one-shot)
```

## Design invariants

- Denials raise a typed `ActionBlocked` (a `PermissionError`) with an
  audit-friendly message that names the action and reason and never leaks
  secrets.
- A harmless companion ships alongside the gated action — e.g.
  `gmail_create_draft` is never gated; only `gmail_send` is — the
  dry-run-first pattern.

## Ambient scope (orchestrator integration)

`safety/context.py` exposes a single contextvar, `active_scope`, and a reader
`current_scope()`. An orchestrator (e.g. `lazypulse.PulseAgent`) sets it for the
duration of a run; a guarded tool reads it when consuming a grant. This is the
one shared object that lets task-bound grants work **without** `lazytools`
importing the orchestrator.
