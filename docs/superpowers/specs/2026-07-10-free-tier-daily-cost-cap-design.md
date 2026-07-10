# Free-Tier $3/Day Cost Cap — Design

> **Phase B, sub-spec 1 of 3.** Phase B ("guardrails that make the free tier
> safe for real users") decomposes into: **(1) this $3/day cost cap**, (2)
> always-on entitlement gating, (3) event-driven scale-to-zero. The cap ships
> first because it is the actual "without the cost" safety gate — without it,
> every day of the (currently uncapped, dev-only) free trial is unbounded
> Meta inference spend.

**Goal:** Cap a `trial_local` (free-tier) owner's Meta inference spend at **$3
per UTC day** (≈$9 over the 3-day trial), enforced fail-closed at the chat
chokepoint, measured by a tamper-proof backend cost computation, stored on an
isolated counter — so the free tier can be exposed to real users without
runaway cost.

**Non-goals (owned by other sub-specs / phases):** always-on entitlement
enforcement (channels/webhooks/routines gating — sub-spec 2), scale-to-zero
(sub-spec 3), the trial-expired → paywall → provider-upgrade conversion flow
and the upgrade *action* behind the cap's CTA (Phase C).

---

## Context (verified 2026-07-10)

- **Phase A is merged** (`#1201`, `9619189e`, deployed to **dev only**). A
  brand-new personal signup auto-provisions a `trial_local` container on Meta
  Muse Spark (`meta/muse-spark-1.1`). It is **uncapped** today — this sub-spec
  closes that.
- **Meta spend records as $0 today.** `usage_service.record_usage` computes
  Isol8 spend only for `amazon-bedrock/` models; every other provider
  (ChatGPT OAuth / OpenAI / BYO / **meta**) is treated as user-paid and
  recorded as spend 0. Meta free-tier inference is **Isol8-paid**, so this is
  a real cost currently tracked nowhere.
- **OpenClaw's `estimatedCostUsd` is unusable for meta.** The meta extension's
  `modelCatalog` hardcodes `muse-spark-1.1` `cost: { input: 0, output: 0,
  cacheRead: 0, cacheWrite: 0 }`, and OpenClaw derives cost as
  `tokens × rate`. So its per-turn `estimatedCostUsd` for meta is **$0**.
  Muse Spark is brand-new (launched 2026-07-09) and is not in the external
  OpenRouter/LiteLLM pricing catalogs either. **The cap must not rely on any
  OpenClaw-reported dollar figure.**
- **Token counts are real.** `inputTokens` / `outputTokens` / `totalTokens`
  come from Meta's own API response (independent of the zeroed rate), so the
  backend can recover cost as `tokens × our own rate`.
- **The analytics `usage-counters` table is not trusted** (cache tiles always
  0, ~30× Bedrock undercount historically, unrun reconcile scripts). The cap
  uses its **own isolated counter**, not that pipeline.

### Meta Muse Spark pricing (our authoritative constant)

`meta/muse-spark-1.1`: **$1.25 / 1M input tokens**, **$4.25 / 1M output
tokens** (reasoning/"thinking" tokens bill as output). Lives as a backend
constant, the single source of truth for the cap.

---

## Architecture

Three moving parts, all in the trusted backend:

1. **Isolated daily counter** — a date-keyed field on the existing
   `billing_accounts` row.
2. **Backend cost function** — `tokens × our rate`, tamper-proof.
3. **Reserve → gate → reconcile** at the existing chat chokepoint.

No new DynamoDB table, no scheduler, no dependence on container config or
OpenClaw's numbers.

### 1. Data model — isolated daily counter

A new attribute on the `billing_accounts` item (the row `gate_chat` already
reads):

```
meta_daily_spend_microdollars: { "<UTC-date>": <int microdollars> }
    e.g. { "2026-07-10": 214000, "2026-07-11": 41000 }
```

- **Microdollars** (int, 1 USD = 1e6) to keep atomic integer arithmetic;
  matches the existing `*_microdollars` convention in `usage_service`.
- **Atomic** DynamoDB `ADD meta_daily_spend_microdollars.<date> :delta` so
  concurrent turns cannot race the counter (no read-modify-write).
- **Self-resetting**: a new UTC day is a new map key → a fresh $3, with no
  reset job. Old keys are harmless (bounded by the ≤3-day trial; a TTL-style
  prune is optional, out of scope).
- Only written for `trial_local` owners. Never touched for paid owners.

### 2. Backend cost function (tamper-proof)

```
cost_microdollars(input_tokens, output_tokens) =
    round(input_tokens  * META_INPUT_RATE_MICRO_PER_TOKEN)
  + round(output_tokens * META_OUTPUT_RATE_MICRO_PER_TOKEN)
```

Rates derived from the pricing constant above and held in backend code — a
free user editing `openclaw.json` cannot change them. **Cache-token caveat:**
cache-read/write token counts may not reach the backend (`sessions.list`
omits them). The cap ignores the cache portion, which *under*-counts a small,
cheap slice of real cost — it errs toward the user, acceptable for a $3 gate.
(We separately set the real rates in the trial container's config so the
in-app cost UI does not show users $0; that is cosmetic and is **not** the
cap's source of truth.)

### 3. Reserve → gate → reconcile

The chat chokepoint decides *before* a turn runs, but real cost is known
*after* it finishes. Reserve-worst-case up front, true it down on completion:

```
PRE-SEND  (gate_chat, before forwarding the turn to the container)
  spent = sum(meta_daily_spend_microdollars[today])   # this owner, today
  if spent >= CAP ($3):
      return { blocked: true, code: "meta_daily_cap", message: ... }   # BLOCK
  reserve = worst_case_turn_cost()                    # clamp-bounded, see below
  ADD meta_daily_spend_microdollars.today += reserve  # atomic reservation
  remember `reserve` for this run (keyed by session/run id)

TURN RUNS

POST-TURN (usage callback, on chat.final)
  actual = cost_microdollars(input_tokens, output_tokens)   # backend-computed
  ADD meta_daily_spend_microdollars.today += (actual - reserve)   # reconcile
      # net effect: counter reflects true spend; the reservation already
      # prevented any parallel burst from crossing $3 mid-flight
  FAIL-CLOSED: if the turn errors / disconnects / actual is unavailable,
      the reserve STAYS (no reconcile, no refund).
```

- **Concurrency:** the atomic reservation is the guard. N parallel turns each
  reserve worst-case before running; the one whose reservation would cross $3
  is blocked at pre-send. No TOCTOU window.
- **Worst-case reserve** is bounded by the per-turn output clamp (below):
  `worst_case = cost_microdollars(contextTokens, maxTokens)` ≈
  `200000 × $1.25/M + 32000 × $4.25/M ≈ $0.386`. Because we reconcile down to
  the (usually far smaller) real cost, a normal day buys **many** turns; the
  worst-case reserve only bites a burst of parallel turns.

### Per-turn clamps (set here, deferred from Phase A)

Phase A intentionally deferred these; the cap needs them to bound the reserve
and per-turn cost. Set on the trial container's generated `openclaw.json`
(`agents.defaults`, meta path):

- `effort: medium` (Muse Spark defaults high; thinking bills as output)
- `contextTokens: 200000`
- `maxTokens: 32000`

These belong in `config.py`'s meta/`trial_local` config generation.

---

## Data flow

```
Client ──chat──▶ backend chat chokepoint (gate_chat, pre-send)
                    │  read meta_daily_spend[today]; ≥$3 → BLOCK (structured)
                    │  else atomic ADD reserve; record reserve for run
                    ▼
                 OpenClaw container (Meta Muse Spark)   ──▶ Meta API
                    │  chat.final { inputTokens, outputTokens, ... }
                    ▼
                 usage callback (post-turn)
                    │  actual = tokens × our rate
                    └─ atomic ADD (actual − reserve)   [fail-closed: keep reserve]
```

**Touchpoints (to confirm exact lines at plan time):**
- Pre-send gate: `core/gateway/connection_pool.py::gate_chat`, invoked from
  `routers/websocket_chat.py` before forwarding.
- Post-turn reconcile: the usage/`chat.final` callback path
  (`_safe_record_usage` in `routers/websocket_chat.py` → `usage_service`).
- Counter storage: `core/repositories/billing_repo.py` (new atomic
  reserve/reconcile helpers) on the `billing_accounts` item.
- Clamps + cosmetic in-container rates: `core/containers/config.py`.

---

## UX

- **At the cap:** `gate_chat` returns the existing structured blocked shape
  the client already renders — `code: "meta_daily_cap"`, message *"You've used
  today's free allowance — resets tomorrow. Upgrade to keep going."* The
  upgrade **action** is Phase C; this sub-spec renders the message plus a
  placeholder CTA.
- **Daily reset:** keyed by **UTC date**; a new UTC day is a fresh $3, no job.
  "Resets tomorrow" = UTC midnight. (Revisit to account-local later if
  desired — noted, not in scope.)
- **Trial window vs daily cap are separate gates.** $3/day for the trial's
  duration (`trial_end`, ~3 days → ~$9 total). Hitting the daily cap ≠ trial
  over — the user returns tomorrow with a fresh $3. `trial_end` passing is the
  *separate* trial-expired gate (Phase C conversion), not owned here.
- **Scope:** only `trial_local` owners' meta usage is metered/capped. Paid
  owners are entirely unaffected (no reserve, no counter writes, no gate).

---

## Edge cases

- **Reserved but never reconciled** (container crash / mid-turn disconnect):
  reserve stays (fail-closed) — the owner loses that slice of today's budget;
  self-heals at UTC rollover. Correct direction for a cost gate.
- **Concurrent burst:** atomic reservation blocks the turn that would cross
  $3 at pre-send.
- **Missing/partial token counts on `chat.final`:** treat as fail-closed —
  keep the worst-case reserve rather than reconcile to an unknown/low value.
- **Cache tokens absent:** accepted small under-count (errs toward user).
- **Clock/date boundary:** UTC date computed server-side at gate time; a turn
  reserved just before midnight and reconciled just after applies both writes
  to whichever key each step reads — bounded, harmless (at most one turn's
  worth crosses the boundary).

---

## Testing

Unit tests (backend):
- `cost_microdollars` math for representative token counts (input/output).
- Atomic reserve then reconcile: counter ends at the real cost, not
  `reserve + real`.
- **Block at ≥$3**: a pre-send with `spent ≥ CAP` returns the structured
  blocked payload and does **not** forward the turn.
- **UTC rollover**: spend on day N does not count against day N+1's budget.
- **Fail-closed**: a turn that errors / yields no usable cost leaves the
  reserve applied (no refund).
- **Concurrency**: simulate N parallel pre-send reservations; the total
  reserved never lets forwarded turns exceed the cap (the crossing turn
  blocks).
- Paid (`active`) owner path: no counter write, no gate — untouched.

---

## Open items to resolve at plan time (not blocking the design)

- Confirm the exact `chat.final` usage payload fields the backend already
  receives (input/output token names; whether any cache counts are present).
- Confirm the run-id/session-key used to correlate a pre-send reserve with its
  post-turn reconcile.
- Decide the microdollar rate constants' exact integer form
  (`META_INPUT_RATE_MICRO_PER_TOKEN` etc.) to avoid float drift.
