# Managed OpenRouter inference: contract preflight

Status: **Phase 1 is not cleared for rollout.** This readback was performed on
2026-09-20 against AWS `isol8-admin`, account `877352799272`, and development
credentials only. The script
`apps/backend/tests/manual/openrouter_contract_probe.py` reads Secrets Manager,
Stripe test mode, and Metronome; it prints HTTP codes and selected booleans
(and non-secret external IDs with `--details`), never credentials. Run it from the repository root with
`python3 apps/backend/tests/manual/openrouter_contract_probe.py` after AWS SSO
login. Its two Metronome POSTs call documented **read** endpoints.

## Verified readback

| Check | Result |
| --- | --- |
| Dev Stripe key | `sk_test_` credential; `GET /v1/account` returned 200. |
| Dev Metronome | `listConfiguredBillingProviders` returned 200 with one Stripe integration. Its `stripe_account_id` does **not** match the account returned by the dev Stripe key. |
| Metronome credit Product | Configured dev Product resolved (200); its `stripe_product_id` returned 404 from the dev Stripe test account. A fully paginated test-account Product list has five active Products and no managed-AI credit Product. |
| Metronome rate card | Configured dev rate card resolved (200). This does not prove a new OpenRouter metric or rate exists. |
| Microcent sandbox proof | A disposable `SUM` metric with string property `provider_cost_microcents="12345"` and rate `0.0001` cents per unit reduced a disposable customer's unbilled 100-cent credit balance to **98.7655 cents**: exactly **1.2345 cents = $0.012345**. Replaying the same transaction ID left it at 98.7655 cents; event search marked the replay duplicate. |
| Nanodollar sandbox proof | A separate disposable `SUM` metric with string property `provider_cost_nanodollars="12345678"` and rate `0.0000001` cents per unit reduced a 100-cent unbilled test balance to **98.7654322 cents**: exactly **1.2345678 cents = $0.012345678**. Replay left that balance unchanged. |
| OpenRouter management key | On 2026-09-21, the dev secret `lightbulb/dev/openrouter/management` created a disposable capped completion key (201), returned one-time plaintext and a hash, then allowed get, disable, and delete (all 200). No completion request was made and no credential was logged. |
| OpenRouter workspaces | On 2026-09-21, a dedicated `Lightbulb Dev` workspace was created with slug `lightbulb-dev` and ID `5db3e7d3-4447-43b8-ac39-b1e35e3f54a9`. Set that exact ID as `OPENROUTER_WORKSPACE_ID_DEV`; do not repurpose the account's existing default workspace. |
| Workspace-specific key proof | The management key created and deleted a disposable daily-capped key inside `Lightbulb Dev` (create 201, delete 200), and the returned workspace ID matched `5db3e7d3-4447-43b8-ac39-b1e35e3f54a9`. No inference request was made. |
| OpenRouter credits | `GET /api/v1/credits` returned 200 with `total_credits=0` and `total_usage=0`. Funding the dev credit pool is required before a paid inference or settlement smoke. |
| Free-model smoke | On 2026-09-21, `liquid/lfm-2.5-2.6b:free` returned `LIGHTBULB_FREE_MODEL_OK` from a fixed harmless prompt in `Lightbulb Dev` (completion 200; reported cost 0). The disposable daily-capped key was deleted (200). A 512-token ceiling was needed because the currently available free models spend part of their response budget on hidden reasoning. |
| Selected free-model smoke | On 2026-09-21, the selected `nvidia/nemotron-3-ultra-550b-a55b:free` returned `LIGHTBULB_ULTRA_OK` from a fixed harmless prompt in `Lightbulb Dev` (completion 200; reported cost 0). The disposable daily-capped key was deleted (200). A first request returned no completed choice, so availability should be handled as transient during dev validation. |

The existing [workspace billing runbook](workspace-stripe-metronome-configuration.md)
describes the repair order. Until the integration and Product mapping are
reconciled, a dev payment-gated top-up cannot count as an end-to-end acceptance
test. A readback alone does not repair either mapping.

### Exact dev repair checklist (prepared, not applied)

1. Target the Stripe **test** account `acct_1UADdZEcGJXHk5N7` and the Metronome
   **dev sandbox** only. Before any write, repeat the probe with `--details`;
   stop if these identifiers or the one-integration count have changed.
2. In Metronome **Developer → Integrations → Stripe**, authorize that Stripe
   test account. Today's sole delivery method is
   `9cde4c2e-25af-4df2-9ab3-bced6abdf446`, bound to the wrong account
   `acct_1TBLi5I54BysGS3r`. Keep it until affected customers, contracts, and
   invoices are reconciled; do not assume a new second integration will be
   selected by provisioning code. Either make the intended integration the
   unambiguous default or add explicit delivery-method selection first.
3. Create one active, nonrecurring managed-AI credit Product in the target
   Stripe test account, separate from Workspace
   (`prod_VAYv70rvV7wbbn`) and Ultra (`prod_VEjcjzH6zn1ea3`). None of the
   five active Products currently serves that role. The Metronome credit
   Product `6dab1634-020d-4288-b5e6-99568a30404c` currently maps to
   `prod_UgNqukPjQEp9Nq`, which returns 404 in the target account.
4. Change only that Metronome Product's `stripe_product_id` custom field to
   the new Stripe Product ID, preserving every other custom field. In the
   intended Metronome Stripe integration, map it to the Stripe invoice-item
   Price. Confirm `leave_invoices_in_draft=false` in the integration UI: the
   API response omitted this field, so omission is not a false value.
5. Paginate dev workspace customers and read their root billing-provider
   configurations, active contracts, local billing rows, and Stripe customers.
   Use Metronome's supported billing-provider change flow for each affected
   contract; do not null local IDs or recreate legitimate customers. Preserve
   the dev rate card `99576f5a-37d2-4983-ba64-fea9bad96e08`, USD-cents
   credit type `2714e483-4ff1-48e4-9e25-ac732e8f24f2`, ingest aliases,
   balances, and alerts.
6. Read back one intended Stripe integration, an active mapped credit Product,
   automatic collection, and correct root/contract links before a dev
   payment-gated top-up. Then use one purchase UUID and replay it to verify
   one invoice and one balance grant. No such payment test was run here.

## Verified documentation; live behavior still unproven

The pinned upstream Hermes base image reports source revision
`d5b3cad7b98b3bcd157ea8b585759b0bb4fb8a6d`. At that revision,
`runtime_provider_backends._resolve_openrouter_runtime` reads
`model.provider=custom`, `model.base_url`, and `model.api_key` from config;
`config._env_ref_lookup` expands `${LIGHTBULB_GATE_BEARER}` under the active
profile secret scope, and `auxiliary_client._resolve_custom_branch` accepts
explicit per-task base URL/key. This verifies the intended code path, but it
does not replace a streamed, tool-using smoke against our built Hermes image.

- [OpenRouter pricing](https://openrouter.ai/pricing) lists Management API keys
  on Standard, a five-workspace Standard limit, and provider-passthrough
  inference rate limits. One workspace per Lightbulb environment is compatible
  with that published workspace limit; the target account's actual plan and
  quota have not been read back. No published management-call rate limit or
  maximum key count was found, so capacity remains unverified.
- [Management keys](https://openrouter.ai/docs/guides/overview/auth/management-api-keys)
  are administrative and cannot make completions. The [create-key API](https://openrouter.ai/docs/api/api-reference/api-keys/create-keys)
  accepts a USD limit/reset and returns plaintext only on creation. The
  [list](https://openrouter.ai/docs/api/api-reference/api-keys/list),
  [get](https://openrouter.ai/docs/api/api-reference/api-keys/get-key), and
  [update](https://openrouter.ai/docs/api/api-reference/api-keys/update-keys)
  APIs expose hash, usage, remaining limit, and disabled state. These API
  shapes are documented; none was exercised with a Lightbulb key.
- [Generation metadata](https://openrouter.ai/docs/api/api-reference/generations/get-generation)
  documents `total_cost`, selected model/provider, token counts, and
  `cancelled`. [Activity](https://openrouter.ai/docs/api/api-reference/analytics/get-user-activity)
  can filter by key hash, but covers only the last 30 **completed** UTC days.
  Generation availability delay, cancellation settlement, and activity
  completeness still require a live disposable-key test.
- Metronome documents [SUM metrics](https://docs.metronome.com/guides/implement-metronome/core-concepts/create-billable-metrics)
  and [event ingestion](https://docs.metronome.com/api-reference/usage/ingest-events).
  String properties and fractional-cent rates have also passed the isolated
  microcent and nanodollar sandbox proofs below. This does not prove the future production
  OpenRouter event or existing customer contracts are configured to use it.

### Isolated sandbox precision evidence

On 2026-09-20 local time, a disposable Metronome customer and contract were
created without a billing-provider configuration. An ungated 100-cent **test
credit** was added; no Stripe API call, invoice collection, or payment occurred.
The test's unique `openrouter_inference`-equivalent event used a string
`provider_cost_microcents` property with value `"12345"`. Its own metric,
Product, and rate card were isolated from the deployed dev rate card. The rate
API returned `0.0001` cents per metric unit; the balance readback was
`100 → 98.7655` cents. The identical transaction replay returned success but
the balance stayed `98.7655`; event search showed one matched original and
one duplicate. This proves the proposed unit precision and transaction-ID
dedup **in the sandbox configuration tested**.

The first disposable run ingested immediately after metric creation and did
not match the metric. A control run after a 20-second activation interval
matched both string and numeric properties. Metric propagation must be part
of deployment acceptance; usage sent before activation cannot be assumed
billable. The successful run's customer `897ffc49-415b-48cb-8cbc-6338515b0492`,
rate card `40a93fa6-6da5-4dcc-a4c4-79efb51af6b4`, Product
`3dd546a4-0bfc-4f9c-a328-7c440b4b9f0c`, and metric
`f7dc38cb-776a-4c61-885b-adcec2964802` all returned archive timestamps
on readback. Metronome retains the historical event and archived records.

An additional no-Stripe sandbox run on 2026-09-20 local time tested finer
provider costs. Metronome's rate API returned `1e-07` cents per unit for a
`SUM` string property `provider_cost_nanodollars="12345678"`. The unbilled
balance changed `100 → 98.7654322` cents, the exact expected debit of
`1.2345678` cents (`$0.012345678`). Replaying the identical transaction ID
left the balance unchanged. Event search before cleanup showed one matched
original; a later search marked the replay duplicate. The disposable
customer `2f108ee8-e9e7-4985-82f7-cb808b00321b`, rate card
`47f5ea8c-2283-496c-ad98-68ca7ccc313b`, Product
`6fb0b10f-adf9-4770-aca4-260e23189d1b`, and metric
`5bb5b72f-9766-498d-88a0-e4c7a6d1bd1c` all returned archive timestamps
on readback. This proves Metronome did **not** round that rate or debit to
zero in the isolated dev setup.

**Use nanodollars for the proposed OpenRouter event.** Cheap models can cost
less than a microcent per token (for example, $0.13 per million input tokens
is 130 nanodollars per token). Convert OpenRouter `total_cost` from its raw
decimal dollar representation to an integer nanodollar count using decimal
arithmetic, with one documented rounding rule if a generation has fractional
nanodollars. Emit that integer as a string, then rate at `0.0000001` cents
per nanodollar. This retains the tested precision and avoids rounding each
generation to whole microcents. The production markup rate still needs its
own exact-value sandbox check before rollout.

### Dev rate card inventory and additive OpenRouter rate

Read-only `getRates` pagination on 2026-09-20 found 12 effective rates on the
dev card `99576f5a-37d2-4983-ba64-fea9bad96e08` (`isol8-bedrock-dev`).
`getRateSchedule` found the same 12 ongoing segments and no future changes.
All are entitled, `FLAT`, and denominated in USD **cents** with credit type
`2714e483-4ff1-48e4-9e25-ac732e8f24f2`. Each Product is active `USAGE`,
has `pricing_group_key=["tier"]`, and points to a `SUM` metric filtered to
`event_type=bedrock_invocation`, with `group_keys=[["tier"]]`:

| Product / metric | haiku | sonnet | opus |
| --- | ---: | ---: | ---: |
| Output tokens: `59662e68-454a-4c4a-82c1-d87d898481da` / `3ce673ca-403e-4aff-b856-f354c7fd2e78` (`output_tokens`) | 0.0005665 | 0.0016995 | 0.0028325 |
| Input tokens: `d046a25f-7487-40fa-932f-2e368556a7a4` / `709f74cc-d69d-48c8-a4ff-1612ec6a8923` (`input_tokens`) | 0.0001133 | 0.0003399 | 0.0005665 |
| Cache write: `e45a70c8-97c1-490b-ae6a-dc7ed7b2b7c6` / `ce0e2823-4360-4aac-902a-18f527020d0f` (`cache_write_tokens`) | 0.0002266 | 0.0006798 | 0.001133 |
| Cache read: `ec17ed0b-384f-4f1e-b5a6-871c4f015f11` / `05fb5742-06a8-45ab-b39e-5ee3d6ab7fac` (`cache_read_tokens`) | 0.00001133 | 0.00003399 | 0.00005665 |

Prices in this table are **cents per token**, read from the API, not the
card description's old markup text. The Bedrock metrics and Products must
remain active for unmigrated owners and rollback.

[Metronome's pricing-change guide](https://docs.metronome.com/guides/pricing-packaging/make-pricing-changes/make-a-pricing-change)
documents `addRates` on an existing rate card as the all-customer path; card
changes generally flow to contracts, while package overwrite overrides are
an explicit exception. Inference: contract overrides may also change an
owner's effective rate, so each selected contract needs its own readback.
The smallest safe dev change is therefore **additive**, when implementation
is authorized and after a fresh inventory:

1. Create one `SUM` metric filtered to `event_type=openrouter_inference`,
   `aggregation_key=provider_cost_nanodollars`, and an `exists` property
   filter on that key. Use no `tier` group key; the cost is already the
   priced quantity. Wait for metric activation before sending usage, as the
   sandbox propagation test above demonstrated.
2. Create one new `USAGE` Product linked to that metric, with no pricing group
   key. Add **one** entitled `FLAT` rate to the **existing dev card** with
   `price=0.0000001` cents per nanodollar and the existing USD-cents credit type.
   Choose a future UTC-midnight `starting_at` coordinated with the dev gate
   rollout. Do not edit, archive, or replace the 12 Bedrock rates.
3. Before enabling any owner, exhaust `getRates` and `getRateSchedule` pages:
   expect exactly the unchanged 12 Bedrock rows above plus the one new
   OpenRouter row at/after its start. Read the new Product and metric back;
   confirm their linkage, event filter, SUM key, no tier key, entitlement,
   price, and credit type.
4. For each dev owner selected for migration, read that **existing contract's**
   effective `getContractRateSchedule` after the new rate starts. Confirm its
   original Bedrock rates and the new OpenRouter rate are both effective,
   with no package/contract override masking either. Then repeat the unbilled
   12,345,678-nanodollar event, exact debit, and duplicate replay against a
   disposable contract on this card before customer traffic. A 13-row card
   readback alone does not prove an existing contract inherits the new rate.

**Applied in dev 2026-09-23** by `apps/backend/scripts/setup_metronome_openrouter_pricing.py`
(markup from repo-root `openrouter-pricing.json`, owner ruling 2026-09-23: 1.2, so the rate is
`1.2e-7` cents per nanodollar): metric `c4293481-4375-4b15-b270-b0deb4ee814b`, Product
`4c39b72e-a9bc-4272-a3b3-eddd2b5e5001`, rate from `2026-09-23T00:00:00Z`. Readback: 13 rows on the
card (the 12 Bedrock rows unchanged), and the one existing dev contract's
`getContractRateSchedule` shows the new rate with no override. A non-persisting
`previewEvents` on that contract priced 1,000,000 units at 0.12 cents, 12,345,678 at 1.48148136
cents and 1 at 1.2e-7 cents. Prod is dry-run only until the owner approves `--env prod --apply`.

A uniquely versioned **replacement** rate card is possible for new contracts:
copy these same 12 Product/tier/price rows unchanged, add the OpenRouter
Product at `0.0000001` cents per nanodollar, then read back all 13 rows before
changing the dev `METRONOME_RATE_CARD_ID` deployment reference. But that
reference affects new contract creation only. The public
[`/v2/contracts/edit` schema](https://docs.metronome.com/api-reference/contracts/edit-a-contract)
does not expose `rate_card_id`, so a replacement card cannot be assumed to
move existing contracts. Do not switch the deployed reference for an
existing-owner rollout without a separately proven supported migration path;
adding to the present card avoids that gap. No live card or contract was
changed for this inventory.

## Dev funded end-to-end smoke: PASS (2026-09-23)

This is the proof `OPENROUTER_MONEY_CONTRACT_VERIFIED` / `OPENROUTER_RATE_CARD_PROOF_ID`
rests on in dev (`openrouter-gate-stack.ts` `rateCardProof`). Test org
`org_3Jic5qyVa4uifBPJzbuiTNSJIuT` ("OR Paid Smoke P4") was left in place.

| Step | Result |
|---|---|
| Workspace plan | Stripe test subscription `sub_1UIkGXEcGJXHk5N7wQC2XhxA` (`pm_card_visa`); the webhook set the row to active / `standard`; Metronome customer `be448ed2-…` and contract `a9c9e017-…` exist |
| Provider | `openrouter_managed`; `managed_inference_available: true` |
| Top-up | $5 payment-gated PREPAID commit `9a47ba22-…`; Stripe invoice PAID; fast-gate 5,000,000 µ¢ |
| Rate match | The contract's rate card carries product `4c39b72e-…` at FLAT 1.2e-7 ¢/nanodollar; metric `c4293481-…` sums `provider_cost_nanodollars` |
| Paid turn | `billing_mode=paid` with a 48,984 µ¢ reservation (formula-exact) |
| Settle | 28 paid rows settled (agents kept working while settlement was blocked); the smoke row cost 1,249,939 nanodollars |
| Debit | Σ ceil(N×1.2/1000) = 50,591 µ¢, which equals the observed fast-gate drop of 5,000,000 → 4,949,409; reserved back to 0 |
| Idempotent | A settlement re-run settled 0 and left the fast-gate unchanged |
| Metronome | 28 events, 0 duplicates, all matched to customer `be448ed2` and to the "OpenRouter provider cost (nanodollars)" metric |
| Audit | 2026-09-22 reported only `unattributed` $0.00008 (a deleted manual probe key) |

Bugs this smoke found, all fixed: settlement could not decrypt the Metronome key (#3017),
the audit's date filter (#3002), and the audit's unattributed spend and float tolerance (#3006).

**Before the prod paid flip:**
1. Add a prod rate-card proof. Prod card `fdac2edd` has had a dry run only; apply the Metronome pricing first.
2. Deploy #3028 (settlement overshoot metric and alarm W81).
3. Point Metronome webhooks (sandbox and production) at `/api/v1/webhooks/metronome`. None
   reach either environment today, so top-up status stays `requested` and payment-gate
   failures go unseen.

## Acceptance work still required

1. Obtain a written OpenRouter answer covering Lightbulb-funded,
   customer-facing managed inference and the selected model providers' terms.
   [OpenRouter's terms](https://openrouter.ai/terms) bind model usage to model
   terms; Management API support alone is not commercial permission.
2. Confirm the actual account plan, management-call and key-count ceilings,
   concurrent key-cap behavior, pooled-credit low-balance behavior, and
   auto-recharge setting. Exercise a fail-closed 402 path without relying on a
   customer prompt. No OpenRouter account or credential was available to this
   readback.
3. In a disposable dev setup, prove key create/list/get/disable, one-time
   plaintext, limit response, streamed usage, delayed generation lookup, and
   key-hash activity. Record HTTP codes and costs only. Do not log prompts or
   plaintext keys.
4. Reconcile the dev Metronome–Stripe integration and credit Product mapping
   through the existing operator workflow, then separately test a real dev
   payment-gated top-up and duplicate retry. Keep product, delivery method,
   customer, and invoice account IDs within the same environment.

No production cutover is authorized by this document. The paid/key and
OpenRouter probes remain outstanding. The unbilled Metronome precision proof
was explicitly authorized for dev testing and its disposable objects are
archived; the financial integration repair above was prepared but not applied.
