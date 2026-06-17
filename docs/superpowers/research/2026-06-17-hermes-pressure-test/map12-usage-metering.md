# Map 12 — Hermes usage / billing metering surface

Basis: `/Users/prasiddhaparthsarthy/Desktop/hermes-agent` @ `basis-v0.14.0-spec` (commit 4c46c35e).
Mandate: trace the `run.completed` `usage` payload back to source; document exact field
names and whether USD cost is present, and whether usage is per-run only or streamed.
Every claim below is grounded in a file:line I read this session.

---

## The exact payload the spec cites

`gateway/platforms/api_server.py:3046-3050` builds the `usage` dict that ships in the
`run.completed` SSE event (`:3073-3078`) and is stored on run status (`:3080-3085`):

```python
u = {
    "input_tokens":  getattr(agent, "session_prompt_tokens", 0) or 0,   # :3047
    "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0, # :3048
    "total_tokens":  getattr(agent, "session_total_tokens", 0) or 0,    # :3049
}
```

Those three `session_*` attributes originate on the agent object in
`run_agent.py:546-550` (initialized to 0) and are accumulated per-API-call in
`agent/conversation_loop.py:1492-1494`:

```python
agent.session_prompt_tokens     += prompt_tokens      # :1492
agent.session_completion_tokens += completion_tokens  # :1493
agent.session_total_tokens      += total_tokens       # :1494
```

`prompt_tokens` / `completion_tokens` / `total_tokens` are read off a `CanonicalUsage`
produced by `normalize_usage(response.usage, ...)` at `conversation_loop.py:1466-1473`.
`CanonicalUsage` (`agent/usage_pricing.py:30-45`): `prompt_tokens` is a *derived
property* = `input_tokens + cache_read_tokens + cache_write_tokens` (`:40-41`); raw
storage buckets are `input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
reasoning_tokens, request_count` (`:31-36`).

---

## THE LOAD-BEARING SURPRISE: the cited payload carries NO USD cost

The spec's chosen entry point (`api_server.py` `run.completed`) ships **only the three
token integers — `input_tokens`, `output_tokens`, `total_tokens`**. No `cost_usd`, no
`cost_status`, no cache/reasoning breakdown. Verified identical 3-field shape at **every**
usage payload in api_server.py: `:1323-1327` (OpenAI chat-completions compat —
`prompt/completion/total`), `:1452`, `:1644`, `:1987`, `:2011-2015` (Responses-API
`response.completed`), `:2347`. None include cost.

Cost DOES exist in the agent — `session_estimated_cost_usd`, `session_cost_status`,
`session_cost_source` (`run_agent.py:555-557`), accumulated at
`conversation_loop.py:1520-1523`. But api_server.py never copies them into any wire
payload. **The metering hook Isol8 needs would have to read the agent attributes directly
or use a different gateway path — the documented `run.completed` `usage` object is
insufficient for USD billing.** This contradicts any spec assumption that the gateway run
output is billing-complete.

A *different* gateway (`tui_gateway/server.py:1302-1318`) DOES emit cost: it calls
`estimate_usage_cost(...)` and sets `usage["cost_status"]` + `usage["cost_usd"]`
(`:1316-1318`). So Hermes has two divergent usage contracts; only the TUI gateway path
includes money. If the spec line-references point at `api_server.py`, the cost it implies
is in the other server.

---

## Cost is an ESTIMATE from a local pricing table, frequently `None`

Even where cost is surfaced, `estimate_usage_cost` (`usage_pricing.py:745-819`) returns a
`CostResult` whose `amount_usd` is `Optional[Decimal]` (`:71`) and whose `status` is at
best `"estimated"`, never `"actual"` for self-hosted/OpenRouter:
- `status` literal set: `"actual" | "estimated" | "included" | "unknown"` (`:17`).
- Returns `amount_usd=None, status="unknown"` when no pricing entry exists for the
  model/route (`:765`), or when input/output/cache pricing fields are missing
  (`:770-791`). For NousResearch Hermes models on a self-hosted vLLM endpoint or an
  unrecognized base_url, `get_pricing_entry` will commonly miss → **cost is `None`**.
- When pricing exists, `status="estimated"` and `label="~$X.XX"` (`:804-805`) — Hermes
  computes cost locally from a docs snapshot table (`_OFFICIAL_DOCS_PRICING`,
  `:85+`), it does NOT read an authoritative provider-billed cost for these routes.
- OpenRouter is explicitly flagged as estimate-only ("estimated from the models API until
  reconciled", `:810-811`).
- `source` literal includes `provider_cost_api` (`:19`) — i.e. an authoritative path
  exists upstream — but the basis falls through to `official_docs_snapshot`/`none` for
  self-hosted routes.

Implication for Isol8: a metering hook that keys billing off Hermes's `cost_usd` inherits
an estimate that silently becomes `None` for the exact (open-weight, self-hosted) models
Hermes is most likely to run. Isol8 would need to compute cost itself from the token
counts + its own price table, the same way it does for Bedrock today — Hermes does not
hand it a trustworthy dollar figure.

---

## Per-run vs streamed

Usage is **per-run / terminal-only**, not streamed incrementally:
- The `usage` object is attached only to terminal events: `run.completed` (`:3074-3078`),
  the Responses `response.completed`/`response.failed` envelopes (`:2011`, `:1987`), and
  the sync chat-completions return (`:1323`).
- Streaming events during a run carry NO usage: `tool.started` (`:2836`),
  `tool.completed` (`:2844`), `reasoning.available` (`:2853`), `message.delta` (`:2959`),
  `approval.request` (`:2990`) — none include a usage field.
- The numbers are cumulative session totals (the `session_*` accumulators), so the
  terminal payload reflects the whole run, including all internal API calls and tool
  loops, in one shot. There is no mid-run token meter to drive real-time budget cutoff;
  Isol8's `check_budget()`-style pre-flight gate cannot be fed from the run stream.

---

## Cross-checks worth flagging to the drift agent

- Delegated/child runs accumulate cost separately and are rolled into the parent at
  `tools/delegate_tool.py:2289` (`parent_agent.session_estimated_cost_usd = current +
  _children_cost_total`) and child tokens read at `:1680-1681`. A naive Isol8 meter that
  only reads the top-level agent's `usage` may **double-count or miss** delegated subagent
  spend depending on timing — worth verifying the parent totals are final at
  `run.completed`.
- Per-call token deltas are ALSO persisted to a session SQLite DB
  (`conversation_loop.py:1532-1550`, `update_token_counts(... estimated_cost_usd=...)`).
  That is on EFS-style local storage — same WAL/SQLite-on-EFS corruption risk Isol8 already
  hit with OpenClaw (see memory `project_efs_sqlite_wal_two_task_corruption`). Billing data
  living in a per-session SQLite file is a reliability concern for the metering design.

---

## Six-bullet summary — exact usage field shape

- The `run.completed` `usage` payload (`api_server.py:3046-3050`, emitted `:3078`, stored
  `:3084`) is exactly THREE integer fields: **`input_tokens`, `output_tokens`,
  `total_tokens`** — sourced from `agent.session_prompt_tokens /
  session_completion_tokens / session_total_tokens` (`run_agent.py:546-550`, accumulated
  `conversation_loop.py:1492-1494`).
- **NO USD cost in that payload.** `cost_usd`/`cost_status`/`cost_source` exist on the
  agent (`run_agent.py:555-557`, accumulated `conversation_loop.py:1520-1523`) but are
  never written into any api_server.py wire payload — the spec's cited entry point is
  billing-incomplete.
- Cost is surfaced only by a DIFFERENT gateway, `tui_gateway/server.py:1316-1318`
  (`usage["cost_usd"]`, `usage["cost_status"]`), via `estimate_usage_cost`.
- That cost is an **estimate** (`status` max = `"estimated"`, `usage_pricing.py:804`)
  computed from a local docs-snapshot price table; it returns **`amount_usd=None`,
  `status="unknown"`** whenever the model/route has no pricing entry
  (`usage_pricing.py:765, 770-791`) — the common case for self-hosted Hermes / OpenRouter.
- Usage is **per-run terminal-only, not streamed**: no usage on `message.delta`,
  `tool.*`, `reasoning.available`, `approval.request` (`api_server.py:2836-2990`); the
  three numbers are cumulative session totals delivered once at run completion — no
  mid-run meter for real-time budget gating.
- The OpenAI-compat path uses different key names — **`prompt_tokens` /
  `completion_tokens` / `total_tokens`** (`api_server.py:1323-1327`, `:2011-2015`) — so a
  metering hook must handle both naming conventions; delegated-subagent cost is folded into
  the parent separately (`delegate_tool.py:2289`) and per-call deltas are written to a
  session SQLite DB (`conversation_loop.py:1532-1550`).
