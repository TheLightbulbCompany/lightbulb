# Wake-Admission Gate — Design

**Status:** APPROVED, build now (not flag-gated per owner decision; and — per a
later owner decision — no env kill-switch either: the gate is one unconditional
code path. Rationale: fewer gating variables, and prod has no users yet, so
there is no blast radius to hedge against.)
**Date:** 2026-07-25
**Fixes:** #1426 at the source. Move 2 / Property B of
`2026-07-24-onboarding-architecture-remediation-design.md`.

## Problem (plain)

A new free user signs up. Friday's intro run burns the $3/day allowance. The
platform then keeps **waking** her — task assignments, automations — and each
wake dies instantly at the bedrock-gate with `trial_daily_cap`. The user's first
experience is a wall of failed runs instead of one clear "out for today, upgrade".

Observed in the campaign: **12 dead runs in ~3 minutes** (6 automation + 6
assignment; zero periodic heartbeat).

Money was never at risk — the bedrock-gate's synchronous pre-flight reservation
refuses admission before a token is generated. This is an **experience** bug.

## Root cause (located precisely)

The budget predicate is already checked at most doors: container start
(`ecs_manager:1186`), `container_wake`, `/container/recover`, chat-steer, SES
inbound, and the Teams BFF guards (`routers/teams/_guards.py:62`).

It is **not** checked at one door: the **operator-relay RPC dispatch**. Paperclip's
seed agents use `adapter_type="openclaw_gateway"` pointed at `wss://ws-<env>.isol8.co`,
and connect as an operator relay (`websocket_chat.py:478`,
`is_operator_relay = x_client_id == PAPERCLIP_OPERATOR_CLIENT_ID`; line 492 notes
these are explicitly NOT frontend chat). Their runs arrive as `msg_type == "req"`,
while `gate_chat` is called **only** on `msg_type == "agent_chat"`
(`websocket_chat.py:2145`) — the human-typed path. So assignment/automation wakes
bypass every budget check.

**Correction found during implementation:** the relay does NOT fall through to
the generic `msg_type == "req"` handler. `ws_message` short-circuits operator
traffic *above* it — `if connection.get("connection_type") == "operator":` →
`handle_operator_message(...)` → `return`. So the relay bypasses BOTH the typed
`agent_chat` handler AND the RPC proxy (including the proxy's
`_RUN_DISPATCH_METHODS` refusal). The gate therefore belongs inside that
operator branch, not at the `req` handler.

The run-starting method is exactly **`agent`**: the adapter sends
`client.request("agent", agentParams)`
(`packages/adapters/openclaw-gateway/src/server/execute.ts:1289`) and then
`client.request("agent.wait", {runId})` (:1319) to await it. Only the former is
gated — refusing `agent.wait` would strand a run that has already started.

## Fix

Call **the same `pool.gate_chat`** at the operator-relay dispatch that the human
`agent_chat` path already calls. Agent wakes and human sends then obey identical
rules — one contract, no drift.

### Why reuse gate_chat (provider safety — the load-bearing reason)

`gate_chat` (`connection_pool.py:2417`) is already provider-aware:

| Path | Layer 1 (subscription active) | Layer 2 ($3/day trial cap) | Layer 3 (Metronome credits) |
|---|---|---|---|
| Free trial (`trial_local`) | ✅ | ✅ gated on `subscription_status == "trial_local"` | — |
| Paid Bedrock (`bedrock_claude`) | ✅ | skipped | ✅ |
| ChatGPT OAuth | ✅ | skipped | **skipped** (no prepaid draw) |
| BYOK | ✅ | skipped | **skipped** (spend on own key) |

So OAuth/BYOK owners can NEVER be blocked on budget they don't consume; paid
Bedrock owners are blocked only on genuinely exhausted credits. A trial-only
predicate (`trial_block_reason`) would have been the WRONG choice here — and note
the deliberate warning in `trial_cap.py`: there is no cap-only primitive, because
four doors once used one and checked only half the predicate.

### Park semantics (owner decision)

**Park requires user action.** No queue, no auto-resume, no burst at reset. The
refused wake is dropped with a clear over-budget state; the user upgrades or
returns after reset. Rationale: nothing to drain, no surprise run storm at
midnight.

### Refusal shape — MUST make Paperclip park, not retry

The refusal payload must use the signal the fork's **retry-parking** patch treats
as terminal/parked, NOT a generic error that re-arms its retry machinery —
otherwise we recreate #1426 from the other side. The blocked codes gate_chat
already returns (`subscription_inactive`, `trial_expired`, `trial_daily_cap`,
out-of-credits) are what the frontend renders limit cards for; the relay refusal
carries the same code so the Teams/inbox surface can render one signal.

**Confirmed from the adapter source** (`server/execute.ts`):
`GatewayWsClient.handleMessage` rejects the pending request on `ok: false`,
reading `error.message` / `error.code` / `error.details` into a thrown
`GatewayResponseError`. Its retry classifier is a **substring match on that
message** — it retries only `econnrefused` / `econnreset` / `socket hang up` /
`timeout`. Our gate messages contain none of those, so the adapter itself does
not retry a refusal. (A test pins this: a gate message must never contain one of
those markers.)

**Now also confirmed — the refusal PARKS.** Paperclip's
`scheduleBoundedRetryForRun` fires only when `errorFamily == "transient_upstream"`
(or the codex/claude transient errorCodes, or max-turn exhaustion). The
openclaw-gateway adapter **never sets `errorFamily`/`retryNotBefore`** — verified
zero occurrences in its `execute.ts` — so its failures are terminal by
construction. Nothing re-dispatches a refused wake. The wire shape still lives in
ONE helper, `_wake_refusal_payload()`, so retuning it stays a single edit.

**Downstream consequence:** the routine circuit breaker
(`routine-circuit-breaker.ts`) counts each parked run toward
`consecutiveFailureCount`, and at `autoPauseThreshold` pauses the whole routine
with `autoPauseReason: "consecutive_failures"`. That halts the storm — but the
routine then needs a MANUAL resume even after the cap resets or the owner
upgrades. Consistent with the "park requires user action" decision, but it
pauses the routine, not just the wake.

### Discovered during implementation: a dormant purpose-built hook

The fork adapter already contains **`evaluateRunAllowance`** (`execute.ts:1055+`),
an "Isol8 free-tier pre-run allowance gate" that GETs an
`adapterConfig.runAllowanceUrl`, blocks on `{"allowed": false}` with
`errorCode: "isol8_run_blocked"` **before dialing the gateway**, and fails open on
error/timeout/non-200 — the same fail-open philosophy adopted here. Its own
comment argues `execute()` is the single funnel every Paperclip-initiated run
passes through.

**It is inert: `runAllowanceUrl` appears NOWHERE in this repo** (zero matches
across `apps/backend` and `apps/infra`), so the hook returns immediately and has
never gated anything. That is a large part of why #1426 happened.

The two mechanisms are complementary, not redundant:

- The relay gate (this change) needs no fork change, no image rebuild and no
  per-agent config migration, and covers agents whose `adapterConfig` was seeded
  long ago and will never be updated — the recurring legacy-config trap.
- `runAllowanceUrl` blocks earlier (before the WS dial + device-auth handshake),
  so a refused wake costs nothing, but wiring it needs a new backend endpoint
  **and** a re-seed of every existing agent's `adapterConfig`.

Recommend shipping the relay gate now and treating `runAllowanceUrl` as a
separate follow-up decision.

### Error handling (gate read fails)

`gate_chat` deliberately does NOT swallow exceptions on the chat path (fail
closed — the safe direction for a cost cap). For the WAKE path, prefer
**fail-open** (dispatch the wake) on a gate-read error: spend is already fenced
by the bedrock-gate's synchronous reservation, so a transient DDB/network blip
should not silently drop a paid owner's automation. Log + emit a metric on that
branch. (Different from chat because the failure modes differ: a dropped wake is
lost work; a dropped chat send is a visible error the user can retry.)

### Kill-switch — REMOVED

An earlier draft specified an env `WAKE_GATE_ENABLED` (default ON). The owner
dropped it: no flag, no env var, one unconditional code path. Prod has no users,
so there is nothing to hedge, and a gating variable is one more thing to reason
about. A revert is the off-switch.

## Testing

- Unit: operator-relay dispatch blocked for `trial_local` over cap; NOT blocked
  for ChatGPT OAuth owner with zero credits; NOT blocked for BYOK; blocked for
  `bedrock_claude` with exhausted credits; blocked for inactive subscription;
  fail-open on gate exception. The provider-matrix cases run against the REAL
  `gate_chat`, not a stub, so a regression in the predicate itself fails there.
- Regression: human `agent_chat` path behavior unchanged.
- Live (dev): capped trial owner → assignment/automation produces ZERO new runs
  and one limit signal, instead of a storm.

## Non-goals

- Not touching the bedrock-gate (the real fence) or the meter.
- Not queueing/auto-resuming parked work.
- Not changing the trial cap value or the kickoff prompt (already shipped).
