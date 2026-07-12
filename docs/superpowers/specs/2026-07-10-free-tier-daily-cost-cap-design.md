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

### Meta Muse Spark pricing (our authoritative rate)

`meta/muse-spark-1.1`: **$1.25 / 1M input**, **$4.25 / 1M output** (reasoning
tokens bill as output). Cache-read/write rates: **TBC at plan time** (confirm
from Meta / OpenClaw meta docs; if unknown, set `cacheRead = input rate` as a
conservative upper bound so the in-container `estimatedCostUsd` cannot
under-count cache). Used in two places: **(a)** written into the trial
container's `models.providers.meta.models[0].cost` so `estimatedCostUsd` is
correct, and **(b)** a backend constant for the pre-send **worst-case reserve**.

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
meta_daily_spend_microcents: { "<UTC-date>": <int microdollars> }
    e.g. { "2026-07-10": 214000, "2026-07-11": 41000 }
```

- **Microcents** (int; 1 USD = 1e6 microcents) to keep atomic integer
  arithmetic and match the unit `record_usage` / `estimatedCostUsd` already
  produce (`connection_pool.py:1040`). Named accurately — the existing
  `total_spend_microdollars` is a misnomer that *also* stores microcents;
  don't propagate the wrong name. Cap constant: `CAP = 3_000_000` ($3).
- **Atomic** DynamoDB `ADD meta_daily_spend_microcents.<date> :delta` so
  concurrent turns cannot race the counter (no read-modify-write).
- **Self-resetting**: a new UTC day is a new map key → a fresh $3, with no
  reset job. Old keys are harmless (bounded by the ≤3-day trial; a TTL-style
  prune is optional, out of scope).
- Only written for `trial_local` owners. Never touched for paid owners.

### 2. Cost source: in-container rate → `estimatedCostUsd` (cache-inclusive)

OpenClaw computes a per-turn `estimatedCostUsd` that **includes cache tokens**
(from its internal per-run snapshot). It reads $0 for meta today only because
the meta model catalog bakes the rate to 0. **Fix the rate in the trial
container's config** — `models.providers.meta.models[0].cost =
{ input, output, cacheRead, cacheWrite }` in USD-per-million — and
`estimatedCostUsd` becomes the authoritative, cache-inclusive per-turn cost.

**Why not compute from token counts in the backend:** `sessions.list` (the
backend's post-turn token source) **omits** cache-read/write token counts, and
for a cached conversation cache is up to ~97% of real cost
(`connection_pool.py:1028-1035`). Computing from the backend's input+output
tokens alone would under-count by up to ~30× — the same hole that made the
Bedrock ledger 30× low. `estimatedCostUsd` sidesteps it because OpenClaw
applies the (now-correct) rate to the full cache-inclusive snapshot internally.

**The reconcile point already reads it.** `_fetch_and_record_usage`
(`connection_pool.py:1036-1042`, call to `record_usage` at `:1080`) converts
`estimatedCostUsd` → `raw_cost_microcents` and passes it through. Two small
changes make it flow for meta:
- **`record_usage`** (`usage_service.py:87`) currently honors
  `raw_cost_microcents` only for `amazon-bedrock/` models — extend it to honor
  the meta ref too (else meta still records spend 0 via the token path).
- The cap reads that same per-turn cost to reconcile its counter (§3).

**Tamper posture:** the rate now lives in container config on EFS, so a free
user with exec could edit it back to 0. That vector is closed by **sub-spec 2
(entitlement)**, which locks config/exec for free users; until then the
**fail-closed reserve** (§3) is the floor — a turn whose reconciled cost comes
back 0/missing keeps its worst-case reserve rather than being refunded, so a
zeroed rate cannot fully bypass the cap.

*Unit note:* the internal spend unit is **microcents** (1 USD = 1e6
microcents), despite the misleadingly-named `*_microdollars` fields — mirror
`connection_pool.py:1040` (`round(usd * 1_000_000)`) and stay in microcents
throughout.

### 3. Reserve → gate → reconcile

The chat chokepoint decides *before* a turn runs, but real cost is known
*after* it finishes. Reserve-worst-case up front, true it down on completion:

```
PRE-SEND  (gate_chat, before forwarding the turn to the container)
  spent = sum(meta_daily_spend_microcents[today])   # this owner, today
  if spent >= CAP ($3):
      return { blocked: true, code: "meta_daily_cap", message: ... }   # BLOCK
  reserve = worst_case_turn_cost()                    # clamp-bounded, see below
  ADD meta_daily_spend_microcents.today += reserve  # atomic reservation
  remember `reserve` for this run (keyed by session/run id)

TURN RUNS

POST-TURN (in _fetch_and_record_usage, after the turn settles)
  actual = raw_cost_microcents        # from estimatedCostUsd — cache-inclusive
  if actual is a valid positive number:
      ADD meta_daily_spend_microcents.today += (actual - reserve)   # reconcile
      # net: counter reflects true (cache-inclusive) spend; the reservation
      # already prevented any parallel burst from crossing $3 mid-flight
  else:   # FAIL-CLOSED — turn errored / disconnected / cost 0-or-missing
      leave the reserve in place (no reconcile, no refund)
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
- **`estimatedCostUsd` missing / 0 / unparseable** (errored turn, or a zeroed
  in-container rate): fail-closed — keep the worst-case reserve, no reconcile.
- **Cache-inclusive by construction:** cost comes from `estimatedCostUsd`
  (OpenClaw applies the rate to the full snapshot), so the ~30× cache
  under-count of the token-based path does not apply.
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

- Confirm Meta muse-spark `cacheRead`/`cacheWrite` rates for the in-container
  cost config (else conservative `cacheRead = input rate`).
- **Reserve↔reconcile correlation** (now grounded): the reserve is placed at
  the gate where `session_key` is in scope (`websocket_chat.py:1828`), and
  settled in `_fetch_and_record_usage(session_key, …)`
  (`connection_pool.py:978`). Confirm a per-owner in-memory reserve map keyed
  by `session_key` is the cleanest carrier — `run_id_for_send` is computed
  *after* the gate (`:1958`), so it cannot key the reserve.
- Integer rate form to avoid float drift (microcents-per-token is numerically
  USD-per-million; mirror `bedrock_pricing.ModelRate`).

---

## REVISION 2026-07-11 — count every run + per-entrance stop

`/code-review` (xhigh) found the cap only enforced at the webchat `gate_chat`
chokepoint, so Teams/Paperclip/steer/concurrent-session Meta runs escaped it.
Owner decision: **interactive Teams is free-but-capped; autonomous
cron/channels/routines are switched off (sub-spec 2).** So the cap moves to:
**measure every run** at `_fetch_and_record_usage` (the one layer that observes
all runs — webchat + channel + cron + Paperclip), keying on `is_meta_model` +
session source; **stop at each entrance** — webchat `gate_chat`, interactive
Teams via a read-only over-cap check in the Teams BFF, steer via a cap check;
and gate the pre-send block on the **provisioned provider** (container-row
marker) not the mutable `subscription_status`. Delta plan:
`docs/superpowers/plans/2026-07-11-free-tier-cap-rework-count-all.md`.
