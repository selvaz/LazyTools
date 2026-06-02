# Gmail

Give an agent **safe** Gmail access — structured inbox reads plus a guarded
outbox. `lazytools.connectors.gmail` ships a thin Gmail REST client plus a
`ToolProvider` that exposes four tools: two read tools (`gmail_list_emails`,
`gmail_get_email`) and two write tools — a harmless `gmail_create_draft` and a
guarded `gmail_send` — the latter wired to LazyTools' allow-list and one-shot
confirmation gate so an LLM can read and draft freely but can never send a
flood.

!!! warning "Compliance & liability — your responsibility"
    This connector accesses Gmail through Google's APIs. **You are solely
    responsible for ensuring your use complies with Google's
    [Terms of Service](https://policies.google.com/terms) and the
    [API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy)**,
    and with any applicable laws (privacy, anti-spam, data protection).
    Automated access and bulk sending can get an account rate-limited or
    suspended. LazyTools is provided **"as is", without warranty, and the
    authors accept no liability** for how it is used (see
    [LICENSE](https://github.com/selvaz/LazyTools/blob/main/LICENSE)). Use
    least-privilege OAuth scopes and obtain the necessary consent before
    deploying.

!!! info "Status & install"
    **Status: alpha.** Install the Gmail extra:
    ```bash
    pip install 'lazytoolkit[gmail]'   # adds google-api-python-client, google-auth, google-auth-oauthlib
    ```
    The package is `lazytoolkit` (PyPI); the import root is `lazytools`. Only
    building a real `GmailClient.from_credentials(...)` needs the extra — every
    other symbol (`GmailTools`, `parse_authentication_results`, the
    `GmailService` protocol) imports cleanly without Google libraries, so tests
    inject a fake client and never touch the network.

## Synopsis

Gmail is the canonical *dangerous outbound* connector: drafting is harmless, but
sending an email is an irreversible external action. LazyTools models that split
directly with the **dry-run-first** pattern:

- **`gmail_create_draft`** — never gated. The draft lands in the Gmail UI; a
  human still has to press send. Safe to expose to any agent.
- **`gmail_send`** — gated by an `Allowlist` (recipient must be permitted) **and**
  a `ConfirmationGate` (one outstanding, one-shot approval per send). A blocked
  send raises `GmailSendBlocked`.

Reading is harmless and ungated: **`gmail_list_emails`** runs a structured Gmail
search (filter by `sender` / `subject` / `contains` / `unread`, or a raw
`query`) and **`gmail_get_email`** fetches one message's headers + snippet — so
an agent can triage an inbox before deciding what to draft.

This is the same connector LazyPulse uses to let an always-on agent answer mail —
which is why the confirmation grants can be **bound to a task id**, so under
concurrency an approval issued for one task can never be spent by another.

## How it works

The module is two cleanly separated layers:

```
GmailClient (REST wrapper)          GmailTools (ToolProvider)
─────────────────────────           ─────────────────────────
from_credentials(...)               as_tools() ─┬─ gmail_list_emails    (read)
list_message_ids()                              ├─ gmail_get_email      (read)
get_message()                                   ├─ gmail_create_draft   (ungated)
create_draft()                                  └─ gmail_send           (gated)
send_message()                      confirm_once() / confirm_send()
                                    ── Allowlist(allowed_recipients)
                                    ── ConfirmationGate(require_confirmation)
```

- **Duck-typed seam.** `GmailTools` depends only on the `GmailService` *protocol*
  (`list_message_ids`, `get_message`, `create_draft`, `send_message`), not on the
  concrete client. Inject any object with that shape — a fake in tests, an
  alternative backend in production.
- **Lazy Google import.** The `googleapiclient` / `google-auth` libraries are
  imported *inside* `GmailClient.from_credentials`, so importing the module is
  cheap and gives a friendly `ImportError` (pointing at the `gmail` extra) only
  when you actually build a credentialed client.
- **Async send, threaded API call.** `gmail_send` is an `async` tool so the
  task-bound grant check can read the worker's task context (LazyBridge runs async
  tools in-context); the blocking Gmail API call is offloaded with
  `asyncio.to_thread` so it never stalls an orchestrator's tick loop.
- **Metadata-scope reads.** `get_message` requests `format="metadata"` for a fixed
  header set (`From`, `To`, `Subject`, `Date`, `Authentication-Results`), which is
  what lets a deployment stay on the narrow `gmail.metadata` OAuth scope.

## Signature

```python
from lazytools.connectors.gmail import (
    GmailClient,                 # production GmailService (googleapiclient-backed)
    GmailService,                # Protocol — the duck-typed seam
    GmailTools,                  # ToolProvider — drop into Agent(tools=[...])
    GmailSendBlocked,            # raised on a denied send (subclass of ActionBlocked)
    parse_authentication_results # DKIM/SPF/DMARC header parser
)


# Build a credentialed client (needs the gmail extra).
GmailClient.from_credentials(
    *,
    credentials_path,   # str — path to the OAuth client-secret JSON
    token_path,         # str — cached token file (created/refreshed; chmod 0600)
    scopes,             # list[str] — e.g. ["https://www.googleapis.com/auth/gmail.modify"]
)


# Wrap it as a tool provider.
GmailTools(
    client,                       # GmailService
    *,
    allowed_recipients=None,      # list[str] | None — None permits all; [] denies all
    require_confirmation=True,    # bool — gate gmail_send on a one-shot grant
)

# Grant exactly one send (call after a human approves):
tools.confirm_once(*, task_id=None)              # one send to any allowed recipient
tools.confirm_send(*, to="a@x.com", task_id=None)  # one send to a specific recipient
tools.require_confirmation                        # bool property
```

### `GmailTools` parameters

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `client` | `GmailService` | — | The Gmail client (real or fake) implementing the protocol. |
| `allowed_recipients` | `list[str] \| None` | `None` | Case-insensitive recipient allow-list. `None` → permit any recipient; `[]` → deny everything. |
| `require_confirmation` | `bool` | `True` | When `True`, every `gmail_send` must consume an outstanding grant. Set `False` only when the allow-list alone is sufficient guard. |

### `GmailClient.from_credentials` parameters

| Parameter | Type | Meaning |
|---|---|---|
| `credentials_path` | `str` | OAuth *client-secret* JSON downloaded from Google Cloud. |
| `token_path` | `str` | Where the long-lived user token is cached. Created on first run (browser consent), refreshed thereafter, and `chmod`-ed to `0600`. |
| `scopes` | `list[str]` | OAuth scopes. Use `gmail.metadata` for read-only metadata; `gmail.modify` / `gmail.send` to draft and send. |

## Tools it exposes

| Tool | Gated? | Args | Returns | Raises |
|---|---|---|---|---|
| `gmail_list_emails` | No | `sender: str`, `subject: str`, `contains: str`, `unread: bool = False`, `query: str`, `max_results: int = 10` (all optional; AND-combined into one Gmail query) | Matching message ids + headers, or `"No messages found…"` | — |
| `gmail_get_email` | No | `message_id: str` (from `gmail_list_emails`) | The message's headers + snippet | — |
| `gmail_create_draft` | No | `to: str, subject: str, body: str` | `"draft created: <id>"` | — |
| `gmail_send` | **Yes** | `to: str, subject: str, body: str` | `"sent: <id>"` | `GmailSendBlocked` |

**Scopes.** `gmail_get_email` works on the narrow `gmail.metadata` scope — the
client fetches messages with `format="metadata"` (headers + snippet, no body).
`gmail_list_emails`, however, issues a Gmail search (`q=`), and the
`users.messages.list` API **rejects `q` under `gmail.metadata`** — so a
read-and-triage deployment that uses the search tool needs `gmail.readonly`
(or `gmail.modify`). Use `gmail.metadata` only if you fetch known message ids
without searching.

`gmail_send` runs two checks in order: (1) `Allowlist.permits(to)` — else
`GmailSendBlocked("… recipient … not in the allow-list")`; (2)
`ConfirmationGate.consume(to, scope=current_scope())` — else
`GmailSendBlocked("… no outstanding confirmation …")`. Both messages are
audit-friendly and never leak the body.

## When to use it

- **You want an agent to handle email** but a draft-only workflow is too weak —
  it should be able to *send*, under explicit human approval per message.
- **You're on LazyPulse** and want an always-on assistant that triages an inbox
  and replies, with each reply gated by a task-bound confirmation.
- **You want least-privilege reads** — pair `gmail.metadata` scope with
  `parse_authentication_results` to tell genuine senders from spoofs before acting.

## When NOT to use it

- **Draft-only is enough.** If a human always sends from the Gmail UI, expose just
  the draft tool (or drop `gmail_send` from the agent) and skip the OAuth send
  scope entirely.
- **High-volume transactional mail.** This is an assistant outbox, not a bulk
  mailer — use a transactional email API (SES/SendGrid) for campaigns.
- **You need labels/threading/attachments.** The client surface is intentionally
  minimal (list ids, get metadata, draft, send). Extend `GmailService` yourself if
  you need more of the Gmail API.

## Example

=== "Draft + gated send"

    ```python
    from lazybridge import Agent
    from lazytools.connectors.gmail import GmailClient, GmailTools

    client = GmailClient.from_credentials(
        credentials_path="credentials.json",
        token_path="token.json",
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
    )

    # Drafting is always allowed; sending is allow-listed + confirmation-gated.
    tools = GmailTools(client, allowed_recipients=["teammate@example.com"])
    agent = Agent("claude-opus-4-8", tools=[tools])

    # The agent can draft freely:
    agent("Draft a thank-you note to teammate@example.com")

    # A send attempt is blocked until you authorize exactly one:
    tools.confirm_send(to="teammate@example.com")   # human-in-the-loop approval
    agent("Now send it")                            # consumes the single grant
    ```

=== "Reply-bot (allow-list only)"

    ```python
    # A bot that only ever emails one address can drop confirmation and rely on
    # the allow-list as the sole guard.
    tools = GmailTools(
        client,
        allowed_recipients=["owner@example.com"],
        require_confirmation=False,
    )
    # gmail_send now succeeds for owner@example.com without an explicit grant,
    # and still raises GmailSendBlocked for anyone else.
    ```

=== "LazyPulse (task-bound grant)"

    ```python
    # Under max_concurrent_inbound > 1, bind the grant to the approved task so a
    # concurrent task can never spend it. task_id is the id returned by
    # PulseAgent's approve_task / schedule.
    tools.confirm_send(to="customer@example.com", task_id=task_id)
    # gmail_send reads the active task via current_scope() and only consumes a
    # grant whose scope matches.
    ```

=== "Spoof check before acting"

    ```python
    from lazytools.connectors.gmail import parse_authentication_results

    msg = client.get_message(message_id)             # metadata scope is enough
    headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    auth = parse_authentication_results(
        headers.get("Authentication-Results"),
        trusted_authserv_id="mx.google.com",         # pin to your receiving MTA
    )
    if not (auth["dkim"] and auth["spf"] and auth["dmarc"]):
        ...  # treat as unverified — do not auto-action
    ```

## Authentication-results parsing

`parse_authentication_results(header, *, trusted_authserv_id=None)` returns
`{"dkim": bool, "spf": bool, "dmarc": bool}`. A method is `True` **only** when an
authoritative result token for it is exactly `pass`; `fail`, `none`, `neutral`,
`softfail`, a missing header, or an unparseable one all yield `False`. Three
hardening rules defend against forged `pass` tokens:

1. **Authserv-id pinning.** With `trusted_authserv_id="mx.google.com"`, only a
   header whose leading authserv-id is *exactly* that value is parsed. The match
   is exact, so a look-alike like `mx.google.com.evil.com` is rejected. Pass the
   **top-most** `Authentication-Results` header — the one your receiving MTA
   prepended.
2. **Comments stripped first.** RFC 8601 CFWS comments (`spf=fail (note: spf=pass)`)
   are removed before parsing, so a `pass` buried in a comment is never read.
3. **Standalone-token anchoring.** Matching is anchored to a `;`/whitespace
   boundary, so `x-dkim=pass` or `reason-spf=pass` can't impersonate a real result.

## Security & safety

- **OAuth token hardening.** `from_credentials` `chmod`s the cached token file to
  `0600` whenever it exists — including a still-valid token written by an older,
  looser version — so another local user can't steal the long-lived refresh token.
- **Two independent guards.** `Allowlist` and `ConfirmationGate` compose; a send
  must pass both. See [Safety](safety.md) for the full model.
- **One-shot, not sticky.** Each `confirm_*` authorizes exactly one send and is
  consumed on use — an approved message can't silently authorize more.
- **Audit-friendly denials.** `GmailSendBlocked` (a `PermissionError` via
  `ActionBlocked`) names the action and reason and never includes the body.
- **Verify before you act on inbound mail.** Use `parse_authentication_results`
  with `trusted_authserv_id` pinned to your MTA before trusting a sender.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ImportError: requires the 'gmail' extra` | Google libs not installed | `pip install 'lazytoolkit[gmail]'` |
| `GmailSendBlocked: recipient … not in the allow-list` | `to` not in `allowed_recipients` | Add the address, or use `allowed_recipients=None` for trusted contexts |
| `GmailSendBlocked: no outstanding confirmation` | No grant, or it was already spent / scope-mismatched | Call `confirm_once()` / `confirm_send(to=…)` before sending; match `task_id` under concurrency |
| All `parse_authentication_results` values `False` | Header missing, or `trusted_authserv_id` didn't match exactly | Pass the top-most header; check the pinned authserv-id equals your MTA's exactly |
| Browser consent prompt every run | `token_path` not writable / not persisted | Point `token_path` at a stable, writable location |

## Pitfalls

- **`allowed_recipients=[]` denies everything** (vs. `None`, which permits all).
  An empty list is a deny-all, not a no-op.
- **Scope binding only works in the async send.** `gmail_send` is async on purpose;
  a task-bound grant relies on `current_scope()` being readable in the worker's
  context. A sync re-implementation would not see the task id.
- **Pass the right header to the auth parser.** A later/forwarded
  `Authentication-Results` header can be attacker-controlled — always use the
  top-most one and pin `trusted_authserv_id`.
- **`require_confirmation=False` removes the gate, not the allow-list.** Sends
  still must pass `allowed_recipients`; don't combine `False` with
  `allowed_recipients=None` unless the agent is fully trusted.

## See also

- [Safety](safety.md) — the `Allowlist` + `ConfirmationGate` primitives this
  connector composes, with the full one-shot / scope-binding semantics.
- [Telegram](telegram.md) — the same gated-outbound pattern for chat messages.
- [Tools overview](connectors.md) — every connector at a glance.
- [Tool](https://core.lazybridge.com/guides/basic/tool/) — how a `ToolProvider`
  expands into the LazyBridge tool surface.
