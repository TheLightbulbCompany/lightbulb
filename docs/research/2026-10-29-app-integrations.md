# Apps and the tools a company already uses (Shopify, HubSpot, Stripe, Google…)

Date: 2026-10-29. For: `docs/superpowers/specs/2026-10-28-apps-design.md` §4 ("Kinds of software"), answering
the owner's review note: *"I wonder how this changes based on the integrations and the stripe projects
stuff, for example what if they are using Shopify etc. I'm sure Lovable handles this already and maybe
Dyad too."*

Tags: **[P]** primary source (vendor docs, the vendor's own CLI output, or our code on `origin/main`),
**[S]** secondary (third-party write-up, forum, search summary), **[U]** unverified, or my inference.
Every web source was fetched on 2026-10-29. Lightbulb code was read with `git show origin/main:<path>`
at commit `0f10ae6b` (29 Oct 2026, 19:39 ET). The Stripe Projects catalog was read live with
`stripe projects catalog --json` (plugin v0.50.0, catalog `last_updated` 2026-10-30T00:34Z), read-only.

---

## 0. The answer in one page

1. **Every builder has two separate things; the design has one.** One is *services provisioned for the
   app* (database, hosting, email): our `services:` and Stripe Projects. The other is *tools the
   company already runs* (its Shopify, HubSpot, Stripe, Google Sheets): Lovable, Replit and Base44 call
   them **connectors**, Retool calls them **resources**. Nobody provisions these; they connect the
   account that exists. §4 needs an `integrations:` list beside `services:`.
2. **Runtime access has converged on a gateway.** Lovable apps call connectors through "a gateway
   between your app and the third-party service": "Gateway connectors keep all credentials on
   Lovable's side". The project holds only an opaque key [P]. Replit: "credentials and tokens are
   managed by the platform, not stored in individual apps" [P]. Base44 proxies workspace integrations
   [S]. The exception everywhere is a key the company makes for one app (Stripe secret, Resend), which
   is stored as a per-project secret for server code [P].
3. **Shopify.** Lovable, Replit and Stripe Projects all *create a new store* by default. Only Lovable
   documents *connecting an existing store*, by installing "the Lovable app" [P]. For a company already
   on Shopify, a Next.js server reads and writes with the **GraphQL Admin API**. The **Storefront
   API** is only for a custom storefront and cart. Admin-created custom apps (permanent `shpat_`) ended
   on 1 January 2026. Access now comes from Dev Dashboard apps: 24-hour client-credentials tokens in
   the merchant's own organisation, or OAuth with expiring offline tokens [P].
4. **Lightbulb holds most pieces, but in the agent's custody.** Of 22 providers on main, most are
   MCP-scoped OAuth refreshed in the container, which a server cannot reuse. The code says an Attio MCP
   grant "would retain something that 403s" for REST [P]. Shopify (paste), Microsoft 365
   (backend-refreshed), PostHog and the hidden `*_api` rows are what the backend could use. HubSpot and
   Airtable are absent.
5. **Recommendation:** `integrations:` with three modes and one card:
   - **gateway** (default): the app calls our gateway with its own app key, and the backend enforces a
     per-app grant (read-only by default, named operations, rate limit, audit);
   - **app_key**: credentials made for one app (Stripe restricted key, Resend key, Storefront token)
     go through the secret form into Secrets Manager under the app, then Vercel;
   - **provision**: a new resource through Stripe Projects, e.g. a *new* Shopify store.

   The person sees one grant card per app per tool ("crew-schedule wants to **read** your Shopify
   orders"). That card is a Lightbulb grant, not a second provider OAuth, because Shopify and HubSpot
   install one app per store or portal and do not issue per-app sub-tokens (§3.4).

---

## 1. How the builders connect a generated app to a company's existing tools

### 1.1 Lovable (the most complete model)

Lovable's integrations page sorts everything into these kinds [P]
https://docs.lovable.dev/integrations/introduction (2026-10-29):

| Kind | Examples | Who uses it | Whose account | Scope |
|---|---|---|---|---|
| **Shared app + chat connector** | Slack, Notion, HubSpot, Google Workspace, Google Sheets | The **published app at runtime** *and* the agent in chat | "Runs on one shared account" | Admins control which connectors the workspace offers; a builder links a connection to a project |
| **Personal chat connector (MCP)** | Notion docs, Linear tickets, Miro | **Build-time only**: "Lovable reads your own tool accounts as context"; "not part of the published app" | The builder's own | Personal |
| **App user connector** | Gmail, Salesforce | Runtime: "Each end user of your published app connects their own account, and the app acts with their permissions" | Each app visitor | Per end user |
| **Custom MCP server** | Internal CRM | Build-time chat context; admins can add MCP registries | | Workspace |
| **Custom connector (any REST API)** | Anything with an API | App and chat; "adds any REST API to the catalog for the whole workspace"; requests go through the connector gateway | Shared | Workspace, defined once by an admin |
| **Any API (direct)** | Anything else | Runtime; "Lovable writes the integration code for you"; the key is stored as a secret and used from an edge function (https://docs.lovable.dev/integrations/any-api) | Whoever's key | Project |

**Credentials and the gateway** [P] https://docs.lovable.dev/integrations/security (2026-10-29):
- "Most app + chat connectors route through a gateway": the app asks the gateway, the gateway
  authenticates with the stored credentials, fetches live data, and returns it. It also "handles OAuth
  and refreshes expiring tokens".
- "Gateway connectors keep all credentials on Lovable's side and do not expose them in the project."
  "Tokens are stored securely in the gateway and are not visible in project settings or accessible to
  workspace admins." Saved credentials cannot be read back by members, admins or Lovable.
- Each project gets two keys: `LOVABLE_API_KEY`, which identifies project and workspace, and a
  per-connection opaque key that "stands in for the connection's secrets". `LOVABLE_API_KEY` can be
  rotated per project, with up to an hour of overlap.
- Default limit: 1,000 requests per minute per connector per project. Deleting a connection breaks the
  apps that use it.
- Non-gateway connectors and secrets marked public (a browser publishable key) *are* visible in the
  project.

**Concrete connectors:**
- **HubSpot** [P] https://docs.lovable.dev/integrations/hubspot.md: "uses service keys for
  authentication, which support REST API requests only" (`pat-…`). Scopes are chosen when the key is
  created in HubSpot. The connection is private by default and can be shared to the workspace, after
  which it is "available across all projects in the workspace". Calls count toward the customer's
  HubSpot limits. Service keys "can't receive webhooks". Only admins and owners can connect it.
- **Stripe (bring your own)** [P] https://docs.lovable.dev/integrations/stripe.md: the user pastes an
  `sk_…` or `rk_…` into a "Connect Stripe" form in chat (no OAuth, no Connect). The form validates it
  against Stripe and saves it as backend secret `STRIPE_SECRET_KEY`, "never in your app's code, and it
  does not pass through the chat". Edge functions call Stripe with it. Webhooks are off by default
  (the app polls Stripe); subscription state is matched by email. "The key you provide determines the
  mode", so with a live key, "payments made from the preview are real". This is distinct from
  Lovable's *built-in* payments, where "Lovable creates and manages the payment provider account for
  you" (Paddle or Stripe; see `2026-10-29-lovable-ux-deep.md`).
- **Shopify**: §2.1.
- **Secrets form** (from `2026-10-29-lovable-ux-deep.md` [P] docs/features/secrets): the agent asks
  through a secure card with name, why, where to get it, and **Skip**. A key pasted into chat is
  rescued into a secret, and Lovable offers **Connect** to turn it into a connector.

**What Lovable gets right for us:** the split between build-time (personal MCP) and runtime (shared
connector) is explicit. Runtime access is one shared account behind a gateway. The app never holds the
provider token, and admins decide which connectors exist. **What it lacks:** the docs describe no
per-app scoping beyond the provider's own scopes. A shared HubSpot connection is "available across all
projects", so any app in the workspace gets the same access [P, same page]. Our per-app grant (§5)
is stricter than Lovable here.

### 1.2 Bolt and 1.3 v0

**Bolt** [P] https://support.bolt.new/llms.txt: integrations are GitHub, Figma, Stitch, Supabase,
Stripe, Netlify, Expo and Google SSO. There is **no business-tool catalog** (no HubSpot, Shopify or
Notion). Stripe: "Paste your test key into the modal"; "All payment processing happens server-side
through Supabase edge functions"; Bolt registers webhooks itself; **Retrieve my products** imports the
user's Stripe products [P] https://support.bolt.new/integrations/stripe.md. Other keys go into
per-project secrets read by server functions, which the agent asks for after coding [P]
https://support.bolt.new/bolt-cloud/database/secrets.md.

**v0** [P] https://vercel.com/changelog/vercel-marketplace-integrations-now-available-in-v0 (14 Mar
2025): Marketplace integrations (Upstash, Neon, Supabase) set "environment variables, available to
both Vercel and v0". These are *provisioned resources*, the same category as Stripe Projects. MCP
connections give the agent build-time tools [S] https://237.v0.build/docs/MCP. An existing tool
means code plus env vars set by hand. No Stripe or Shopify connector was found [U].

### 1.4 Replit

[P] https://docs.replit.com/replitai/integrations.md (2026-10-29) has four kinds:
- **Replit-managed:** Database, App Storage, Replit Auth, Domains.
- **Connectors:** OAuth "sign in once", e.g. Google Workspace, Notion, Linear, HubSpot, Stripe, Shopify.
  "Connections are tied to your Replit account and persist across all your apps."
- **External integrations:** API keys in the app's Secrets (OpenAI, Discord…).
- **Agent services:** paid APIs without keys, billed to Replit credits.

[P] https://docs.replit.com/replitai/managing-connectors.md: "Credentials and tokens are managed by the
platform, not stored in individual apps." Connections are **Personal** or **Workspace**. Admins "set up
a service once" and "builders use it across apps". Admins can "revoke or rotate access without code
changes". On Enterprise you can bring your own OAuth app, with callback
`https://replit.com/connectors/oauth/callback` [S, search summary of the same docs].

Runtime mechanism: the docs do not say. A forum thread notes `REPLIT_CONNECTORS_HOSTNAME` appearing in
production secrets of apps that use connectors [S]
https://replit.discourse.group/t/strange-new-secrets-auto-added-in-production/7062. That suggests the
deployed app fetches a short-lived token from a Replit connectors service rather than holding a
refresh token [U].

**Shopify on Replit** [P] https://docs.replit.com/features/integrations/shopify.md: the Agent creates a
development store "under Replit's Shopify Partner account", installs "the Replit Sales Channel app",
and connects it, "with no Connectors-panel setup and no credentials". "Each Shopify store is scoped to
a single app." Claiming happens in Shopify. Connecting an existing store is not documented [U].

### 1.5 Base44

[S, search summary of] https://docs.base44.com/Integrations/Using-integrations.md and
https://docs.base44.com/developers/backend/resources/connectors/shared-connectors (2026-10-29):
- Three paths: **built-in integrations** (Invoke LLM, SendEmail), **connectors** for OAuth tools, and
  **backend functions** that call any API with per-app **Secrets** (e.g. `STRIPE_SECRET_KEY`).
- Connectors hand backend code "a raw OAuth access token": `connectors.getConnection(<type>)` inside a
  backend function. Base44 runs the OAuth and stores credentials.
- **Shared connector:** "everyone who uses your app shares that connection". **App user connector:**
  each person connects their own; only owners and admins can set it up [P]
  https://docs.base44.com/Integrations/Connectors.md.
- Some connectors use Base44's OAuth client; others need the customer's own Client ID and secret, with
  callback `https://connectors.base44.com/api/oauth/callback` "for all of your apps" [P, same].
  Scopes are Read-only, Read & write or Custom, and "scope changes apply to new connections" [P].
- Workspace **custom OpenAPI integrations** are "proxied through Base44's backend, so credentials are
  never exposed to the frontend" [S].
- Listed connectors include Gmail, Outlook, Google Workspace and Calendar, Slack, GitHub, Snowflake and
  Databricks [P]. Shopify, HubSpot and Airtable are not on that page [U whether they exist].

Base44 hands a raw token to server code. Lovable keeps the token behind a gateway. Both keep it out
of the browser.

### 1.6 Dyad

From `docs/research/2026-10-29-dyad-source-deep.md` (source read at commit `0ee4a524`) [P]:
- **Backends:** Supabase and Neon only, through the agent tool `add_integration` (agent tools are FSL
  in `src/pro`). Provider OAuth goes through Dyad's relay `oauth.dyad.sh`. Tokens live on the user's
  machine in Electron `safeStorage` (`src/ipc/utils/secret_storage.ts`, Apache).
- **MCP:** Dyad is an MCP *client* for user-configured servers (stdio or HTTP, OAuth, encrypted
  secrets, consent per tool; `src/ipc/utils/mcp_manager.ts`). These are **build-time tools for the
  agent**. The build profile even excludes MCP servers: "no sub-agents, Engine tools, … or MCP
  servers" (`chat_stream_handlers.ts:3104-3106`).
- **Runtime env:** `.env.local` on the host and a push to Vercel (`vercel_neon_sync.ts`, with an
  "owned-key list" so it never deletes keys the user set). There is no runtime connector layer, no
  Shopify, and no business-tool catalog.

**Takeaway:** Dyad does *not* handle the Shopify case. It is a local desktop builder where the agent
holds the user's tokens, which is the model our design rules out. The useful parts are the owned-key
list for env pushes (already in the design) and the per-tool MCP consent idea.

### 1.7 Retool and Softr

Retool: "A resource is a set of user-configured properties that determine how Retool interacts with a
data source". It is defined once and referenced by name from every app, with access through
permission groups [P] https://docs.retool.com/data-sources/concepts/resources. OAuth credentials are
shared only if "Share OAuth2.0 credentials between users" is on [S]
https://docs.retool.com/data-sources/concepts/authentication. Softr's data sources (Airtable, HubSpot,
Salesforce, Notion, Google Sheets, SQL, REST…) make the company's existing tool the app's database [P]
https://docs.softr.io/data-sources. The internal-tool pattern is "define once in the workspace, the
server holds the credential, grant per app": a gateway.

### 1.8 Summary table

| Builder | Existing-tool catalog | Runtime path | Credential custody | Build-time agent access | Per-app scoping |
|---|---|---|---|---|---|
| Lovable | Yes, large (shared, personal, app-user, custom REST, MCP) | Gateway with opaque per-project key; or secret + edge function | Lovable gateway; per-project secrets | Personal MCP + shared connectors | Provider scopes only; shared connection reaches all projects [P] |
| Replit | Yes (OAuth connectors) | Platform-managed; apparently a connectors service [U] | Platform | Agent uses connectors | Workspace or personal connection |
| Base44 | Yes (OAuth connectors, OpenAPI) | Raw token given to the backend function; OpenAPI proxied | Platform | Agent builds flows | Shared or per-user; scope presets |
| Bolt | No (Stripe only) | Secret + server function | Project secrets | GitHub, Figma | n/a |
| v0 | No (Marketplace resources) | Env vars + code | Vercel env | Marketplace + MCP tools | n/a |
| Dyad | No (Supabase/Neon) | `.env.local` → Vercel | User's machine | MCP client | n/a |
| Retool | Yes (resources) | Retool server | Retool | n/a | Permission groups |
| Softr | Yes (data sources) | Softr server [U] | Softr [U] | n/a | n/a |

---

## 2. Shopify specifically

### 2.1 What Lovable's Shopify integration does

[P] https://docs.lovable.dev/integrations/shopify (2026-10-29):
- "Lovable supports both creating new Shopify stores and connecting existing Shopify stores."
- **New:** an automatically created development (sandbox) store with no Shopify cost that cannot take
  real payments. "Shopify removes unclaimed development stores after 30 days." Claiming makes the
  claiming user the owner and starts a 120-day trial on a Shopify plan. KYC and payments setup happen
  in Shopify Admin.
- **Existing:** "you choose it in Shopify and install the Lovable app". Shopify lists the stores your
  account can access. So it is an **installed Shopify app with OAuth on the merchant's store**, not a
  pasted token.
- **What the agent can do:** create, update and delete products, variants and discount codes, and
  fetch products to display. Batches are capped at 20 creates or updates and 50 deletes.
- **What the app does:** a storefront (product display, collections, filtering, reviews, wishlists,
  cart and "buy now"). "Shopify handles payments, inventory, and logistics." The public URL is the
  Lovable app, and the myshopify domain is "used just for your backend".
- "Shopify sessions last up to 24 hours", and Lovable prompts to reconnect. "Each collaborator
  connects their own Shopify account, and their permissions follow their Shopify role." Projects with
  an active Shopify connection cannot be remixed.
- **Which API:** the page does not say [U]. The features (products and cart on a custom front end,
  checkout on Shopify) match the **Storefront API** for the live site plus the **Admin API** for the
  agent's catalog edits. A third-party guide says the Storefront token is kept out of the browser
  through edge functions [S] https://www.rapidevelopers.com/md/lovable-integration/shopify.
- Lovable's Payments page says "Physical products can use the Shopify integration" (from
  `lovable-ux-deep.md` [P] docs/features/payments). **Shopify is Lovable's answer for physical
  e-commerce, not for back-office dashboards.** No Lovable doc describes an inventory dashboard or an
  orders report over an existing store [U].

### 2.2 Replit and Stripe Projects: both create a new store

- **Replit:** a development store under Replit's Partner account, the "Replit Sales Channel app",
  one store per app, claimed in Shopify (§1.4) [P].
- **Stripe Projects `shopify/store`** [P] live catalog plus https://setup.shopify.com/provider/llm_context.md
  (2026-10-29): "Launch your own online store with Shopify. Build your store for free for 4 months."
  Plans are `trial` (default; creation-only), `basic` ($39/mo after $1 for 3 months), `grow`
  ($105/mo) and `advanced` ($399/mo). Config: `store_name`, `plan`. Credentials synced:
  `SHOPIFY_SHOP_DOMAIN`, `SHOPIFY_SHOP_LOGIN_URL` and a short-lived `SHOPIFY_SIGNUP_TOKEN`, a "server
  secret" used to join the store as staff through the hidden `shopify store stripe-auth --store …
  --scopes … --signup …` (Shopify CLI ≥ 4.4.0). "Always rotate the Shopify resource before using it."
  "Only the store's current account owner can change the plan" (`not_account_owner`).
  - **This is a provisioned resource, not a connection to an existing store.** The service has no
    "link existing store" option. `stripe projects link <provider>` associates an existing *provider
    account* with the Stripe account [P] https://docs.stripe.com/projects. For Shopify, though, the
    only service is `store`, a new store [P, catalog].
  - The signup-token flow is designed for a **coding agent** that drives the Shopify CLI. It gives
    the *agent* staff access, which is exactly what our rule forbids for the builder. Using it would
    mean the backend mints the Admin token, never the builder [U, my reading].
- Also in the catalog under `ecommerce`: **Wix `headless`** ("let your customers purchase products and
  services through your Stripe-powered site") and **Squarespace `website`** [P, catalog].

### 2.3 What Shopify allows (October 2026)

- **Legacy admin-created custom apps (`shpat_`, permanent):** merchants can no longer create new ones
  since 1 January 2026. Existing ones keep working [S, several sources, e.g.
  https://help.shopify.com/en/manual/apps/managing-apps]. Our own `providers.yaml` records the same
  fact: "Shopify ended admin-created custom apps on 2026-01-01" [P].
- **Dev Dashboard apps** [P] https://shopify.dev/docs/apps/build/dev-dashboard/get-api-access-tokens:
  - *Client credentials grant:* Client ID and secret → Admin API token. "Only works when the app and
    the store belong to the same Shopify organization." The token lives 24 hours (`expires_in` 86399).
    "Don't send a scope parameter": the token carries whatever scopes the store granted the app.
  - *Authorization code grant* (for stores outside your organisation, via custom distribution or
    public app) and *token exchange* (embedded apps).
- **Expiring offline tokens** [P] https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens/index
  and https://shopify.dev/changelog/expiring-offline-access-tokens-required-for-all-public-apps-as-of-january-1-2027:
  1-hour access tokens and 90-day refresh tokens. Required for new public apps since 1 April 2026 and
  for all public apps by 1 January 2027. Custom and merchant-created apps are excluded.
- **Storefront API** [P] https://shopify.dev/docs/storefronts/headless/building-with-the-storefront-api/getting-started
  and https://shopify.dev/docs/api/usage/authentication: install the **Headless** channel, then
  **Create storefront** to get a *public* token (`X-Shopify-Storefront-Access-Token`, safe in a
  browser) and a *private* token (`Shopify-Storefront-Private-Token`, server). The limit is 100
  storefronts and tokens per shop, with permissions set per storefront. It covers products,
  collections, cart and checkout hand-off. It does **not** cover orders, inventory management or
  customers' admin data, which need the Admin API [P on scope; the "not" is my reading of the API's
  purpose].
- **Customer Account API**: buyer sign-in with OAuth and PKCE, for "my orders" pages [P, same auth page].
- **Hydrogen and Oxygen**: Shopify's own React framework and hosting for headless storefronts. It is an
  alternative to *our* Next.js-on-Vercel starter, so it is not needed. Our starter can call the
  Storefront API directly [S, general knowledge; not fetched].

### 2.4 The cleanest path for an agent-built Next.js app

| What the app does | API | Credential | Where it runs |
|---|---|---|---|
| Inventory dashboard, order report, low-stock alerts, fulfilment board (internal) | **GraphQL Admin API**, read scopes (`read_orders`, `read_products`, `read_inventory`) | Admin token from the store's installed app | Server route only, behind our door |
| Custom storefront or ordering site on the existing catalog | **Storefront API** | Public token in the browser; private token on the server | Browser and server; checkout hands off to Shopify |
| "Track my order" for customers | Customer Account API, or Admin read behind our door | Buyer OAuth, or the Admin token | Server |
| Writes (adjust inventory, tag orders, create discounts) | Admin API, write scopes | Admin token | Server, behind an owner-approved write grant |

For the Admin token there are two realistic sources:
1. **What Lightbulb already does for the agent (paste rail).** The owner creates a Dev Dashboard app
   in their own organisation and pastes shop, Client ID and Client secret. Something mints a 24-hour
   token by client credentials (`skills/shopify/token.sh`) [P]. The backend retains this credential in
   Secrets Manager (`acquisition: paste_token`, `refresh_owner: none`, so `_retains_secret()` is
   true; `apps/backend/routers/connections.py:59`) [P]. **A backend gateway can therefore mint Admin
   tokens for app requests today, with no new consent.** The weakness: the token carries every scope
   the owner granted that Dev Dashboard app (probably read and write), so per-app narrowing has to
   happen in our gateway.
2. **A Lightbulb Shopify app (OAuth install, custom or public distribution).** The owner clicks
   Install, picks the store and approves scopes, which is what Lovable's "install the Lovable app"
   does. We would need a Partner app, expiring offline tokens with refresh (the backend as the single
   refresher), and app review if public. It is better UX than three pasted fields, but it is work
   beyond Apps [U on review effort].

**Pushing a Shopify Admin credential into the app's Vercel env is the wrong default.** The client
secret is store-wide and long-lived, and a 24-hour token needs a refresher. Either way the app would
hold more than it needs, and a leak in a generated app would expose the whole store.

---

## 3. Lightbulb's side (read from `origin/main`)

### 3.1 The catalog

`apps/backend/core/services/credentials/providers.yaml` (2,392 lines) [P]. What matters for apps is
whether the **backend** holds a credential it could use (`_retains_secret()`,
`apps/backend/routers/connections.py:59`: true unless `refresh_owner: actor`, or when `retain_secret`
is set):

| Backend holds a usable credential | Agent-only (MCP-scoped and/or refreshed in the container) |
|---|---|
| **Shopify** (paste shop + Client ID + secret; skill `token.sh` mints 24-hour tokens) | **Stripe** (OAuth to `mcp.stripe.com`, scope `mcp`) |
| **Microsoft 365** (backend-refreshed, read-only Graph) | **Google** (`gog` CLI; EFS keyring is sole custodian) |
| PostHog (`retain_secret`, for HogQL rollups) | Notion, Linear, Intercom, Cloudflare, Attio, Apollo (MCP OAuth) |
| `attio_api`, `apollo_api` ("dashboard access" paste keys, `backend_only`, hidden in UI) | **QuickBooks** (Intuit allows one refresher; the container MCP server is it, "FORCED") |
| GitHub, Sentry, Meta Ads, Vercel, Parallel | X, Instagram (CLI) |

**HubSpot, Airtable, Salesforce, Square, Toast, Webflow, Resend and Twilio are not connectable today**
[P, absent from the file]. The **platform-info catalog** (`apps/backend/routers/platform_info.py`) is
a public projection of the registry (`key`, `display_name`, `byo_required`, `actor`, `requestable`).
The in-container skill curls it so agents never hard-code the list, and a builder can read it too [P].

### 3.2 How agents get credentials today

- **MCP rail:** OAuth tokens land in the container. The MCP server block goes into the agent profile,
  and the agent sees tools [P, `providers.yaml` header comments, `mcp_server.py`].
- **CLI and skill rail:** a materializer writes credentials into the profile (`gog_cli`, `gh_cli`,
  `shopify_cli` → `skills/shopify/credentials.json` plus `token.sh`, `qbo_dotenv`, `skill_cli`) [P,
  `materializers/`].
- **Per-turn broker:** `POST /runtime/credentials/u/{owner_id}`
  (`apps/backend/routers/runtime_rpc.py:31`) vends short-lived access tokens for backend-refreshed
  providers to the workspace's Hermes bridge as `MCP_<KEY>_ACCESS_TOKEN`, with `Cache-Control:
  no-store` [P]. **This is already a small gateway**: the backend holds the refresh grant and hands
  out only access tokens. It authenticates the *bridge* by hook token, not an app.
- **Backend-only Tier 2 keys:** the `*_api` rows exist because "a REST-capable key is a SEPARATE
  credential, and the owner grants it separately". The MCP grant "would retain something that 403s"
  for REST reads [P, `providers.yaml` comment above `attio_api`]. The frontend hides both rows because
  nothing uses them: "a second Attio/Apollo key the workspace would hold for dashboard reads nothing
  makes" (`apps/frontend/src/components/settings/ConnectionsList.tsx:39-43`) [P]. **Apps are the thing
  that would use them.**
- **Storage:** retained secrets live at `lightbulb/<env>/integrations/<owner>/<provider>` in Secrets
  Manager (`integration_secret_store.py`) [P].

### 3.3 Can a generated app use them?

- **Yes, through a backend gateway:** the left column of §3.1. Shopify's retained client credentials
  can mint Admin tokens today. Microsoft 365's broker already mints Graph tokens. The `*_api` rows give
  REST access to Attio and Apollo.
- **Stripe: not through the connection.** That connection is an MCP-scoped grant refreshed in the
  container. The company's **own** Stripe account sits on our Connect platform
  (`core/services/company_stripe.py`: "The company's own Stripe account on our Connect platform
  (Accounts v2)") [P], which is the better route (§4.2).
- **Google, Notion, Linear, QuickBooks: no.** Each needs a second, backend-held grant: the `*_api`
  "dashboard access" pattern. For QuickBooks it would be a second Intuit connection, because Intuit
  allows exactly one refresher per grant.
- **HubSpot and Airtable:** not connectable yet.

So reusing the workspace connection works for about a third of the catalog. The rest needs the
second-credential pattern the code already invented and the UI hides.

### 3.4 The three candidate models

1. **Reuse the workspace connection directly** (push its token into the app's Vercel env).
   - It is impossible for actor-refreshed providers.
   - For retained ones it puts a store-wide or account-wide secret in every app.
   - Pushing a refreshable token to Vercel creates a second refresher, which breaks rotating refresh
     tokens (QuickBooks, Microsoft) [P, the `refresh_owner` comments].
   - **Reject.**
2. **Per-app scoped tokens from a second provider OAuth consent** ("let crew-schedule read your Shopify
   orders"). Providers issue *one install per store or portal per client app*:
   - A Shopify app is installed on a store once, and a re-install updates its scopes. It does not mint
     a second token set per sub-app [P on install model; per-app sub-tokens are my inference from the
     OAuth design].
   - HubSpot service keys are per account, with scopes picked at creation [P, Lovable HubSpot page;
     https://developers.hubspot.com/changelog/service-keys].
   - Per-app provider tokens would need **one provider-side app per Lightbulb app**: one Dev Dashboard
     app or one HubSpot service key each. That is possible with paste rails but costs the owner a
     trip to the provider's admin per app.
   - **Allow as an option for the owner who insists, not the default.**
3. **Proxy through Lightbulb (gateway) with a per-app grant.**
   - One workspace credential, held by the backend, gives provider scopes that are a ceiling.
   - Each app gets a grant: provider, operations (read, write), named resources (e.g. `orders`,
     `products`, `inventoryLevels` for Shopify; `contacts`, `deals`, `tickets` for HubSpot), a rate
     limit and an audit log.
   - The app holds only `LIGHTBULB_APP_KEY`, as Lovable apps hold `LOVABLE_API_KEY` [P].
   - Revoking the grant or disconnecting the provider stops the app without touching code.
   - This matches every mature builder (§1.8) and our hard rule. **Recommend.**

The cost of (3) is that the app depends on our gateway's uptime, and we carry its egress. The app
already depends on us for the door (the 15-minute pass), so this is not a new class of dependency.
The gateway also fixes refresh-owner conflicts, because the backend stays the single refresher.

---

## 4. Stripe Projects catalog: new resource or existing tool?

### 4.1 Overlap with tools a business already uses

The live catalog has 70 providers [P, `stripe projects catalog --json`]. Sorted by whether a typical
small company *already has an account it wants used*:

| Already used for business (the company wants *its* account) | Usually new per app (we provision) | Both |
|---|---|---|
| **Stripe** (`stripe/livemode`, `stripe/sandbox`): a key on the Stripe account the project runs on; sandbox "requires an existing active sandbox" | Neon, Supabase, PlanetScale, Turso, Prisma, Upstash, Vercel, Netlify, Render, Railway, Fly.io, Cloudflare | **Resend, Twilio (email), Customer.io, Klaviyo**: a company may already send from its own domain |
| **Shopify** `store`: catalog creates a **new** store only | Clerk, Auth0, WorkOS, Neon Auth (we use our own door instead) | **PostHog, Mixpanel, Amplitude**: the company may already have a project and want the app's events there |
| **Squarespace** `website`, **Wix** `headless`, **WordPress.com** `site`: new sites | Algolia, Upstash Search, Inngest, Cloudinary, Chroma | **Datadog, Sentry** (Sentry is not in the catalog) |
| | AgentMail, AgentPhone, Quo, OpenRouter, ElevenLabs | **Cloudflare** (domains, DNS) |

Points worth noting:
- **Stripe Projects' Stripe key is for the account the project runs on.** Lightbulb runs Projects on
  the *services account* when the company connected an existing Stripe account by OAuth, and on the
  main account only "when the platform created it" (`projects_account_of`,
  `core/services/company_stripe.py`) [P]. So `stripe/livemode` would give a key on the **wrong
  account** for a restaurant that already takes payments on its own Stripe [U: what `livemode`
  returns on a services account was not tested]. Payments for the company's real customers should go
  through its real account, which Lightbulb already holds through Connect (§4.2).
- `stripe projects link <provider>` "associate[s] an existing provider account" and the docs example
  is "Link my existing Neon account" [P] https://docs.stripe.com/projects. This covers **existing
  infra accounts** (an existing Vercel team, Neon org, PostHog org, Resend account) on the same rail.
  It does not cover an existing *store* or *CRM*: those providers are not in the catalog, or only as
  "create new".
- Stripe Projects also has **project variables** (`stripe projects variables set <name> --env-key
  … --value …`) for "API keys you manage yourself" [P, same page]. It is a possible home for app keys,
  but the design already keeps app credentials in our Secrets Manager. Keep one vault [U, my call].

### 4.2 Stripe for a company's own customers: the Connect route

The company's merchant account is `acct_…` on Lightbulb's Connect platform (created v2 merchant, or
connected by Standard OAuth) [P, `company_stripe.py` docstring]. A generated ordering site can create
Checkout Sessions **on that account** in two ways:
- **Gateway route:** the app calls our gateway (`POST …/stripe/checkout-sessions`, constrained to the
  app's allowed prices and products), and the backend calls Stripe with the platform key and
  `Stripe-Account: acct_…`. The platform secret never leaves the backend [U: we would confirm the
  Connect capabilities for created v2 merchant accounts and Standard-connected accounts; direct
  charges with the `Stripe-Account` header are the standard Connect pattern].
- **App key route:** the owner creates a **restricted key** (`rk_…`) in their own Dashboard with only
  Checkout and Prices write, and pastes it in the secret form. It is stored under the app and pushed
  to Vercel, as Lovable and Bolt do with `STRIPE_SECRET_KEY` [P on Lovable's and Bolt's practice]. The
  webhook signing secret is handled the same way.

The gateway route is one tap for the owner. The key route works without any Connect work and is what
every other builder does. Both fit the hard rule.

### 4.3 How `app.yaml` should separate them

```yaml
# app.yaml (proposed)
services:            # NEW resources we provision through Stripe Projects, owned by the company
  - neon/postgres    #   preview + live (Appendix A)
  - vercel/project
  - resend/email     #   only if the app sends mail and the company has no sender of its own
integrations:        # EXISTING tools the company already uses; never provisioned by us
  - provider: shopify          # must be a key in providers.yaml
    via: gateway               # gateway | app_key | provision
    access: read               # read | write (write = a separate owner card)
    resources: [orders, products, inventory]
    why: "Low-stock board for the warehouse team"
  - provider: stripe
    via: gateway               # checkout on the company's own Connect account
    access: write
    resources: [checkout_sessions]
    why: "Take online orders"
```

Rules:
- `services` entries are catalog ids (`provider/service`). `integrations` entries are
  `providers.yaml` keys. The builder never confuses the two because the CLI validates each list
  against a different source: the Stripe catalog and `platform-info`.
- `via: provision` under `integrations` is the "you don't have one yet" fork. The builder asks, "Do
  you already sell on Shopify?" (the one allowed question card, design §5.1). If the answer is no, the
  integration becomes `services: [shopify/store]`.
- The CLI writes **no secret** into `app.yaml`. `lightbulb app up` resolves `integrations` into
  either `LIGHTBULB_APP_KEY` plus the gateway base URL, or app-key env names, pushed by the backend.

---

## 5. Recommendation for the design (§4 and Appendix A)

### 5.1 What §4 gains

Add a paragraph and a column. **"The company's tools come first."** Before choosing switches, the
builder reads which tools the workspace has connected (it can see its own connected integrations, and
`platform-info` lists what is connectable) and what the request names. Any matching tool becomes an
`integration`, and the app reads and writes *there*, not a copy. A new database is provisioned only for
data that has no home yet (the app's own notes, settings, or a join table).

| Kind | Default integrations if the company has the tool | Default switch if it doesn't |
|---|---|---|
| Landing page / content site | Webflow/Framer (edit there; already in §4); Shopify **Storefront** for a product grid | content |
| Form / intake | HubSpot/Attio (create contact), Google Sheets (append row), Slack (notify) | database |
| Internal tool on data | The system of record (Shopify Admin read, QuickBooks read, Attio, Airtable) via gateway | database + sheet import |
| Dashboard | Same, **read-only**; PostHog/Stripe for metrics | database if file data |
| Customer portal | CRM read scoped to the signed-in customer's record (HubSpot company → contacts, deals, tickets) | database + customer role |
| Product with payments | **Company's own Stripe** via Connect gateway; Shopify Storefront + checkout if they sell physical goods on Shopify | `stripe/sandbox` while building; payments switch |
| Automation | Whatever tool fires it; reuses the existing Automations trigger rail (`inbound:` in providers.yaml) | scheduled job |

### 5.2 Consent and credential flow

1. **Builder plans.** `integrations:` is drafted from the request and the workspace's connections.
2. **Missing provider connection.** If the provider has no app-usable workspace connection, one card
   goes to the owner: "To build this, Lightbulb needs Shopify access for apps. Connect in Connections
   (2 min)." It deep-links to the existing Connections row (Shopify paste fields today; a "for apps"
   OAuth row for Google or HubSpot later). This happens **once per workspace per provider**.
3. **Per-app grant card** (every app, every tool):
   > **crew-schedule** wants to **read** your **Shopify** orders and inventory.
   > Used for: low-stock board for the warehouse team. Never: change orders, see payment details.
   > [Allow] [Not now]

   Write access is a second, separate card that names the writes ("adjust inventory levels"). The
   design's rule "generated apps start read-only" (§5.7) becomes enforceable, because the gateway
   refuses writes the grant lacks.
4. **The backend** records the grant (`app_id`, `provider`, `access`, `resources`, `granted_by`,
   `granted_at`). It pushes only `LIGHTBULB_APP_KEY` and `LIGHTBULB_GATEWAY_URL` to Vercel. The
   builder sees env *names*, never values.
5. **Preview vs live.** The preview app hits the gateway with a preview key: reads are real, and
   writes are refused unless the owner turns on "test writes" (or the provider has a sandbox, e.g.
   Stripe test mode on the Connect account). This mirrors §10.5, where preview gets a copy of live
   data, not live writes.
6. **Revocation.** Canvas shows per app: "Reads Shopify orders · Revoke". Disconnecting Shopify in
   Connections lists the apps that will stop.

### 5.3 The gateway, minimally

- One backend route family: `POST /apps-gateway/{provider}/{operation}`, authenticated by the app key.
  It has a **named operation allowlist per provider**, not a raw pass-through. For Shopify that means
  a few GraphQL queries (`orders`, `products`, `inventoryLevels`) with the query text fixed by us or
  validated against an allowed-fields list. For HubSpot, the CRM search and get endpoints for allowed
  objects. A raw pass-through would let generated code use the full store-wide scope.
- It reuses `access_token.get(owner, provider)` and the Shopify client-credentials mint already in the
  repo. The backend stays the single refresher.
- Rate limits per app, an audit row per call, and no response caching of personal data beyond the
  request.
- Start with **Shopify, Stripe (Connect), Google Sheets and HubSpot**, the four in the owner's
  examples and the most common small-business systems. Add the rest when a room asks.

### 5.4 Three worked examples

**A. Shopify store owner: "build us an inventory dashboard".**
- The workspace already has Shopify connected (paste rail), and the backend holds a client
  credential. The builder writes `integrations: [{provider: shopify, via: gateway, access: read,
  resources: [products, inventory, orders]}]` and no `services` beyond Vercel. A dashboard needs no
  database unless it stores thresholds; if it does, it adds Neon.
- The person sees one card: "inventory-board wants to read your Shopify products, inventory and
  orders. Allow." Then a preview with their real SKUs and stock levels and one figure checked against
  Shopify admin, which is §4's done-when ("one figure checked against the source").
- If they later ask to "mark it reordered", the builder asks for write on `inventory` with a second
  card.
- If Shopify is not connected, they get one card to connect it (store domain, Client ID, Client
  secret from the Dev Dashboard; today's form). **Gap:** this is three pasted fields from a developer
  page. A Lightbulb Shopify app with an Install button (§2.4 option 2) is the fix worth scheduling.

**B. Agency: "a client portal over our HubSpot".**
- HubSpot is not in `providers.yaml`. Add a `hubspot_api` row on the paste rail: a **service key**
  (`pat-…`), scopes chosen by the owner, `backend_only`. That is the same shape as `attio_api`,
  validated by a minimal CRM read. Service keys are HubSpot's successor to private apps (beta; "can't
  receive webhooks") [P] https://developers.hubspot.com/changelog/service-keys.
- `integrations: [{provider: hubspot, via: gateway, access: read, resources: [companies, contacts,
  deals, tickets]}]`. The portal's door gives each client a **customer role tied to a HubSpot company
  id**. The gateway enforces "only records associated with company X" for that app key and session.
  This is the portal kind's done-when ("a customer sees only their rows"), enforced server-side rather
  than trusted to generated code.
- **Opening a portal to outside clients** needs the design's external-sharing option (the owner's
  note in §1) plus a sign-in for non-workspace people. That is a separate open question; flag it.

**C. Restaurant: "an ordering site where people pay with Stripe".**
- The company's Stripe is on our Connect platform. `integrations: [{provider: stripe, via: gateway,
  access: write, resources: [checkout_sessions]}]` and `services: [neon/postgres, vercel/project]`
  for the menu and orders.
- The person sees one card: "order-site will take card payments into **your** Stripe account (Joe's
  Diner, acct …1234). Allow." The preview uses Stripe **test mode**, the done-when is "a test checkout
  completes", and going live is the existing publish step.
- If the restaurant has no Stripe yet, the first-time Stripe form already in the design (§3) creates
  it. If it runs Square or Toast for its POS, those are not connectable today. The builder says so and
  takes orders into the app's own database, plus an email or Slack ping, until a room asks for the
  integration.

### 5.5 What the person sees (design §3 table additions)

| Moment | What they do | What they see |
|---|---|---|
| Ask | "inventory dashboard from our Shopify" | The builder says it will use their Shopify (named), not a copy |
| Tool access (new) | One tap per tool | "*app* wants to **read** your *tool* *things*. Used for … Never …" Allow / Not now |
| Tool missing (new) | Connect once | Deep link to Connections for that tool; the builder waits, then continues |
| Later | Canvas | Each app lists the tools it reads or writes, with Revoke |

### 5.6 Open questions for the owner

1. **Gateway vs app key as the default for Stripe payments.** The gateway is one tap and uses the
   Connect account we already hold. The restricted key is what Lovable and Bolt do, and it needs no
   Connect work.
2. **A Lightbulb Shopify app (Install button) vs the paste rail.** It matters because the dashboard
   example is the owner's own first case.
3. **Second Google grant for apps (backend-refreshed Sheets read and append)**, since `gog` owns the
   current one. Sheets is the most common "existing tool" for small companies [U on frequency].
4. **Per-user connectors** (Lovable's "app user connector", e.g. each salesperson's own Gmail):
   **later**. Nothing in the owner's examples needs it.

---

## Appendix: sources

Every source is cited inline with its URL or `origin/main` path; all fetched or read on 2026-10-29.
