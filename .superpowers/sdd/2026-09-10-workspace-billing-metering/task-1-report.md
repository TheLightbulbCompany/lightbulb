# Workspace billing and metering — Task 1

Status: implementation complete for review. Branch `feat/workspace-billing-metering`, base `16f7c1a0b`. No push, deployment, Stripe/Metronome/Apple/Clerk mutation, or multiplayer/runtime change was made.

## Contract

`GET /billing/account` now returns the seat-free `BillingAccountResponse`:

- removed `seat_count`, `team_seat_price_usd`, `team_base_price_usd`, `flat_price_usd`, and `is_seat_subscription`;
- added required `workspace_price_usd` and `ultra_price_usd` (50 and 150 from server settings);
- no longer reads Clerk organization membership to calculate a price.

The public web and mobile clients consume only the new prices. Workspace billing renders one monthly price and unlimited members. When account data has not arrived, web UI says `Loading workspace price…`; it never fabricates `$50`. The Ultra upgrade label likewise uses the account response rather than a frontend constant.

`STRIPE_FLAT_PRICE_ID` / `FLAT_PRICE_USD` are renamed to `STRIPE_WORKSPACE_PRICE_ID` / `WORKSPACE_PRICE_USD`; `ULTRA_PRICE_USD=150` is explicit. CDK and deploy workflow use `STRIPE_WORKSPACE_PRICE_ID_{DEV,PROD}` and inject `STRIPE_WORKSPACE_PRICE_ID` into the backend task.

## Controller ordering rulings recorded

1. Task 1 must retain all legacy checkout calls and routes. Task 2 will add `POST /billing/checkout`, switch every client, and delete the old paths atomically. No client points at a route that does not yet exist.
2. Task 1 must retain `STRIPE_TEAM_*` and `TEAM_*` settings. Legacy team checkout needs them until Task 2 removes its route callers and Task 3 removes webhook/seat-sync callers. Deleting them now would make this otherwise deployable commit fail at runtime.

The retained legacy team settings/routes are intentionally outside this Task 1 contract change; their removal is not silently deferred or replaced with a compatibility flag.

## TDD and verification

The first changed account-contract test failed as expected because `get_billing_account` still awaited `clerk_admin.get_organization_seat_count`. After removing that lookup and returning both prices it passed. The loading-price UI test also failed first (old UI rendered an empty dollar amount), then passed after the no-fallback rendering change.

Passed:

```sh
cd apps/backend && uv run pytest tests/unit/routers/test_billing.py tests/unit/routers/test_billing_account_seats.py tests/unit/repositories/test_billing_repo_plan.py tests/unit/test_config.py -q
# 34 passed

cd apps/backend && uv run pytest tests/unit/services/test_billing_service.py tests/unit/services/test_launch_close.py tests/unit/services/test_launch_settings.py tests/unit/services/test_apple_iap.py -q
# 223 passed

cd apps/frontend && pnpm exec vitest run TeamBillingCard ProvisioningStepper ProviderPicker ChatErrorCard useCheckoutReturn BillingPanel.billing BillingPanel.ultra useBilling --reporter=dot
# 130 passed
cd apps/frontend && pnpm exec tsc --noEmit --incremental false

cd apps/mobile && pnpm test -- src/components/billing/__tests__/PaywallScreen.test.tsx --runInBand
# 21 passed
cd apps/mobile && pnpm run typecheck

cd apps/infra && npm test -- --run test/service-stack.test.ts
# 47 passed
cd apps/infra && npx cdk synth
# passed

git diff --check
rg -n 'seat_count|team_seat_price_usd|team_base_price_usd|is_seat_subscription|flat_price_usd' apps/frontend/src apps/mobile/src
# no matches
rg -n 'STRIPE_FLAT_PRICE_ID|FLAT_PRICE_USD' apps .github
# no matches
```

The focused frontend/mobile tests emit existing React `act(...)` warnings but pass. `apps/infra` `npm run build` remains red on its pre-existing global test type configuration: unrelated `test/ses-inbound-stack.test.ts` lacks Vitest globals and Vite declarations cannot resolve with that tsconfig's resolution mode. Task 1 does not touch those files or the infra tsconfig; focused infra tests and synth pass.

## Deployment handoff

Before any deployment, the operator must copy the exact existing dev and production Stripe Workspace/flat Price IDs into the renamed GitHub secrets `STRIPE_WORKSPACE_PRICE_ID_DEV` and `STRIPE_WORKSPACE_PRICE_ID_PROD`, then verify each injected ECS `STRIPE_WORKSPACE_PRICE_ID` matches its corresponding mode. This implementation did not read or mutate secrets, Stripe, or deployed tasks.
