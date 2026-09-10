# Workspace billing configuration

This runbook is the operator contract for Lightbulb's workspace billing. Apply
changes in development first, then production. Resolve every Stripe, Metronome,
and workspace identifier from the target environment immediately before use;
never copy identifiers between environments or paste credentials into commands,
logs, tickets, or this document.

## Product contract

| Concern | Required state |
| --- | --- |
| Workspace | One active Stripe Product with one active $50/month recurring Price |
| Ultra | A separate active Stripe Product with one active $150/month recurring Price |
| Quantity | Exactly one recurring subscription item, quantity 1 |
| Membership | Unlimited humans and agents; membership changes never touch billing |
| Managed AI | Separate prepaid, workspace-pooled Metronome balance |
| Workspace storage value | `plan=standard` |
| Ultra storage value | `plan=ultra_pro` |
| Checkout input | Public plan is `workspace` or `ultra` |

Workspace and Ultra differ only in runtime capacity. Subscription Checkout must
contain one recurring Price and must not include managed-AI credits. Managed
Bedrock usage debits the workspace Metronome balance; BYO keys and ChatGPT OAuth
do not, although both still require an entitled workspace after trial.

New Apple purchase and restore paths are retired. The mobile application still
has full product access, but its App Store purchase gates and Settings expose no
outbound billing or purchase CTA. A user without a workspace can enter web
workspace onboarding, and an existing Stripe subscriber can manage that existing
subscription in the Stripe portal; new web billing remains outside the iOS
purchase UI. Historical Apple verification, webhook reconciliation, entitlement
teardown, and subscription management remain until readback proves no
Apple-managed subscription can still be live. Any future native billing must
attach the purchase to the active Clerk organization and grant that workspace's
entitlement; it must not recreate a personal owner or mobile-only entitlement.

## Stripe catalog

Configure the Stripe test account for development and the Stripe live account
for production. In each account:

1. Retrieve the account and confirm its mode and expected environment before any
   mutation.
2. Keep distinct active Products named **Workspace** and **Ultra**.
3. Keep one active monthly Price for each current tier: Workspace at $50 and
   Ultra at $150. Prices are immutable; create a replacement instead of editing
   amount, currency, or interval.
4. Keep the managed-AI credit Product separate from both subscription Products.
   It is mapped from Metronome and is not a recurring subscription tier.
5. Create or verify promotion code `ILOVELIGHTBULBS`. Its coupon is 100% off,
   duration `once`, has no first-transaction restriction, and applies only to
   the Workspace and Ultra Products. It must not apply to managed-AI credits.
6. Deactivate misspelled or obsolete promotion codes only after the plural code
   is accepted in that environment and a readback shows no dependency.

Before archiving an old Price or Product, exhaust pagination for nonterminal
subscriptions, subscription schedules, and open Checkout Sessions. Also search
deployed configuration and repository references. Do not archive an object while
any dependency remains.

### Stripe readback

Read Stripe directly with the target environment's credential, without printing
the credential. Confirm:

- the account is the intended test or live account;
- Workspace and Ultra are separate active Products;
- each configured Price is active, monthly, USD, and has the expected amount;
- the two Prices belong to their corresponding Products;
- the promotion code is active and points to a 100%-off, once-duration coupon;
- coupon product scope contains exactly Workspace and Ultra;
- `first_time_transaction` is false;
- the managed-AI Product is excluded; and
- the backend billing webhook subscribes to subscription lifecycle, invoice
  paid/failed, and Checkout completed/async succeeded/async failed events.

Stripe fields that are include-dependent must be requested explicitly. In
particular, read coupon product scope with `include[]=applies_to`; an omitted
field is not evidence of an empty scope.

## Deployment configuration

The deployment synthesizes environment-specific Price IDs from GitHub Actions
secrets and writes them into the backend task definition:

| Environment | Workspace Price secret | Ultra Price secret |
| --- | --- | --- |
| Development | `STRIPE_WORKSPACE_PRICE_ID_DEV` | `STRIPE_ULTRA_PRICE_ID_DEV` |
| Production | `STRIPE_WORKSPACE_PRICE_ID_PROD` | `STRIPE_ULTRA_PRICE_ID_PROD` |

`STRIPE_SECRET_KEY`, `METRONOME_API_KEY`, and webhook secrets come from AWS
Secrets Manager. Never put their values in GitHub Actions variables or synth
output. Metronome rate-card, credit-Product, USD-cents credit type, fast-gate
table, and environment URLs are set by the infrastructure code.

A GitHub secret update does not change a running service. The subsequent
`deploy.yml` synthesis must read the new value, deploy the resulting task
definition, and replace the running backend task. A push to `main` deploys
development. Production is a manual `deploy.yml` dispatch from `main`; never
dispatch a release from a feature branch.

After each deployment, read back the deployed commit, task-definition revision,
and the four non-secret billing references below from the running task:

- `STRIPE_WORKSPACE_PRICE_ID`
- `STRIPE_ULTRA_PRICE_ID`
- `METRONOME_RATE_CARD_ID`
- `METRONOME_CREDITS_PRODUCT_ID`

Resolve those references against Stripe and Metronome. A successful workflow is
not proof that the running task received the intended catalog.

## Metronome and Stripe mapping

Each Metronome environment must connect to the matching Stripe account:

- development Metronome uses the deployed development Stripe test account;
- production Metronome uses the deployed production Stripe live account; and
- no Stripe account, customer, delivery method, Product, or contract identifier
  is copied across environments.

Stripe authorization is performed in the Metronome UI under **Developer →
Integrations → Stripe**. The public customer-configuration API can select an
already authorized billing provider; it cannot authenticate or reconnect Stripe.

Development currently requires operator reconciliation because its configured
Metronome Stripe account does not match the deployed development Stripe account,
and its credit Product mapping does not resolve in the intended Stripe account.
Until this is repaired, development top-up acceptance is blocked.

### Repair order

1. In the development Metronome environment, authorize the existing development
   Stripe test account through the Stripe integration wizard.
2. Prefer one unambiguous development Stripe integration. The application does
   not currently select an account-specific delivery method during new-customer
   provisioning. If another Stripe integration must remain, stop and add explicit
   delivery-method selection before provisioning more workspaces.
3. Keep the existing rate card, metric/rate definitions, USD-cents credit type,
   legitimate workspace customers, contracts, ingest aliases, and balances.
4. Create or select an active managed-AI credit Product in the intended Stripe
   test account, distinct from Workspace and Ultra.
5. Update only the Metronome credit Product's `stripe_product_id` custom field
   and preserve its unrelated custom fields. In the selected Stripe integration,
   also map that field to the Stripe invoice-item Price; the custom field alone
   is insufficient.
6. Bind each legitimate workspace's Metronome root billing configuration to a
   Stripe customer that exists in that same account with automatic collection.
7. Confirm the contract uses the approved root configuration. Use Metronome's
   supported contract billing-provider change flow for an existing contract;
   do not null local identifiers to force reprovisioning.
8. Retain the old integration until its customers, contracts, invoices, and
   payment-gated commits are fully reconciled. Then disconnect it only if the new
   integration remains the unambiguous default.

### Metronome readback

Exhaust pagination and verify:

- the configured billing provider resolves to the same account returned by the
  deployed environment's Stripe credential;
- the selected integration maps the Metronome Product's `stripe_product_id` to
  the Stripe invoice-item Price and is not configured to leave invoices as
  drafts; inspect the integration UI when either setting is absent from API
  output rather than treating omission as false;
- the Metronome credit Product is unarchived and maps to an active Stripe Product
  in that account, separate from Workspace and Ultra;
- the rate card and USD-cents credit type are unchanged;
- the workspace ingest alias is unchanged;
- the root configuration uses the same workspace Stripe customer and automatic
  collection;
- the contract uses that root configuration and has no scheduled reversal;
- the local billing row retains the same workspace, Stripe customer, Metronome
  customer, and contract relationship; and
- exactly one enabled low-balance alert remains at 500 cents and exactly one at
  0 cents. Freshly compare each duplicate pair before archiving only the
  redundant alert; never archive both members of a pair.

Structural readback is not payment proof. In development, use one explicit
purchase UUID to create one payment-gated commit and one Stripe invoice. Retry
the same intent and confirm it creates no second invoice. Failed or
authentication-required collection must grant no spendable balance until that
same payment resolves; then confirm the Metronome balance and fast-gate projection
converge exactly once.

## Founder legacy subscription migration

Run this only after the no-seat billing code and final Price references are
deployed to production. The normal plan-change route correctly rejects the
founder's legacy two-item subscription, so this one-time repair is an operator
mutation against the existing subscription.

### Preflight

Freshly resolve the founder workspace from the authenticated admin identity and
retrieve its billing row. Then retrieve the stored Stripe subscription and
verify all of the following before mutation:

- workspace, customer, and subscription ownership agree;
- exactly one legacy base item and one legacy seat item exist;
- the subscription is nonterminal, automatic collection, has no pending update
  or schedule, and is not canceling;
- the production Workspace Price is active and belongs to the Workspace Product;
- the current billing anchor, period, discount, default payment method, metadata,
  and provider choice are recorded for comparison; and
- a fully paginated customer subscription list contains no second live
  subscription.

Stop on any mismatch. In particular, Stripe's historical provider metadata is
stale and must not overwrite the billing row's current provider.

### Atomic mutation

Perform one idempotent update on the existing subscription:

- replace the legacy base item's Price with the configured Workspace Price;
- set that item to quantity 1;
- delete the legacy seat item;
- use `proration_behavior=none`;
- preserve the billing-cycle anchor;
- set canonical `workspace_id`, matching `owner_id`, `plan=workspace`,
  `purpose=workspace_subscription_v2`, and the next verified plan revision; and
- clear obsolete provider, BYO-provider, and bundled-credit metadata.

Do not cancel and recreate the subscription. Omit customer, collection method,
discount, payment method, trial, tax, and cancellation fields so Stripe preserves
them. Derive one idempotency key from the verified source state and frozen update
body; reuse that same key after an uncertain response.

The intended result is the same customer and subscription with one $50 Workspace
item, no proration, no anchor reset, and the existing founder discount preserved.
The webhook remains the authority for local convergence: it stores Workspace as
`plan=standard`. Do not manually rewrite entitlement, provider, Metronome,
Paperclip, membership, balance, or runtime state.

### Migration readback

Verify the same subscription and customer now have exactly one recurring
Workspace item at quantity 1, no seat item, the same anchor/period, discount and
payment method, and no duplicate subscription or invoice-side proration. Observe
successful processing of the signed subscription update webhook and a consistent
billing row with `plan=standard`, active status, and the pre-existing provider.

## Stripe Tax blocker

Do not enable Stripe Tax yet. Development and production are missing approved
head-office and registration facts. Finance must supply the legal address,
product tax classification, registrations, and effective dates. Automatic-tax
configuration is not evidence that Lightbulb is registered or collecting
correctly.

## Rollback

Keep the previous catalog objects, GitHub secret references, Metronome integration,
and redacted preflight snapshots until acceptance completes.

- **Bad Price deployment:** restore the previous environment Price secret and
  redeploy from `main`. Do not archive either Price until the running task and
  subscription inventory agree on the replacement.
- **Bad promotion:** deactivate the promotion code. Coupons already applied to a
  subscription require separate subscription-level review.
- **Bad Metronome mapping:** stop top-ups, restore the last verified root/contract
  billing-provider configuration through the supported Metronome flow, and
  reconcile the original payment UUID and invoice. Never grant replacement
  credits manually after an uncertain payment.
- **Uncertain founder migration:** retrieve the subscription fresh. If the
  outcome remains uncertain, retry the identical frozen update with the original
  idempotency key. If the update definitively applied but the Workspace Price is
  unusable, perform one reviewed reverse update from the saved preflight item
  state with no proration and the unchanged anchor. Do not run the removed
  seat-management application path.
- **Bad application release:** redeploy the last known-good commit, then repeat
  catalog and billing-row readbacks. A code rollback does not roll back Stripe or
  Metronome state.

After rollback, keep new purchasing disabled until Stripe, Metronome, the deployed
task definition, and the workspace billing row agree again.
