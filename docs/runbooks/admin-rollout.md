# Admin dashboard rollout

Operational runbook for the admin host: `admin.dev.thelightbulb.company` on dev,
`admin.isol8.co` on prod until the flag-day flip moves it to
`admin.thelightbulb.company`. AWS resource names (`isol8-{env}-*`, `isol8/{env}/*`) and
the `isol8-frontend-*` Vercel project names are NOT renamed.

**Tracking issue:** [Isol8AI/isol8#351](https://github.com/Isol8AI/isol8/issues/351)
**Spec:** [`docs/superpowers/specs/2026-04-21-admin-dashboard-design.md`](../superpowers/specs/2026-04-21-admin-dashboard-design.md)
**Plan:** [`docs/superpowers/plans/2026-04-21-admin-dashboard.md`](../superpowers/plans/2026-04-21-admin-dashboard.md)

## What's in v1

- Dedicated subdomain (`admin.` on the environment's root domain) aliased to the existing `isol8-frontend-*` Vercel project.
- Route group at `apps/frontend/src/app/admin/*`, gated by host-based middleware (only the admin host serves these routes).
- Backend `/api/v1/admin/*` endpoints, gated by `Depends(require_platform_admin)` (allowlist driven by `PLATFORM_ADMIN_USER_IDS`).
- Audit table `isol8-{env}-admin-actions` records every write action.
- **No edge SSO gate.** Defense-in-depth = host-check + Clerk + allowlist + audit. (See "When to add an edge gate" below.)

## Prerequisites — one-time per environment

1. **DNS + Vercel domain alias:** `admin.dev.thelightbulb.company` → `isol8-frontend-dev` Vercel project; `admin.isol8.co` (→ `admin.thelightbulb.company` at the flag-day flip) → `isol8-frontend-prod`. Verify with `dig admin.dev.thelightbulb.company CNAME`.
2. **Backend secrets** (one Secrets Manager entry per value, consistent with existing `isol8/{env}/clerk_issuer`, `isol8/{env}/stripe_secret_key`, etc.):
   - `isol8/{env}/platform_admin_user_ids` — comma-separated Clerk user IDs of the Lightbulb team. Wired into the backend task as `PLATFORM_ADMIN_USER_IDS`.
   - `isol8/{env}/posthog_project_api_key` — PostHog personal API key with scopes `person:read`, `events:read`, `session_recording:read`. Wired as `POSTHOG_PROJECT_API_KEY`. Optional; the Activity tab stubs gracefully when absent.

   `POSTHOG_HOST` and `POSTHOG_PROJECT_ID` are plaintext env vars on the backend task definition (not secrets — project ID is public in every PostHog URL).
3. **CDK deploy** pulls the new secrets into the backend task definition at the next service rollout.

## Adding a new admin

1. Get the new admin's Clerk user_id (visible in Clerk dashboard → Users; or query via `clerk_sync_service.get_user_by_email`).
2. Update the `platform_admin_user_ids` secret:

   ```bash
   CURRENT=$(aws secretsmanager get-secret-value \
     --secret-id isol8/dev/platform_admin_user_ids \
     --query SecretString --output text --profile isol8-admin)
   aws secretsmanager update-secret \
     --secret-id isol8/dev/platform_admin_user_ids \
     --secret-string "${CURRENT},${NEW_USER_ID}" \
     --profile isol8-admin
   ```

3. Force a backend rollout (`aws ecs update-service --force-new-deployment ...`). Within ~2 min the new admin can sign into the admin host at `/admin` via Clerk and reach `/admin/users` (`https://admin.dev.thelightbulb.company` on dev, `https://admin.isol8.co` on prod — note there is no `admin-{env}.` record; that form never resolved).

## Removing an admin (immediate)

1. Update `isol8/{env}/platform_admin_user_ids` with a new comma-separated list that omits their user_id.
2. Force a backend rollout. The next API call from any open admin session 403s.
3. Optional: revoke their Clerk sessions via another admin's UI: `Actions → /admin/users/{user_id}/account/force-signout`.

## Breaking glass — disable admin entirely

```bash
aws secretsmanager update-secret \
  --secret-id isol8/{env}/platform_admin_user_ids \
  --secret-string "" \
  --profile isol8-admin
```

Force a backend rollout. Every `/admin/*` API endpoint 403s within ~2 min. The Next.js middleware still serves the host so users hitting `admin.isol8.co/admin` see the not-authorized page (no information leak).

DNS alias and Vercel project remain. Once the incident is resolved, restore the comma-separated list and force another rollout.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `/admin` returns 404 in browser | Wrong host (you're on the app host instead of the `admin.` host) | Check URL |
| `/admin/me` returns 403 | Your Clerk user_id missing from `PLATFORM_ADMIN_USER_IDS` | Add it; redeploy |
| Admin page renders but Stripe panel shows error banner | Stripe API timeout or auth issue; `admin_service` returns partial responses on upstream failure | Check Stripe dashboard health; verify `STRIPE_SECRET_KEY` in Secrets Manager |
| PostHog tab shows "No PostHog activity recorded — user may not have visited the frontend" | User legitimately has no PostHog identify yet; OR `POSTHOG_PROJECT_API_KEY` is unset | Confirm by visiting the frontend as that user; otherwise mint the PostHog project key |
| CloudWatch Logs tab shows "no logs" | User hasn't generated logs in the time window; or IAM missing on the backend task role | Check task role has `logs:FilterLogEvents` on `arn:aws:logs:{region}:{account}:log-group:/ecs/isol8-{env}:*` (added in #355) |
| Write action returns `audit_status: "panic"` | DDB write to `admin-actions` failed after the action executed | Check CloudWatch for `ADMIN_AUDIT_PANIC` log entries; investigate DDB connectivity / IAM. The action *did* run; the audit row is missing. |

## When to add an edge gate (Phase 2 trigger)

V1 deliberately ships without an edge SSO gate to avoid adding a vendor. Defense relies on host-check + Clerk + `require_platform_admin` + audit.

Watch the `admin_api.errors` CloudWatch metric (added in Phase B) for repeated 403s on `/admin/me` from unknown IPs. If probe traffic appears, prioritize Phase 2 backlog item 14 — pick one of:

- **Cloudflare Access** — SSO gate in front of the subdomain. Requires Cloudflare as a vendor + DNS handover.
- **Vercel Deployment Protection** — Vercel-native password / SSO gate. Pro+ plan only.
- **HTTP basic auth via Next.js middleware** — ~15 LOC: if the host is the `admin.` host and there is no `Authorization: Basic` header, return 401 challenge. Username/password in `ADMIN_BASIC_AUTH_*` env vars. Zero new vendor.

## Local development

`apps/backend/.env.local` should include:

```
PLATFORM_ADMIN_USER_IDS=user_<your dev Clerk id>
# Stub PostHog — leave POSTHOG_PROJECT_API_KEY unset to short-circuit the API call
```

Frontend env (`apps/frontend/.env.local`):

```
# dev today, and prod after the flag-day flip
NEXT_PUBLIC_ADMIN_HOSTS=admin.thelightbulb.company,admin.dev.thelightbulb.company,admin.localhost:3000
# prod until the flip
NEXT_PUBLIC_ADMIN_HOSTS=admin.isol8.co,admin.dev.isol8.co,admin.localhost:3000
```

Then `pnpm dev` and visit `http://admin.localhost:3000/admin` (Chrome/Safari resolve `*.localhost` → 127.0.0.1 automatically — no `/etc/hosts` edit).

## Related

- [Phase B onwards](../superpowers/plans/2026-04-21-admin-dashboard.md) — backend router + frontend pages.
- `core/auth.py:242` — `require_platform_admin`.
- `apps/infra/lib/stacks/database-stack.ts` — `admin-actions` table.
