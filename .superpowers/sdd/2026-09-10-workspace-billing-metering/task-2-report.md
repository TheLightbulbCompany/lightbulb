# Task 2 — Workspace checkout

## Delivered

- Added the sole subscription entry point: `POST /billing/checkout` with
  `plan: "workspace" | "ultra"`, workspace-admin authorization, and a return
  to Settings billing.
- A workspace owns its Stripe customer. Customer creation has a deterministic
  Stripe idempotency key; trial-row backfill is a first-writer-wins DynamoDB
  claim. A losing concurrent request reads the winner and idempotently deletes
  its orphan customer.
- Checkout has exactly one recurring item, allows manually entered promotion
  codes, and never sends an automatic discount, provider intent, credits, or
  `automatic_tax`. Tax remains off pending an explicit operational-readiness
  confirmation.
- Existing live subscriptions are updated in place. Stored subscriptions are
  authoritative unless metadata names a different workspace; webhook-in-flight
  recovery only accepts metadata matching this workspace. Open pending sessions
  are reused only when their purpose, plan, and workspace metadata all match.
- Retired the public trial, team, and ultra checkout routes and their focused
  route tests. Web/mobile callers use the new endpoint. Provider comparison
  cards are informational with a single workspace Subscribe action; provider
  selection remains post-entitlement.

## Sequencing ledger

`STRIPE_TEAM_*` / `TEAM_*` settings and legacy webhook/seat-sync code remain
until Task 3. This keeps the Task 2 intermediate deployment safe while the
legacy public checkout routes are gone. Invitation precedence and multiplayer/
runtime files were not changed.

## Focused verification

- `uv run pytest tests/unit/services/test_workspace_checkout.py tests/unit/routers/test_billing_workspace_checkout.py tests/unit/repositories/test_billing_repo.py -q` — 85 passed.
- `pnpm exec vitest run src/hooks/__tests__/useBilling.test.tsx src/components/chat/__tests__/ProviderPicker.test.tsx src/app/settings/__tests__/BillingPanel.billing.test.tsx src/app/settings/__tests__/BillingPanel.ultra.test.tsx` — 25 passed (pre-existing React `act` warnings only).
- `pnpm exec vitest run src/components/chat/__tests__/ProvisioningStepper.test.tsx --reporter=dot` — 60 passed (pre-existing React `act` warnings only).
- `pnpm exec tsc --noEmit` in `apps/frontend` — passed.
- `pnpm run typecheck` and focused `PaywallScreen` Jest suite in `apps/mobile` — passed (pre-existing React `act` warnings only).
