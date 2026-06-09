> Source: dynamic workflow `write-bedrock-metering-plans` (wf_d5c383e2-048), 2026-06-07.
> Companion to the spec `docs/superpowers/specs/2026-06-07-bedrock-metering-enforcement-metronome-design.md` and Phase-1 plans.

# Bedrock Metering Remediation — Deprecation / Deletion Inventory

Scope: everything that becomes dead, deprecated, deleted, or changes ownership once **Phase 1** (AWS invocation-log meter + per-owner AIP/IAM enforcement + thin ledger interface, DDB-backed) lands, and **Phase 3** (Metronome replaces the DDB credit ledger as system-of-record) lands. Phase 2 (dev Metronome sandbox validation) touches no prod code paths, so nothing is deprecated *at* Phase 2 — items move directly from Phase-1 state to Phase-3 deletion.

**Single-deduct-path cutover (the spine):** Today the *only* deduct path is `connection_pool` lifecycle `phase=="end"` → `_record_usage_from_session` → `_fetch_and_record_usage` → `_maybe_deduct_credits` → `credit_ledger.deduct`. Phase 1 introduces the new every-run meter (Plan 3) calling `LedgerBackend.deplete()`. **Plan 4 retires the connection_pool deduct so the meter is the sole biller.** Phase 3 deletes the entire DDB ledger (`credit_ledger.py` + `ddb_ledger_backend.py` + both DDB tables) when Metronome becomes SoR.

Legend — **fate**: `DEPRECATE` (kept but no longer the live path; slated for removal) · `DELETE` (removed) · `KEEP-BUT-CHANGE` (survives with modified behavior/wiring). **WHEN**: Phase 1 | Phase 3.

---

## A. The deduct trigger / interim biller — `connection_pool.py` (RETIRED at Phase 1, deleted at Phase 3)

| file:line | what it is | fate | WHEN | why | replaced by |
|---|---|---|---|---|---|
| `core/gateway/connection_pool.py:1139-1231` | `_maybe_deduct_credits()` — the card-3 deduct on chat completion; applies the `raw * 14 // 10` markup, calls `credit_ledger.deduct`. **The interim biller.** | DELETE | Phase 1 (Plan 4) | This is the web-coupled single billing trigger the spec §2a indicts (needs a live pool connection + a broadcast terminal event → only captures webchat/desktop; 917/917 prod deductions are webchat; autonomous + `phase=="error"` runs unbilled). Replaced by the AWS meter → `deplete()`. Markup moves into the meter (Plan 3 owns pricing; `UsageEvent` carries final `billed_microcents`). | Plan 3 AWS Lambda meter → `LedgerBackend.deplete(event)` |
| `core/gateway/connection_pool.py:1357-1362` | The `stream=="lifecycle"`, `phase=="end"` → `_record_usage_from_session(payload)` billing hook in `_handle_message` | KEEP-BUT-CHANGE → DELETE | Phase 1 (Plan 4) | This is *the* trigger the audit identifies as structurally broken (only fires for `isControlUiVisible==true` broadcasts + needs a live web-coupled connection; never fires on `phase=="error"`). At Phase 1 it must stop driving **deduct**. It may keep driving the *analytics-only* `record_usage` side-write briefly, but the cleanest cutover removes the deduct call from the chain. | AWS meter captures every run regardless of transport/visibility |
| `core/gateway/connection_pool.py:929-973` | `_record_usage_from_session()` — resolves session key → member, spawns `_fetch_and_record_usage` task; tracks `_billing_tasks` | KEEP-BUT-CHANGE → DELETE | Phase 1 (Plan 4) for deduct; full delete with the analytics path | Only purpose is to drive the (broken) capture chain. Once deduct moves to the meter and analytics is sourced elsewhere, this and its `_billing_tasks` machinery are dead. | n/a (meter is out-of-container) |
| `core/gateway/connection_pool.py:997-1138` | `_fetch_and_record_usage()` — RPC `sessions.list`, parses `inputTokens/outputTokens/cacheRead/cacheWrite/estimatedCostUsd`, computes `raw_cost_microcents`, calls `record_usage` + `_maybe_deduct_credits` | DEPRECATE → DELETE | Phase 1 (deduct call removed, Plan 4); DELETE when analytics path retired | The `estimatedCostUsd` interim-cost computation (`:1055-1059`) is the PR #826 cache-inclusive biller — superseded by the meter pricing from AWS-logged token counts (incl. the S3-offload cache path the in-container `sessions.list` can't see). `sessions.list` is tenant-side / web-coupled → not billing-grade. | meter parses cache-correct tokens from the invocation log |
| `core/gateway/connection_pool.py:1880-1912` | The **pre-chat balance gate** — `is_subscription_active` + `credit_ledger.get_balance(owner_id, consistent=True)` → `out_of_credits` block at `<= 0` | KEEP-BUT-CHANGE | Phase 1 | The spec (§2b, §3.2.3) explicitly says **this is the one piece worth keeping** — the synchronous strong-consistent gate. Phase 1: re-point `get_balance` through the `LedgerBackend` interface / local fast-gate flag instead of `credit_ledger` directly. Phase 3: reads the Metronome-synced local fast-gate flag (Metronome balance is async). | `LedgerBackend.get_balance()` (P1) → local fast-gate flag synced from Metronome (P3) |

## B. The DDB credit ledger — `credit_ledger.py` (interim, DELETED whole at Phase 3)

The spec is explicit (§3.5): Plan 1 fixes **only** the `requestId` idempotency hole (via the *new* `DdbLedgerBackend.deplete`, not by editing these functions), and **deliberately leaves** the other holes because the DDB ledger is short-lived. All of `credit_ledger.py` is deleted at Phase 3.

| file:line | what it is | fate | WHEN | why | replaced by |
|---|---|---|---|---|---|
| `core/services/credit_ledger.py` (whole file, 1-307) | The hand-rolled DDB money ledger (`get_balance`/`top_up`/`deduct`/`adjustment`/`set_auto_reload`/`should_auto_reload`) | DELETE | Phase 3 | Spec §3.1/§3.5: "stop maintaining a home-grown money ledger… the existing DDB `credit_ledger` is deprecated → deleted." Metronome becomes SoR for balance/drawdown/idempotency/audit/expiry/reconciliation. | `core/billing/ledger_interface.py` → Metronome adapter |
| `credit_ledger.py:143-227` `deduct()` (incl. **lossy overdraft fallback :173-211** and the `bedrock_invocation_id` arg with **no dedup guard**) | The legacy per-chat deduct | DEPRECATE → DELETE | Phase 1 deprecates (single caller `_maybe_deduct_credits` removed in Plan 4 → becomes dead code); DELETE Phase 3 | Plan 1 §"Why a new `deplete()`" + Self-Review: `deduct()` is **left untouched** (its lossy overdraft fallback at :173-211 is one of the holes deliberately not fixed). The new `DdbLedgerBackend.deplete()` (exactly-once via `TransactWriteItems` on `deduct#{request_id}`, allows negative balance to sidestep the overdraft fallback) replaces it. Once Plan 4 removes the one caller, `deduct()` is orphaned. The lossy-overdraft hole vanishes at Phase-3 deletion. | `DdbLedgerBackend.deplete()` (P1) → Metronome (P3) |
| `credit_ledger.py:103-140` `top_up()` (**non-idempotent**, :111-114 docstring admits "calling twice will credit twice") | Add credits on Stripe top-up webhook | KEEP-BUT-CHANGE (caller-deduped only) → DELETE | Phase 3 | Spec §2b lists `top_up` non-idempotency as a real hole. Plan 1 does **not** fix it (idempotency stays at the webhook-dedup layer in `stripe_webhook_handlers`). Survives Phase 1 unchanged because top-up still funds the DDB interim ledger; deleted at Phase 3 when Stripe Checkout funds a **Metronome prepaid credit** instead. The hole vanishes at deletion. | Stripe Checkout → fund Metronome prepaid commit/credit (spec §3.2.5) |
| `credit_ledger.py:230-260` `adjustment()` (**non-atomic** read-modify-write `max(0, get_balance + amount)` at :242) | Operator manual credit/refund | KEEP-AS-IS (left) → DELETE | Phase 3 | Spec §2b + Plan 1 Self-Review: the non-atomic `adjustment` is a hole **deliberately left** (interim is short). Deleted at Phase 3; Metronome adjustments replace it. Hole vanishes at deletion. **Note:** `adjustment()` has no live caller today (admin grants go through the buggy `issue_credit_for_owner` Stripe-balance path, item C) — so it is already effectively dead. | Metronome credit-grant / adjustment |
| `credit_ledger.py:263-295` `set_auto_reload()` | Persists auto-reload config to the credits row | DELETE | Phase 3 | Auto-reload is a wallet feature Metronome owns natively (auto-recharge, spec §2b). DDB-side config dies with the table. | Metronome auto-recharge |
| `credit_ledger.py:297-307` `should_auto_reload()` | Predicate for auto-reload trigger | DELETE | Phase 3 | **Already dead** — grep shows zero callers (the off-session auto-reload *charge* path was never wired). Removed with the ledger. | Metronome auto-recharge |
| `credit_ledger.py:41-47` `InsufficientBalanceError` | Reserved fail-closed exception | DELETE | Phase 3 | Grep shows zero raise/catch sites. Dead today; removed with the module. | n/a |
| `credit_ledger.py:73-91` `_put_txn()` / `:64-70 _now_iso/_new_tx_id` | Audit-row writer + id helpers | DELETE | Phase 3 | Internal to the deleted module. (`DdbLedgerBackend` writes its own audit row inside the transaction, with `tx_id = "deduct#"+request_id`, NOT via this.) | Metronome audit trail |

## C. The admin-grant bug — `billing_service.py` (left at Phase 1, fixed/retired at Phase 3)

| file:line | what it is | fate | WHEN | why | replaced by |
|---|---|---|---|---|---|
| `core/services/billing_service.py:423-447` `issue_credit_for_owner()` | Admin "credit" that calls `stripe.Customer.create_balance_transaction` — writes the **Stripe customer balance**, NOT the credit ledger (the admin-grant bug: a grant does not unblock a Bedrock user, since the gate reads the ledger) | KEEP-AS-IS (left) → DELETE/REWRITE | Phase 3 | Spec §2b + Plan 1 Self-Review name this as a hole **deliberately left** for the short interim. At Phase 3 the correct path is a **Metronome credit grant**; the Stripe-balance write is wrong-tool (Stripe credits draw down only at invoice finalization, ToS forbids stored-value — spec §2b). Caller `routers/admin.py:383` updates accordingly. | Metronome admin credit grant |
| `core/services/billing_service.py:241-319` `create_credit_top_up_checkout()` | Stripe Checkout (mode=payment) for a one-shot credit purchase; webhook funds the DDB ledger | KEEP-BUT-CHANGE | Phase 3 | Spec §3.2.5 keeps Stripe as **top-up payments only**. The Checkout stays; the post-payment effect changes from `credit_ledger.top_up` (DDB) to **funding a Metronome prepaid credit**. (Description string "Credits deducted at 1.4× Bedrock cost" also needs revisiting — markup now lives in the meter.) | Same Checkout → fund Metronome credit |

## D. Stripe webhook + billing router — the top-up & auto-reload surface

| file:line | what it is | fate | WHEN | why | replaced by |
|---|---|---|---|---|---|
| `core/services/stripe_webhook_handlers.py:30, 315-323` | `credit_top_up` checkout-completed handler → `credit_ledger.top_up(owner_id, …, stripe_payment_intent_id=session.id)` | KEEP-BUT-CHANGE | Phase 3 | The webhook is the idempotency layer top-up relies on (Plan-1 leaves `top_up` non-idempotent on purpose). At Phase 3 the body funds a **Metronome** credit instead of `credit_ledger.top_up`. | Metronome funding call |
| `routers/billing.py:16, 562` `get_credits_balance` → `credit_ledger.get_balance` | Buyer-facing balance read | KEEP-BUT-CHANGE | Phase 3 (route through interface at P1 if desired) | Balance read moves behind `LedgerBackend.get_balance` (P1) and ultimately to Metronome's balance / local flag (P3). | `LedgerBackend.get_balance()` → Metronome |
| `routers/billing.py:607-630, 621` `set_auto_reload` endpoint → `credit_ledger.set_auto_reload` | Auto-reload config endpoint | DELETE | Phase 3 | Auto-reload is Metronome-native; the DDB-backed endpoint + `AutoReloadRequest` schema (:547-550) die with the ledger. | Metronome auto-recharge config |
| `routers/admin.py:383` → `issue_credit_for_owner` | Admin credit-grant endpoint | KEEP-BUT-CHANGE | Phase 3 | Re-pointed to the Metronome grant when item C is rewritten. | Metronome grant |

## E. The provision gate — `provision_gate.py`

| file:line | what it is | fate | WHEN | why | replaced by |
|---|---|---|---|---|---|
| `core/services/provision_gate.py:23, 257-271` (`balance = await credit_ledger.get_balance(owner_id)` at **:261**, `credits_required` Gate) | Pre-provision balance gate for `bedrock_claude` | KEEP-BUT-CHANGE | Phase 1 (interface) / Phase 3 (Metronome) | The gate logic stays (it's the right behavior — block at `<= 0`), but the **source** of the balance read swaps: `credit_ledger.get_balance` → `LedgerBackend.get_balance` (P1) → local fast-gate flag synced from Metronome (P3). Direct `from core.services import credit_ledger` import is removed. | `LedgerBackend` / local fast-gate flag |

## F. Reconciliation + ops scripts that read the DDB/usage ledger

| file:line | what it is | fate | WHEN | why | replaced by |
|---|---|---|---|---|---|
| `scripts/reconcile_bedrock_vs_ledger.py` (whole; reads `usage-counters` token cols + `total_spend_microdollars` RAW spend vs CloudWatch + Cost Explorer) | PR #826 read-only drift reconciler (account-wide; explicitly **not** per-owner because invocation logging wasn't enabled — see its docstring :39-46) | DEPRECATE → DELETE/REPLACE | Phase 1 deprecates its premise; DELETE at Phase 3 | Its entire reason-for-being (the ledger may undercount AWS, account-wide only) is superseded: Phase 1 enables Bedrock **model-invocation logging** (the per-owner, cache-correct source it says is "a future enhancement, deliberately not built here"). Spec §3.7 reduces standing reconciliation to "a one-time cutover validation + a lightweight drift monitor" — Metronome's idempotent ingestion is the accuracy mechanism. The `total_spend_microdollars`/`usage-counters`-reading logic dies with those tables. | Metronome idempotent ingestion + one-time cutover validation + light drift monitor |
| `scripts/sync_bedrock_rates.py` + `core/billing/bedrock_pricing.py` (`cost_microcents`) | Rate-card sync + the pricing table | KEEP-BUT-CHANGE | Phase 1 | Spec §2 ("price via our rate table — the part of #826 that carries forward"): the meter MUST stay application-side (AWS can't meter per-owner $). Pricing **moves** from `usage_service`/`_maybe_deduct_credits` callers into the **Plan 3 Lambda meter**. The module itself survives; its callers change. NOT deprecated. | Same module, called by the Lambda meter |

## G. Analytics usage-counter side-writes — `usage_service.py` + the second call site

These are analytics-only (no deduct), but their *input data* (in-container `sessions.list` / client-supplied `usage` message — cache-blind, web-coupled) is exactly what the AWS meter replaces. Decoupling billing from them is the point; whether the analytics surface is rebuilt on the meter or retired is a Phase-3 call.

| file:line | what it is | fate | WHEN | why | replaced by |
|---|---|---|---|---|---|
| `core/services/usage_service.py:45-155` `record_usage()` (writes `usage-counters` period/lifetime/member rows; **non-Bedrock spend-0 records** at :87-118) | Analytics counter side-write; explicitly records non-Bedrock volume at spend 0 | KEEP-BUT-CHANGE | Phase 3 | Counter-only (docstring :56-60), so not a *billing* path — but it's fed by the same broken web-coupled `_fetch_and_record_usage` (and the tenant-side `usage` message, below), so it under-captures identically. The spend-0 non-Bedrock records were a reconciler aid (the reconciler is item F). Re-source from the meter or retire the `usage-counters` analytics surface at Phase 3. | meter-derived usage analytics (or retire) |
| `routers/websocket_chat.py:639-651` (the `msg_type == "usage"` branch → `_safe_record_usage`, **cache-blind**: `cacheRead/cacheWrite` from client body, no `estimatedCostUsd`, no deduct) | The second usage-recording call site (incident memory's "same pattern at `websocket_chat.py:641`") — a **client/tenant-supplied** analytics record | DELETE | Phase 1 | Tenant-supplied, cache-blind, web-coupled → not billing-grade and now redundant with the AWS meter. The audit's "not tenant-proof" principle (spec §3.1) applies directly: a tenant-posted `usage` message can't be trusted. No deduct here, so deleting it loses only duplicate analytics. | AWS meter (tenant-proof source) |
| `routers/billing.py:24, 94, 134` `get_usage_summary` (admin/buyer usage endpoint reading `usage-counters`) | Usage-summary surface | KEEP-BUT-CHANGE | Phase 3 | If the `usage-counters` analytics surface is retired/re-sourced (above), this endpoint re-points to meter-derived data. | meter-derived analytics |

## H. Infrastructure — the two DDB tables (DROPPED at Phase 3)

| file:line | what it is | fate | WHEN | why | replaced by |
|---|---|---|---|---|---|
| `apps/infra/lib/stacks/database-stack.ts:34, 213-221, 320-323` `creditsTable` (`isol8-${env}-credits`, PK `user_id`) | The atomic balance table | DELETE (drop) | Phase 3 | Spec §3.5: "the existing DDB `credit_ledger` is deprecated → deleted… backfill existing balances into Metronome prepaid credits." Plan 1 explicitly **reuses** this table for the interim (`DdbLedgerBackend`), needs **no GSI** (idempotency is a strong-consistent conditional write). Dropped only when Metronome is SoR. Drop after backfill; respect `removalPolicy` / `pointInTimeRecovery` for the audit tail. | Metronome (balance SoR) |
| `apps/infra/lib/stacks/database-stack.ts:35, 226-235, 324-327` `creditTransactionsTable` (`isol8-${env}-credit-transactions`, PK `user_id` + SK `tx_id`) | Immutable audit log | DELETE (drop) | Phase 3 | Same as above — the `DdbLedgerBackend.deplete` writes its exactly-once `deduct#{request_id}` audit rows here in the interim; Metronome owns audit at Phase 3. | Metronome (audit SoR) |
| `core/config.py` `CREDITS_TABLE` / `CREDIT_TRANSACTIONS_TABLE` settings + (Plan 1-added) `LEDGER_BACKEND` | Table-name config + backend selector | KEEP-BUT-CHANGE | Phase 1 adds `LEDGER_BACKEND="ddb"`; Phase 3 flips to `"metronome"` and the two `*_TABLE` settings are removed with the tables | The `LEDGER_BACKEND` flip is the **one-line cutover** the interface exists for (Plan 1 Task 2). | `LEDGER_BACKEND="metronome"` |

## I. New code added (for completeness — the replacements, all Phase 1)

`core/billing/usage_event.py` (`UsageEvent`), `core/billing/ledger_interface.py` (`LedgerBackend` Protocol + `DepleteResult` + `get_ledger()` factory), `core/billing/ddb_ledger_backend.py` (`DdbLedgerBackend.deplete` exactly-once on `request_id` via `TransactWriteItems` keyed `deduct#{request_id}` + `attribute_not_exists(tx_id)`, allows negative balance) — these are the seam. `DdbLedgerBackend` is itself **DELETE at Phase 3** (replaced by a `MetronomeLedgerBackend` behind the same interface); `usage_event.py` + `ledger_interface.py` are **KEEP** (the Metronome adapter consumes the identical `UsageEvent` and implements the same `LedgerBackend`). Plan 3 (Lambda meter) and Plan 4 (kill-switch + connection_pool retirement) live in their own plans.

---

## Holes confirmed LEFT by Plan 1 vs FIXED (per spec §3.5 + Plan 1 Self-Review)

- **FIXED at Phase 1** (must be, else the every-run meter double-deducts the DDB ledger): `requestId` idempotency — fixed *not* by editing `credit_ledger.deduct` but by the new `DdbLedgerBackend.deplete` (exactly-once `TransactWriteItems`). `credit_ledger.deduct`'s `bedrock_invocation_id`-with-no-dedup is bypassed, not patched.
- **LEFT (deliberately, vanish at Phase-3 deletion):**
  1. `deduct()` lossy overdraft fallback (`credit_ledger.py:173-211`) — `deplete` sidesteps it by allowing negative balances; the legacy fallback stays in dead `deduct()` until deletion.
  2. `top_up()` non-idempotency (`:111-114`) — idempotency stays at the webhook-dedup layer.
  3. `adjustment()` non-atomic `max(0, …)` (`:242`) — also already callerless.
  4. `issue_credit_for_owner` admin-grant-to-Stripe-balance bug (`billing_service.py:423-447`) — wrong store, left until Metronome grants replace it.

All four are short-lived by design and disappear when `credit_ledger.py` + `billing_service.issue_credit_for_owner` + the two DDB tables are deleted/rewritten at Phase 3.

## Relevant file paths (absolute)
- `/Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/bedrock-cost-remediation/apps/backend/core/gateway/connection_pool.py`
- `/Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/bedrock-cost-remediation/apps/backend/core/services/credit_ledger.py`
- `/Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/bedrock-cost-remediation/apps/backend/core/services/usage_service.py`
- `/Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/bedrock-cost-remediation/apps/backend/core/services/provision_gate.py`
- `/Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/bedrock-cost-remediation/apps/backend/core/services/billing_service.py`
- `/Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/bedrock-cost-remediation/apps/backend/core/services/stripe_webhook_handlers.py`
- `/Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/bedrock-cost-remediation/apps/backend/routers/billing.py`
- `/Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/bedrock-cost-remediation/apps/backend/routers/admin.py`
- `/Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/bedrock-cost-remediation/apps/backend/routers/websocket_chat.py`
- `/Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/bedrock-cost-remediation/apps/backend/scripts/reconcile_bedrock_vs_ledger.py`
- `/Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/bedrock-cost-remediation/apps/backend/scripts/sync_bedrock_rates.py`
- `/Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/bedrock-cost-remediation/apps/backend/core/billing/bedrock_pricing.py`
- `/Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/bedrock-cost-remediation/apps/backend/core/config.py`
- `/Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/bedrock-cost-remediation/apps/infra/lib/stacks/database-stack.ts`

Two non-obvious findings the plan-writers should note: (1) `credit_ledger.adjustment()`, `should_auto_reload()`, and `InsufficientBalanceError` are **already dead** (zero callers) before any phase — they're removed for free at Phase-3 deletion; admin grants today route through the buggy `issue_credit_for_owner` Stripe-balance path, not `adjustment`. (2) There are **two** usage-recording call sites, not one: the connection_pool chain (deduct path) and `websocket_chat.py:639-651` (a tenant-supplied, cache-blind, deduct-free `usage` message) — the latter is a clean Phase-1 DELETE since it's redundant-and-untrustworthy under the tenant-proof AWS meter.
