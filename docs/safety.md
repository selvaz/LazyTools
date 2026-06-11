# Safety

Dangerous tools (`gmail_send`, `telegram_send_message`, …) are gated by two
**independent, composable** primitives in `lazytools.safety`. A tool may use
either or both. They carry no orchestration dependency, hold no process-global
state, and are fully unit-testable on their own.

!!! info "Ships in the core package"
    No extra needed — `pip install lazytoolkit`. Import root is `lazytools`.
    ```python
    from lazytools.safety import Allowlist, ConfirmationGate, ActionBlocked, current_scope
    ```

## The model in one breath

A guarded action passes **two** checks: the target must be on an `Allowlist`
**and** there must be an outstanding, one-shot `ConfirmationGate` grant for it.
Denials raise a typed `ActionBlocked` with an audit-friendly, secret-free message.
A harmless companion ships alongside each gated action (e.g. `gmail_create_draft`
is never gated; only `gmail_send` is) — the dry-run-first pattern.

## `Allowlist`

A case-insensitive, string-normalized target allow-list.

```python
Allowlist(allowed: Iterable[object] | None)
allowlist.permits(target: object) -> bool
```

| Construction | Behaviour |
|---|---|
| `Allowlist(None)` | No allow-list configured → **permits everything**. |
| `Allowlist([])` | **Denies everything**. |
| `Allowlist(["a@x.com", 42])` | Permits exactly those targets, string-normalized and case-insensitive (so `42` matches `"42"`, `"A@X.com"` matches `"a@x.com"`). |

`None` vs `[]` is the most common trip-up: `None` is "no list, allow all"; an empty
list is a deny-all.

## `ConfirmationGate`

One-shot, target-bound confirmation grants — **not** a sticky boolean.

```python
ConfirmationGate(*, enabled: bool = True)
gate.grant(target: object, *, scope: str | None = None) -> None       # one send to target
gate.grant_any(*, scope: str | None = None) -> None                   # one send to any target
gate.consume(target: object, *, scope: str | None = None) -> bool     # spend one matching grant
gate.enabled                                                          # bool property
```

- Each `grant` / `grant_any` authorizes **exactly one** action and is consumed on
  use — an approved single message can never silently authorize a flood.
- **Matched most- to least-specific:** `(target, scope)` → `(target, None)` →
  `(any, scope)` → `(any, None)`. A target-bound grant is preferred over an
  any-target one.
- **Scope binding is strict.** A grant bound to a `scope` is only consumable when
  the *same* scope is supplied at `consume` time — and **never** when no scope
  (`None`) is supplied. In LazyPulse the scope is the running task id (read from
  `current_scope()`), so under concurrency an approval for one task can never be
  spent by another.
- **Disabled gate.** `ConfirmationGate(enabled=False).consume(...)` returns `True`
  immediately — the gate is a no-op (rely on the `Allowlist` alone).
- No process-global mutable approval state — grants live on the tool instance.

```python
from lazytools.safety import ConfirmationGate

gate = ConfirmationGate(enabled=True)
gate.grant("alice@x.com")            # authorizes one send to alice
gate.consume("alice@x.com")          # -> True (spent)
gate.consume("alice@x.com")          # -> False (one-shot)
```

## `ActionBlocked`

The typed exception for a denied dangerous action. It subclasses `PermissionError`
(so existing `except PermissionError` handlers keep working) and its message names
the action and reason while never leaking secrets or payloads. Connector-specific
subclasses — `GmailSendBlocked`, `TelegramSendBlocked` — let you catch one tool's
denials precisely.

## `validate_public_url` (SSRF guard)

`lazytools.safety.urls` adds a third, independent primitive for connectors
that fetch from the network: `validate_public_url(url, *, allowed_hosts=None)`.
It refuses non-http(s) schemes, missing hostnames, hosts outside
`allowed_hosts` (when given), and literal IPs that are not globally routable
(loopback, private, link-local, multicast, reserved, unspecified). Denials
raise `UrlBlocked`, a subclass of `ActionBlocked`.

The check is purely syntactic — no DNS, no I/O — so connectors run it on
**every** constructed URL *and* every redirect target before following it. The
[SEC EDGAR](edgar.md) and [market data](marketdata.md) connectors pin their
requests to their service hosts this way.

```python
from lazytools.safety import UrlBlocked, validate_public_url

validate_public_url("https://data.sec.gov/x", allowed_hosts={"data.sec.gov"})  # ok
validate_public_url("http://169.254.169.254/latest/meta-data")  # raises UrlBlocked
```

## Ambient scope (orchestrator integration)

`safety/context.py` exposes a single contextvar, `active_scope`, and a reader
`current_scope()`. An orchestrator (e.g. `lazypulse.PulseAgent`) sets it for the
duration of a run; a guarded tool reads it when consuming a grant. This one shared
object is what lets task-bound grants work **without** `lazytools` importing the
orchestrator.

This is also why the guarded sends (`gmail_send`, `telegram_send_message`) are
`async`: LazyBridge runs async tools in the worker's own context, where the active
scope is visible. A sync tool would run in a fresh thread and not see it.

## Worked example: concurrency-safe approval

```python
from lazytools.safety import ConfirmationGate

gate = ConfirmationGate(enabled=True)

# Two tasks run concurrently. Each approval is bound to its own task id.
gate.grant("customer@x.com", scope="task-A")
gate.grant("customer@x.com", scope="task-B")

# Task B's worker tries to send — current_scope() == "task-B":
gate.consume("customer@x.com", scope="task-B")   # -> True  (spends B's grant)
gate.consume("customer@x.com", scope="task-B")   # -> False (B's grant is gone)

# Task A's grant is untouched and only A can spend it:
gate.consume("customer@x.com", scope="task-A")   # -> True

# A scoped grant is never spendable without the matching scope:
gate.grant("customer@x.com", scope="task-C")
gate.consume("customer@x.com")                    # -> False (no scope supplied)
gate.consume("customer@x.com", scope="task-C")    # -> True
```

## Design invariants

- **Two-key gate.** A guarded action passes the `Allowlist` *and* the
  `ConfirmationGate`; the two are independent and composable.
- **One-shot grants.** Approval authorizes exactly one action and is consumed on
  use — no sticky "approved forever" flag.
- **Scope isolation.** Under concurrency, a task-bound grant can't be spent by
  another task; an unscoped consume never spends a scoped grant.
- **No global state.** Grants live on the tool instance; nothing leaks across
  instances or processes.
- **Audit-friendly, secret-free denials.** `ActionBlocked` names the action and
  reason and never includes payloads.
- **Dry-run first.** A harmless companion ships beside each gated action.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Everything is allowed unexpectedly | `Allowlist(None)` permits all | Pass an explicit list of permitted targets |
| Everything is denied | `Allowlist([])` denies all | Use `None` to permit all, or list the targets |
| `consume` returns `False` right after `grant` | Scope mismatch — grant scoped, consume unscoped (or vice-versa) | Supply the same `scope` at consume time (the task id) |
| Grant works only once | By design — grants are one-shot | Issue one grant per approved action |
| Gate seems to do nothing | `ConfirmationGate(enabled=False)` | Enable it, or rely on the `Allowlist` as the sole guard intentionally |

## See also

- [Gmail](gmail.md) and [Telegram](telegram.md) — the connectors that compose
  these primitives for guarded outbound sends.
- [Tools overview](connectors.md) — every connector at a glance.
