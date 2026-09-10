# Task 1 review package

## Scope proof

- Backend contract and configuration: `apps/backend/schemas/billing.py`, `apps/backend/routers/billing.py`, `apps/backend/core/config.py`.
- Configuration consumers: `apps/backend/core/services/billing_service.py`, `apps/backend/core/services/launch_close.py`, `apps/backend/core/services/launch_settings.py`, `apps/backend/routers/apple_iap.py`, `apps/infra/lib/app.ts`, `apps/infra/lib/stacks/service-stack.ts`, and `.github/workflows/deploy.yml`.
- Client contract consumers: `apps/frontend/src/hooks/useBilling.ts`, settings/checkout display surfaces, and `apps/mobile/src/components/billing/PaywallScreen.tsx`.
- No edits under messaging, AppSync, OpenClaw runtime, or vendor integration state.

## Required reviewer checks

1. Verify the account response has only server-owned workspace/Ultra prices and does not call Clerk seat count.
2. Verify `rg` finds no public frontend/mobile use of retired seat response fields and no flat setting name in `apps` or workflow configuration.
3. Verify legacy checkout code and `STRIPE_TEAM_*` remain for Task 2/3, per the controller rulings in `task-1-report.md`.
4. Verify new deploy-secret names are populated with the same per-environment Stripe Price IDs before deployment; no actual secret migration occurred in this task.
5. Review the known unrelated `apps/infra npm run build` type-configuration failure separately; it does not invalidate the focused stack test or successful synth.
