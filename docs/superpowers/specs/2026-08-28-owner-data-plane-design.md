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

- **Create**: **Accounts v2** — `POST /v2/core/accounts` with the `merchant`
  configuration (`card_payments` requested) **and the `developer`
  configuration requesting the `projects` capability** (the Provisioning
  API's onboarding path; OPEN with Stripe whether both configurations coexist
  on one `dashboard: full` account — their example uses `dashboard: none` —
  see §8), `dashboard: full`, and
  `defaults.responsibilities = {fees_collector: stripe, losses_collector:
  stripe}`; prefilled from the company name and the owner's Clerk email;
  hosted onboarding via v2 account links (`use_case.type =
  account_onboarding`, `configurations: [merchant, developer]` pending §8;
  single-use, ~10-minute
  TTL, minted on click, never stored or emailed). Decided 2026-08-28: v2 is GA
  for all Stripe users (dahlia, 2026-04-22), our dev platform already serves
  `/v2/core/accounts`, and `stripe` 14.4.1 exposes `StripeClient.v2.core`.
  v2 also lets the account act as a *customer* later (bill the company's
  Lightbulb subscription to its own account — Money → Spend, P1+).
- **Connect existing**: Standard OAuth for an owner who already runs Stripe —
  v1 by Stripe's own limitations list; v2 endpoints manage the resulting
  account too. (An account already controlled by another platform gets a
  fresh account — Stripe's 2021 rule.)
- Status vocabulary stays the v1 trio (`details_submitted` /
  `charges_enabled` / `payouts_enabled`), read via v1 endpoints that accept
  v2 ids, so both doors and the existing `account.updated` webhook share one
  shape. v2 thin events are optional later. Not on v2: `card_issuing_*` and
  Treasury — if Stripe Issuing is ever chosen for agent cards, that lives on a
  v1 Custom account, not this one.

Both yield a Standard account the owner logs into and keeps; **zero Connect
fees, no application fee**; we read with the platform key + `Stripe-Account`
and receive Connect webhooks. Revoking the platform link leaves a normal
Stripe account.

The spine is three things at once: the owner's payments ledger; the identity
Stripe Projects hangs providers off (`Stripe-Context: acct_…`); the billing
profile with per-provider spend limits. Whether an account can provision is
`GET /v2/provisioning/eligibility` — `is_eligible` plus `requirements` such as
`representative.name`, `representative.dob`, `tos.stripe` — resolved through
hosted onboarding (or `POST /v2/provisioning/kyc`); `contact_email` alone never
satisfies it, and a return to our `return_url` proves nothing.

**When it appears: a Friday-raised approval card, never a gate before chat.**
Friday raises it the first time a task needs money or infrastructure (or in
the first-run brief on paid plans). The card renders in Inbox, pushes to
mobile/desktop, and opens in whichever surface the owner is in.

**Trigger split (owner decision 2026-08-28) — applies to every rail:**

| Who | Does what | When |
|---|---|---|
| Platform | silently creates what needs no human: the bundle.social team, the Projects project shell, minting Stripe/portal links | on upgrade to paid / on click |
| Friday (skill) | knows every door and its rules, raises the card, never signs up in the browser, reads `isol8-facts.json` so she never asks twice; does the work once the door is open | when a task needs it; pre-staged as one sentence in the first-run brief when the stated business goal implies selling or posting |
| Owner | one click per account: Stripe's hosted form, the social portal, a provider's terms | when they're ready |

First run stays "type an idea, watch it become a company" — never a setup
checklist. Trial owners see the same sentence framed as what upgrading
unlocks (the paywall inversion in the growth doc).

### 3.2 Provisioning door (Stripe Provisioning API — Projects for Platforms)

Access granted 2026-08-28 (allowlisted private preview; every call carries
`Stripe-Version: unsafe-development`; "the contract can change"). Every field
below is taken from Stripe's two preview documents (spec rev `f918b509`,
integration guide) — re-verify on the shared Slack channel before deploying a
field change, and tolerate unknown response properties.

**Backend-owned, Friday-asked, owner-approved (decided 2026-08-30).** The API
has two authentication modes: *direct* (the account's own key — what the public
Projects CLI uses) and *platform* (our platform key + `Stripe-Context: acct_…`).
The platform key acts on every connected account by changing one header, so it
never leaves the backend; Stripe's guide places the whole operational loop in
"trusted server-side code" and says never from generated application code or a
model prompt. (A Project *ephemeral key* exists, but the guide reserves it for
"a trusted server-side process" — a container credential is a question for
Stripe, not a plan.) So the backend runs the loop, and:

- **Friday reaches it through the container door the marketplace and §3.1
  already use** — hook-token routes under
  `/api/v1/company/provisioning/u/{owner_id}/…` (`catalog`, `requests`,
  `requests/{id}`), plus `isol8-facts.json`. She never sees a credential value,
  a checkout URL, a redirect URL, or a deep link. The skill carries the *when*
  (a task needs infrastructure or a paid service; cards are never a gate;
  `requires_review` means tell the owner); the *how* is code.
- **The owner reaches it through Settings → Connections and Inbox cards**, the
  same cards/rows as the Stripe card, opened with the two-phase popup.
- **The CLI-in-container path is retired** (no Stripe CLI in the image, no
  pairing card, nothing on EFS). Accounts we cannot configure for Projects
  (connect-existing Standard accounts, pending Stripe's answer) simply do not
  provision — the card says so.

**Mapping onto Stripe's objects.** Tenant = the company = §3.1's connected
account (`billing.company_stripe_account_id`). Project = **one per company**
(`"<Company name>"`, `catalog: prod`); per-app preview/production Projects are
a later feature. Stripe's "Approval" = our consent card's payload plus a
snapshot on the workflow row. Stripe's "Provisioning workflow" = a
`provisioning-workflows` DynamoDB row. "Secret binding" = the existing
`integrations` row `projects/{provider}` + Secrets Manager blob + `env.vars`
(§3.5). Payment profile + usage limits = the owner's, on their account.

**The loop** (workflow states are ours; Stripe's are in code font):

1. **Ask.** Friday `POST …/requests {provider, service, configuration?,
   issue_id, reason}` (or the owner, from Connections, later). Backend:
   `GET /eligibility` — `is_eligible=false` → `failed` with a safe message;
   `requirements` non-empty → a **Finish Stripe setup** card (hosted
   onboarding link, §3.1). Re-read the account catalog
   (`catalog/services?provider_name=`), reject unavailable services, validate
   `configuration` against `configuration_schema`, snapshot provider, service
   (`service_id` → `service_ref`), configuration, `pricing.paid_pricing`,
   terms, the proposed usage limit → row `awaiting_approval` → **consent card**
   (§3.1's card shape; `payload.isol8_action = provider_consent`, now carrying
   the snapshot and a limit the owner can edit).
2. **Approve.** On approval the backend re-reads the catalog; if price, terms,
   configuration or availability changed, a fresh card. Paid service with no
   payment profile → **payment card**: `POST /payment_method_requests` with
   only `usage_limits` (the owner's own card, collected on Stripe's hosted
   page — the `checkout_session_url` is opened from the card and never stored),
   then poll `GET /payment_profile`. *Fallback if Stripe says hosted collection
   is unavailable in platform context:* `payment_method_owner=platform` with
   the owner's subscription PaymentMethod as `source_*` and the same limits,
   passed through at cost on their Lightbulb invoice.
3. **Connect the provider** (`connecting_provider`). `GET
   /provider_connections`; none `active` → `POST /account_requests {provider,
   project_id}`. `pending_auth` → **Connect <Provider>** card whose button
   opens `redirect_url` (`awaiting_user_action`; Stripe owns the OAuth
   callback; we poll the account request). `needs_information` → the card
   renders `needs_information_schema` with a constrained renderer (string,
   number, boolean, enum; anything else → `requires_review`) and submits via
   `submit_information`. `complete` → show `provider_account_details`
   (display name, e-mail, `link_action`) so the owner knows which provider
   account now exists.
4. **Provision** (`provisioning`). `POST /resources {service_ref, provider,
   project, name, configuration}` (project-scoped) — persist the returned id
   before anything else. Poll with bounded exponential backoff and jitter to
   a deadline (then `requires_review`, ids kept); `needs_information` → card;
   `errored` → `failed` with `error_message` only if safe. Plan/deployable
   dependencies: create the plan, wait `complete`, re-read, re-approve if
   changed, then the deployable.
5. **Bind** (`binding`). `GET /environment_info?reveal=true&scope=SERVICE&
   project&provider&service`, select the entry whose `resource_id` matches the
   row, `land_credentials` (Secrets Manager `projects/{provider}`, pointer
   with `env_keys`, per-key `env.vars`) → `active` → `write_owner_facts`.
   Friday's next turn sees the variable *names*.
6. **After.** Rotation: `POST /resources/{id}/rotate_credentials` → `complete`
   → re-bind; `pending` → `requires_review` (no retrieval flow in preview).
   Remove/unlink: **owner-only, explicit, separate confirmation stating the
   billing and infrastructure effect** — the only path that ever reaches a
   provider. Spend: `GET /charges` (per provider, `start_time/end_time/
   livemode` required) → Money → Spend. Dashboards: `POST /deep_links
   {purpose: dashboard, provider, project}` on click, never stored.

**Non-negotiables from the preview contract.** No webhooks → a backend task
per workflow polls, and a startup sweep resumes every non-terminal row (same
pattern as `_resume_wake_resize_transitions`). **No idempotency contract → a
state-changing POST that times out is never repeated**; the row goes
`requires_review`, known ids are retrieved, an admin line shows it. Provider
HTTP errors are translated — branch on Provisioning codes
(`payment_method_required`, `provider_reauth_required` → new account request,
`invalid_resource_configuration` → re-render schema,
`resource_count_constraint_exceeded`, `provider_failure`, `not_found`).
`llm_context`, terms text and schemas are untrusted data. Secrets, checkout
URLs, redirect URLs and deep links never reach a browser payload we did not
mint for that click, a log, analytics, or a prompt. 45-second client timeout;
follow `next_page_url`, never construct pages.

**Two rails, one Connections page.** The OAuth integrations rail (existing:
`integrations` row `<provider>`, MCP servers, tokens for agents to *act
through* an account) and this rail (`projects/<provider>`: a resource
*created* on the owner's identity, a resource-scoped credential as env) are
different relationships and both stay. Stripe's `account_requests` is Stripe's
own connection for provisioning — it gives us no token to call the provider,
so it never replaces our OAuth rail. Rendering: one card per provider with two
facets, "Account connected" and "Resources" (level per §3.4), and after a
resource completes, an offer to OAuth-connect *that* provider account (the
`provider_account_details` e-mail) so reads work (§4.2 — PostHog is the case:
Projects gives the site's write token, HogQL reads still need OAuth).

Verified 2026-08-28 with our dev platform key: `posthog/analytics` is free
tier (config `region` + `organization_name`); providers include Cloudflare,
Vercel, Supabase, Neon, Clerk, PostHog, Twilio, Resend, AgentMail,
Squarespace/Spaceship/WordPress.com (domains), Sentry, E2B. Paid *test*
provisioning is cohort-gated — free resources only until Stripe confirms.

### 3.3 Rails Projects does not reach

Same shape — vendor's own "make the human the owner" primitive is the one
human step, rendered as an approval card:

| Rail | Vendor | Owner step | Notes |
|---|---|---|---|
| Social publishing | **bundle.social** (owner decision 2026-08-28, replaces Ayrshare): one Lightbulb organization, **one team per company created via API**, hosted portal link / hosted connect flow where the owner OAuths their own accounts; 15+ networks (X, LinkedIn, Instagram, Facebook, TikTok, YouTube, Threads, Bluesky, Reddit, Pinterest, Mastodon, Snapchat, Discord, Slack, Google Business); create-post with per-platform overrides + scheduling; post- and account-level analytics (not for X); webhooks (`post.published`, `post.failed`, account connect/disconnect); TypeScript SDK, CLI **and an MCP server** — so Friday can drive it directly | links their networks on the portal | flat: Pro $100/mo (10k posts), Business $400/mo (100k posts), **unlimited social accounts, no per-seat/per-profile fee**; X billed $0.015/post ($0.20 with a link) from prepaid credits — fence X as pass-through. Whether X needs our own app keys is UNVERIFIED (docs don't say) |
| Formation + EIN + RA | **doola** API (what Naive resells at $349), owner as member | KYC + pays checkout | verify organizer semantics + compliance/bookkeeping API with doola sales |
| KYC | **Stripe Identity** ($1.50; `related_person` reuses it for Connect) | ID + selfie | |
| Phone/SMS | **Surge** (sole-prop 10DLC pre-EIN, standard brand after) | none | do not sell "receives any OTP" — VoIP numbers are unreliable for Google/bank OTPs |
| Cards | owner's **Mercury/Ramp/Brex OAuth** with interval caps first; Stripe Issuing + Connect Custom only if per-authorization approve/decline is needed | OAuth | |
| Domains | **Route 53 Domains** (owner is registrant, we pay $16/yr and rebill, zone in our account, transfer-out after 60 days) OR Projects registrars — decide in P4 | approves registrant block + ICANN 15-day email | |
| Mail on own domain | SES identity per owner domain on the existing per-owner tenant (`agent_mail_tenant.py`) | none | DKIM/MAIL FROM/DMARC auto-published, zone is ours |
| Hosted fallback pages | CloudFront SaaS Manager tenants (`<slug>.flickmeon.ai`, `support.<slug>.flickmeon.ai`) | none | |
| SEO/AEO data | DataForSEO (platform key) | none | P5 |
| Content (images/video) | **Compose in the container, zero vendors (decided 2026-08-28):** carousel slides = HTML template screenshotted by the container's Chrome over **Pexels** portrait stills (free API, commercial use, no attribution); wall-of-text reel = Pexels portrait clip + ffmpeg burned caption; green-screen = owner's selfie clip chromakeyed over a clip, captions via Amazon Transcribe ($0.006/min) or whisper.cpp; X/LinkedIn = LLM path; feedback votes → per-company format weights. Marginal cost ≈ $0/post. **Bedrock has NO active text-to-image model** (Nova Canvas Legacy, EOL 2026-09-30; Titan EOL; Stability gen models gone) — so generated imagery is opt-in on the owner's identity via Projects: Gemini 2.5 Flash Image via OpenRouter (~$0.039/img), HeyGen avatars (~$1/min), ElevenLabs voice; gpt-image-1 only for BYO-OpenAI-key owners. Remotion ($0.01/render, $100/mo floor) only if ffmpeg typography becomes the complaint | none (Pexels is platform-keyed, free) | P3 |
| Enrichment | existing Apollo/Attio/Parallel rails | OAuth | |

Not adopted: merchant-of-record (Paddle/Polar/Lemon Squeezy — 4–5% or no
owner account), BaaS backends, model routers, trading/x402/USDC wallets,
cloud phones, browser-typed signups (ToS-prohibited at Cloudflare, Supabase,
Shopify, X, LinkedIn, TikTok, Meta, Buffer; phone/ID walls from datacenter
IPs).

### 3.4 Company-level vs agent-level resources

Company-level: Stripe account, domain, PostHog project, bundle.social team,
formation, cards. Agent-level: mailbox (`{agent}@{slug}.…`, existing), Google
account binding (existing `google_accounts`), phone number (optional). State
this per resource in the Accounts page.

### 3.5 Credential injection

Three patterns exist in production; pick per credential class:

- **Proxy** (`apps/infra/openclaw/plugins/aws-exec-profile` — agent never
  sees the key): use for the connected-account Stripe key and anything with
  money authority.
- **Per-run injection** (`paperclip-exec-creds`): use for short-lived tokens.
- **Static env** (Provisioning `environment_info` landed as OpenClaw
  `env.vars`, MCP OAuth store): acceptable for provider write tokens (PostHog
  project token, Vercel deploy token) scoped to the owner's own resources.
  `build_provisioned_env` re-asserts them at every provision from Secrets
  Manager, injecting only the names each pointer's `env_keys` declares.

The provisioning rail also keeps a durable **workflow record**
(`provisioning-workflows`: owner, action, state, Stripe ids — account request,
resource, project — the approval snapshot, safe error code, timestamps). A
browser session, provider redirect, backend restart or deploy must never lose
an account-request id, a resource id, or an uncertain write.

Backend copies of provider credentials live in Secrets Manager at
`lightbulb/{env}/integrations/{owner}/projects/{provider}` (existing store,
`credentials/integration_secret_store.py`; the `projects/` segment keeps this
rail off the OAuth rail's key — a bare key made the provider look
OAuth-connected and injected a live MCP server), never DynamoDB.

### 3.6 Gates, org semantics, exit, limits

- **Paid-only.** The accounts layer sits behind `require_always_on_entitlement`
  like automations. Trial owners see the card as the upgrade trigger.
- **Orgs.** The org admin who completes Stripe onboarding is the
  representative and the sole money-approver (mirrors
  `oauth_connector_user_id` for ChatGPT OAuth).
- **Company name first.** The name-first invariant now also feeds the Stripe
  account name, PostHog org name and bundle.social team. Onboarding brief forces
  a real name before the Stripe card.
- **Exit / wipe.** `account_wipe`, `entitlement_teardown`,
  `owner_secret_sweep` revoke the Connect link and delete *our* credential
  copies. They **never** call Provisioning `remove`/`unlink` or touch
  owner-owned provider resources; `remove` exists only behind the owner's own
  explicit, separately confirmed click.
- **Marketplace export** (`agent_export.py`) must exclude provider
  credentials; nothing on this rail writes to EFS any more, and `.projects/` /
  `.stripe` stay in `_SKIP_DIR_NAMES` only as a named backstop. A hired agent
  arrives with no accounts; its "accounts to connect" to-do reuses the
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
- **Social**: bundle.social post- and account-level analytics (none for X).
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
and spent together; Projects per-provider limits and bundle.social/X pass-through
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
| Content | LLM copy + Pexels media + Chrome/ffmpeg compositions in the container; feedback votes = learning loop; generated art/avatars opt-in via Projects | new |
| Publish | bundle.social (team per company; portal link; MCP for Friday) + Paperclip routines | new |
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

- **P0 Foundations** — Stripe card + Connect create/connect (P0a, shipped
  as PR #2157); the Provisioning API workflow — eligibility, consent, payment,
  connect, provision, bind — with its cards and the two-facet Connections page
  (P0b); Connect webhook coverage; retained PostHog token; rollup + scheduled
  writer; credential proxy for the Stripe key; paid gate; wipe/export rules;
  work-product register-on-create; Lightbulb MCP. Screens: Connections,
  Inbox reshaped.
- **P1 Money + Home** — Payments, Products, Customers, Spend, Home.
- **P2 Knowledge + Support** — Brain UI; Support widget + hosted page +
  inbox; Library feed; redaction widening.
- **P3 Growth** — Publish (bundle.social), Content grid (in-container compositions over Pexels; Projects opt-ins), Leads, Blog;
  Growth add-on pricing fence.
- **P4 Company** — Domain & Site, Identity (doola + Stripe Identity), Ads
  (Google Ads rail), mail on own domain.
- **P5 Long tail** — SEO & AEO, A/B Testing, Compliance, Contracts.
- **Cross-cutting** — IA migration + panel registry, mobile parity.

Each phase ships only on rails that exist by then. Vendor waits: Stripe's answers
to §8 6–8 (P0b ships free resources on created accounts meanwhile), Meta App
Review (Ads).

## 7. Owner actions (outside the repo)

Accept Stripe's shared Slack channel (offered 2026-08-28) and put §8 6–8 to
them · open a bundle.social Business
account (confirm whether X needs our own app keys) · doola partner
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
6. Stripe: can one Accounts v2 account carry both the `merchant` and
   `developer` configurations with `dashboard: full`, and can a
   connect-existing Standard account be given the `projects` capability?
7. Stripe: is hosted payment collection (owner's own card, no `source_*`)
   available under `Stripe-Context`, or is `payment_method_owner=platform`
   the only path in the preview cohort?
8. Stripe: which operations does a Project ephemeral key authenticate — and
   is the paid *test* cohort enabled for our platform?

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
images/video composed in-container over Pexels P3; generated art/avatars/voice via Projects (OpenRouter/HeyGen/ElevenLabs) on request; skip clips. Browser → real Chrome with owner takeover
(have, better). Social → bundle.social. SEO/AEO/app/travel/reviews/places/e-com
→ DataForSEO for SEO/AEO only. People/company/social data → Apollo/Parallel.
Cards → §3.3. Trading/x402/wallet → skip. Customer billing → Connect.
Identity/formation → Stripe Identity + doola. Credits → flat $50 + pass-
through.

## Appendix B — Evidence

Stripe: docs.stripe.com/projects, /projects/platform-integration,
/connect/standard-accounts, /connect/hosted-onboarding, /connect/pricing,
/cli/login. PostHog: posthog.com/docs/integrate/provisioning, /terms §2.1(b),
/docs/revenue-analytics/connect-to-customers. bundle.social:
info.bundle.social/llms.txt, bundle.social/pricing. Route
53: RegisterDomain / TransferDomainToAnotherAwsAccount API docs. doola:
docs.doola.com/api/introduction. Result: result.dev, docs.result.dev
(sdk/*), app.result.dev/mcp, ycombinator.com/launches/QkS-…. Naive:
usenaive.ai/docs/llms.txt and per-primitive `.md` pages.
