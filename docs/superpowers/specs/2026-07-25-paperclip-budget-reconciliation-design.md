# Paperclip budget-fence reconciliation

Status: implemented (PR `feat/paperclip-budget-reconciliation`)
Closes the structural half of #1426 (trial-cap run storms).

All Paperclip citations are against the **deployed fork commit**
`2fdafe831` (`Isol8AI/paperclip`, branch `isol8/2026.7-circuit-breaker`,
image digest pinned in `paperclip-version.json`). The local
`~/Desktop/paperclip-fork` working tree sits on the older 2026.6 line —
line numbers there do **not** match.

---

## 1. The problem

Paperclip already owns a pre-run budget fence. Isol8 has never armed it.

`budgets.getInvocationBlock` (`server/src/services/budgets.ts:718`) is
consulted by every Paperclip-initiated agent wake — the heartbeat wake
funnel (`heartbeat.ts:15376`), issue-assignment wakes, run continuation
(`heartbeat.ts:9231`, `:10710`), the recovery service, the productivity
reviewer, and the plugin host. When it returns a block the caller records
a *skipped request* (`heartbeat.ts:15381`, `writeSkippedRequest("budget.blocked")`)
and throws `conflict(...)` — **before** any `heartbeat_runs` row is
inserted.

Routine dispatch calls that funnel with `rethrowOnError: true`
(`routines.ts:1865`), so a blocked wake deletes the just-created issue,
finalizes the routine run as `failed`, and ticks the circuit breaker
(`routines.ts:1899` → `routine-circuit-breaker.ts:20`). Three consecutive
failures (`instance-settings.ts:190`/`:201`, `autoPauseDefaultThreshold: 3`,
`autoPauseDefaultEnabled: true`) auto-pause the routine.

That is exactly the "park, don't storm" behaviour we want, and it is
native. Today `paperclip_provisioning.provision_org` creates companies
with no budget state at all, so Paperclip happily dispatches wakes into a
container whose inference the Isol8 bedrock-gate is already refusing.

## 2. Paperclip's budget-policy model (what it actually is)

A budget policy is a row in `budget_policies`:

| field | type | notes |
|---|---|---|
| `scopeType` | `company` \| `agent` \| `project` | `constants.ts:790`-ish, `BUDGET_SCOPE_TYPES` |
| `scopeId` | uuid | must belong to the company |
| `metric` | `billed_cents` — **the only value** | `constants.ts:793` |
| `windowKind` | `calendar_month_utc` \| `lifetime` — **the only two** | `constants.ts:796` |
| `amount` | int, cents | `0` deactivates the policy |
| `warnPercent`, `hardStopEnabled`, `notifyEnabled`, `isActive` | | defaults 80 / true / true / true |

Write API: `POST /api/companies/{companyId}/budgets/policies`
(`routes/costs.ts`, `upsertBudgetPolicySchema` in
`packages/shared/src/validators/budget.ts:9`), plus the convenience
`PATCH /api/companies/{companyId}/budgets` `{budgetMonthlyCents}` which
writes the column *and* upserts the equivalent company-scope monthly
policy.

`getInvocationBlock(companyId, agentId, ctx)` evaluates, in order
(`budgets.ts:718`-`860`):

1. `company.status === "paused"` → **block** (`budgets.ts:745`). Reason
   text depends on `company.pauseReason`.
2. active company-scope `billed_cents` policy with `hardStopEnabled` and
   `amount > 0`, and `computeObservedAmount >= amount` → block.
3. `agent.status === "paused" && agent.pauseReason === "budget"` → block
   (`budgets.ts:782`).
4. same as (2) for the agent scope, then the project scope.
5. otherwise `null`.

`computeObservedAmount` (`budgets.ts:143`) is
`SUM(cost_events.cost_cents)` over the policy's scope and window.

### 2.1 Why an amount-based policy cannot express Isol8's predicate

Three independent blockers:

- **Wrong ledger.** Observed spend comes from Paperclip's own
  `cost_events`, written only by `heartbeat.updateRuntimeState` from what
  the adapter reports (`costs.createEvent`, heartbeat). Isol8's
  authoritative numbers live elsewhere: the free-tier daily counter is
  written by the Bedrock invocation-log meter Lambda into DynamoDB
  (`billing_repo.get_trial_daily_spend`), and paid credits live in
  Metronome. Neither is ever written to Paperclip. The openclaw-gateway
  adapter only emits `costUsd` when the OpenClaw agent meta carries a
  positive one, so for Isol8 tenants `SUM(cost_cents)` is ~0 forever and a
  hard-stop amount would never trip.
- **Wrong window.** `BUDGET_WINDOW_KINDS` is `["calendar_month_utc",
  "lifetime"]` (`constants.ts:796`). The free tier's cap is **$3 per UTC
  day**. There is no daily window and no way to add one without a fork
  change.
- **Wrong shape for credits.** Metronome credit exhaustion is a balance
  crossing zero, not a spend total against a fixed monthly ceiling.

So arming the fence via `amount` is structurally impossible without
modifying the fork.

### 2.2 The lever we do use: company pause

`companies.status = "paused"` is checked **first** and is completely
independent of cost events. It is also the budget subsystem's *own*
representation of a hard-stopped scope — `pauseAndCancelScopeForBudget`
(`budgets.ts:252`) writes exactly `status: "paused", pauseReason: "budget"`
when a policy trips, and `resumeScopeFromBudget` (`budgets.ts:261`)
reverses it. Writing that state from Isol8's billing predicate *is*
arming the native fence; nothing is invented.

Write API: `PATCH /api/companies/{companyId}` with `{"status": "paused"}`
/ `{"status": "active"}`. `updateCompanySchema`
(`packages/shared/src/validators/company.ts:26`, `status` at `:29`) admits
`COMPANY_STATUSES = ["active","paused","archived"]` (`constants.ts:1`).
Authz is `assertSameCompanyCeoAgentOrBoard` (`routes/companies.ts:415`
→ `:93` → `assertCompanyAccess`), which a board actor holding company
membership satisfies. The Isol8 **instance-admin** Better Auth session is
such an actor: `POST /api/companies` grants the creating actor `owner`
membership, and `provision_org` creates every Isol8 company with the admin
cookie — so no per-owner sign-in is needed.

Blast radius of company pause is small and correct: across the whole
deployed server, `companies.status === "paused"` is only read by
`budgets.getInvocationBlock` (`budgets.ts:745`), the budget scope-record
resolver, and the `paused → active` agent-restore branch of
`PATCH /companies/:id`. It does **not** break Teams reads, the inbox, issue
CRUD, or the user's own Isol8↔OpenClaw chat (that path never touches
Paperclip; `gate_chat` fences it).

### 2.3 Rejected alternatives

- **Agent pause.** `getInvocationBlock` only honours it when
  `pauseReason === "budget"` (`budgets.ts:782`), which
  `POST /api/agents/{id}/pause` does not set; it also fights the circuit
  breaker's own auto-pause and any manual resume in the Teams UI, and costs
  one write per agent instead of one per company.
- **Mirroring Isol8 spend into `POST /companies/{id}/cost-events`.** Needs
  a per-agent attribution Isol8 does not have (the meter is per-owner), the
  meter Lambda has no Paperclip reachability, and the daily window still
  does not exist.
- **Setting `pauseReason: "budget"`.** Not expressible —
  `updateCompanySchema` has no `pauseReason` field. See §6.

## 3. The predicate

New module `core/services/paperclip_budget_fence.py`, function
`fence_reason(owner_id, account=None) -> str | None`. It mirrors
`connection_pool.gate_chat`'s layering and is **provider-aware**:

```
account = billing_repo.get_by_owner_id(owner_id)

# no billing row at all -> OPEN (see below)
if account is None:                      return None

# L1 entitlement: a row that exists and is not active
if not is_subscription_active(account):  return "subscription_inactive"

# L2 free tier: the single trial predicate (expired OR $3/day cap)
if account["subscription_status"] == "trial_local":
    return await trial_cap.reason_for_account(owner_id, account)   # or None

# L3 paid: ONLY bedrock_claude draws Isol8-billed credits
if account.get("provider_choice") == "bedrock_claude":
    if await metronome_gate.is_owner_blocked(owner_id):
        return "credits_exhausted"

# chatgpt_oauth / byo_api_key / anything else -> OPEN, always
return None
```

**Provider safety.** The single worst failure is fencing a ChatGPT-OAuth
or BYOK owner: their inference is on their own account/key, so Isol8 budget
says nothing about whether they may run. Three structural guarantees:

1. L2 is gated on `subscription_status == "trial_local"`, not on
   `provider_choice`. An OAuth/BYOK owner is never `trial_local` (the free
   tier is Bedrock-only), and even a `trial_local` row whose
   `provider_choice` was written early by `POST /billing/trial-checkout`
   is judged by `trial_cap.reason_for_account`, which itself re-checks
   `subscription_status == "trial_local"` and returns `None` otherwise.
2. L3 is gated on `provider_choice == "bedrock_claude"` — the same single
   `_CREDIT_DRAWING_PROVIDER` fence `bedrock_enforcement.enforce_on_low_balance`
   uses. A Metronome balance of 0 is *normal* for an OAuth/BYOK owner
   (they have no credit grant) and must never be read as exhaustion; this
   is the exact bug that once killed free-tier signups ~30s after
   provisioning.
3. There is no default-deny branch. Anything the ladder does not
   positively identify as out-of-runway returns `None`.

**Deliberate divergence from `gate_chat`: a MISSING billing row is OPEN,
not blocked.** `gate_chat` blocks it (a user staring at a chat box gets a
card and can act). The fence is a background write with no card; absence
of data is not evidence of exhaustion, and fencing on it would pause a
legacy owner's whole company silently. Rows that exist with a terminal
status (`canceled`, `unpaid`, `past_due`, …) still fence via L1.

## 4. Reconciliation, not set-once

A policy written once at provision time is worse than none: it would
fence a now-paid owner forever. The fence is therefore **derived state**,
recomputed and re-applied at three triggers.

### 4.1 Applied-state stamp

Reconcile must not issue a Paperclip round-trip on every trigger, and must
never resume a pause it did not create. Both are solved by a stamp on the
paperclip-companies **owner row**: `budget_fence_state ∈ {"open","fenced"}`
(absent ⇒ `"open"`), written by `PaperclipRepo.set_budget_fence_state`
alongside `budget_fence_reason` / `budget_fence_updated_at`.

```
desired = "fenced" if fence_reason(...) else "open"
applied = row.budget_fence_state or "open"
if desired == applied:   return              # no HTTP at all
if desired == "fenced":  PATCH {"status":"paused"}; stamp fenced
else:                    PATCH {"status":"active"}; stamp open
```

The asymmetry is the safety property: **we only ever write
`status: "active"` to undo a pause we ourselves stamped.** If Paperclip's
own budget subsystem (or an operator) paused the company, our stamp says
`open`, `desired == applied`, and we do nothing. This matters because
`GET /api/companies/{id}` does not project `pauseReason`
(`services/companies.ts:247`), so we cannot tell the two apart over the
wire — the stamp is the only reliable discriminator.

The converse hazard is also closed: because we pause with
`pauseReason = NULL` (unsettable — §6), `resumeScopeFromBudget`'s
`WHERE pauseReason = 'budget'` filter (`budgets.ts:261`-`297`) cannot lift
our fence, so an operator setting a monthly budget in the Paperclip UI
does not silently disarm it.

`budget_fence_state` is deliberately **not** carried by
`PaperclipRepo.put()` (the whole-item write). A reprovision that re-puts
the row drops the stamp back to `open`, which is the safe direction: the
next reconcile re-derives and re-applies.

### 4.2 Triggers

| # | Site | Covers | Needs a browser tab? |
|---|---|---|---|
| 1 | `paperclip_provisioning.provision_org`, after the row goes `active` | initial arming; reprovision of an already-capped owner | no |
| 2 | `provision_gate.evaluate_provision_gate` (the `/container/status` poll, ~3s) | trial cap crossed mid-session, UTC-midnight reset, trial→paid upgrade, subscription cancel | yes |
| 3 | `metronome_gate.apply_fresh_balance` — "the single *a fresh Metronome balance just arrived* hook" | paid-Bedrock credit exhaustion **and** top-up restore | **no** |

Trigger 3 is the important one: it is fed by the Metronome low-balance
webhook and by the gate's own lazy refresh, both of which fire without any
user present, and it is the one place where revoke and restore already
share a chokepoint — so the fence cannot disagree with the IAM revoke.

Triggers deliberately **not** added:

- `connection_pool.gate_chat`. Any chat send implies an open tab, which
  implies trigger 2 already ran within ~3s. Not worth a hook on the
  hottest path in the system.
- The Stripe subscription webhook. An upgrade happens with the user in the
  browser (checkout redirect), so trigger 2 covers it within one poll.

All three call `schedule_reconcile(owner_id)`, which wraps
`reconcile(owner_id)` in `asyncio.create_task` and swallows everything —
the fence must never add latency to, or fail, the path it hangs off. It also
no-ops entirely when `PAPERCLIP_INTERNAL_URL` is unset (local dev / unit
tests: no Paperclip, nothing to fence, not even a DynamoDB read).

`schedule_reconcile` allows **one in-flight reconcile per owner**. That is
not just poll hygiene — it is what bounds a genuine cycle: trigger 3 runs
inside `apply_fresh_balance`, and the reconcile's own predicate calls
`metronome_gate.is_owner_blocked`, which on a stale fast-gate row refreshes
and calls `apply_fresh_balance` again. If the `set_balance` write ever fails
(it is best-effort and swallowed) the row stays stale and that becomes an
unbounded Metronome-call loop. The guard caps it at depth 1.

### 4.3 Accepted residual gap

A `trial_local` owner who crosses the $3 cap with **no tab open** stays
un-fenced until their next poll. That window is bounded in practice:

- trial owners cannot create routines at all
  (`routers/teams/_guards.require_always_on_entitlement`), so the dominant
  storm source does not exist for them;
- their container scales to zero with the tab, and no Paperclip dispatch
  path calls `start_user_service`, so wakes hit a dead container and are
  bounded by the circuit breaker (3 consecutive failures → routine
  auto-pause);
- the meter Lambda has already denied the gate token and set
  `desiredCount=0`, so no tokens are being generated regardless.

Closing it properly needs a periodic sweep, which the codebase explicitly
avoids ("no long-running workers, prefer event-based"). Documented, not
built.

## 5. Failure modes

| Failure | Behaviour | Why acceptable |
|---|---|---|
| Paperclip unreachable / 5xx on PATCH | stamp is **not** written; logged + `paperclip.budget_fence.error` metric | next trigger retries; fence is idempotent by construction |
| Admin session expired (401) | one retry after `invalidate_admin_session()`, same as provisioning | matches the established pattern |
| Billing read raises | `reconcile` swallows, no PATCH, no stamp | fail-**open**: never fence on unknown state |
| `metronome_gate.is_owner_blocked` raises | swallowed by the same handler → no fence | it already never raises on Metronome/DDB trouble; belt-and-braces |
| Owner has no Paperclip company (or it is not `active`) | no-op | nothing to fence |
| Stamp says `fenced`, company was manually resumed in Paperclip UI | next reconcile sees `desired == applied` and does nothing — the fence stays lifted | operator override wins; the meter Lambda + bedrock-gate are still the real enforcement |
| Concurrent reconciles for one owner | last writer wins; both compute the same `desired` | the operation is idempotent; no lock needed |
| Reprovision drops the stamp | next reconcile re-applies | safe direction |

The fence is **UX/blast-radius control, not enforcement**. The authoritative
stop remains the bedrock-gate's pre-flight reservation plus the meter
Lambda's backstop kill. Nothing here is allowed to be the only thing
standing between an owner and a runaway spend.

## 6. Fork-side gaps (not blocking, but worth knowing)

1. **`pauseReason` is not settable over the API.** `updateCompanySchema`
   has no such field, so an Isol8-fenced company gets
   `pauseReason = NULL` and `getInvocationBlock` returns the generic
   *"Company is paused and cannot start new work."* rather than the budget
   wording. As noted in §4.1 this is a net safety win (it decouples our
   fence from `resumeScopeFromBudget`), so it is being left alone rather
   than patched.
2. **`GET /api/companies/{id}` does not project `pauseReason`**
   (`services/companies.ts:247`), so an external caller cannot tell *why* a
   company is paused. Worked around with the DDB stamp.
3. **No daily budget window and no non-`billed_cents` metric.** Adding
   `daily_utc` to `BUDGET_WINDOW_KINDS` would let a future version express
   the $3/day cap as a real policy — but it would still need Isol8 spend
   mirrored into `cost_events`, so it is not obviously worth it.

None of these blocks this implementation; no fork change is required.
