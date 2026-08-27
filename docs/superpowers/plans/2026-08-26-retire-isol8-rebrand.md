# Retire isol8 → Lightbulb AI (flickmeon.ai) — Program Plan

> **For agentic workers:** this is the PROGRAM plan (phases, inventory, vendor
> checklists, runbooks). Each phase gets its own bite-sized implementation plan
> (superpowers:writing-plans format) when it starts; execute those with
> superpowers:subagent-driven-development. Steps use `- [ ]` for tracking.
> Working doc — DELETE when the retirement ships (git history is the record).

**Goal:** Retire the `isol8` brand, domain and identity completely. Product
= **Lightbulb AI** at **`flickmeon.ai`**; company = **The Lightbulb
Companies** at **`thelightbulb.company`** (humans, vendor logins,
platform sender/support mail). No dual-domain window: hard cutover, then
tear down everything `isol8`.

**Approach:** (1) make the repo domain-agnostic with zero behavior change,
(2) flip dev, (3) flip prod in one window with the text-only brand swap,
(4) visual rebrand when the logo/brand manual lands, (5) legal-entity-gated
vendor updates when the amended entity paperwork lands, (6) internal
identifiers, each with its migration cost decided explicitly.

**Spec:** the decisions in the 2026-08-26 session, recorded under "Decisions".

## Global Constraints

- Hard cutover. `isol8.co` is never served alongside `flickmeon.ai`. The only
  transition mechanic is the repo's existing dev-first / prod-on-dispatch model
  (per-env `rootDomain` pin), plus the SES receipt-rule-set singleton (see P2).
- Users may be asked to sign in again. Backfills are acceptable if scoped.
- Order: domain → visual (after logo) → legal-entity-gated.
- Legal entity is a RENAME of the existing company (same EIN) — vendor
  updates are name changes + re-verification, not new accounts.
- Everything means everything: runtime code, infra, CI, tests, CLAUDE.md,
  docs, `.claude/skills`, memory/context files, image/skills/plugins, fork
  repos, vendor dashboards, DNS, AWS resource names (P6, cost-scoped).
- E2E verification is Claude-in-Chrome on DEV with a signed-in dev account.
- Container rolls go through the admin UI. Prod deploys are a
  `workflow_dispatch` into an EMPTY queue (count the 14 prod jobs).
- `deploy.yml` is generated — every `apps/infra/lib` change runs `cdk synth`
  (OrbStack) and commits the regenerated file in the same PR.

## Decisions (locked 2026-08-26)

| Topic | Decision |
|---|---|
| Product domain | `flickmeon.ai` (Route53 zone `Z03529981ZCUW1R3FM806`, currently NS/SOA only) |
| Product name | Lightbulb AI |
| Company domain | `thelightbulb.company` — humans, vendor logins (Aden, Google Workspace), platform sender + support mail |
| Legal name | The Lightbulb Companies (rename of the existing entity) |
| Dual domain | NO. Hard cutover. |
| GooseTown | Dead. Tear down with the zone; future = own domain + own AWS account |
| Sessions | Forcing re-login is acceptable |
| iOS | App never shipped (rejected ×2, W-9 blocked, no IAP product) → NEW bundle id + app record + product id |
| Desktop | Has installs (team). Old installs die with the domain (updater endpoint) → ship a fresh DMG; identifiers renamed (default, see Naming) |

### Open decisions (defaults apply until overridden)

- [ ] Deep-link scheme: default `lightbulbai://` (web callback pages + Clerk native callback follow).
- [ ] Desktop bundle id: default `ai.flickmeon.desktop` (fresh install; device identity re-pairs).
- [ ] iOS bundle id / product id: default `ai.flickmeon.mobile` / `ai.flickmeon.mobile.plan.monthly`.
- [x] **SEO / link equity: DO NOT keep `isol8.co` alive as a redirect** (owner, 2026-08-26). Evidence: the SEO landing pages + `seo.tsx` landed **2026-08-21** (#1962), docs + blog rebuild **2026-08-26** (#2051); sitemap is 16 URLs, 6 blog posts, 5 docs pages — 1–5 days old, nothing meaningful indexed or backlinked. Park the domain at flip time; skip GSC Change of Address. Cheap substitute: verify `flickmeon.ai` in GSC and submit the sitemap on day 1.
- [x] **Apple SIWA: do NOT preserve `co.isol8.signin` — create `ai.flickmeon.signin` fresh** (owner, 2026-08-26). Evidence: of 18 prod `agent-email-addresses` rows exactly 3 carry an `@privaterelay.appleid.com` verified sender (velvet-ante, meridian-labs, flint) and **all 3 owners are `trial_local`**; none of the 4 paying accounts (3 users + `org_3H3viG…`) has one. Losing SIWA `sub` continuity costs at most 3 expired trials, who re-sign-up as new users. NOTE: SIWA must still EXIST on the new brand (Apple requires it alongside other social logins; mobile uses `expo-apple-authentication`) — so this is "new Services ID", not "drop Apple". Evidence gap: the Clerk per-user provider census returned HTTP 403 (error 1010), and 4 of 22 owners have no agent-email row, so a non-relay Apple user cannot be fully excluded.
- [ ] pnpm scope: default `@lightbulb/*`; cargo crate `lightbulb-desktop`.
- [ ] GitHub org: default rename to `LightbulbAI` in P6 (OIDC trust first).
- [ ] DynamoDB table prefix / CDK stack names: default KEEP (P6 lists the cost).
- [ ] Who hosts DNS for `thelightbulb.company` (needed for SES DKIM/SPF of the platform sender)?

## Naming map (old → new)

**Hosts** (all `isol8.co` → `flickmeon.ai`, same prefixes):
`isol8.co`→`flickmeon.ai`, `www.`, `dev.`, `admin.`, `admin.dev.`, `api.`/`api-dev.`,
`ws.`/`ws-dev.`, `bv.`/`bv-dev.`, `company.`/`dev.company.` (Better Auth origin
identity only — NXDOMAIN today, stays NXDOMAIN), `clerk.`/`accounts.`/`clkmail.`
(Clerk). **No mail hosts on `flickmeon.ai`** — agent mail moved to
`{agent}@{slug}.thelightbulb.company` (see Mail below).

**Mail — ALL of it on ONE domain, `thelightbulb.company`** (owner, 2026-08-27).
`flickmeon.ai` is the PRODUCT URL ONLY and gets **no MX, no SES identity, no mail
records of any kind**. This is a straight rename of the pattern already running in
prod on `isol8.co`, not a new design:
- platform sender (apex): `hello@` / `support@` / `tickets@` / `admin@thelightbulb.company` — the SES outbound to customers (`EMAIL_FROM_WELCOME`, `EMAIL_FROM_TICKETS`, `EMAIL_SUPPORT_ADDRESS` in `core/config.py:460-468`)
- agent mail (subdomains): `{agent}@{slug}.thelightbulb.company`, e.g. `friday@acme.thelightbulb.company`
- **ONE SES domain identity** (`thelightbulb.company`) covers both — SES authorizes subdomain sends via the parent identity, which is why `agent_mail_tenant._identity_domain()` can stay a single-domain function and the IAM grant stays one ARN (`service-stack.ts:495`)
- apex `MX → smtp.google.com` (pri 1) for humans + `support@`; wildcard `MX *.thelightbulb.company → inbound-smtp.us-east-1.amazonaws.com` for agent mail — the two do not collide (`*.` never matches the apex), exactly as `isol8.co` runs today
- apex SPF carries BOTH senders in one record: `v=spf1 include:_spf.google.com include:amazonses.com ~all` (mirrors `ses-inbound-stack.ts:181`)
- `AGENT_EMAIL_DOMAIN` = `thelightbulb.company` (prod) / `dev.thelightbulb.company` (dev)
- DMARC rua → `admin@thelightbulb.company`. Platform-admin gate domain → `@thelightbulb.company`.

**Brand strings:** `Isol8`/`isol8` → `Lightbulb AI`; `isol8 Inc.` →
`The Lightbulb Companies` (interim: "Isol8 Inc., doing business as Lightbulb AI"
until the amendment is effective); Slack manifest `Isol8 Agent` → `Lightbulb AI
Agent`; DCR `client_name` → `Lightbulb AI`; SES sender display names.

**Native/app ids:** `co.isol8.mobile`→`ai.flickmeon.mobile`, `co.isol8.desktop`→
`ai.flickmeon.desktop`, `co.isol8.mobile.plan.monthly`→`ai.flickmeon.mobile.plan.monthly`,
scheme `isol8://`→`lightbulbai://`, Expo `name` Lightbulb AI / `slug`
`lightbulb-ai-mobile` / `owner` (rename Expo account; `projectId` + `updates.url`
unchanged), Tauri `productName` Lightbulb AI, sidecar `isol8-browser-service`→
`lightbulb-browser-service`, `Isol8.app`→`Lightbulb AI.app`, app-support dir follows
bundle id, keychain service follows bundle id, `/tmp/isol8-desktop.log`.

**Internal (P6, each cost-scoped):** `@isol8/*` packages, `isol8-desktop` crate,
`ISOL8_*` env vars, `x-isol8-*` headers, `.isol8-guard`/`.isol8-protocol`,
`isol8-facts.json`, `isol8-setup-seed.json`, `isol8-marketplace` plugin id,
`isol8` skill dir + `isol8-mail.sh`, `isol8.notifications` fork plugin,
`PAPERCLIP_ISOL8_*`, localStorage keys `isol8:*`, `isol8-file://`, DDB
`isol8-{env}-*`, Secrets `isol8/{env}/*`, SSM `/isol8/*`, KMS alias, S3 buckets,
ECR `isol8/*`, GHCR `ghcr.io/isol8ai/*`, CDK stack/pipeline names, IAM roles,
log groups, Cloud Map `isol8-{env}.local`, AWS SSO `isol8.awsapps.com`, CLI
profile `isol8-admin`, GitHub org `Isol8AI`, image tags `-isol8.N`, CloudWatch
metric namespace `Isol8/*`, Cloudflare Zero Trust team, Mailsac prefix.

---

## Phase 0 — Long poles + the doors that CLOSE (no code)

> Findings detail: `2026-08-26-retire-isol8-findings.md` §2/P0, C47, B0.
> **B0: the mail move gates the AWS account, not the other way round.** Every item
> below needs a *readable* `admin@isol8.co` mailbox and an interactive confirmation
> click. None of it can be done after Aden retires the Workspace — which the earlier
> draft scheduled in P3 with nothing before it. This is now the critical path.

- [ ] **`thelightbulb.company` — registration IN_PROGRESS, NOT delegated** (checked 2026-08-27). This is the domain we will actually use for humans, vendor logins and the platform sender. Hosted zone already exists in-account: **`Z09913772LJJ6H4X9JJX0`**, NS `ns-1135.awsdns-13.org`, `ns-369.awsdns-46.com`, `ns-1870.awsdns-41.co.uk`, `ns-919.awsdns-50.net`. Zone holds NS+SOA only; no public delegation yet. **Wait for `REGISTER_DOMAIN` = SUCCESSFUL, then confirm the public NS set matches those four** (the duplicate-zone check that passed for the other domain).
- [x] **`thelightbulbcompanies.com` — registered + delegated, but NOT the email domain.** `REGISTER_DOMAIN` SUCCESSFUL, zone `Z03664973B72V7SFCSH00`, delegation verified. Superseded 2026-08-27 by `thelightbulb.company`. Its Google verification TXT was added and then **DELETED** (change `C05518211HT7R9CGNDTY9`, INSYNC; zone back to NS+SOA). Keep the registration — decide later whether it redirects to the company site or is simply parked.
- [ ] **Google Workspace on `thelightbulb.company`** (Aden): supplies a NEW verification TXT + MX for this domain — the previous token was domain-scoped and is now void. We add them once the domain delegates. Order stays: verify → mailboxes → MX (`smtp.google.com` priority `1`; the 5-record `aspmx…` set is legacy).
- [ ] ⚠️ **The apex TXT set will be load-bearing — append, never replace.** Route53 holds every TXT value for a name in ONE record set, so adding SPF as a separate apex TXT would DELETE the Google token and un-verify the Workspace. Exactly the trap `ses-inbound-stack.ts:170-183` documents for `isol8.co`. When this zone comes under CDK, `google-site-verification=…` and `v=spf1 include:amazonses.com ~all` go in the SAME values array.
- [ ] **Mailboxes/groups on `thelightbulb.company`** (Aden): `hello@ support@ tickets@ admin@` + human addresses. Blocks the SES platform-sender identity.
- [ ] **AWS Organizations root-account email** is `admin@isol8.co`. Change it (root creds + MFA) BEFORE the Workspace dies, or account recovery is lost.
- [ ] **4 SNS alarm subscriptions + 4 Budgets subscribers** on `admin@isol8.co` — SNS re-subscribe requires clicking a confirmation link in the new mailbox. Do it while both mailboxes are readable.
- [ ] **Aden's own IAM Identity Center identity is `aden@isol8.co` (us-east-2).** He can retire the Workspace and lose his own AWS console access in the same action. Move it in step 1 of the forced order below — NOT as part of the human email move, which waits behind a code fix + backfill.
- [ ] **3 `google#account#…@isol8.co` rows in `isol8-prod-integrations`** (aden@, admin@, support@) — the address is in the SORT KEY, so there is no UPDATE: delete + re-connect under the new addresses, and re-point the dangling `google#assign#` + `google_watch_channels` rows. Retiring the Workspace without this silently kills every Google-backed routine on the founder org.
- [ ] Aden: inventory what the `isol8.co` Workspace holds (users / groups / aliases / delegated mailboxes / shared drives) before retiring it, and say which of `hello@ support@ tickets@ admin@` are mailboxes vs groups (it changes how we configure SES sending). **No mailboxes on `flickmeon.ai`** — that domain is product-only and carries no mail records at all (superseded 2026-08-27: agent mail is `{agent}@{slug}.thelightbulb.company` via SES, and the platform sender is on `thelightbulb.company` too — see Mail above).
- [ ] Apple: keep the in-flight individual→company migration going. **Confirm in App Store Connect that no IAP product exists and no TestFlight tester holds an installed build** before committing to a new bundle id (see the Apple IAP correction — the rail is live in prod, contrary to CLAUDE.md).
- [ ] Meta: plan re-review for both apps (`1049347507456011`, `2080424579211211`).
- [ ] Termly: prepare "Isol8 Inc. d/b/a Lightbulb AI" edits for the 7 docs.
- [ ] Decide + record the **brand residue we accept** (see Done criteria): customer Slack apps named "Isol8 Agent", `isol8-connector` CFN stacks in customer AWS accounts, `isol8.co` strings inside litestream SQLite replicas and frozen marketplace artifacts.
- [ ] Produce the **per-owner comms matrix** (computable today from `integrations`, `oauth-tokens`, `channel-links`, trigger rows across 22 owners / 4 paid).

## The human email move — forced order (VERIFIED 2026-08-27)

> The email move is **not** a P0 step. It sits behind a code fix and a backfill,
> because the backfill is a ONE-WAY DOOR. Every claim below was verified in code
> or against live AWS.

**0. Create the new mailboxes. Move nothing.** `hello@ support@ tickets@ admin@ aden@
thelightbulb.company`. Prerequisite for everything; changes nothing.

**1. AWS account identity — must happen while `admin@isol8.co` is still readable.**
Root-account recovery email; 4 SNS alarm subs + 4 Budgets subscribers (SNS sits in
`PendingConfirmation` until a human clicks, so it is impossible afterwards); Aden's
Identity Center identity (**us-east-2**, not us-east-1). Also **delete the
`assets.town.isol8.co` ACM cert** — verified `InUseBy: 0`, `NotAfter 2026-09-25`,
`RenewalEligibility: INELIGIBLE`, so it will fail renewal and email warnings into the
mailbox being retired.

**2. Ship the P1 code fix (deploy dev → prod) BEFORE any Clerk email changes.**
- `PaperclipCompany.paperclip_email` written at all 4 row sites, read at all 6 Better-Auth sign-in doors
- add each admin's **future** `@thelightbulb.company` address to `_PLATFORM_ADMIN_EXTRA_EMAILS` (`core/auth.py:280`; the gate at `:293` is `endswith(domain) OR in extra_emails`, so this is additive and safe)
- do-not-rename markers on `BOOTSTRAP_EMAILS` / `ADMIN_EMAIL`
**Why before:** a Clerk primary-email change takes effect on the **next token mint**
(minutes) while the code fix needs a deploy. That gap is an admin lockout.

**3. Run the `paperclip_email` backfill. ← ONE-WAY DOOR.**
Seed from **Aurora** (`SELECT email FROM "user" WHERE id = :paperclip_user_id`), never
from Clerk. It must finish **while Clerk still holds the old addresses** — once a primary
flips, nothing in the system knows what address Better Auth was seeded with, and recovery
is a manual per-owner SQL query. Verify: every `status=active` row has a non-empty value.
**Skip this and every org member-add for that owner 401s — and Better Auth's sign-in
limit is per-IP, so Teams degrades fleet-wide, not for one user.**

**4. Move the email — on the EXISTING Clerk user, never a new one.**
Add the new address as a **second** address, flip primary, and **keep the old one as a
verified secondary**. Creating new Clerk users orphans marketplace `seller_id` rows, org
memberships, and every `PaperclipCompany.user_id`.
The old-as-secondary is the lever that keeps everything working: `/admin` via the
extra-emails list, Teams via the stored `paperclip_email`, the curator gallery because
`clerk_admin` queries Clerk by `email_address` (matches **any** address, not primary),
and the wipe guard because `account_wipe` enumerates all `email_addresses` before
checking `BOOTSTRAP_EMAILS`.

**5. Domain work:** P1 remainder → P2 dev → P3 prod flip.

**6. Last:** remove the old addresses, remove the interim `_PLATFORM_ADMIN_EXTRA_EMAILS`
entries, retire the Workspace.

## Phase 1 — Domain-agnostic repo (no behavior change) — IN PROGRESS

**Shipped 2026-08-27:**
- [x] **#2061 MERGED** (`8e81ae37f`) — Better Auth identity survives a Clerk email-domain move: `paperclip_email` on the row (4 constructions + both adopt branches), read at all 6 Clerk-bound sign-in doors via `_auth_email` (stored-first, never the reverse), `_PLATFORM_ADMIN_EMAIL_DOMAINS` tuple accepting `@isol8.co` AND `@thelightbulb.company`, DCR redirect-URI re-registration, 5 do-not-rename markers, and `scripts/backfill_paperclip_email.py`. Review caught two blockers, both fixed before merge: the backfill's `UpdateItem` (an upsert) lacked `attribute_exists(user_id)` and could RESURRECT a deleted row malformed — `KeyError` on every `get()` plus a permanently unwinnable seed claim; and the runbook omitted the deploy-first ordering.
- [x] **#2062 MERGED** — infra root domain is a per-environment parameter (`lib/domains.ts`: `ROOT_DOMAIN`, `PLATFORM_MAIL_DOMAIN`) threaded through 6 stacks + `app.ts`. Proof of no behaviour change: base-vs-branch synth A/B, **0 of 23 CloudFormation templates differ**; `deploy.yml` byte-identical; 298 infra tests pass.
- [ ] **#2063** — frontend/mobile one-domain-source collapse (5 site-URL copies, CSP, middleware host sets, company rewrites, desktop-updater allowlist, deep-link scheme, mobile config). 2990 frontend + 357 mobile tests pass; CSP verified against the RUNNING build. Fixes a live docs defect: `.env.example` documented `admin-dev.isol8.co`, which has no DNS record — the real host is `admin.dev.isol8.co`, and `NEXT_PUBLIC_ADMIN_HOSTS` is not set in Vercel prod, so the middleware default is what runs.
- [ ] **PR-D** — backend literals → settings (gateway regex, cookie domain, marketplace host allowlist, OpenAPI servers, agent-mail body URLs, `agent_mail_tenant` identity shape, `billing_service`'s module-level getenv), plus `RESERVED_SLUGS` += lightbulb/lightbulbai/flickmeon (all verified FREE in prod+dev) and the `curator.py` no-TTL cache fix.

**NEXT AFTER PR-D — the one-way door.** Once the dev deploy has fully rolled:
`backfill_paperclip_email.py --env dev --dry-run` → real → verify exit 0. Then the same on prod
BEFORE any Clerk email moves. An old task still serving during a rollover silently blanks
backfilled rows (pre-deploy `put` has no `paperclip_email` key; deploys run at
`maxHealthyPercent: 200`).

**Still open from the #2061 review (pre-move checklist, not code bugs):**
- `agent_email.py` seeds `verified_senders` from the Clerk address at mint time → agents stop waking on owner email after a move. Self-serve fixable via `routers/agent_mail.py`.
- The adopt path trusts the row's `paperclip_user_id` without checking what `sign_in_user` actually resolved — a one-line mismatch check would make the duplicate-user condition alarmable at the moment it is detectable.

### Original Phase 1 scope (for reference)

> Findings detail: §2/P1, C1–C12, C49–C52, B2, B5, B6. Everything from the earlier
> draft still applies; the corrections below replace specific bullets.

- [ ] All the parametrization work from the earlier draft (stage `rootDomain` + `platformMailDomain` threaded through the 9 CDK files, the backend literals, the frontend/mobile/desktop config collapse).
- [ ] **`cdk.context.json` is ADDED TO, not purged** — 3 copies. Purging forces a fresh lookup and can fail the synth.
- [ ] **`paperclip_adapter_config._GATEWAY_URL_RE` is fail-CLOSED and raises** — it must accept the new host in the SAME commit that any new-host adapter is written, not after.
- [ ] **Paperclip sign-in by stored email covers 6 doors, not 1** — `paperclip_user_session.py:58-62` is one of six; the corrected site list is in C6. The backfill seeds from the Paperclip user row, NOT from Clerk (C7).
- [ ] **Add 9 do-not-rename markers** before anyone runs a cleanup grep — especially `BOOTSTRAP_EMAILS` (`paperclip_user_delete.py:49`), whose rename silently disarms the guard that stops an admin wipe from deleting the Paperclip bootstrap admin → fleet-wide provisioning outage (B6).
- [ ] **Drop the P1 verify grep as written** — it is unachievable and aiming at it causes the above (C9). Replace with: grep must reduce to the KEEP allowlist (see Done criteria).
- [ ] **DCR clients never re-register on a redirect-URI change** (`oauth_dance.py:152-156` compares scope only) — the "owner reconnects" remedy cannot work for 9 providers. One-line fix, lands in this PR (B3).
- [ ] Tests: **281 files / 1,821 lines** carry isol8 strings (2.5× the earlier estimate); only 599 are hostnames. ~33 files need NEW cases, not find/replace (C10, C41).

## Phase 2 — Dev on `flickmeon.ai` (prod untouched)

> **SES leg still needs a decision** (C2 — the SES stack is prod-gated, its jobs are
> dispatch-gated, and the identity is derived from the zone). NOTE the mail domain is now
> `thelightbulb.company`, NOT `flickmeon.ai`, so the dev domain flip and the mail move are
> INDEPENDENT — flipping dev to `flickmeon.ai` does not touch mail at all. Remaining
> question is only when dev agent-mail moves from `dev.isol8.co` to
> `dev.thelightbulb.company`: (a) leave it until P3, or (b) land the code change so the
> receipt rule carries both recipient sets. **(a) is now the cheap default** — dev agent
> mail on the old domain costs nothing while the product domain flips.

- [ ] Vercel: **team-level domain add + verification precedes any alias**, and the alias step runs INSIDE a deploy job (B4).
- [ ] `NEXT_PUBLIC_API_URL_DEV` — and note `_PROD` was never mentioned in the earlier draft (C52).
- [ ] **The wildcard cert is a cross-stack CFN Export — editing it in place deadlocks the whole stage** (B1). Create the new cert alongside, migrate consumers, then delete.
- [ ] **The WS authorizer's Clerk issuer is a CDK literal, not the secret** — miss it and chat dies while REST works (B8-adjacent, C3).
- [ ] Rehearse the full backfill on dev; `dev-clean-slate-reset` if wrong.
- [ ] E2E via Claude-in-Chrome on dev, including `admin.dev.flickmeon.ai`.

## Phase 3 — Prod flag day (+ text-only brand swap)

> Findings detail: §2/P3, C13–C28, B8, B11, B34, B35, B36.

Pre-stage, flip order, and teardown as in the earlier draft, WITH these corrections:

- [ ] **Add `apps/mobile/app.json` to the flip PR** — it appears nowhere in the earlier draft and holds every native identifier. Without it the flip ships flickmeon env inside a `co.isol8.mobile` binary with `applinks:isol8.co` (C14).
- [ ] **Add the three Apple IAP env vars** (`service-stack.ts:1370,1374,1378`) — the rail is LIVE in prod; `:1378` fails verification *silently* (C13).
- [ ] **`tauri.conf.json` has 10 brand-bound lines, not 4** (C15).
- [ ] **The brand swap reaches ZERO containers without a pin flip + reprovision** (B11, C23). Real shape: brand PR → `dev.tag` PR → validate → `prod.tag` PR → **reprovision** (not "fleet re-roll" — only the scale-from-zero branch re-runs `write_user_configs`, `ecs_manager.py:2057-2064`).
- [ ] **Remove two false self-heal claims** (C21, C22): the EFS credential helper does NOT re-materialize on boot, and Friday's `SOUL.md` does NOT re-render. Both become explicit work; SOUL.md needs a NEW heal migration modelled on `_migrate_legacy_user_md`.
- [ ] **Do NOT rename `_SELF_GROUNDING_HEADING`** naively — it is both writer and idempotence matcher; changing it double-appends a grounding section to every existing Friday, permanently (C20).
- [ ] **Clerk webhook edit belongs AFTER the deploy**, same as Stripe (C25); do not "remove old endpoints" — they were edited in place, not duplicated (C26).
- [ ] **Stripe Connect refresh/return URLs are CDK env, not dashboard work** (C28).
- [ ] **The Clerk-domain-change → deploy gap is a full prod auth outage whose length is the deploy's length** (B8). Pre-build and stage the deploy.
- [ ] **`www.isol8.co` gets re-added by any prod dispatch from a pre-flip ref** (B35) — land the flip PR before any later dispatch.
- [ ] **Support-mailbox retirement is not deploy-atomic**: 27 `support@` sites span the Vercel bundle, the mobile OTA, and `SKILL.md` (image roll only). Sequence: strings → deploy → OTA → image roll → *then* retire (B36).
- [ ] **Backfill scope is measured, not estimated** — 24 stores: 13 flag-day, 8 self-healing, 3 owner-action-only. Full spec in findings §4. Includes: our own tenant slug is **`isol8-2`** (so founder agent mail would become `friday@isol8-2.thelightbulb.company` with a retired verified sender — fix in the same backfill); a Paperclip company literally named **`Isol8`**; the marketplace **seller row `handle: isol8 / display_name: Isol8`** shown as publisher on 14 listings (a DDB row, one-line fix); and agent-written Aurora prose (56 issues, 118 comments, 38 work products, 9 routines, 20 companies). Leave `heartbeat_runs.result_json` alone — rewriting it falsifies the run ledger.
- [ ] **GooseTown teardown in the earlier draft is ~5% of the real estate**, and it includes a `goosetown-github-actions` role holding **AdministratorAccess** (C46).

## Phase 4 — Visual rebrand (after logo / brand manual)

- [ ] **The logo IS the numeral 8** (`globals.css:1525`, animated `landing.css:2077`, rendered live by `Logo.tsx:20-29`). A text-only swap leaves an orphan "8" beside "Lightbulb AI" — so P4 is not fully optional after P3.
- [ ] Asset inventory (nobody had one): `public/og-image.png`, `src/app/favicon.ico` (277KB — regenerate anyway), `src/app/icon.png`, `apps/desktop/src-tauri/icons/**` (50 files), `apps/mobile/assets/images/*`. Delete-don't-rebrand: `public/goosetown-preview.png` (1.1MB, shipped every deploy), `public/goosetown/*`, unreferenced `public/logos/*`.
- [ ] Three greppable `alt: "isol8"` strings missed by every sweep: `layout.tsx:67`, `blog/page.tsx:22`, `blog/[slug]/page.tsx:41`.
- [ ] Vendor-side visual: Clerk email templates + account portal, Stripe Checkout/portal branding, Meta app icons, App Store screenshots.

## Phase 5 — Legal-entity-gated (after the amendment is effective)

Unchanged from the earlier draft. Note the Apple Team ID (`WZX4U3C22Y`) is hardcoded in
6 places and its scope change is the same class of hazard as the Services ID.

## Phase 6 — Internal identifiers (decide each; cost table in findings §5)

24 cost-scoped families. Two corrections to the earlier table:
- [ ] **`pnpm --filter` on a renamed scope EXITS 0** (verified in-repo). A `@isol8/*` rename without the same-PR CI edits gives fully green CI that ran NOTHING across 12 steps — including the infra synth and the entire mobile suite. Add a guard test.
- [ ] **The GitHub org rename is a two-key operation and one key has no code home** — the deploy role's OIDC trust is console-only (B12, C32).

## Rollback

**Reversibility ends at P3 step 2, the Clerk domain change.** Before it: revert PR.
After it: roll-forward only (a revert means a second auth outage plus a second Apple
relay re-verification).

- Reversible: the per-env `rootDomain` pin, Vercel domains/aliases, all webhook URLs (edit back), CloudFront (disable ≠ delete).
- One-way / expensive: Clerk domain change; SES identity delete; ACM delete (ordering per C43); Apple bundle ids; Expo account rename; GitHub org rename; AWS root email; Workspace retirement.
- Preconditions before any destructive step: snapshot BOTH zones' `list-resource-record-sets` to a file; Aurora snapshot + note the DDB PITR timestamp immediately before the step-4 backfill; do NOT delete the `isol8.co` SES identity in the same window as the flip (tenant re-association must land first, C56).

## Freeze windows

1. **Merge freeze on `main`** across the P3 dispatch — any merge kills a pending dispatch as `completed/cancelled` with **0 prod jobs**, silently (already bit 1 of 6 dispatches). Dispatch into an EMPTY queue; verify by counting the 14 prod jobs, never by reading the conclusion.
2. **Agent freeze** — routines and the nightly CEO loop write fresh `isol8.co` URLs into Aurora and EFS *while* the backfill runs. Pause routines, or re-run the backfill after.
3. **Marketplace publish freeze** — artifacts are immutable the moment they land.

## Owner comms

22 owners / 4 paid. Send BEFORE the flip, from an address that survives. Per-owner
action set is computable from `integrations` / `oauth-tokens` / `channel-links` /
trigger rows. Must cover: re-arm inbound triggers (the fire URL is also the signature
audience), reconnect BYO-Google and DCR integrations, and **reinstall the desktop app**
— old installs die as a blank WKWebView with no reinstall affordance, not as an update
prompt.

## Monitoring during the flip

The 4 Route53 health checks and their `isol8-{env}-P13/P14` alarms go red BY DESIGN —
decide up front whether paging is armed and to which mailbox (the SNS subs may be
mid-move). Watch: `email.send result=ok` (the only signal platform mail survived — a
rejection is swallowed), Stripe/Clerk/Metronome webhook delivery panes, gateway pool
connect errors, `Isol8/provision.gate.blocked`. Touch nothing on the bedrock gate or meter.

## Done criteria + the KEEP list

Done = `git grep -i isol8` reduced to the KEEP allowlist, zero rows in the measured data
stores, zero live AWS resources outside the KEEP list, and a signed vendor checklist.

**KEEP — never rename (a naive cleanup pass WILL break these):** `isol8-connector` (IAM
role AND the customer-side CFN stack name, also baked into frozen marketplace artifacts);
the `-isol8.N` rollback tags in `openclaw-version.json:14` / `paperclip-version.json:12`;
`friday_identity.py:802,806` legacy byte-matchers; `paperclip_user_delete.py:49`
`BOOTSTRAP_EMAILS`; the `isol8:` uuid5 seeds (`catalog_service.py:349`,
`run_analytics.py:51`); `isol8.hire_suggestion`; the `Isol8*` CloudWatch namespaces and
the IAM role names the meter matches on (`bedrock-usage-meter/index.py:886,888,907`).

**Accepted residue (cannot fix, decided at P0):** customer Slack apps named "Isol8 Agent";
`isol8-connector` stacks in customer AWS accounts; `isol8.co` inside litestream SQLite
replicas and published marketplace artifacts.

## Verification matrix

| Phase | Proof |
|---|---|
| P0 | delegation live for `thelightbulb.company`; root email changed; SNS/Budgets re-confirmed; Aden's Identity Center moved |
| P1 | dev deploy green; smoke unchanged; grep reduced to the KEEP allowlist |
| P2 | full E2E on `dev.flickmeon.ai`; backfill rehearsal diff |
| P3 | 14 prod jobs ran; task-def env; prod smoke; `dig` clean; webhooks delivering; backfill row counts zero |
| P4/P5 | vendor dashboards verified; App Store review passes |
| P6 | per family; CI guard proves `--filter` still selects packages |


## Leftover local-dev defaults (harmless in deployed envs; sweep before isol8.co dies)

These are fallbacks used only when the env var is unset. CDK sets the real value in dev
and prod, and `desktop-build.yml` sets the desktop ones, so the deployed surface is
unaffected — but they must move before `isol8.co` is torn down or local dev silently
points at a dead host.

- `apps/backend/core/config.py:146` `WS_GATEWAY_URL` default, `:442` `FRONTEND_URL` default, `:67` comment
- `apps/desktop/src-tauri/src/lib.rs:31,205,534` `option_env!` fallbacks; `pair_state.rs:142` hardcoded `gateway_url`
- `apps/frontend/src/app/auth/desktop-callback/page.tsx:8` `NEXT_PUBLIC_API_URL` fallback
- Comments only: `packages/shared/src/api/url.ts:6`, `components/desktop/DesktopUpdater.tsx:15,101`
- `.github/workflows/mobile-ota.yml:110` — the guard asserting the DEV host is absent from the PROD bundle; correct as written today, must flip with prod.

## Surfaces CDK parametrization structurally cannot reach (verified 2026-08-27)

The flip is one line for anything threaded through the stage props. The real risk is
everything else. Found and fixed during the dev flip:
- **GitHub secrets** — `NEXT_PUBLIC_API_URL_DEV` feeds the dev Vercel build; invisible to CDK. (`_PROD` still to do at flag day.) Values are write-only via the API, so verify through the deployed bundle, not by reading the secret.
- **Non-generated workflows** — `frontend-ci.yml`, `e2e-dev.yml`, `desktop-build.yml` all hardcode hosts.
- **The Vercel alias steps inside `app.ts`** used the module-level `ROOT_DOMAIN` DEFAULT rather than the stage's value, so flipping the stage prop did not reach them. The 298 infra tests did NOT catch this — they synth with default props, so they assert the parametrized default, not the per-stage wiring. **Verify every flip by grepping the synthesized assembly (`cdk.out/assembly-{env}`), not by running the unit tests.**

## Appendix A — Live state (2026-08-26)

Route53 `isol8.co` (43 records): apex A→Vercel, MX→Google, SPF google+ses,
GSV TXT ×2, Aikido CNAME, `*.isol8.co` + `*.dev.isol8.co` MX→SES, `_dmarc`,
SES DKIM ×3, Clerk `clk`/`clk2` DKIM + `accounts`/`clerk`/`clkmail`, Resend DKIM
(leftover), `admin`/`dev`/`admin.dev`/`goosetown`/`dev.goosetown` A→Vercel,
`api`/`api-dev`/`ws`/`ws-dev`→API GW, `bv`/`bv-dev`→CloudFront, `send.isol8.co`
MAIL FROM, GooseTown ALB/APIGW/CloudFront records, `www` CNAME→Vercel.
ACM: `*.isol8.co` ×2 (in use), `assets.town.isol8.co`, `*.goosetown.isol8.co` ×2.
API GW domains: api, api-dev, ws, ws-dev (+2 goosetown). CloudFront: bv, bv-dev
(+2 goosetown). SES: identity `isol8.co`, 3 owner tenants. Vercel team `isol8`:
projects `frontend`, `goosetown`, `iso-app`; domain `isol8.co`. Clerk prod pk
decodes to `clerk.isol8.co`; dev = `up-moth-55.clerk.accounts.dev`.

## Appendix B — Inventory pointers

Counts (tracked files): 258 contain `isol8.co`, 290 `Isol8`, 792 `isol8`.
Tests: ~359 backend + ~180 frontend/mobile + ~60 infra hostname assertions.
Full file:line inventories live in the 2026-08-26 session transcript; the
workflow report (Appendix C, to be added) supersedes them.

## Appendix C — Clerk dashboard, verified in-browser 2026-08-26

Workspace "Personal workspace" (**Hobby** plan), app **Isol8**
`app_37u3y3RLngWkiUpnGBAtYqMuDKX`, prod instance `ins_38duYVCNiumxSyegmw2BVyBnEjm`,
domain `dmn_38duYTyIsZWufQzkZq90O8y6G1Y`.

- **Primary domain `isol8.co`, Verified**, SSL issued for Frontend API + Account
  portal. Satellites are a **Pro** feature (irrelevant — hard cutover).
- **"Change domain" EXISTS on Hobby**, under Developers → Domains → *Danger zone*,
  warning: "Changing the domain will result in downtime."
- **The 5 CNAMEs to recreate on `flickmeon.ai`** (re-read them from the dashboard
  after the change — the `krbk05duqpl1` instance label may be re-issued):
  `clerk`→`frontend-api.clerk.services`, `accounts`→`accounts.clerk.services`,
  `clkmail`→`mail.krbk05duqpl1.clerk.services`,
  `clk._domainkey`→`dkim1.krbk05duqpl1.clerk.services`,
  `clk2._domainkey`→`dkim2.krbk05duqpl1.clerk.services`.
- **Webhooks: exactly ONE endpoint**, `https://api.isol8.co/api/v1/webhooks/clerk`,
  0.0% error rate, described as "Isol8 prod backend. Drives welcome email,
  user/org record-keeping, and Stripe customer email sync."
- **SSO: Apple + Google enabled for sign-in, Hugging Face disabled.** Both use
  **custom credentials** (Clerk requires them on production instances) — so both
  vendor consoles need edits:
  - **Apple** — Services ID **`co.isol8.signin`** (a THIRD Apple identifier, in no
    repo file), Team `WZX4U3C22Y`, Key ID `4956TDC55J`, and **Email Source for
    Apple Private Email Relay = `bounces+59218596@clkmail.isol8.co`**. That relay
    source is registered and verified in Apple's developer portal against
    `clkmail.isol8.co`; when the Clerk domain moves it must be re-registered and
    re-verified, or Sign in with Apple's private-relay addresses stop delivering.
  - **Google** — client `1011290189072-7hro9nd8aj1utkoqmb5hs1odmv89hrps.apps.googleusercontent.com`,
    **Authorized Redirect URI `https://clerk.isol8.co/v1/oauth_callback`**, scopes
    openid + userinfo.email + userinfo.profile. Clerk warns these credentials are
    **shared with any existing Google EASIE connections**, so editing them changes
    that config too.
- Clerk's own transactional email sends from the primary domain, so verification /
  magic-link mail moves to `flickmeon.ai` automatically with the domain change.

**Vendor access note:** the Stripe dashboard is blocked for browser automation by
the permission classifier (financial site). Stripe steps are either done by a human
or explicitly allow-listed first; the webhook-URL-edit fact above was confirmed from
Stripe's API reference instead.

## Appendix D — Vendor endpoints, read LIVE 2026-08-27 (not inferred from code)

### Stripe — 4 endpoints, not the 3 the code implied
Read with the prod key from Secrets Manager `isol8/prod/stripe_secret_key`
(`GET /v1/webhook_endpoints`). **Edit `url` IN PLACE after the prod deploy** — Stripe's
update endpoint permits editing `url` and does NOT regenerate the signing secret, so all
three `isol8/prod/stripe_*_webhook_secret` values stay valid. Adding new endpoints instead
would 400 every billing event, because `routers/billing.py:1139` verifies against a SINGLE
`STRIPE_WEBHOOK_SECRET`.

| id | current url | what |
|---|---|---|
| `we_1TDZ22I54BysGS3r5mHVJ8Z9` | `/api/v1/billing/webhooks/stripe` | billing, 8 events |
| `we_1U471RI54BysGS3rthcgsgNA` | `/api/v1/marketplace/webhooks/stripe-marketplace` | marketplace platform, 3 events |
| `we_1U471TI54BysGS3rNAY5x1vH` | same path | marketplace **Connect**, `account.updated` |
| `we_1U6OwYI54BysGS3rTwq0MzRS` | `/api/v1/hooks/t/org_3H3viGAfq2SXc9dIdcatmGXg8Td/ab0ddd6d91aa50c27b2285ae` | **an OWNER'S AGENT TRIGGER** — "Fires Vera's Revenue recovery routine on payment failures" |

⚠️ The 4th was invisible to every code grep — it is an inbound-trigger URL a customer pasted
into Stripe, on the founder org. The fire URL is ALSO the signature audience
(`paperclip_trigger_proxy.py:422`), so it must be **re-armed**, not just edited, or signature
verification fails. Assume other owners have equivalents in GitHub/Meta/PostHog: enumerate
from the trigger rows before the flip, do not rely on grep.

### Metronome — domain-bound after all
Outbound is to `api.metronome.com` (no change), but Metronome **holds a webhook pointing at
us**: `POST {PUBLIC_API_BASE}/api/v1/webhooks/metronome`, signed with
`isol8/prod/metronome_webhook_secret`. It is load-bearing — it sets the local fast-gate flag
that gates paid chat and fires the Bedrock kill-switch. If it stops delivering, the flag
holds its last value (the meter kill-switch is the backstop). Update the URL in the
Metronome dashboard at flag day; an API key exists at `isol8/prod/metronome_api_key`.

### Bedrock — NO domain dependency at all
Verified live: containers reach the gate at a Lambda **Function URL**
(`https://mf462bo2u67funisxc6qy6tetu0rsqiw.lambda-url.us-east-1.on.aws/` prod,
`uq7hitwxidhzf5zbcwdaguj4bu0eewor` dev), discovered via SSM `/isol8/{env}/bedrock-gate-url`.
AWS-owned hostnames. Nothing about Bedrock, the gate, or the meter changes with the rebrand.

### Full inbound-webhook surface (URLs third parties hold)
`/api/v1/webhooks/{stripe,stripe-marketplace,apple-iap,metronome,clerk,ses-inbound,ses-events,paperclip-events}`,
`/api/v1/hooks/meta`, and `/api/v1/hooks/t/{owner}/{public_id}` (per-owner triggers).

## Appendix E — late finds (2026-08-27, during execution)

- **`isol8.ai` is an INSTAGRAM HANDLE, not a domain.** It appears as `username: "isol8.ai"` in
  `apps/backend/tests/unit/services/credentials/test_instagram_token_script.py` (the stored IG
  credential shape). We do NOT own `isol8.ai` as a domain (no DNS). So: rename the Instagram
  account, and treat the social handles as their own vendor row (Instagram, LinkedIn
  `/company/isol8ai/`, X if any). Not on any earlier checklist.
- `docs/integrations/apollo-prospect-source.schema.json` `$id: https://isol8.ai/schemas/…` —
  a JSON-Schema identifier, never dereferenced, but it names a domain we do not own. Cosmetic;
  fix for consistency. JSON, so a markdown grep never surfaces it.
- **Root `AGENTS.md` is a STALE FORK of CLAUDE.md** carrying facts that are false today: a dead
  `VPC Link V1 → NLB` diagram, `api-staging.isol8.co` (an env that never existed), and
  `~/Desktop/agent` as the writable fork. It was deliberately NOT rebranded — a find-replace
  would mint `api-staging.flickmeon.ai`, i.e. a brand-new false fact. **Decision needed: delete
  it, or rewrite it from CLAUDE.md.** Sweeping it is the wrong answer.
- Two stale CLAUDE.md claims corrected in passing: the Apple IAP rail is LIVE (CDK sets
  bundle/product/app-apple-id unconditionally in both envs), and the Paperclip pin is
  `v2026.813-isol8.6`, not `.3`.
- `.claude/skills/**` is **gitignored**, so it cannot ride a branch — those edits necessarily
  land in the working checkout and are not part of any PR.

## Appendix F — the adapter-URL backfill, PROVEN on dev 2026-08-27

Executed after the dev flip; 37/37 rows rewritten, 0 left on the old host, every
`?token=` JWT preserved. Use the same shape on prod (cluster `isol8-prod-paperclip-db`,
secret `isol8-prod-paperclip-db-credentials`, database `paperclip`).

**Why it is needed:** the flip DELETES the old API Gateway custom domains and their
Route53 records (verified: `api-dev`, `ws-dev`, `bv-dev` on `isol8.co` all gone within
minutes). Every Paperclip agent's `adapter_config.url` still points at the dead WS host,
so no agent can connect until this runs. `adapter_config` is **jsonb**, and the URL
carries a `?token=` JWT that MUST survive — so this is a host-only rewrite, never a
whole-value replace.

```sql
-- dry run first
SELECT COUNT(*) FROM agents WHERE adapter_config->>'url' LIKE 'wss://ws.isol8.co%';

UPDATE agents
   SET adapter_config = jsonb_set(
         adapter_config, '{url}',
         to_jsonb(replace(adapter_config->>'url','wss://ws.isol8.co','wss://ws.flickmeon.ai'))
       )
 WHERE adapter_config->>'url' LIKE 'wss://ws.isol8.co%';

-- verify: new = total, old_left = 0, and the token survived
SELECT COUNT(*) FILTER (WHERE adapter_config->>'url' LIKE 'wss://ws.flickmeon.ai%') AS new,
       COUNT(*) FILTER (WHERE adapter_config->>'url' LIKE '%isol8.co%')             AS old_left,
       COUNT(*) FILTER (WHERE adapter_config->>'url' LIKE 'wss://ws.flickmeon.ai?token=%') AS token_ok,
       COUNT(*) FILTER (WHERE adapter_config->>'url' IS NOT NULL)                   AS total
  FROM agents;
```

⚠️ **The `WHERE` clause is the anchor.** `replace()` alone is unanchored; the WHERE is what
makes this a prefix-only rewrite. Do not drop it.

**Ordering on prod:** deploy → ECS rollover COMPLETED → run this → verify → only then treat
the fleet as live. `validate_gateway_url` is fail-CLOSED but has exactly ONE caller (the
write path in `synthesize_openclaw_adapter`), so existing rows are not re-validated on read
— which is why stale rows fail by *not connecting* rather than by raising.

## ✅ Appendix G — PROD IS NOT BLOCKED ON CLERK (Appendix G v1 RETRACTED, 2026-08-27)

**This appendix previously said prod was blocked. That was wrong, and the error was mine
twice over.** First I claimed the new origins could simply be allow-listed (no source, and
false). I corrected that to "satellite domains are required, and satellites are paid-only in
production" — which is a true sentence about satellites, but **satellites are not the
mechanism this cutover needs**, so recording their price as our blocker was a second error.

**Satellite domains exist to serve ONE app across SEVERAL domains simultaneously** — the
primary holds the session and satellites read it cross-domain. We are doing the opposite: a
**hard cutover** where `isol8.co` is retired and only one domain is ever served. For that you
**change the instance's primary domain**, which is a first-class documented operation with
**no plan gate on its page**. Clerk's pricing lists *"Custom domain"* under **Hobby** and
*"Satellite domains"* under Pro — different features, different gates.

Source: `clerk.com/docs/guides/development/deployment/changing-domains`.

**Live state of the prod instance (read via the Clerk API, 2026-08-27):**

```
environment_type : production
domains          : isol8.co   (primary, no satellites)
frontend_api     : https://clerk.isol8.co
allowed_origins  : (none set)
```

One domain, no satellites — exactly the shape `change_domain` is for.

### How the flip is actually performed

Any one of:

- Dashboard → **Domains** → **Change domain**
- `POST https://api.clerk.com/v1/instance/change_domain` with `{"home_url": "https://flickmeon.ai"}`
- `npx clerk@latest api instance/change_domain --instance prod -d '{"home_url":"https://flickmeon.ai"}'`

Production instances only. A **dev** instance's domain cannot be changed — which is exactly
why dev flipped without touching Clerk at all (dev is `*.clerk.accounts.dev`, not domain-bound).

### Documented consequences — sequence around these

| consequence | note |
|---|---|
| **A new `pk_live_` is auto-generated** | Clerk: *"Failing to update your Publishable Key will result in Clerk failing to load."* The interval between the domain flip and the frontend redeploy carrying the new key **is a broken-app window.** Have the redeploy ready to fire. |
| New DNS records in the `flickmeon.ai` zone | **The record set is rendered per-instance in the Dashboard and is NOT documented.** Do not pre-write records from a guessed list — read them off Domains after the change. (Appendix G v1's "5 CNAMEs" was a guess; treat it as unverified.) |
| New SSL certificates | Clerk issues them; CAA records blocking LetsEncrypt / Google Trust Services stall issuance. |
| Social-connection redirect URIs | Google + Sign-in-with-Apple must be repointed at the new Clerk host. |
| JWT issuer / JWKS | Only if external services consume them — `isol8/prod/clerk_issuer` is ours and must be updated. |
| Account Portal moves | to `accounts.flickmeon.ai`; any hardcoded `accounts.isol8.co` link breaks. |
| Downtime | Clerk warns a domain change *"can result in downtime… depends on your DNS provider"*, up to 48h propagation. |

**Two things the docs do not state, and I will not assert:** whether existing sessions
survive, and whether the **Secret Key** rotates. Both are silences, not assurances. Sessions
are moot — the owner has accepted a fleet-wide sign-out. The Secret Key should be re-read from
the Dashboard after the change rather than assumed stable.

### Still an owner-gated action, for a different reason

Not blocked, but **not unattended either.** It re-keys production auth for 22 owners (4
paying) and has a self-inflicted broken-app window between the flip and the redeploy. The
prerequisite work (CDK prod stacks, Vercel aliases, Stripe/Metronome webhook URLs, the
Paperclip adapter backfill) is all reversible and can land first; the Clerk flip is the flag
-day step to run with the owner awake.

**Cheap pre-flight, read-only:** open Dashboard → Domains and confirm the **Change domain**
control is enabled rather than wearing a `Pro` badge. Free-plan availability is established
here by the *absence* of a documented gate plus the pricing page, not by an affirmative
sentence — so look before flag day.

---

## Appendix H — the Clerk flag day, as a runbook (mapped 2026-08-27)

Every consumer of the prod Clerk identity, found by grep + read live from AWS. This is the
list that has to move together, because **the publishable key encodes the frontend-API host**:

```
pk_live_Y2xlcmsuaXNvbDguY28k   ->  base64 -d  ->  clerk.isol8.co$
```

That is *why* Clerk mints a new key on a domain change — the old key names the old host and
cannot work against the new one. Expect `pk_live_Y2xlcmsuZmxpY2ttZW9uLmFpJA`
(`clerk.flickmeon.ai$`, base64 with padding stripped as Clerk does), but **read the real value
off the Dashboard — never ship a predicted key.**

### Consumers

| # | what | where | current value |
|---|---|---|---|
| 1 | `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY_PROD` | **GitHub Actions secret**, consumed by `deploy.yml:31` | (secret) |
| 2 | `EXPO_PUBLIC_CLERK_PUBLISHABLE_KEY` | **hardcoded in the repo** — `apps/mobile/eas.json:37` | `pk_live_Y2xlcmsuaXNvbDguY28k` |
| 3 | `EXPO_PUBLIC_CLERK_PUBLISHABLE_KEY` | **hardcoded in the repo** — `apps/mobile/package.json:9` (`start:prod`) | same |
| 4 | `isol8/prod/clerk_issuer` | Secrets Manager (read live) | `https://clerk.isol8.co` |
| 5 | `CLERK_ISSUER` → backend | `service-stack.ts:1562`, from secret #4 | — |
| 6 | `CLERK_ISSUER` → WS Lambda authorizer | `api-stack.ts:357` | — |
| 7 | `isol8/prod/clerk_secret_key` | Secrets Manager | docs are **silent** on whether this rotates — re-read after the change, do not assume |
| 8 | `isol8/prod/clerk_webhook_secret` | Secrets Manager | docs do not tie webhook secrets to a domain change; verify |

**#2 and #3 are the trap:** they are literals committed to the repo, not secrets. A flag day
that only rotates GitHub/AWS secrets ships a mobile binary still pointed at `clerk.isol8.co`.

### Order (the broken-app window is self-inflicted; keep it short)

1. Land + prod-deploy everything domain-related **except** Clerk. All reversible.
2. Clerk Dashboard → Domains → **Change domain** → `flickmeon.ai`.
3. Immediately read: the new `pk_live_`, the DNS records Clerk renders (**per-instance, not
   documented — read them, don't guess**), and re-read the Secret Key.
4. Add those DNS records to the `flickmeon.ai` Route53 zone; wait for Clerk to mark the domain
   verified and certificates issued.
5. Update #1 (GitHub secret) and #4 (Secrets Manager), commit #2 and #3, then redeploy frontend
   + backend. **Until this step lands, prod auth is down** — Clerk fails to load with a stale key.
6. Repoint Google + Sign-in-with-Apple redirect URIs at the new Clerk host.
7. Verify: sign-in on `flickmeon.ai`, a WS chat message (exercises the Lambda authorizer via
   #6), and one Paperclip/Teams call (exercises the backend via #5).

Every prod user is signed out at step 2. The owner has accepted that.

---

## Appendix I — `support@isol8.co` is uncovered, and it is a live mailbox (2026-08-27)

Swept `apps/frontend/src` + `packages/shared/src` for the old apex. After the flip work, the
non-comment, non-test hits fall into exactly two buckets — and **both are mail, not product
URL**, so neither the domain flip nor the brand PR touches them:

| bucket | where | count |
|---|---|---|
| `support@isol8.co` in user-facing copy | `settings/page.tsx`, `support/page.tsx`, `ProvisioningStepper.tsx` (7×), `FirstRunSteps.tsx`, `SupportTicketDialog.tsx` | ~14 |
| agent-mail examples | `landing/WorkspaceDemo.tsx`, `landing/demoData.ts` (`friday@northwindceramics.isol8.co`) | 2 |

**Verified not covered:** `#2069` (brand) changes 110 files and **zero** lines mentioning
`support@`. Nothing else open touches them either. Backend has the same string in
`routers/users.py` and `EMAIL_SUPPORT_ADDRESS`.

### Why this is a sequencing hazard, not just a rename

`support@isol8.co` is a **mailbox people actually write to**, and it is the address we hand a
user at exactly the moments things are already going wrong — a failed account deletion, a stuck
provision, a support dialog that could not submit. Two ways to get this wrong:

- **Switch too early** — the copy points at `support@thelightbulb.company` before that mailbox
  exists in Workspace, and support mail silently bounces.
- **Switch too late** — `isol8.co` is retired with the copy still pointing at it, and support
  mail dies with the domain.

So the ordering is fixed: **Aden creates `support@thelightbulb.company` in Workspace → verify it
receives → change the copy → only then retire `isol8.co` mail.** The MX/SPF/DKIM for
`thelightbulb.company` are already live (verified from public resolvers), so the mailbox is the
only remaining prerequisite.

**Not doing this now, deliberately.** It depends on a mailbox I cannot create or verify from
here. It is listed so it is not mistaken for finished when the product domain is done.
