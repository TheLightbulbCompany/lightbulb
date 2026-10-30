# Rivals to Stripe Projects for platforms: who else lets a platform provision third-party services for its users, with billing handled

Researched 2026-10-29. Tags: [P] primary (vendor doc fetched 2026-10-29 unless dated), [S] secondary, [U] could not verify. Builds on `2026-10-28-small-software-stripe-projects-read.md` (what the Provisioning API is), `2026-10-27-small-software-stripe-projects-catalog.md` (the 60+ providers) and `2026-10-27-small-software-hosting-options.md` (hosting substrates). Those are not repeated here. WebSearch/WebFetch only.

## The question, sharpened

Stripe Projects for platforms does four things at once: (1) a catalog of 60+ providers, (2) creates the **end customer's own** provider account and hands back credentials, (3) holds **one payment method on the customer's connected account**, tokenised per provider, with per-provider spend caps, (4) exposes all of it as an API a platform calls on the customer's behalf (`Stripe-Context`). Nobody else does all four. What exists is three partial shapes:

- **Marketplace rails** (Vercel, Heroku, AWS/GCP): the host bills its own user and pays the vendor; only for users with a host account; no third party can drive it.
- **Single-vendor partner programs** (Neon, Supabase, Prisma, Convex, PlanetScale, Railway, Turso): one product, created either in the platform's org (platform pays) or in the user's org via OAuth (user pays and must already have an account).
- **Agent-payment layers** (Skyfire, Nevermined, Paid.ai) and **agent-auth layers** (Composio, Nango, Arcade, Vercel Connect): move money or tokens; provision nothing.

## 1. Vercel Marketplace native integrations, CLI, and Vercel Connect

- **Provisions:** partner products (Neon, Upstash, Prisma, AWS, Mem0, Shopify sandboxes…) as resources in a Vercel team; env vars injected into projects. "Vercel calls the provider API… to manage resources, handle installations, and process billing." [P] https://vercel.com/docs/integrations/create-integration/marketplace-api
- **Account and bill:** the Vercel user's team. Partner submits usage and invoices; "Vercel handles all taxation since Vercel issues the invoices." Users "can configure a different payment method for each integration installation." 9 retries, overdue after 15 days. [P] https://vercel.com/docs/integrations/create-integration/billing
- **Platform acting for a user:** only Vercel itself. Providers need a Pro team and approval via integrations@vercel.com. [P] https://vercel.com/docs/integrations/create-integration/marketplace-product. The user-side rail is the CLI: `vercel integration add neon --plan pro --format json`, non-interactive, plus a sandbox **claim** flow that polls "until ownership transfers from sandbox to your team". [P] https://vercel.com/docs/cli/integration. No endpoint for a third party to provision into someone else's team was found. [U]
- **Revenue share:** not stated on the docs or the program page. [U] https://vercel.com/marketplace/program
- **Vercel Connect** (GA): "short-lived access to third-party APIs like Slack, GitHub…"; "Use a Vercel Integration instead when you want a marketplace-managed install for a provider-billed product." Pro $3.00 per 1,000 token requests. Brokers tokens; creates no accounts. [P] https://vercel.com/docs/connect
- **Read:** the closest shape to Stripe Projects (one card, host invoices, partner provisions, creds to env), but the customer must be a Vercel team and only Vercel can drive it.

## 2. Cloudflare for Platforms

- Workers for Platforms: platform pays $25/mo for the fleet; end customers never see Cloudflare. [P] 2026-10-27, hosting note. Cloudflare for SaaS: custom hostnames, 100 included on Free/Pro/Business, "$0.10" per additional hostname; the SaaS provider is the Cloudflare customer; no end-customer billing described. [P] https://developers.cloudflare.com/cloudflare-for-platforms/cloudflare-for-saas/plans/
- **Read:** a substrate for running customers' code under the platform's account, not a rail for giving customers their own accounts. Cloudflare is also a Stripe Projects provider (catalog, [P] 2026-10-27).

## 3. Netlify extensions and Netlify Database

- Partner extensions are "listed in the extensions directory for all Netlify users"; author must be in the technology partner program; nothing on billing or revenue share. [P] https://developers.netlify.com/sdk/publish/partner-extensions/
- Netlify Database: "a fully managed Postgres database built into the Netlify platform"; "When a database is active, it consumes credits". Neon names "Netlify DB" as an Embedded Postgres implementation. [P] https://docs.netlify.com/build/data-and-storage/netlify-db/
- **Read:** Netlify is itself a consumer of Neon's program and bills its own users in credits. No path for a third party.

## 4. Railway and Render

- Railway "Login with Railway": OAuth 2.0 + OIDC; users pick which workspaces/projects to share; "The token carries the permissions the user granted"; tokens expire after one hour. [P] https://docs.railway.com/integrations/oauth. Whether an app can create projects in the user's workspace is not stated; no partner program found. [U] User pays Railway.
- Render: REST API with account API keys; MCP OAuth for coding tools (July 2026). [S] https://render.com/docs/api. No partner/OAuth program for third parties found. [U]

## 5. Supabase: OAuth apps and "Supabase for Platforms"

- OAuth apps: tokens "grant your application full access to the Management API on behalf of the user"; `POST /v1/projects` creates a project; "Only some features are available until we roll out fine-grained access control." [P] https://supabase.com/docs/guides/integrations/build-a-supabase-integration
- Supabase for Platforms: create projects via the Management API in an org **you** own, hand them to users, later transfer "from your org to your user's org while you continue to retain access" via OAuth. Nano scale-to-zero: "Scale to zero pricing only applies to Nano instances", select customers, form. [P] https://supabase.com/docs/guides/integrations/supabase-for-platforms
- Bill: "Supabase bills separately for each organization." Plans cannot mix in one org. [P] https://supabase.com/docs/guides/platform/org-based-billing. So platform-org = platform pays; user-org = user pays and must sign up.
- Customers: Lovable, Bolt, v0, Tempo; "pricing options for AI Builders" by form. [P] https://supabase.com/solutions/ai-builders
- **Read:** same claim/transfer pattern as Prisma and Neon; no payment collection from the end user on the platform's behalf.

## 6. Neon: four models and the Agent plan

- Models: **Embedded Postgres** ("project-per-user model… via the Neon API", consumption limits per user), **Claimable** ("Claiming moves the project into the user's organization"; private preview; TTL default 24 h; one use), **OAuth** ("Users maintain full ownership of their Neon accounts"), **AI Agents** (two orgs). "Built On Databricks validation is nomination-only and is aimed at a small number of large platforms." [P] https://neon.com/docs/guides/platform-integration-intro and https://neon.com/docs/workflows/claimable-database-integration
- Agent plan: "Neon creates two organizations in your account"; free org: "You are not charged for usage in this organization"; "Pricing applies to the paid organization only"; up to $25,000 credits; compute $0.106/CU-hour, storage $0.35/GB-month; needs an active Scale plan and "approval from the Neon team". [P] https://neon.com/docs/introduction/agent-plan
- **Read:** the most complete vendor-side program. The platform is the payer and bills its users itself ("consumption metrics… for building usage-based billing into your platform").

## 7. Prisma Postgres partner integration

- Partner creates a project in its own workspace (`POST /v1/projects`, service token), later transfers via OAuth2: "Only the user who completed the OAuth2 flow can claim the database"; afterwards "the database is now owned by the user's workspace" and users "go beyond the free database usage limits". Access: "Contact the Prisma team from the Prisma Partners page to request access to the REST API." [P] https://www.prisma.io/docs/management-api/partner-integration
- **Read:** same claim pattern; platform pays until claim. Co.dev and Deno are cited partners. [S] https://www.prisma.io/blog/how-deno-and-prisma-partnered-to-power-per-branch-databases

## 8. Convex Platform APIs

- Team tokens "authorized to create new projects, create new deployments within projects"; OAuth apps: "your users authorize your product to manage their own Convex team or projects"; unverified apps capped at 100 teams. Billing not stated. [P] https://docs.convex.dev/platform-apis/oauth-applications
- **Read:** OAuth into the user's account; user pays; no account creation.

## 9. PlanetScale, Turso, Upstash

- PlanetScale OAuth apps with user/organization/database/branch scopes; privacy policy and consent required; nothing on billing. [P] https://planetscale.com/docs/api/reference/oauth
- Turso Platform API: "Create and manage databases on behalf of your end users", "build your own database-as-a-service on top of Turso"; token-based only; plans billed monthly to the org, no reseller tier. [P] https://docs.turso.tech/api-reference/introduction and https://turso.tech/pricing
- Upstash Developer API "is only available to native Upstash accounts"; "Accounts created via third-party platforms like Vercel or Fly.io are not supported"; no OAuth or partner model. [P] https://upstash.com/docs/devops/developer-api
- **Read:** Turso is a clean platform-pays model; PlanetScale is OAuth-into-user; Upstash is neither.

## 10. AWS Marketplace and Google Cloud Marketplace

- AWS: SaaS public offers 3%, private offers 3%/2%/1.5% by TCV; buyer is an AWS account. [P] https://docs.aws.amazon.com/marketplace/latest/userguide/listing-fees.html. GCP: 3% since 2021. [S] https://www.techradar.com/news/google-cloud-slashes-its-cut-of-third-party-sales
- **Read:** procurement for existing cloud accounts; no API for a platform to buy for a non-account-holder.

## 11. Heroku add-ons (the ancestor)

- "When a customer makes a request to provision your add-on, Heroku sends a POST request… to the /heroku/resources endpoint"; "billed from when the provisioning request is sent and pro-rated to the second." [P] https://devcenter.heroku.com/articles/building-an-add-on. Customer fees "invoiced monthly in arrears" by SFDC. [P] https://www.heroku.com/policy/heroku-elements-terms. Revenue share sits in an unreadable agreement PDF. [U]
- **Read:** the template Vercel and Stripe copied; Heroku users only.

## 12. Agent commerce and agent auth

- Skyfire: Buyer/Seller agents with wallets; `kya`/`pay`/`kya-pay` tokens; sellers settle through Skyfire. [S] https://docs.skyfire.xyz/docs/platform-structure
- Nevermined: "Payments Infrastructure for AI Agents"; spend mandates; per-call catalog paid "over x402 or MPP"; $20/mo, merchant listing from $50/mo. [P] https://nevermined.ai
- Paid.ai: "Meter, price, bill and prove the value your agents deliver." [P] https://paid.ai
- Composio ("Composio-managed auth is the default"), Nango ("handles OAuth, API keys, token refresh…"), Arcade ("handles OAuth and manages user tokens"): connect to accounts that already exist. [P] https://docs.composio.dev/docs/welcome, https://nango.dev/docs/getting-started/intro-to-nango, https://docs.arcade.dev/home
- Blaxel, Daytona, E2B: providers inside Stripe Projects (catalog [P] 2026-10-27). [S] https://blaxel.ai/blog/stripe-projects-blaxel-integration
- OpenAI, Anthropic, Google: no provisioning-with-billing product found. [U]
- **Read:** none creates an account; they pay per call or hold tokens.

## 13. Open-source and self-hosted

- Terraform/OpenTofu: Supabase provider is Public Alpha [S] https://supabase.com/docs/guides/deployment/terraform; Neon's is community, "not maintained or officially supported by Neon" [S] https://neon.com/docs/reference/terraform; Pulumi bridges those. [S] https://www.pulumi.com/registry/packages/neon/
- Zeabur: GraphQL API, key "tied to your personal account", no OAuth. [P] https://zeabur.com/docs/developer/public-api. Dokploy: "complete API and CLI access", self-hosted. [P] https://dokploy.com. Coolify: `POST /api/v1/projects`, team-scoped token. [S] https://skills.sh/spqw/skill-coolify/coolify. Crossplane: no Neon/Supabase/Vercel providers found. [U]
- **Read:** all run with the platform's credentials; the bill lands on the platform; none creates an account in the user's name or takes their card.

## Comparison table

| Candidate | Provisions | Account owner / payer | Platform acts via | Fee / rev share | Maturity | Substitute? |
|---|---|---|---|---|---|---|
| Stripe Projects (platform) | 60+ providers | End customer (connected account) / customer's card | Platform key + `Stripe-Context` | Free; list prices | Allowlisted private preview | Baseline |
| Vercel Marketplace + CLI | Partner products into a Vercel team | Vercel user / Vercel invoices | Only Vercel; CLI for the user | Not stated [U] | GA | No: must be a Vercel team |
| Vercel Connect | Tokens to existing SaaS | n/a | SDK | $3/1k tokens | GA | No: no accounts |
| Cloudflare for Platforms | Workers, hostnames under your account | Platform | API | $25/mo; $0.10/hostname | GA | No: substrate |
| Netlify ext. / DB | Netlify's own users | Netlify user (credits) | n/a | Not stated | GA | No |
| Railway OAuth | User's workspace | User | OAuth, 1 h tokens | n/a | Undated | No: one vendor, user signs up |
| Supabase for Platforms | Projects in your org, transfer later | Platform, then user after claim | Mgmt API + OAuth | Custom "AI builder" pricing | Live; nano by approval | Partial: one vendor |
| Neon (Embedded/Claimable/Agent) | Projects per user; sponsored free org | Platform (free org sponsored), user after claim | API; claim is private preview | $0.106/CU-h; $25k credits | Agent plan by approval | Partial: one vendor |
| Prisma partner API | Project per user, OAuth claim | Platform, then user | Service token + OAuth | Not stated | Partner access by request | Partial: one vendor |
| Convex Platform APIs | Projects/deployments | Platform or user (OAuth) | Token / OAuth | Not stated | Unlabelled; verification at 100 teams | Partial |
| PlanetScale / Turso / Upstash | DBs | User (OAuth) / platform / self only | OAuth / token / key | n/a | GA | No |
| AWS / GCP Marketplace | SaaS subscriptions | Cloud account holder / cloud invoices | n/a | 3% | GA | No |
| Heroku add-ons | Add-ons for Heroku apps | Heroku customer / SFDC invoices | n/a | In agreement [U] | GA | No |
| Skyfire / Nevermined / Paid.ai | Nothing; payments | Agent wallet / vendor | API | Per call; $20–50/mo | Live | No |
| Composio / Nango / Arcade | Nothing; auth | n/a | SDK | Seats/usage | GA | No |
| Terraform / Pulumi / Coolify / Dokploy / Zeabur | Resources with your keys | Platform | API/IaC | OSS | GA/alpha | No |

## Verdict

**No true substitute exists today.** Nothing found combines (a) many providers, (b) an account in the end customer's name, (c) the customer's own card tokenised to each provider with spend caps, and (d) an API a third-party platform calls for its tenant. Marketplaces keep (b)(c) for their own users only and never (d); vendor programs keep (d) for one product and make the platform the payer until a claim; agent-payment layers move money and create nothing.

**Nearest two:**
1. **Vercel Marketplace native integrations + `vercel integration add`**: same mechanics (partner provisions, host invoices on one card, credentials into env, sandbox then claim) but the customer must be a Vercel team and only Vercel can drive it.
2. **Neon's platform program (Embedded Postgres + Claimable + Agent plan)**: the most complete vendor-side offer with a sponsored free org and per-user consumption limits, but it is Postgres only and the platform is the bill-payer; Supabase for Platforms is the same shape one step behind.

## What this means for Lightbulb's Apps design

1. Keep Stripe as the one rail. Nothing else gives "customer's own account, customer's card, many providers, platform API" and that is the whole reason Apps don't make Lightbulb a reseller.
2. The only second rail that exists is "Lightbulb-owned vendor orgs" (Neon Agent plan, Supabase for Platforms, Turso): Lightbulb pays Neon and bills the workspace itself. That is a different business (COGS, margins, abuse, dunning), so it should be a deliberate decision, not a fallback that creeps in.
3. If a second rail is ever added, scope it to the **free starter** only (Neon's sponsored org costs nothing; Supabase nano is by approval) and keep every paid resource on Stripe.
4. Claim/transfer is now the industry pattern (Neon, Prisma, Supabase, Vercel sandboxes): design the `provision` stage so a resource can be platform-owned first and handed over later; Stripe's `resources/link` is the adopt-existing-infra half of that.
5. Keep the rail interface narrow (provision, reveal, rotate, remove, limit) so a vendor-direct adapter can sit behind the same approval card if Stripe's private preview changes; the Stripe note already flags "The API contract can change."
6. Do not build on Vercel Marketplace or Heroku-style add-ons: they require the customer to live inside the host, the opposite of a room-scoped app.
7. Composio/Nango/Arcade/Vercel Connect answer the other question (an app reaching the customer's existing QuickBooks or Slack); they may pair with Apps for data access, never for provisioning.
8. Skyfire/Nevermined-style per-call payments are irrelevant to Apps today; revisit only if a provider Lightbulb needs is sold per call and absent from the Stripe catalog.
