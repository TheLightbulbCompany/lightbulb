> Source: dynamic workflow `write-bedrock-metering-plans` (wf_d5c383e2-048), 2026-06-07.
> Companion to the spec `docs/superpowers/specs/2026-06-07-bedrock-metering-enforcement-metronome-design.md` and Phase-1 plans.

# Metronome Ingestion Contract & UsageEvent Mapping — Research Report

**Researched:** 2026-06-07 against live `docs.metronome.com` + Metronome SDK repos. Anything not confirmable from live docs is marked **UNVERIFIED (sandbox/sales-gated)**.
**Aligns with:** spec `docs/superpowers/specs/2026-06-07-bedrock-metering-enforcement-metronome-design.md` §3.2.3, §3.4, §3.5 (Phases 2/3).

---

## 1. Usage-event INGESTION

### Endpoint
- **`POST https://api.metronome.com/v1/ingest`** — base API URL is `https://api.metronome.com/v1`, Bearer-token auth (`Authorization: Bearer <TOKEN>`).
- Request body is a **JSON array of event objects** (SDK shape: `usage=[ {...}, ... ]` → `client.v1.usage.ingest(usage=[...])`).
- **Always retry until you receive a 200** (transaction_id makes retries safe).
- **Batch/array size limit: UNVERIFIED** — not stated on the public ingest/idempotency pages. Treat as conservative (small batches), confirm against the API reference / sandbox before Phase 2.

### Event payload schema (all 5 top-level fields)

```json
{
  "transaction_id": "string",   // REQUIRED — unique id; idempotency key
  "customer_id":    "string",   // REQUIRED — Metronome customer UUID OR an ingest_alias
  "timestamp":      "string",   // REQUIRED — RFC 3339, 4-digit year, e.g. "2025-01-23T01:23:45Z"
  "event_type":     "string",   // REQUIRED — kind of event, e.g. "heartbeat", "page_view"
  "properties":     { }          // OPTIONAL — free-form key/value bag
}
```

Verbatim SDK example (Python):
```python
client.v1.usage.ingest(usage=[{
    "transaction_id": "90e9401f-0f8c-4cd3-9a9f-d6beb56d8d72",
    "customer_id": "team@example.com",
    "event_type": "heartbeat",
    "timestamp": "2024-01-01T00:00:00Z",
    "properties": {"cluster_id": "42", "cpu_seconds": 60, "region": "Europe"},
}])
```

### Idempotency / dedup window (the load-bearing one)
- The **`transaction_id` is the idempotency key.** Verbatim: *"Once a usage event has been accepted with a given transaction ID, Metronome ignores subsequent events with the same transaction ID within the next 34 days."*
- **Window = 34 days** (confirmed verbatim, repeatedly). This matches the spec's claim. So keying `transaction_id = Bedrock requestId` gives exactly-once **only within 34 days of first acceptance** — see flag in §6.
- Distinct from the general POST **`Idempotency-Key` header** (retained "at least 24 hours"; same key + different params → `409 Conflict`). That header is **not** what `/ingest` uses for dedup; ingest dedup is purely `transaction_id`.

### Timestamp / backdate rules
- Format: **RFC 3339, 4-digit year** (`2025-01-23T01:23:45Z`).
- Verbatim: *"Timestamps more than 24 hours in the future are rejected by the API."* Future cap = +24h.
- **Backdating into the past:** the docs do **not** state an explicit max-backdate. The only hard rule documented is the +24h future cap. The **34-day** number is the *dedup retention* window for `transaction_id`, **not** a documented backdate limit. The spec conflates "34-day dedup" with "34-day backdate window" — these are the same number in practice for our purposes (re-sending a >34-day-old `requestId` would no longer dedup), but **how far in the past a timestamp may be set is UNVERIFIED**. Conservative reading: ingest promptly; do not rely on backfilling events older than ~34 days.

### Customer identity mapping (owner_id ↔ Metronome customer ↔ Stripe)
- `customer_id` in an event accepts **either** the Metronome customer **UUID** **or** an **ingest_alias**.
- **Ingest aliases** map *your* internal id to a Metronome customer: *"Each customer in Metronome may have multiple ingest aliases, and usage events with a `customer_id` matching any of those aliases can be attributed towards that customer's usage."* They must be **globally unique**; can be set at customer-create time or later.
- **→ Set `ingest_aliases: ["<our owner_id>"]`** on the Metronome customer, then send our `owner_id` directly as `customer_id` in every event. No id-translation table needed.
- Customer creation: **`POST /v1/customers/create`** with `{ "name", "ingest_aliases": [...] }`.
- **Stripe link is set separately** (customer billing config, **not** at customer create): `POST /v1/customers/{id}/setCustomerBillingProviderConfigurations` (a.k.a. `customer_billing_provider_configurations`) with:
  - `"billing_provider": "stripe"`
  - `"delivery_method": "direct_to_billing_provider"`
  - configuration: `"stripe_customer_id"` (links to an **existing** Stripe customer — verbatim: *"The Stripe customer ID for the customer that gets issued a Stripe invoice"*), `"stripe_collection_method"` ∈ `"charge_automatically" | "send_invoice"`.

### event_type + properties bag
- **`event_type`**: free string; used by billable metrics to filter (e.g. `WHERE event_type = 'bedrock_invocation'`). No formal naming convention documented.
- **`properties`**: free-form key/value. **Critical rule, verbatim:** *"All keys and values in the `properties` map should be represented as strings, even though the values are often numeric."* (Example shows `"bytes": "1234"`.) Note the SDK example sends `"cpu_seconds": 60` as a raw int — so native numbers may be tolerated, but **docs say stringify**, so **stringify all numeric properties** to be safe.
- Multiple numeric measures in one event = **separate property keys** (e.g. `input_tokens`, `output_tokens` as distinct keys); each is fed to a **separate billable metric**. Verbatim: *"create separate metrics to aggregate the value from each property."*
- **Billable metrics** aggregate one numeric property via **SUM / COUNT / MAX / LATEST** (streaming) or arbitrary **SQL** metrics (`count(distinct …)` etc., higher latency). To group/filter by a property (e.g. `model_id`), the property must be declared in the metric's property filters with an `Exists`/`In` filter.

---

## 2. Prepaid credits / wallet

- Objects: **Contract** (the customer's billing agreement; references a **Rate Card** and holds commits/credits) → **Commit** (a prepaid, paid-for credit allocation = "prepaid credits") vs **Credit** (free/promo grants). Burn-down draws against commit/credit balance as rate-carded usage is metered.
- **Grant/fund:** add a prepaid commit via **`POST /v1/contracts/edit`** with `add_commits`. Verbatim: *"Upon success, Metronome creates a commit object to increase the customer balance."*
- **Stripe-payment-gated funding** (this is our top-up flow): commit carries `"payment_gate_config": {"payment_gate_type": "STRIPE"}`. Verbatim: *"credits are only granted once payment is confirmed successful"* / Stripe-side: *"a Stripe invoice must be paid before credits are released."* On failure Metronome **voids** the commit/invoice.
- **Expiry:** set on the commit's `access_schedule` (e.g. `"ending_before": "2025-04-01T00:00:00.000Z"`). Not-yet-started future segments are excluded from spendable balance.
- **Auto-recharge** (`credit_balance_threshold_configuration`): `threshold_amount` (recharge trigger), `recharge_to_amount` (target balance), `is_enabled` (auto-disabled on payment failure). Minimums (from changelog/threshold docs): **threshold ≥ $5**, and **recharge_to ≥ threshold + $10**. *"Metronome handles all of the recharge actions asynchronously based on customer usage."*

---

## 3. Balance read + low-balance signals (drives the local fast-gate flag)

### Reading balance
- **`POST /v1/contracts/customerBalances/getNetBalance`** → single aggregated remaining balance (filterable by balance type, currency, pending charges). SDK: `client.v1.contracts.getNetBalance()`.
- **`POST /v1/contracts/customerBalances/listBalances`** → per-commit/credit `balance` (spendable amount; excludes not-yet-started segments). Params: `include_ledgers`, `include_balance`, `include_contract_balances`, `starting_at`.
- **Freshness/latency:** docs call balances *"real-time"* but give **no low-latency SLA** and note balance can be shown including/excluding **pending (not-yet-finalized) charges**. There is **no documented strong-consistency guarantee** for a synchronous per-request gate. **→ Confirms the spec's design:** do **not** put `getNetBalance` on the synchronous chat hot path; keep the **local fast-gate flag** as the in-line gate, fed by webhooks + optimistic decrement, with Metronome as truth + the IAM/`desired=0` kill-switch as backstop. **(Exact ingest→balance propagation latency is UNVERIFIED — measure in the sandbox during Phase 2.)**

### Low-balance webhook (the signal that sets the flag)
- Create the alert: **`POST https://api.metronome.com/v1/alerts/create`** with `alert_type`, `credit_type_id`, `name`, `threshold`, `customer_id` (optional `group_values`, `invoice_types_filter`).
- Relevant alert/webhook event types: **`alerts.low_remaining_credit_balance_reached`**, `alerts.low_remaining_commit_balance_reached`, `alerts.spend_threshold_reached`, `alerts.usage_threshold_reached`, `alerts.invoice_total_reached`.
- **Webhook payload (verbatim example):**
```json
{
  "id": "445b849e-1366-4580-a6cc-f488e27c059f",
  "type": "alerts.low_remaining_credit_balance_reached",
  "properties": {
    "customer_id": "ac39ecc3-87ee-4d58-8ec0-24041464dddd",
    "alert_id": "445b849e-1366-4580-a6cc-f488e27c059f",
    "timestamp": "2024-10-07T15:08:44.865Z",
    "threshold": 20000,
    "alert_name": "Credit balance low",
    "credit_type_id": "2714e483-4ff1-48e4-9e25-ac732e8f24f2",
    "remaining_balance": 5000,
    "triggered_by": "usage"
  }
}
```
- **Signature verification:** header **`Metronome-Webhook-Signature`** = `HMAC_SHA256(secret_key, DATE_HEADER + "\n" + raw_body)`, hex-compared. (Concatenate `Date` header + `\n` + exact request-body bytes.)
- **Status model:** alerts go **`in_alarm`** once threshold crossed; poll synchronously via **`/customer-alerts/get`** / **`/customer-alerts/list`**. **Recovery/bidirectional-crossing webhook (re-arming when balance rises): UNVERIFIED** — docs don't confirm a fire-on-recovery event. **Plan to re-arm the local flag on the next successful top-up / on a poll, not on a recovery webhook.**

---

## 4. Stripe coexistence (our Stripe = top-up payments only)

- **"Metronome with Stripe Subscriptions"** pattern is explicitly supported: keep our existing $50/mo Stripe Subscription as-is; Metronome handles usage/credit metering & invoicing **separately on the same Stripe Customer**. Verbatim: *"Metronome processes them and pushes usage invoices to Stripe on the same Stripe Customer. The customer receives both subscription invoices (from Stripe) and usage invoices (from Metronome)."*
- Settlement is **one-way Metronome→Stripe**: Metronome creates `InvoiceItem`s + `POST /v1/invoices` on the Stripe Customer; **Stripe payment status syncs back to Metronome automatically.**
- Division of labor (verbatim): *"Metronome handles usage-based billing, credit-based pricing… Stripe handles payment collection, tax calculation, revenue recognition… Radar."*
- **→ For us:** Stripe Checkout collects a top-up payment → fund a **payment-gated commit** in Metronome → credits released on Stripe-paid. Stripe never meters and never gates. Matches spec §3.2.5.

---

## 5. Sandbox vs production

- **Environment is token-scoped (verbatim):** *"The environment is determined entirely by your API token. A sandbox token only works with sandbox data; a production token only works with production data."* Same base URL; different token.
- Docs recommend signing up for **sandbox first** to test workflows.
- **What requires the sales-led production contract: UNVERIFIED (sales-gated).** The public docs do **not** enumerate sandbox feature limits vs production. Metronome's go-to-market is sales-led (no public self-serve production tier surfaced; "Contact sales"/pricing-index only). Treat **all Phase-3 prod specifics — production rate-card/contract terms, pricing, any feature gating, exact balance-propagation latency, ingest batch limits, backdate limits, recovery-webhook behavior — as confirmable only after the sales engagement + a prod account.** Matches spec §4 decision 2.

---

## 6. UsageEvent → Metronome ingest mapping

Our normalized `UsageEvent` (spec §3.1/§3.2.2) → Metronome `/v1/ingest` event. Assume `event_type = "bedrock_invocation"` and **all property values stringified**.

| UsageEvent field | Metronome destination | Notes / flags |
|---|---|---|
| `owner_id` | **`customer_id`** | Send as the customer's **ingest_alias** (`ingest_aliases:["<owner_id>"]` on the Metronome customer). No translation table. |
| `request_id` | **`transaction_id`** | Bedrock `requestId` = idempotency key. **FLAG:** dedup is exactly-once **only within 34 days**; a replay of a >34-day-old record would re-deplete. Fine for live metering; relevant only for very-late DLQ replays. |
| `model_id` | **`properties.model_id`** | The AIP/model id. Declare as an `In`/`Exists` property filter in any per-model billable metric. |
| `input_tokens` | **`properties.input_tokens`** | Stringify. Own SUM billable metric. |
| `output_tokens` | **`properties.output_tokens`** | Stringify. Own SUM billable metric. |
| `cache_read_tokens` | **`properties.cache_read_tokens`** | Stringify. Own SUM metric (the cache-blind-undercount fix lives here). |
| `cache_write_tokens` | **`properties.cache_write_tokens`** | Stringify. Own SUM metric. |
| `raw_cost_microcents` | **`properties.raw_cost_microcents`** | Pre-markup cost we computed. **FLAG (decision needed):** Metronome can rate usage *itself* via rate cards, OR accept a **pass-through precomputed amount** as a property the rate card multiplies by 1. **Pass-through pricing as a property is UNVERIFIED on the live docs** — confirm in sandbox. Until then, treat our cost fields as **audit/debug properties** and let Metronome's rate card do the rating, OR (simpler, recommended) rate on token-count properties in Metronome and keep our dollar fields informational. |
| `billed_microcents` | **`properties.billed_microcents`** | Post-markup amount. Same flag as above — either the value Metronome bills (pass-through) or an audit property if Metronome rates from tokens. |
| `markup_multiplier` | **`properties.markup_multiplier`** | Audit/debug property. **Alternative:** encode markup in the Metronome **rate card** instead of per-event, so it's centrally managed — recommended for Phase 3. |
| `region` | **`properties.region`** | Bedrock fan-out region. Property filter/group key if regional pricing is ever needed. |
| `run_status` | **`properties.run_status`** | `end`/`error`/`aborted`. **No native Metronome field** — lives in properties. We still **ingest error/aborted events** (spec bills them); if we ever want to *exclude* a status from billing, do it via a billable-metric event filter (`WHERE run_status != '...'`), not by dropping the event. |
| `occurred_at` | **`timestamp`** | RFC 3339. **FLAG:** must be ≤ now+24h or rejected; emit promptly so DLQ-replayed events aren't stale-rejected/missing the dedup window. |
| `source` | **`properties.source`** | e.g. `webchat`/`heartbeat`/`cron`/`channel`. Audit/analytics property; no native field. |

### Fields with no clean Metronome home
- `run_status`, `markup_multiplier`, `source` → **properties only** (no first-class Metronome slot). Fine — properties are the intended home.
- `raw_cost_microcents` / `billed_microcents` → **mapping is a design fork:** Metronome's billing model rates *usage* via **rate cards**, not arbitrary pre-priced dollar amounts. **Recommended:** rate from **token-count properties** inside Metronome (markup in the rate card), and carry our dollar fields as **audit properties** for the one-time cutover reconciliation (spec §3.5 Phase 2). **Whether Metronome supports direct pass-through dollar billing per event is UNVERIFIED — confirm in sandbox before locking the rating model.**

### Metronome-required fields our UsageEvent lacks
- **None missing.** Metronome requires exactly `transaction_id`, `customer_id`, `timestamp`, `event_type`; we supply all four (`request_id`, `owner_id`-as-alias, `occurred_at`, and a constant `event_type`). `properties` is optional. **`event_type` is the only field our UsageEvent has no explicit column for** — it's a constant we add at adapter time (`"bedrock_invocation"`), not a gap.

---

## Sources (fetched live)
- https://docs.metronome.com/connect-metronome/send-usage-events/
- https://docs.metronome.com/developer-resources/use-api/idempotency/
- https://docs.metronome.com/api-reference/idempotency
- https://docs.metronome.com/connect-metronome/design-usage-events/
- https://docs.metronome.com/connect-metronome/create-billable-metrics/
- https://github.com/Metronome-Industries/metronome-python/blob/main/README.md
- https://docs.stripe.com/billing/how-metronome-works-with-stripe
- https://docs.metronome.com/launch-guides/prepaid-credits/
- https://docs.metronome.com/pricing-packaging/apply-credits-commits/remaining-balance/
- https://docs.metronome.com/enhance-customer-experience/customer-controls/
- https://docs.metronome.com/guides/platform-configuration/setup-webhooks
- https://docs.metronome.com/launch-guides/quickstart/
- https://docs.metronome.com/guides/customers-billing/manage-customers/provision-a-customer
- https://docs.metronome.com/integrations/invoice-integrations/stripe
- https://docs.metronome.com/developer-resources/use-api/authorization/ (auth/env-token, via search)
- https://docs.metronome.com/guides/customers-billing/optimize-customer-experience/customer-dashboards-and-reporting (getNetBalance/listBalances, via search)

### Open items to resolve in Phase-2 sandbox / sales (all marked UNVERIFIED above)
1. Ingest **batch/array size limit** per `/ingest` call.
2. **Max backdate** for `timestamp` (only +24h future cap is documented).
3. **Ingest→balance propagation latency** (justifies/sizes the local fast-gate flag's staleness tolerance).
4. Whether a **recovery/re-arm webhook** fires when balance rises back above threshold (else re-arm on top-up/poll).
5. Whether Metronome supports **per-event pass-through dollar billing** vs. rate-card rating from token counts (decides where `raw_cost_microcents`/`billed_microcents`/`markup_multiplier` truly live).
6. Sandbox-vs-production **feature gating** and prod contract terms/pricing.
