# Apps production spike log (29 Oct 2026)

Workspace: Lightbulb Demo Lab (prod, `org_3Jshao85twVsEky1nB16fVWqFeW`, company Stripe `acct_1963hsLzPYUYxHpf` via pasted key). Driver: the founder's Chrome, Settings → Connections. Backend at `c825a2f0a` (PR #4473, services account requests the developer configuration only).

## Spike 1: services-account onboarding (timing + screens)

| When (UTC) | Step | Result |
|---|---|---|
| 03:21:19 | Click "Add a services account" | Refused by Stripe. Toast: "You cannot set `dashboard` unless the account is configured as a merchant or a recipient with the stripe_balance.stripe_transfers capability." No account created; page unchanged. |

Read: the create call (`company_stripe.py`, `v2.core.accounts` with `dashboard=full` + developer configuration) must drop `dashboard` for a Projects-only account. Stripe's platform guide's example body has no `dashboard` field. Fixed by the rail session in PR #4478 (`dff472a39`): no `dashboard`, `defaults` carries only `currency` (Stripe also refuses `defaults.responsibilities` without merchant, found by probing the dev Sandbox); verified by creating `acct_1UVjvmIBaSdAwWVf` in the dev Sandbox. Prod deploy run 40812050858; spike 1 restarts when the backend has it.

Requirements on a FRESH developer-only account (dev Sandbox, read by the rail session): 8 entries, all `from: user`: `contact_email`, the ToS attestation (date + ip), `representative.given_name`, `representative.surname`, `representative.date_of_birth` (day, month, year). No SSN, ID document, bank, entity type or business details. The earlier "email only" reading came from an account whose other details were already filled.

| 03:50:20 | Click "Add a services account" (backend `dff472a39`, PR #4478) | `POST /api/v1/company/stripe/services-account` → account created in ~10 s. Button becomes "Finish services account setup". Stripe's embedded `account-onboarding` component mounted in the card: "Add information to start accepting money / isol8, Inc. uses Stripe to help you receive payments and keep your personal bank and details secure" + [Add information]. |
| ~03:52 | Card re-rendered | The component had disappeared; only "Finish services account setup" remained. UI bug: the component should stay mounted (or the button should be the only door). |
| ~03:58 | Click "Finish services account setup" | `POST /api/v1/company/stripe/account-session` 200 → component mounted again, same first screen. So the flow is: our button → Account Session → Stripe component → "Add information" → Stripe's form steps. |
| ~03:59 | Click "Add information" | Button entered its loading state; the founder stopped the run here to question the UX. Form steps not yet recorded. Timing dropped at the founder's request; screens only. |

Founder's read of the UX (03:59): this Settings → Connections path is not how Apps users should do it. The Apps design §6 already says: create the services account silently on the first app request and show Stripe's form once from a card in our words. Stripe's "start accepting money" copy is its component default; whether a Projects-only account can get other wording is a question for Stripe (docs research launched 29 Oct, `docs/research/2026-10-29-stripe-services-account-onboarding-docs.md`). Two UI bugs for the rail: the vanishing component, and the two-button dance.

| ~04:05 | "Finish services account setup" again → "Add information" | Spinner never ends (5+ min), on a fresh Account Session too. Rail owner: the embedded component never got past this step in its testing; Stripe-collected requirements need a Stripe sign-in popup ("includes a popup to a Stripe-owned window", cannot be removed); an automated browser blocks it. Every completed onboarding so far used hosted Account Links. |
| ~04:20 | Hosted door: `POST /api/v1/company/stripe/onboarding-link {"target":"services"}` from the page with the Clerk token, same tab navigated | connect.stripe.com "Onboard your account": "isol8, Inc. uses Stripe for secure financial services. Let's get started. Email address (Have a Stripe account? You can use the same email.)" Handed to the founder for the email and any code. |
| 00:31 (local) | Rail owner merged PR #4480 (`65d457eeb`) | Both services-account buttons open the hosted Account Link in a new tab. Deployed (run 40818448752). |
| ~00:50 (local) | Owner ruling (via the rail session, per Rami at Stripe): onboarding stays on Stripe's EMBEDDED component | #4480 reverted (PR #4482, `8ff6b7bc8`, deploy run 40824725653 queued). The rail owner will verify the embedded popup in the founder's real Chrome before calling it broken. The spike's hosted run may finish as is; same account either way. |

Docs audit of the flow: `docs/research/2026-10-29-stripe-services-account-onboarding-docs.md` (create body confirmed; `projects_ready` ignores `requirements`, deferred by the rail owner until an active-but-due case appears; copy question for Stripe drafted there).

| ~06:30–07:40 | Founder's own clicks in the embedded component | Stripe sign-in popup opened on a real click; networked screen offered to reuse the founder's existing (CrowdVolt) Stripe details or create a new account; "Verification required" for the account's contact email. That email had been copied from the clicking Clerk login (External Review, a login-only test user with no mailbox), so the code was undeliverable; fixed by one platform API call setting `contact_email` to the founder's gmail (`POST /v2/core/accounts/{id}`), then Resend, code, Agree and continue. |
| ~07:45 | Done | Settings card shows only "Open Stripe Dashboard / Disconnect"; `GET /company/stripe/status` → `projects_ready: true`, `services_account_id: acct_1UVkM5I8PJQBZmgC`, `details_submitted: true`. |

Screens Stripe showed (embedded, developer-only account): sign-in popup → "Select an existing account to get paid sooner" (reuse CrowdVolt details / create new) with "Agree and continue" (Connected Account Agreement, Acquirer Disclosure) → "Verification required: 6-digit code to <contact email>" → done. No bank, tax number or ID document asked beyond what the reused account already held. Rulings: onboarding stays embedded (per Rami); no change to how contact_email is chosen (every real Lightbulb login is Clerk-verified; the misfire was a test login).

## Spike 2: what `neon/postgres` and `vercel/project` reveal

Started once `projects_ready` was true: the founder's request (posted as him) to the Demo Lab CEO agent in #lightbulb-demo-lab: `neon/postgres` free and `vercel/project` hobby with configuration name `spike-app`; approvals in the Demo Lab Inbox are the founder's.

### Run on Demo Lab, 29 Oct (UTC)

| When | Step | Result |
|---|---|---|
| 08:30 | Demo Lab CEO asked (in its DM, via the founder's Chrome) to request `neon/postgres` free + `vercel/project` hobby `spike-app` | First attempt: agent catalog empty. Root cause: the rail joined catalog services to providers on `provider_id`; the live 2026-09-30.preview service row carries `provider` (id) and `provider_name`, no `provider_id`. Unit fixtures had invented `provider_id`. Fixed: PR #4486 (`provider_of`), merged on the founder's instruction, prod deploy run 40838562712. |
| 08:31 | Retry after the deploy | Agent sees the catalog. `provider_name` slugs must match case-insensitively (agent first sent `Neon`/`Vercel`, got 422, then lowercase). Neon: row → `awaiting_approval`, card filed. Vercel: card filed, then 500 on the `ask → awaiting_approval` transition: `TypeError: Float types are not supported` (a float in Vercel's catalog row reached DynamoDB). Row stuck in `ask`, agent retry reused it. Fix: PR #4488 (`ddb_safe` on every workflow-repo write), awaiting merge + deploy. |
| 08:37 | Neon card approved (Activity → Needs you → Approve; limit field shows $50/month default) | |
| 08:38 | Owner `GET /company/provisioning/workflows` | Neon `active`, resource `postgres-85a63d`, ~6 min after approval, no Neon sign-in or redirect. |

**What `neon/postgres` reveals (key names only):** `branch_id`, `branch_name`, `connection_string`, `database_name`, `org_id`, `project_id`. No API key. Consequences for the Apps plan: (1) Neon branching for previews and version snapshots is not possible with revealed credentials, so plan 1 Task 9 takes its fallback (dump/restore snapshots; previews share the live database; migrations run on `publish`); (2) the app's `DATABASE_URL` is set from `connection_string`; (3) these generic lower-case names land in every roster agent's `.env` today (gap 3 in design §6), and two Neon resources would overwrite each other.

### What the revealed Neon credential can do (tested 29 Oct on Demo Lab's provisioned database, then cleaned up)

- Server: Postgres **18.6**, direct (non-pooled) endpoint. Role `neondb_owner` has `CREATEDB` and `CREATEROLE`.
- **Preview database:** `CREATE DATABASE <app>_preview` works with the revealed role; it is a separate database in the same Neon project, so a preview with a schema change never touches live rows. No second card.
- **Template copy** (`CREATE DATABASE v TEMPLATE live`) snapshots and restores rows + schema, but fails whenever any session is on the source, and ended sessions take seconds to drain. Not usable against a live app.
- **pg_dump / pg_restore** (client **18** required; client 14 refuses a v18 server): a custom-format dump taken while 3 sessions held the database open succeeded; restore = `DROP DATABASE live WITH (FORCE)`, `CREATE DATABASE live`, `pg_restore --no-owner --no-acl` → 1,000/1,000 rows and the original 2 columns back in ~5 s.

**Decision for plan 1 (replaces Neon branching):** versions snapshot with `pg_dump -Fc` (stored per version under `apps/{owner}/{app}/db/v{n}.dump` in `OWNER_STATE_BUCKET`); rollback = force-drop + create + `pg_restore`; previews use `<app>_preview` restored from the latest live dump on each `up`; the backend image needs `postgresql-client-18` (PGDG apt). No Neon API key needed.

### Vercel, after PR #4488 (29 Oct)

| When | Step | Result |
|---|---|---|
| after deploy 40952163705 | Stuck row `pwf_8cb92eeb…` cancelled through the owner route (`POST /company/provisioning/workflows/{id}/cancel`) | `cancelled` |
| +1 min | CEO agent re-requested `vercel/project` hobby `spike-app` | `pwf_eaf29372…` → `awaiting_approval` with its card: the float fix works in production |
| +2 min | Card approved (Activity → Needs you) | `provisioning` → `active` in ~30 s; no Vercel sign-in or redirect |

**What `vercel/project` reveals (key names only):** `org_id`, `project_id`, `project_link`, `project_url`, `token`. A Vercel token is revealed, so the platform can call Vercel's API for the app (env vars, deployments from files, domains, rollback) without the owner pasting anything. Not yet tested: that the token's scope allows file deployments and domain changes on that project (next step).

### End to end: a platform-deployed app on the customer's own Vercel + Neon (29 Oct)

Script in the session scratchpad (`e2e_deploy.py`, `e2e_check.py`, `e2e_rollback.py`), credentials read from Secrets Manager, never printed.

| Check | Result |
|---|---|
| Token scope | The revealed Vercel token lists exactly one project, `spike-app` (team `prabuddha-9877-stripe`). It is scoped to the app, not the account. |
| Default protection | `ssoProtection: all_except_custom_domains`: Vercel's login wall is on for every URL except custom domains. `PATCH /v9/projects/{id}` with `ssoProtection: null` turned it off (200). Our door must replace it. |
| Env vars | `POST /v10/projects/{id}/env?upsert=true` set `DATABASE_URL` (from Neon's `connection_string`) for production and preview (201). |
| Deploy from files | `POST /v2/files` ×3 + `POST /v13/deployments` (target production, framework nextjs): READY in 31 s; served at `spike-app-ashy.vercel.app`. Vercel refused a known-vulnerable Next.js (15.5.6): "Vulnerable version of Next.js detected"; the starter must pin current versions. |
| Database | The live page creates a table, inserts a row and reads the count on each request; the count increments across requests. App ↔ Neon works with the revealed credentials. |
| Custom domain | `POST /v10/projects/{id}/domains` for `spike-app.apps.thelightbulb.company` → 200, `verified: false`, needs TXT at `_vercel.thelightbulb.company`; recommended CNAME `c8245b9daf7c985f.vercel-dns-017.com.` (per-project) or `cname.vercel-dns.com.`. Removed after the test. Route53 records not written. |
| Rollback | A second deployment went live (page showed "v2"); `POST /v1/projects/{id}/rollback/{v1}` → 201 and the live URL served v1 again within 6 s. |

**Conclusion:** the hosted-apps path works on the customer's own accounts with no human step beyond the services-account form and two approval cards: Stripe provisions Neon and Vercel, reveals a project-scoped Vercel token and a Neon connection string, and the platform can set env vars, deploy from files, turn off Vercel's login wall, attach a subdomain and roll back, all by API. Leftover on Demo Lab for later cleanup: Neon resource `postgres-85a63d` (table `hits`), Vercel project `spike-app` (3 deployments, `DATABASE_URL` env var).
