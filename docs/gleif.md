# GLEIF

Five read-only tools over GLEIF's public Global LEI Index REST API: search and
fetch Legal Entity Identifier (LEI) records, and walk their reported parent
and child relationships. Public, documented, and keyless.

```python
from lazybridge import Agent, LLMEngine
from lazytools.connectors.gleif import GLEIFTools

agent = Agent(name="entities", engine=LLMEngine("deepseek-v4-flash"),
              tools=[GLEIFTools()])
```

Install with the extra: `pip install "lazytoolkit[gleif] @ git+https://github.com/selvaz/LazyTools.git"`.
There is no key to configure — the endpoint is public.

## What it is, and what it is not

**Live and stateless.** Nothing is stored, and every tool reaches the network
on every call. Records reflect the vendor's data as of the call, including
whatever `next_renewal_date` and registration status happen to be current —
there is no local cache and no history.

**Read-only by construction, not by policy.** GLEIF's public API is a read
surface; issuing or updating an LEI registration is done through an
accredited Local Operating Unit (LOU) and is not part of this API at all —
there is no write tool to gate, unlike the messaging connectors.

**Reference data, not a search engine.** `gleif_search` matches on exact or
near-exact legal names against the vendor's index; it is not a fuzzy or
semantic search. Use `gleif_fuzzy_search` first when the caller only has a
partial or possibly misspelled name, then resolve the LEI it returns with
`gleif_search`/`gleif_get_record`.

## A 404 from the vendor means "no parent," not "lookup failed"

This is the gotcha most likely to be misread as a bug. GLEIF's own API is
inconsistent between parent and child relationships, and this connector
absorbs that inconsistency so a caller never has to special-case it:

* **Parents:** most entities report **no** parent at all — including large,
  well-known companies with no reported corporate owner. GLEIF's
  `/lei-records/{lei}/direct-parent` and `/ultimate-parent` endpoints signal
  this with an HTTP **404**, not an empty payload. `gleif_parents` treats
  that 404 as the normal, expected case: it returns `has_parent=False,
  parent=None` and does **not** raise. A caller (or an agent) that expects an
  error here and instead gets a clean `has_parent=False` is seeing the
  correct answer, not a partial failure.
* **Children:** the vendor is more consistent here — no reported children
  normally arrives as an empty `data` list on `/direct-children` /
  `/ultimate-children`, not a 404. `gleif_children` accepts either shape
  defensively (both an empty list and a 404 map to `count=0,
  children=[]`), so this asymmetry between the parent and child endpoints
  never leaks into the tool surface.

Do not build agent instructions or downstream logic that treats
`gleif_parents` returning `has_parent=False` as something to retry, log as
an error, or otherwise flag — it is the majority-case, correct result.

## The tools

| Tool | What it does | Notes |
|---|---|---|
| `gleif_search` | search LEI records by legal name, or by exact LEI when `exact_lei=True`; optional country filter | empty `country` (the default) sends no filter at all, not a literal empty-string filter |
| `gleif_get_record` | one LEI record by exact LEI code | `found=False` rather than an error on an unknown or invalid LEI |
| `gleif_parents` | an entity's reported direct (`ultimate=False`, default) or ultimate (`ultimate=True`) parent | `has_parent=False, parent=None` is the normal result for most entities — see the gotcha above |
| `gleif_children` | an entity's reported direct or ultimate children/descendants | `count=0, children=[]` whether the vendor answered an empty list or a 404 |
| `gleif_fuzzy_search` | legal-name/LEI candidates from a partial or misspelled name | use before `gleif_search` when the caller's name is uncertain |

All listing tools (`gleif_search`, `gleif_children`, `gleif_fuzzy_search`)
cap `limit` at 100 rows — `MAX_ROWS` in `lazytools.connectors.gleif.tools` —
independent of whatever page size the vendor itself would accept.

### The record shape

Every tool that returns entity data uses the same flattened record:
`lei`, `legal_name`, `status`, `registration_status`, `legal_form`,
`jurisdiction` (legal address country), `headquarters_country`, `bic_codes`,
`next_renewal_date`. This is a deliberately reduced view of GLEIF's full
JSON:API resource — see `lazytools.connectors.gleif.client.LEIRecord` for
the dataclass, and `client.py`'s `_to_record` for exactly which vendor
fields feed each one and how missing/malformed values degrade rather than
raise.

## Through the MCP server

Mounted as the provider id `gleif`, always read-only — there is no write
tool to gate, so `--allow-unsafe` changes nothing about it.

```bash
lazytools-mcp --providers gleif
```

One environment knob, the same convention as `polymarket`/`tradingview`:
`LAZYTOOLS_GLEIF_MAX_CALLS` (default `200`; `0` removes the guard).

## Limits worth knowing before you rely on it

* **The parent/child endpoints report what was filed, not corporate reality.**
  LEI relationship data ("Level 2" data, in GLEIF's terminology) is
  self-reported and optional at registration — a real parent can be
  genuinely unreported, and `has_parent=False` reflects that filing gap as
  much as it reflects an entity with no corporate parent. Treat it as "no
  parent *on file*," not a verified ownership fact.
* **Name search is exact-ish, not fuzzy.** `gleif_search` on `legal_name`
  matches against the registered legal name as filed; small variations in
  punctuation, suffixes (`Inc.` vs `Incorporated`), or transliteration can
  miss a real match. Route uncertain names through `gleif_fuzzy_search`
  first.
* **No history, no series.** Every tool answers with the vendor's current
  state — a status or relationship as of an earlier date is not available
  here.
* **A per-client call budget guards against loops**, not against a published
  vendor rate limit (GLEIF does not publish one for this API): 200 calls by
  default, configurable via `LAZYTOOLS_GLEIF_MAX_CALLS` when mounted through
  the MCP server, or the `max_calls` constructor argument otherwise. A `429`
  response raises immediately and is never retried.

!!! warning "Terms of use — your decision, not this package's"
    The Global LEI Index is published by GLEIF under CC0, a public-domain
    dedication with no reuse restrictions. This connector still sends
    whatever it returns to whichever model provider your agent runs on — a
    transmission to a third party whatever the intent. Establishing that
    your use is permitted is your responsibility; see the compliance note in
    the [tools overview](connectors.md).
