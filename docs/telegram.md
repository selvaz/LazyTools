# Telegram

Give an agent a guarded Telegram outbox. `lazytools.connectors.telegram` ships a
minimal Bot API client plus a `ToolProvider` exposing a single tool,
`telegram_send_message`, wired to the same allow-list + one-shot confirmation
guards as the [Gmail](gmail.md) connector.

!!! info "Status & install"
    **Status: alpha.** Install the Telegram extra:
    ```bash
    pip install 'lazytoolkit[telegram]'   # adds httpx
    ```
    The package is `lazytoolkit` (PyPI); the import root is `lazytools`. Only
    `TelegramClient.from_token(...)` needs `httpx` — `TelegramTools` and the
    `TelegramService` protocol import without it, so tests inject a fake client
    and never touch the network.

## Synopsis

Sending a Telegram message is an outbound, externally-visible action, so it gets
the same treatment as `gmail_send`: a chat allow-list plus a one-shot
`ConfirmationGate`. There is no "draft" half here — a message is either sent or
blocked — so the two common deployments are:

- **Reply freely to a known chat** (a personal/ops bot): allow-list the chat and
  set `require_confirmation=False`. Every send is still bounded to that chat.
- **May message arbitrary chats**: keep confirmation on and grant one send per
  approved task via `confirm_send(chat_id=…)` / `confirm_once()`.

## How it works

```
TelegramClient (Bot API over HTTPS)     TelegramTools (ToolProvider)
───────────────────────────────────     ────────────────────────────
from_token(token)                        as_tools() ── telegram_send_message (gated)
get_updates(offset, timeout, limit)      confirm_once() / confirm_send()
send_message(chat_id, text)              ── Allowlist(allowed_chat_ids)
                                         ── ConfirmationGate(require_confirmation)
```

- **Why a tiny HTTP wrapper, not aiogram/python-telegram-bot?** Those ship a
  *dispatcher* that runs its own polling loop — a second loop competing with an
  orchestrator's tick loop. LazyPulse needs only two Bot API methods
  (`getUpdates` + `sendMessage`), so a small `httpx`-based wrapper keeps the
  dependency surface minimal. Swap in your own `TelegramService` (e.g.
  aiogram-backed) if you prefer.
- **Duck-typed seam.** `TelegramTools` depends only on the `TelegramService`
  protocol (`get_updates`, `send_message`), so any object with that shape works.
- **Lazy `httpx` import.** `httpx` is imported inside `from_token`, giving a
  friendly `ImportError` (pointing at the `telegram` extra) only when you build a
  real client.
- **Async send, threaded API call.** `telegram_send_message` is `async` so a
  task-bound grant can read the worker's task context; the blocking Bot API call
  is offloaded with `asyncio.to_thread`.
- **Error surfacing.** The client raises `RuntimeError` on a transport failure or
  any Bot API response where `ok` is not `true`.

## Signature

```python
from lazytools.connectors.telegram import (
    TelegramClient,        # production TelegramService (httpx-backed)
    TelegramService,       # Protocol — the duck-typed seam
    TelegramTools,         # ToolProvider — drop into Agent(tools=[...])
    TelegramSendBlocked,   # raised on a denied send (subclass of ActionBlocked)
)


# Build a client from a bot token (from @BotFather). Needs the telegram extra.
TelegramClient.from_token(token, *, timeout=30.0)


# Wrap it as a tool provider.
TelegramTools(
    client,                       # TelegramService
    *,
    allowed_chat_ids=None,        # list[int | str] | None — None permits all; [] denies all
    require_confirmation=True,    # bool — gate the send on a one-shot grant
)

# Grant exactly one send (call after a human approves):
tools.confirm_once(*, task_id=None)                  # one send to any allowed chat
tools.confirm_send(*, chat_id=123, task_id=None)     # one send to a specific chat
tools.require_confirmation                            # bool property
```

### `TelegramTools` parameters

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `client` | `TelegramService` | — | The Telegram client (real or fake) implementing the protocol. |
| `allowed_chat_ids` | `list[int \| str] \| None` | `None` | Case-insensitive (string-normalized) chat allow-list. `None` → permit any chat; `[]` → deny everything. |
| `require_confirmation` | `bool` | `True` | When `True`, every send must consume an outstanding grant. |

## Tools it exposes

| Tool | Gated? | Args | Returns | Raises |
|---|---|---|---|---|
| `telegram_send_message` | **Yes** | `chat_id: int \| str, text: str` | `"sent: message_id=<id>"` | `TelegramSendBlocked` |

The send runs two checks in order: (1) `Allowlist.permits(chat_id)` — else
`TelegramSendBlocked("… chat … not in the allow-list")`; (2)
`ConfirmationGate.consume(chat_id, scope=current_scope())` — else
`TelegramSendBlocked("… no outstanding confirmation …")`.

## When to use it

- **A personal or ops bot** that pushes notifications/answers to a known chat.
- **An always-on LazyPulse assistant** reachable over Telegram, replying under
  per-task human approval.
- **You want a minimal dependency** — `httpx` only, no dispatcher/polling-loop
  framework competing with your own loop.

## When NOT to use it

- **You need rich bot features** (inline keyboards, media groups, webhooks,
  callback queries). Use a full bot framework and adapt it behind
  `TelegramService` if you still want the LazyTools guards.
- **Tools live in your own Python.** This connector is specifically the guarded
  Telegram outbox; for arbitrary logic, write a plain `Tool`.

## Example

=== "Reply freely to one chat"

    ```python
    from lazytools.connectors.telegram import TelegramClient, TelegramTools

    client = TelegramClient.from_token("BOT_TOKEN")        # from @BotFather

    # A bot that only ever messages your own chat: allow-list it and drop
    # confirmation. Sends are still bounded to that chat id.
    tools = TelegramTools(
        client,
        allowed_chat_ids=[123456789],
        require_confirmation=False,
    )
    ```

=== "Gated send to any chat"

    ```python
    from lazybridge import Agent
    from lazytools.connectors.telegram import TelegramClient, TelegramTools

    client = TelegramClient.from_token("BOT_TOKEN")
    tools = TelegramTools(client)                          # confirmation ON, no allow-list
    agent = Agent("claude-opus-4-8", tools=[tools])

    # A send is blocked until you authorize exactly one:
    tools.confirm_send(chat_id=987654321)                  # human-in-the-loop approval
    agent("Message chat 987654321: the deploy finished")   # consumes the single grant
    ```

=== "LazyPulse (task-bound grant)"

    ```python
    # Bind the grant to the approved task so a concurrent task cannot spend it.
    tools.confirm_send(chat_id=987654321, task_id=task_id)
    # telegram_send_message reads the active task via current_scope() and only
    # consumes a grant whose scope matches.
    ```

## Security & safety

- **Two independent guards.** A send must pass both the chat `Allowlist` and the
  `ConfirmationGate`. See [Safety](safety.md).
- **One-shot, not sticky.** Each `confirm_*` authorizes exactly one send and is
  consumed on use.
- **Task-bound grants.** Under concurrency, bind a grant with `task_id=` so an
  approval for one task can never be spent by another.
- **Audit-friendly denials.** `TelegramSendBlocked` (a `PermissionError` via
  `ActionBlocked`) names the chat and reason and never includes the message text.
- **Keep the bot token secret.** It lives on the client, not in tool results.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ImportError: requires the 'telegram' extra` | `httpx` not installed | `pip install 'lazytoolkit[telegram]'` |
| `TelegramSendBlocked: chat … not in the allow-list` | `chat_id` not in `allowed_chat_ids` | Add the chat id, or use `allowed_chat_ids=None` for trusted contexts |
| `TelegramSendBlocked: no outstanding confirmation` | No grant, already spent, or scope mismatch | Call `confirm_once()` / `confirm_send(chat_id=…)` first; match `task_id` under concurrency |
| `RuntimeError: Telegram API error on sendMessage: …` | Bot API returned `ok=false` (bad chat id, bot blocked, etc.) | Check the `description` in the error; ensure the bot can message that chat |
| `RuntimeError: TelegramClient has no HTTP client` | Constructed without `from_token` / injected `http=` | Use `TelegramClient.from_token(...)`, or inject an `httpx.Client` |

## Pitfalls

- **`allowed_chat_ids=[]` denies everything** (vs. `None`, which permits all).
- **Chat ids are string-normalized** for matching, so `123` and `"123"` are
  equivalent in the allow-list and grants.
- **Scope binding only works in the async send** — the same reason as Gmail; the
  task id is read from `current_scope()` in the worker's context.
- **No draft half.** Unlike Gmail there's no harmless companion tool — a message
  is sent or blocked, so keep confirmation on whenever the chat surface is open.

## See also

- [Gmail](gmail.md) — the same gated-outbound pattern, plus a harmless draft tool
  and inbound auth-header verification.
- [Safety](safety.md) — the `Allowlist` + `ConfirmationGate` primitives.
- [Tools overview](connectors.md) — every connector at a glance.
