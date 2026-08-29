# Owner data plane — accounts, data, surfaces

Status: design, approved in conversation 2026-08-28 (owner). Disposable per
CLAUDE.md — delete when the last phase ships.

## 1. Goal

Give every Lightbulb company the business surface result.dev sells (money,
growth, support, brain, company) and the real-world footing usenaive.ai sells
(identity, accounts, phone, cards) — **without becoming the system of record
for the owner's business.** Positioning stays "you own everything, 100% of
your revenue" (see `docs/growth/2026-08-17-blitzscaling-strategy.md` §4.0;
revenue share and umbrella accounts are REJECTED there).

Competitor mechanics this is measured against (verified 2026-08-28):

- **Result** = Next.js on Vercel, per-business Supabase-shaped Postgres, Vercel
  wildcard `*.withresult.com`, Paddle merchant-of-record (or connected Stripe),
  passthrough OpenAI/Google, in-house analytics script + support widget, MCP
  via Descope (69 tools / 6 groups). Unevidenced on their site: A/B Testing,
  Compliance, Contracts, own-domain mail, AI-drafted support replies, rollback.
- **Naive** = own governor/approvals/kits/vault/event-router/brain over resold
  vendors (OpenRouter, fal.ai, DataForSEO, Apify, Surge, Resend, Vercel +
  Supabase, Mobilerun, Coinbase CDP, Stripe-shaped prepaid cards, doola).
  Durable runtime "not placed on production yet"; Apify account suspended.

## 2. The one rule

**A page never renders from data only Lightbulb holds about the owner's
business.** Truth lives in accounts the owner owns (their Stripe, their
PostHog, their provider accounts). We hold pointers, caches, and the records
we originate (support threads, brain, agent work). If we ever find ourselves
the sole holder of a business number, push it back to an owner-owned
provider. This is the structural difference from Polsia (umbrella
Render/GitHub/Neon/Postmark accounts; 1.8/5 Trustpilot, ~50% month-one churn)
and it is what makes every competitor page cheap: a page is a view, never a
copy.

Pattern name used throughout: **owner-owned, agent-operated.**

## 3. Accounts layer

### 3.1 The Stripe spine

One Standard-style connected account per company on the **Connect platform we
already run** (`core/services/payout_service.py`, Express accounts for
marketplace payouts; Connect webhook secret + `account.updated` handler in
`routers/marketplace_purchases.py`). Two doors, one code path:

- **Create**: `POST /v1/accounts` with Standard-style controller properties
  (`fees.payer=account`, `losses=stripe`, `dashboard=full`), prefilled from
  the company name and the owner's Clerk email; hosted onboarding via Account
  Links (`type=account_onboarding`; single-use, minutes TTL, minted on click,
  never stored or emailed).
- **Connect existing**: Standard OAuth for an owner who already runs Stripe.
  (An account already controlled by another platform gets a fresh account —
  Stripe's 2021 rule.)

Both yield a Standard account the owner logs into and keeps; **zero Connect
fees, no application fee**; we read with the platform key + `Stripe-Account`
and receive Connect webhooks. Revoking the platform link leaves a normal
Stripe account.

The spine is three things at once: the owner's payments ledger; the identity
Stripe Projects hangs providers off (`Stripe-Context: acct_…`); the billing
profile with per-provider spend limits. Projects requires **verified email,
date of birth and phone** on the account — onboarding must collect more than
`contact_email`.

**When it appears: a Friday-raised approval card, never a gate before chat.**
Friday raises it the first time a task needs money or infrastructure (or in
the first-run brief on paid plans). The card renders in Inbox, pushes to
mobile/desktop, and opens in whichever surface the owner is in.

### 3.2 Provisioning door (Stripe Projects)

Behind an interface with two implementations:

1. **Projects for Platforms API** (private preview; request access at
   projectsforplatforms@stripe.com — owner action, send in P0). Backend calls
   with `Stripe-Context: <acct>`: catalog → account request (`complete` |
   `needs_information` → approval card) → provision → poll → reveal credentials
   → store → inject. Provider accounts are created on the owner's identity;
   deep links open the provider's own dashboard.
2. **Fallback until access**: Stripe CLI in the container, paired once by the
   owner via `stripe login --non-interactive` (prints `browser_url` +
   `verification_code`; owner confirms in any surface; Friday runs
   `next_step`). Session refreshes itself; `stripe projects add …` with
   `--accept-tos` after the owner's one-time consent; vault at
   `.projects/vault` on the owner's EFS.

Verified 2026-08-28: catalog answers with our dev platform key; `posthog/
analytics` is free tier, config `region` + `organization_name`; providers
include Cloudflare, Vercel, Supabase, Neon, Clerk, PostHog, Twilio, Resend,
AgentMail, Squarespace/Spaceship/WordPress.com (domains), Sentry, E2B. PostHog
hands over only the public write token (`NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN`)
— reads need the PostHog OAuth token (§4.2).

### 3.3 Rails Projects does not reach

Same shape — vendor's own "make the human the owner" primitive is the one
human step, rendered as an approval card:

| Rail | Vendor | Owner step | Notes |
|---|---|---|---|
| Social publishing | **Ayrshare Business** (one Lightbulb account, per-owner profiles, hosted OAuth linking page; one Lightbulb X app) | links their networks | ~$12/owner/mo @100, ~$4 @1000 — fence as Growth add-on |
| Formation + EIN + RA | **doola** API (what Naive resells at $349), owner as member | KYC + pays checkout | verify organizer semantics + compliance/bookkeeping API with doola sales |
| KYC | **Stripe Identity** ($1.50; `related_person` reuses it for Connect) | ID + selfie | |
| Phone/SMS | **Surge** (sole-prop 10DLC pre-EIN, standard brand after) | none | do not sell "receives any OTP" — VoIP numbers are unreliable for Google/bank OTPs |
| Cards | owner's **Mercury/Ramp/Brex OAuth** with interval caps first; Stripe Issuing + Connect Custom only if per-authorization approve/decline is needed | OAuth | |
| Domains | **Route 53 Domains** (owner is registrant, we pay $16/yr and rebill, zone in our account, transfer-out after 60 days) OR Projects registrars — decide in P4 | approves registrant block + ICANN 15-day email | |
| Mail on own domain | SES identity per owner domain on the existing per-owner tenant (`agent_mail_tenant.py`) | none | DKIM/MAIL FROM/DMARC auto-published, zone is ours |
| Hosted fallback pages | CloudFront SaaS Manager tenants (`<slug>.flickmeon.ai`, `support.<slug>.flickmeon.ai`) | none | |
| SEO/AEO data | DataForSEO (platform key) | none | P5 |
| Image/video | fal.ai (platform key) | none | P3 |
| Enrichment | existing Apollo/Attio/Parallel rails | OAuth | |

Not adopted: merchant-of-record (Paddle/Polar/Lemon Squeezy — 4–5% or no
owner account), BaaS backends, model routers, trading/x402/USDC wallets,
cloud phones, browser-typed signups (ToS-prohibited at Cloudflare, Supabase,
Shopify, X, LinkedIn, TikTok, Meta, Buffer; phone/ID walls from datacenter
IPs).

### 3.4 Company-level vs agent-level resources

Company-level: Stripe account, domain, PostHog project, Ayrshare profile,
formation, cards. Agent-level: mailbox (`{agent}@{slug}.…`, existing), Google
account binding (existing `google_accounts`), phone number (optional). State
this per resource in the Accounts page.

### 3.5 Credential injection

Three patterns exist in production; pick per credential class:

- **Proxy** (`apps/infra/openclaw/plugins/aws-exec-profile` — agent never
  sees the key): use for the connected-account Stripe key and anything with
  money authority.
- **Per-run injection** (`paperclip-exec-creds`): use for short-lived tokens.
- **Static env** (Projects `.env`, MCP OAuth store): acceptable for provider
  write tokens (PostHog project token, Vercel deploy token) scoped to the
  owner's own resources.

Backend copies of provider credentials live in Secrets Manager at
`isol8/{env}/integrations/{owner}/{provider}` (existing store,
`credentials/integration_secret_store.py`), never DynamoDB.

### 3.6 Gates, org semantics, exit, limits

- **Paid-only.** The accounts layer sits behind `require_always_on_entitlement`
  like automations. Trial owners see the card as the upgrade trigger.
- **Orgs.** The org admin who completes Stripe onboarding is the
  representative and the sole money-approver (mirrors
  `oauth_connector_user_id` for ChatGPT OAuth).
- **Company name first.** The name-first invariant now also feeds the Stripe
  account name, PostHog org name and Ayrshare profile. Onboarding brief forces
  a real name before the Stripe card.
- **Exit / wipe.** `account_wipe`, `entitlement_teardown`,
  `owner_secret_sweep` revoke the Connect link and delete *our* credential
  copies. They **never** call Projects `remove` or touch owner-owned provider
  resources.
- **Marketplace export** (`agent_export.py`) must exclude `.projects/`,
  Stripe CLI config and provider credentials before Projects ships. A hired
  agent arrives with no accounts; its "accounts to connect" to-do reuses the
  paused-trigger to-do Automations already produces.
- **Countries.** Connect Standard and Projects paid tier have country lists
  (US in both). Outside them: connect-existing + read-through only.
- **AWS connector role** (`isol8-connector` in the customer's account) is the
  existing precedent for owner-owned, agent-operated; Projects generalises it.

## 4. Data layer

### 4.1 Provider-native truth

- **Money**: owner's Stripe. Daily revenue = `balance_transactions` by day;
  customers, subscriptions, products, payouts = list calls; Connect webhooks
  (`customer.*`, `payment_intent.*`, `invoice.*`, `customer.subscription.*`,
  `payout.*`) on the existing endpoint push changes.
- **Traffic / funnel / attribution**: owner's PostHog. Their site carries the
  Projects-provisioned write token; we query HogQL with the retained OAuth
  token. PostHog's warehouse syncs their Stripe natively and joins to persons,
  so attribution is a HogQL query in *their* PostHog — we build no join.
- **Social**: Ayrshare per-post/per-profile analytics.
- **Provider resources**: Projects API list.

### 4.2 Retained PostHog token

The `posthog` provider is remote-OAuth MCP with no backend copy
(`materializers/mcp_oauth.py` writes only the container's OAuth store).
Change: the materializer also retains the token in Secrets Manager for
`posthog` so the backend can query. Scopes already cover reads.

### 4.3 What we hold

- **Cache**: DynamoDB owner-day rollup, `usage-counters` shape
  (`owner#day` → revenue, visitors, signups, followers, open threads, agent
  spend). Refreshed by webhook (Stripe) and by a scheduled HogQL pull
  (PostHog) run from the update-service scheduled worker — the backend is
  `desiredCount: 1` with no cron.
- **Truth we originate**: support threads (extend `agent-mail-inbox` with a
  `support` kind and per-thread reply address), brain index (wiki on EFS),
  agent work (Paperclip issues, runs, work products).
- **Pointers**: `stripe_accounts` fields on `billing-accounts`;
  `provider_resources` on the `integrations` row.
- **No Aurora records store.** Considered and rejected (ponytail): every
  Result page renders from provider-native truth + cache; no page needs a
  join PostHog or Stripe cannot serve. Paperclip's Aurora is one shared DB
  scoped by `company_id`, Drizzle-owned — not ours to extend.

### 4.4 Work products are the join

Paperclip work products (`routers/teams/issues.py` §work-products: typed
deliverables with `url`, `reviewState` `needs_board_review → approved |
changes_requested`, company-wide gallery) are:

1. **The ledger of what the company made** (Result's Library). Keep distinct
   from Brain (what the company knows).
2. **The receipt for every account-layer action**: when Friday creates a
   Stripe product, publishes a post, provisions a resource, registers a
   domain, files with doola, or replies to support, the rail registers a work
   product pointing at the provider object. Pointer, never a copy. This is the
   audit trail for the policy layer; `needs_board_review` is its approval
   gate.
3. **The attribution key**: a published post's work-product id is its
   `utm_campaign`; PostHog first-touch UTM + `metadata.user_id` on the Stripe
   customer close the loop. No new id scheme.

Gap to close: provider actions over MCP/CLI do not auto-register — add a
register-on-create convention per rail plus a line in the `lightbulb` skill.

### 4.5 Policy and ledger

Naive's real moat, and the prerequisite for money. Reuse, don't invent:
`agent_budget` (per-agent caps), exec approvals, Paperclip approvals, the
email send-policy table, Sage, the hook token, and work products as the audit
trail. Add: a per-capability policy row on the owner (`requiresApproval` per
rail: money moves, outreach, publishing, provisioning), an approvals inbox
that shows the rationale, and one **revoke-all** that disconnects Connect and
deletes our credential copies. Money approvals are org-admin-only.

### 4.6 Inference spend is money

`agent_budget`, `spend_service`, `usage_service`, `trial_cap`, Metronome
already meter what the company spends on agents. Money → Spend shows earned
and spent together; Projects per-provider limits and Ayrshare pass-through
appear there.

### 4.7 Redaction and the two PostHogs

ISOA-189: agent prose is not redacted today. Support transcripts carry
customer PII and env carries provider keys — widen `agent_redaction`/`curator`
to support threads and brain writes before P2. Our own PostHog
(`posthog_admin.py`, project 380894, admin growth) is never read for owner
pages; Numbers reads only the owner's.

## 5. Surfaces

### 5.1 Target IA (29)

**Home** · **Chat** · **Inbox** (approvals, needs-board-review, Friday-raised
cards; Activity as a tab) · **Team** (roster, org chart, budgets, Hire,
Publish agent)

**Money**: Payments · Products · Customers · Spend
**Growth**: Content · Publish · SEO & AEO · Ads · Leads · Blog
**Operations**: Support · Tasks (Goals/Projects as views) · Automations · A/B
Testing
**Knowledge**: Brain (Dreams folds in) · Library (artifacts)
**Company**: Identity · Domain & Site (Result's Website page) · Accounts ·
Compliance · Contracts
**Settings**: Channels · Integrations · Skills & Proposals · Model & Credits ·
Sessions · Members & Billing

All 21 Result tabs are present by name or one-to-one. Additions Result
lacks: Inbox, Team, Spend, Accounts, Library-as-separate, Goals/Projects/
Activity (demoted). Deliberately one company per owner (Result sells 3–10).

### 5.2 Mechanics

The dockable workspace stays. The sidebar is a left nav in the shell; every
new page is a panel key registered in
`apps/frontend/src/components/workspace/panelRegistry.ts` (the sole source
of panel identity), routed through the existing Teams/Control panel routers
or a new `business` panel kind. Existing Control tabs move under Settings.
Mobile mirrors the tree as its control tree does today. Marketplace stays a
public route and appears in-app as Team → Hire / Publish agent; company
packages are Home's empty-state path.

Design language to adopt from Result: mono uppercase micro-labels, hairline
borders, one accent per meaning, verb-chip empty states, status steppers for
async back-office jobs, one real record per card — on our cream/green brand.

### 5.3 Where each page's data comes from

| Page | Source | New/Have |
|---|---|---|
| Home | rollup | new |
| Payments / Products / Customers | Connect reads + webhooks | new |
| Spend | agent_budget / usage / Projects spend | reshape Usage |
| Content | LLM + fal.ai rail; feedback votes = learning loop | new |
| Publish | Ayrshare + Paperclip routines | new |
| SEO & AEO | DataForSEO + Google rail | new (P5) |
| Ads | `meta_ads` rail (+ Google Ads) | partial |
| Leads | Attio/Apollo rails | new page |
| Blog | owner hosting via Projects; PostHog UTM | new (P3) |
| Support | agent mail (`agent_mail_autoreply` drafts) + widget + hosted page | new |
| Tasks / Automations | Paperclip | have |
| A/B Testing | owner's PostHog experiments | new (P5) |
| Brain | memory-wiki vault | new UI |
| Library | work products | have |
| Identity | Stripe Identity + doola | new (P4) |
| Domain & Site | Projects (Vercel/Supabase/Neon) + harness deploy + Route 53/SES | new (P4) |
| Accounts | Projects list + `integrations` + Connect status; deep links; revoke | new |
| Compliance / Contracts | doola compliance API; Brain drafts + e-sign rail | new (P5) |

## 6. Phases

- **P0 Foundations** — Stripe card + Connect create/connect; Connect webhook
  coverage; retained PostHog token; rollup + scheduled writer; credential
  proxy for the Stripe key; paid gate; wipe/export rules; work-product
  register-on-create; Lightbulb MCP; preview request. Screens: Accounts,
  Inbox reshaped.
- **P1 Money + Home** — Payments, Products, Customers, Spend, Home.
- **P2 Knowledge + Support** — Brain UI; Support widget + hosted page +
  inbox; Library feed; redaction widening.
- **P3 Growth** — Publish (Ayrshare), Content grid (fal.ai), Leads, Blog;
  Growth add-on pricing fence.
- **P4 Company** — Domain & Site, Identity (doola + Stripe Identity), Ads
  (Google Ads rail), mail on own domain.
- **P5 Long tail** — SEO & AEO, A/B Testing, Compliance, Contracts.
- **Cross-cutting** — IA migration + panel registry, mobile parity.

Each phase ships only on rails that exist by then. Vendor waits: Projects
preview (P0 fallback = CLI), Meta App Review (Ads).

## 7. Owner actions (outside the repo)

Send the Projects-for-Platforms preview request · open an Ayrshare Business
account · register one Lightbulb X developer app · doola partner
conversation (organizer semantics, compliance API, iFrame tier) · PostHog
partner registration if the Provisioning API is used for owners without
Projects · decide Route 53 vs Projects registrars (P4).

## 8. Open questions

1. Connections long tail: owner-added MCP servers/plugins cover technical
   owners; add Composio-as-MCP for non-technical ones, or not.
2. Domain registrar: Route 53 (transfer-out API, zone in our account) vs
   Projects registrars (billing on the owner's Stripe).
3. Pricing fence for pass-through vendors: Growth add-on vs Ultra vs credits
   via Metronome.
4. Per-agent phone numbers vs one per company.
5. User/Aden asks: not yet checked against this design — collect and diff.

## 9. Non-goals

Merchant-of-record, BaaS backend, model router, trading/wallets, cloud
phones, multiple businesses per owner, browser-typed signups, any Lightbulb
umbrella account.

## 10. Verification

Every phase is verified by Claude-in-Chrome on dev with a signed-in dev
account, proven from the container log / EFS / the owner's own provider
dashboard (e.g. the Stripe account visible in the owner's Dashboard, the
PostHog project in the owner's org). Unit tests cover the rollup writer,
webhook handlers, policy gate, export exclusions and wipe rules.

## Appendix A — Naive primitives → ours

Agent profile → company (Friday + spine + mail + phone + card). Governance/
approvals/kits/ledger/revoke → §4.5. Vault → Secrets Manager. Brain → wiki.
Events/loops/webhooks → routines + trigger rail (have). Teams/durable
runtime → Paperclip + OpenClaw (in production; theirs is not). Connections →
18 rails + channels + owner MCP servers. Sessions/logs → Sessions/Activity +
Lightbulb MCP. Apps/db/storage/functions/auth/compute/queue/sandbox →
Projects on the owner's identity; our container is compute. Domains/email/
phone → §3.3. LLM/audio/voice/images/video/clips → Bedrock/OAuth/BYO have;
fal.ai P3; skip voice/clips. Browser → real Chrome with owner takeover
(have, better). Social → Ayrshare. SEO/AEO/app/travel/reviews/places/e-com
→ DataForSEO for SEO/AEO only. People/company/social data → Apollo/Parallel.
Cards → §3.3. Trading/x402/wallet → skip. Customer billing → Connect.
Identity/formation → Stripe Identity + doola. Credits → flat $50 + pass-
through.

## Appendix B — Evidence

Stripe: docs.stripe.com/projects, /projects/platform-integration,
/connect/standard-accounts, /connect/hosted-onboarding, /connect/pricing,
/cli/login. PostHog: posthog.com/docs/integrate/provisioning, /terms §2.1(b),
/docs/revenue-analytics/connect-to-customers. Ayrshare:
ayrshare.com/docs/multiple-users/api-integration-business, /pricing. Route
53: RegisterDomain / TransferDomainToAnotherAwsAccount API docs. doola:
docs.doola.com/api/introduction. Result: result.dev, docs.result.dev
(sdk/*), app.result.dev/mcp, ycombinator.com/launches/QkS-…. Naive:
usenaive.ai/docs/llms.txt and per-primitive `.md` pages.
