# The Credential Problem in Lightbulb — Discovery Report

**Author:** Claude (research synthesis)
**Date:** 2026-05-21
**Status:** Problem-discovery report. Establishes the problem, current state, and acceptance criteria. Does **not** select a solution.

---

## 0. What this document is, and why it exists

A previous architecture brief jumped to vendor selection (Nango, Infisical) before establishing what problem we are actually solving. The result was a competent solution to *part of* the credential problem — end-customer SaaS OAuth — that ignored two other parts: how the platform's own agents (Jarvis) bootstrap themselves, and how marketplace sellers bundle agents that need credentials. This document fixes that mistake.

It does three things, in order:

1. **Maps the problem space.** Three credential subjects × four tool primitives × three personas × lifecycle phases. There is no single "credential problem" in Lightbulb — there are nine, and they don't share a vendor.
2. **Documents the current state with evidence.** Every claim about how things work today cites either the OpenClaw source (`/Users/prasiddhaparthsarthy/Desktop/openclaw`, HEAD `510628ca6f`), a verified competitor URL, or a passage from your Jarvis transcript. No hand-waving.
3. **States acceptance criteria.** What does "solved" mean for each persona? If a solution proposal can't be measured against these criteria, the criteria are wrong; if the proposal fails them, it isn't a solution.

The deliverable from this document is a clear problem statement that any future architecture brief can be evaluated against. Solution selection (Nango vs Klavis, Infisical vs Vault, wrapper vs proxy) is **explicitly out of scope** here. Those decisions should follow this document, not precede it.

---

## 1. The shortest possible statement of the problem

> Lightbulb wants to ship pre-built agents to non-technical buyers. Those agents need to act in third-party systems (Slack, Greenhouse, Google) under the buyer's identity, while the agent itself also needs its own identity for things the buyer doesn't own (its GitHub PAT, its AWS role). OpenClaw — the runtime Lightbulb builds on — was designed for one operator on one machine, has no concept of "another tenant," and stores credentials as inline JSON values or files on the operator's disk. Every credential path in OpenClaw today fails when "the operator" is no longer a single trusted human.

That single paragraph compresses ~85 hours of upstream design assumptions. The rest of this document unpacks why.

---

## 2. Scope

### In scope

- **Third-party SaaS credentials** the customer's agent needs (Slack, Greenhouse, Google Workspace acting as the customer, Salesforce, HubSpot, Notion, GitHub-as-customer, etc.)
- **Agent identity credentials** the agent uses to act as itself (its own GitHub PAT for repo work, its AWS IAM keys, its persistent Google refresh token if it has its own mailbox)
- **Platform-operator credentials** Lightbulb itself uses (the `admin@isol8.co` Google account, prod Stripe key, the OAuth `client_id`/`client_secret` Lightbulb registers at every provider so the consent screen says "Lightbulb")
- **The marketplace** install flow's role in provisioning all three
- **The four tool primitives** that consume these credentials: MCP servers, CLIs, plugins, skills
- **The lifecycle**: initial bootstrap, ongoing use, refresh, revocation, rotation, audit

### Out of scope

- **LLM provider auth.** OpenClaw's `auth.profiles` machinery handles this (verified: `src/agents/auth-profiles/`, `~/.openclaw/agents/<id>/agent/auth-profiles.json`). Out of scope per the user's existing prompt.
- **Static platform secrets already handled.** AWS Secrets Manager + Fernet-encrypted DDB for things like Stripe webhook signing keys. Out of scope.
- **Audit logging at the per-API-call level.** This is a "what does the agent do *with* the credential" problem, downstream of having the credential. Treat as a separate workstream.
- **Per-call OAuth scope enforcement.** Same — a refinement, not the base problem.

### Method

Three primary sources, all reproducible:

1. **The Jarvis transcript** the user supplied — a real session with their own engineering agent in a real Lightbulb ECS container, surfacing concrete failure points.
2. **The OpenClaw source** at HEAD `510628ca6f` (verified at `~/Desktop/openclaw`). All file paths and line numbers below are clickable in your editor.
3. **Competitor UX research** against ten platforms' public documentation (Anthropic Connectors, ChatGPT Apps, Lindy, Gumloop, n8n, Pipedream Connect, Zapier, Make, Anthropic MCP API, Workato). Cited inline.

---

## 3. The personas

Every credential decision is made by, for, or about one of four personas. The same word ("user") means different people in different contexts; this document keeps them distinct.

### 3.1 Platform operator — Lightbulb the company

- **Who:** the Lightbulb engineering team. Plausibly 1–10 people in v1.
- **Their credentials:** `admin@isol8.co` Google Workspace (for sign-ups, password resets, Jarvis's mailbox), root AWS account, Stripe platform key, Vercel team owner, GitHub org owner, and one OAuth `client_id`/`client_secret` *per integrated provider* (because Lightbulb must register its own OAuth apps at Google, Slack, Greenhouse, etc., so end-customer consent screens read "Lightbulb" — see §6).
- **Frequency of credential work:** initial bootstrap is intense (need to register apps at every provider once); steady state is rare (rotate yearly, respond to incidents).
- **Technical level:** high. This is the only persona we can assume reads documentation.

### 3.2 Marketplace seller — an agent author

- **Who:** anyone publishing an agent into the Lightbulb marketplace (the user's own team for seed agents Friday and Atlas; third parties later).
- **Their relationship to credentials:** they don't have any. Sellers ship an agent definition (tarball + manifest + plugins/MCPs/skills). They cannot embed credentials. They must declare credential *requirements* in the manifest so the install flow can provision them at the right tier.
- **Frequency of credential work:** per-agent, at authoring time. They need to know which provider names map to which env vars.
- **Technical level:** high (a developer publishing an agent). But they hate writing auth code and just want it handled.

### 3.3 End customer — the buyer

- **Who:** the person whose business is going to be helped by the agent. A small-business owner, an HR lead, a solo founder, an ops person at a 200-person company.
- **Their credentials:** their company's Slack, their Greenhouse, their Google Workspace, their Salesforce. They own these credentials; they should never type or paste them anywhere.
- **Frequency of credential work:** at deploy time per agent (connect Slack, connect Greenhouse), then never again until something expires.
- **Technical level:** zero assumed. This is the load-bearing persona for the entire product. If credential setup requires them to know what "OAuth client" means, the product fails.

### 3.4 The agent itself

- **Who:** Friday, Atlas, Jarvis, marketplace agents at runtime.
- **Their credentials:** all of the above, in their own ECS Fargate container, available as env vars or files at the right moment.
- **Frequency of credential work:** every spawn (MCP server boot), every tool call (CLI invocation), every long-lived session (token refresh mid-stream).
- **Technical level:** they read what's handed to them. They cannot run OAuth flows.

**Why this persona breakdown matters:** every existing OpenClaw credential mechanism conflates the platform operator and the end customer (or assumes they're the same person). That works for "one user on their own machine." It catastrophically fails when those are three different people who never meet.

---

## 4. The current state of OpenClaw — credential surfaces, verified

OpenClaw has **three orthogonal credential mechanisms**, all designed for one operator on one machine. This section catalogs each with source citations so we can reason about what we can extend, replace, or work around.

### 4.1 `auth.profiles` — LLM/model auth (out of scope, included for completeness)

- **Where it lives:** `~/.openclaw/agents/<agentId>/agent/auth-profiles.json` (resolution at `src/agents/auth-profiles/path-resolve.ts:12-15`).
- **Schema:** `ApiKeyCredential | TokenCredential | OAuthCredential` discriminated union (`src/agents/auth-profiles/types.ts:25-69`).
- **Setup:** `openclaw models auth login/add/paste-token/setup-token/order` (`src/cli/models-cli.ts:292`).
- **Multi-tenant aware?** No. Keyed by `agentId` only — multiple agents on one host can share, but there is no notion of separate tenants. Comment at `types.ts:60`: refresh tokens "are not portable by default" between agents.
- **Status for us:** existing prompt declares this out of scope. Leave it alone.

### 4.2 `mcp.servers[].env` — MCP environment variables

- **Schema:** `env?: Record<string, string|number|boolean>` (`src/config/zod-schema.ts:302-326`, types at `src/config/types.mcp.ts:1-21`). **Confirms issue #76493**: cannot hold SecretRef objects today.
- **Spawn delivery:** `src/agents/mcp-stdio-transport.ts:50-62`:
  ```ts
  const baseEnv = { ...getDefaultEnvironment(), ...this.serverParams.env };
  const child = spawn(preparedSpawn.command, preparedSpawn.args, { env: preparedSpawn.env, shell: false, ... });
  ```
  MCP children get the MCP SDK's curated `getDefaultEnvironment()` (PATH, USER, HOME, SHELL, LOGNAME, PWD, LANG, TERM) **plus** explicit `mcp.servers[].env`. **Full parent env is NOT inherited.**
- **Secret interpolation today:** there is a global config-load-time `${VAR_NAME}` pass at `src/config/env-substitution.ts` that substitutes env vars into any config string. So `"env": {"FOO": "${FOO}"}` accidentally works, but the schema doesn't *promise* it and there's no per-MCP-server documentation of the pattern.
- **Setup:** hand-edit `openclaw.json` or use `openclaw mcp set --command ... --env KEY=VALUE`. There is no `openclaw mcp auth` command.
- **Multi-tenant aware?** No. The MCP servers list is inline in one config file owned by one operator. Open issue #84294 explicitly notes: "bundle-mcp: support OAuth 2.1 client_credentials for remote MCP servers (currently limited to static bearer headers)."

### 4.3 Channel plugin credentials — per-plugin token resolution

Each channel plugin (Telegram, Discord, Slack, WhatsApp, etc.) implements its own credential resolution. Telegram is representative.

- **File:** `extensions/telegram/src/token.ts:101-204`.
- **Precedence (per account):** `accounts.<id>.tokenFile` → `accounts.<id>.botToken` → channel-level `tokenFile` → channel-level `botToken` → `process.env.TELEGRAM_BOT_TOKEN` (only for default account).
- **Setup:** `openclaw channels add --channel telegram` runs `telegramSetupAdapter` (`extensions/telegram/src/setup-core.ts:77-84`), which **prompts the user to message @BotFather in Telegram, copy the token, and paste it into the wizard.** That token then gets written inline into `openclaw.json` under `channels.telegram.accounts.<id>.botToken` or as a file path the user supplied.
- **Discord:** same shape minus `tokenFile` (`extensions/discord/src/token.ts:59-107`).
- **Multi-tenant aware?** No. Each plugin author wrote bespoke resolution; there is no central "channel credential" abstraction; tokens live inline in the operator's config file.

### 4.4 Skills credentials

- **Schema:** `SkillConfig = { enabled?: boolean; apiKey?: SecretInput; env?: Record<string, string>; config?: Record<string, unknown> }` (`src/config/types.skills.ts:3-8`).
- **`SecretInput = string | SecretRef`**, so skills *can* reference structured secrets through the resolver. But:
- **Setup:** there is no `openclaw skills auth` CLI. The user hand-edits `openclaw.json` to set `skills.entries.<key>.apiKey`.
- **Multi-tenant aware?** No, same single-config-file model.

### 4.5 `secrets.providers` — the SecretRef resolver

This is the closest OpenClaw comes to a generalized credential abstraction. It's also where PR #72548 lives.

- **`SecretRef`:** `{ source: "env" | "file" | "exec"; provider: string; id: string }` (`src/config/types.secrets.ts:12-16`).
- **Provider types built in:**
  - **Env** — `EnvSecretProviderConfig`, optional allowlist (`src/config/types.secrets.ts:255-265`).
  - **File** — `FileSecretProviderConfig` with JSON-pointer addressing inside files (`src/secrets/resolve.ts:358-408`). Requires absolute paths, no symlinks unless opted in, file must be owned by current uid.
  - **Exec** — `ExecSecretProviderConfig` shells out to a local binary expected to print `{protocolVersion: 1, values: {...}}` on stdout (`src/secrets/resolve.ts:426-538`).
- **PR #72548 status (verified live):** OPEN, no review decision, last updated 2026-05-20. Adds a fourth source via plugin SDK (`openclaw/plugin-sdk/secret-provider`). **Explicitly defers** opening `mcp.servers[].env`, `skills.entries.*.env`, `plugins.entries.*.config` to accept SecretRef objects.
- **Multi-tenant aware?** No. All three built-in sources assume "the operator's box" — `env` reads `process.env`, `file` requires file ownership match, `exec` runs a local binary.

### 4.6 OAuth credentials directory — `~/.openclaw/credentials/`

This is **only** for shared LLM-provider OAuth refresh tokens (Anthropic, OpenAI Codex login, Google AI login) when multiple agents on a host share them. **It is NOT a generic channel-credentials store** (despite a confusing line in the repo CLAUDE.md). Resolved at `src/config/paths.ts:267-292`. Out of scope per §2.

### 4.7 What the docs and issues explicitly say about multi-tenancy

This is the most important piece. The codebase contains *explicit, repeated* statements that multi-tenant deployment is not supported.

> *"By default, OpenClaw is a personal agent: one trusted operator boundary. Shared/multi-user setups require lock-down."* — `docs/start/onboarding.md:33-34`

> *"HTTP bearer auth here is not a narrow per-user scope model … There is no separate non-owner/per-user tool boundary on this endpoint; once a caller passes Gateway auth here, OpenClaw treats that caller as a trusted operator."* — `docs/gateway/openai-http-api.md:51-54`

> *"Exec approvals reduce accidental execution risk, but are not a per-user auth boundary."* — `docs/tools/exec-approvals.md:59`

And in the open issue tracker:

- **#60127** — "[Feature]: Multi-tenancy support (shared server + RBAC, not N instances)" — explicit user quote: *"we spin up a separate instance per team member just to keep workspaces isolated."* Tagged `needs-product-decision`.
- **#43903** — "Support multiple gateway tokens for multi-user session isolation."
- **#26370** — "Isolated cron scheduled jobs per agent (per-agent jobs.json) in multi-user deployments."
- **#71058** — "Support for multiple Azure/Teams bots on a single OpenClaw Gateway."
- **#76493** — "Allow SecretRef objects in `mcp.servers[].env` schema."

**The conclusion:** OpenClaw is *not* a hosted SaaS substrate today, and upstream knows this. Lightbulb cannot wait for the upstream gap to close; Lightbulb's value proposition is built directly atop it. Every credential mechanism above is single-tenant-by-design. Our job is to bolt on the missing tier without forking.

---

## 5. Current user experience — four annotated journeys

Each journey below is a story with a beginning, middle, and end. Each one ends in failure or excessive friction. These are the experiences Lightbulb must replace.

### Journey A — Jarvis bootstraps himself (verbatim from the user's transcript)

**Persona:** platform operator (Lightbulb team) creating their internal engineering agent.

**Steps actually observed in the user's transcript:**

1. User boots a new container running OpenClaw, names it Jarvis, opens the chat: *"Hey Jarvis, hows it going. I just created you and your purpose is to be my engineering agent."*
2. Jarvis introspects its container. `aws sts get-caller-identity` → `aws: not found`. AWS CLI is not installed in the OpenClaw image.
3. Jarvis introspects further: `gh` is installed but not authenticated. Git, Node available. Docker not (expected in Fargate). `AWS_DEFAULT_REGION=us-east-1` is set; task IAM role may exist but unverifiable without the AWS CLI.
4. Jarvis reports back: *"What I need from you: GitHub personal access token … Decision on AWS CLI install … Lightbulb repo URL."*
5. User asks the meta question: *"if I give you your own email would you be able to do it autonomously?"*
6. Jarvis's answer (verbatim): *"You'd still need to create an IAM user/role and give me the credentials … Most of these sign-ups require email verification (click a link). I'd need either: Access to the inbox (via the himalaya or gog skill I have), or You forward me the verification codes."*
7. User commits to `admin@isol8.co` as Jarvis's identity. Jarvis lays out the gog (Google) setup:
    > *"1. OAuth Client Credentials (you provide once). You need to create a Google Cloud OAuth client … Go to Google Cloud Console → APIs & Credentials → Create an OAuth 2.0 Client ID (Desktop app type) → Enable the Gmail, Calendar, and Drive APIs → Download the client_secret.json → Paste the contents to me here."*
8. User pushes back: *"This is a problem because a non technical person is not going to be able to this."*

**Where this breaks:**

- **Step 2-3** — there's no platform mechanism to ensure the right CLIs are present in the container image. Jarvis hits "AWS CLI not found" and has to ask. This is a container-build-time problem masquerading as a credential problem.
- **Step 4-6** — Jarvis can describe the OAuth dance but cannot execute it. Every step assumes a human who can leave the chat, click into Google Cloud Console, configure things, return, paste a result.
- **Step 7** — the "create an OAuth client" step is the wall. It is not optional. Every Google API access requires it. The same wall exists for Slack, for GitHub (creating an OAuth App or GitHub App), for Microsoft (Entra ID app registration), for Salesforce (Connected App). **OpenClaw has no answer for this step today.**
- **Step 8** — the user has identified the right problem. A non-technical person cannot complete this. But Jarvis is *not* a non-technical person's agent — Jarvis is the *platform team's* agent. The right question is "should the platform team have to do this for every single Lightbulb instance, or once?" (Answer: once. But the mechanism for "once and then re-used" doesn't exist either.)

**Acceptance criterion this journey produces:** Jarvis (and any platform-internal agent) should boot with its identity credentials already present in the container, refreshed automatically, never requiring a human to paste anything.

### Journey B — End customer deploys Atlas the recruiter

**Persona:** end customer, non-technical. Hypothetical but grounded in the user's stated product vision.

**Best-case scenario (what we want):**

1. Customer signs into Lightbulb, browses marketplace, picks Atlas.
2. Deploy page shows: *"Atlas needs to connect to your Greenhouse, your Slack, and your Google Calendar."*
3. Customer clicks "Connect Greenhouse" → Greenhouse OAuth page reads *"Lightbulb wants access to your Greenhouse"* → customer clicks Authorize → returns to Lightbulb → green checkmark.
4. Repeat for Slack, Google Calendar.
5. Customer clicks "Deploy" → Atlas is running in their container 30 seconds later, fully credentialed.
6. Customer never types a token, never opens Greenhouse's API console, never sees the word "OAuth."

**Today, with OpenClaw alone (what the customer would experience):**

1. There's no marketplace primitive — they'd be hand-editing `openclaw.json` to add Atlas's MCP server entries.
2. Greenhouse credential: Atlas's Greenhouse MCP server needs a token. The MCP server's env in `openclaw.json` is `string|number|boolean` only (verified, §4.2), so the customer must hand-type their Greenhouse API key as a literal string into the JSON. They have to know to go to Greenhouse → Settings → Dev Center → API Keys → "Generate New Key" and copy the result. This is the n8n experience documented in §6.5 — eleven steps, two products, four context-switches, must be a workspace admin.
3. Slack: same. Telegram-style channel setup wizard at best, with manual BotFather instructions.
4. Token rotation: when Greenhouse rotates their key in 90 days, Atlas silently fails until the customer notices, then repeats step 2.

**Acceptance criterion this journey produces:** end customers click one button per provider, see a consent screen branded "Lightbulb," and never touch tokens or API consoles. Token refresh is invisible.

### Journey C — Marketplace seller publishes Atlas

**Persona:** marketplace seller (could be the Lightbulb team for seed agents).

**Steps:**

1. Seller writes Atlas's agent definition: a workspace tarball, an `openclaw.json` config slice, a manifest listing the MCP servers and CLIs it bundles, a list of skills.
2. Atlas's Greenhouse MCP server needs a `GREENHOUSE_API_KEY`. Today, the seller has three options inside OpenClaw:
   - Hardcode the seller's own key into the tarball — destroys multi-tenant isolation, leaks the seller's credential to every buyer.
   - Leave the env value blank, document "buyers must set this themselves" — pushes Journey B's wall onto every buyer.
   - Use `${GREENHOUSE_API_KEY}` interpolation hoping the env var is set by *something* before OpenClaw loads — works by accident, no platform contract.
3. Atlas bundles a `gh` CLI for some operations. The seller assumes the buyer has authenticated `gh`; if not, Atlas fails opaquely.
4. Atlas bundles a custom CLI the seller wrote. That CLI reads `GREENHOUSE_API_KEY` from env. The seller has no way to declare this requirement in a machine-readable manifest — there's no Lightbulb manifest schema today, just OpenClaw's config schema.
5. Atlas is published. Buyer adopts. Buyer's Atlas container boots with nothing wired. Atlas tries to call Greenhouse, fails, surfaces a confusing error.

**Where this breaks:**

- **No marketplace manifest spec for credentials.** The seller cannot say "Atlas needs Greenhouse OAuth, Slack OAuth, a GitHub PAT for the buyer's repo, and a Google Calendar OAuth scope" in a way the platform can act on.
- **No credential-injection contract.** The seller's CLI reads env, but the platform has no documented promise to populate that env consistent with what the seller named in the manifest.
- **No bundling safety.** The seller could ship a tarball containing their own `xoxb-...` token; OpenClaw has no scanner to reject this. (The user has an existing `marketplace_safety.scan_tarball` step — this needs to learn about credentials.)

**Acceptance criterion this journey produces:** sellers declare credential requirements in the manifest using a documented vocabulary; the platform provisions them; sellers write zero auth code; bundles with hardcoded credentials are rejected at publish.

### Journey D — A credential rotates mid-stream

**Persona:** end customer, ambient.

**Scenario:** Customer connected Slack two weeks ago. Slack's `access_token` (the customer's, returned to Lightbulb by Slack's OAuth) is normally long-lived, but suppose it gets rotated by Slack's IT admin out-of-band. Or: customer connected Salesforce, and Salesforce's session-token-style auth expires hourly.

**What happens today on OpenClaw:**

1. Token sits in `openclaw.json` as a literal string (best case) or in a file referenced by `tokenFile` (worse case).
2. Atlas tries to call Slack, gets 401.
3. Atlas surfaces error to customer: "Slack action failed."
4. Customer doesn't know what to do. They go to the connections page (doesn't exist on plain OpenClaw), can't see the problem, opens a support ticket, problem festers.

**What good looks like:**

1. Refresh is automatic for refreshable tokens.
2. For non-refreshable revocations, customer sees a red badge on their Slack connection in Lightbulb's UI within minutes.
3. One-click reconnect → re-OAuth → Atlas resumes.
4. No support ticket, no waiting.

**Acceptance criterion this journey produces:** all token state is observable; refresh is invisible-when-possible; revocation surfaces a clear UI cue with a one-click recovery; agents do not silently degrade.

---

## 6. What good looks like — evidence from competitors

Documented competitor flows give us a calibrated sense of the design space. The full UX research is appended; this section extracts the four patterns that separate platforms that succeed at non-technical-user credential setup from platforms that don't.

### 6.1 The dividing line: who registered the OAuth client

| Platform | OAuth client registered by | Consent screen brand | Non-technical user can complete? |
|---|---|---|---|
| Zapier | Zapier | "Zapier wants access" | **Yes** |
| Lindy (top integrations) | Lindy | "Lindy wants access" | **Yes** |
| Gumloop | Gumloop | "Gumloop wants access" | **Yes (solo)** |
| Pipedream Connect (default) | Pipedream | "Pipedream wants access" | **Yes** |
| Make (native modules) | Make | "Make wants access" | **Yes** |
| Claude.ai directory connectors | Anthropic | "Claude wants access" | **Yes** |
| ChatGPT first-party connectors | OpenAI | "ChatGPT wants access" | **Yes** |
| **— DIVIDING LINE —** | | | |
| n8n self-hosted | End user themselves | "[your app] wants access" | **No** (11-step setup per provider) |
| Make (generic HTTP OAuth2) | End user themselves | "[your app] wants access" | **No** |
| Anthropic MCP API | Developer | Developer-named | **No** (not an end-user product) |
| Pipedream Connect (BYO OAuth) | Developer | Developer-named | Yes for end-user, but the dev pays the registration cost forever |

**The pattern is mechanical: if the platform has signed paperwork with Google/Slack/Microsoft and registered ONE OAuth client per provider, end-customers get a one-click "Connect" experience.** If the platform has NOT done this — and pushes registration onto each end-customer — the experience is "leave my product, go to Slack's API console, follow 11 steps, return with three secrets to paste."

**There is no third option.** Every platform doing one-click consent has Lightbulb-the-platform registering one OAuth app per provider, holding the `client_secret` server-side, and using it on behalf of every customer.

### 6.2 The brand-on-consent-screen tradeoff

When Google shows "Zapier wants access to your Gmail," users transfer trust to Zapier-the-brand. When the same screen says "Some App You've Never Heard Of wants access," users (rightly) get suspicious. Zapier *explicitly refuses* to white-label this screen even in their Powered-by-Zapier enterprise tier, because consent-screen brand is a forcing function for trust, not a vanity concern.

Pipedream learned this in reverse: they shipped BYO OAuth clients in June 2024 because enterprise customers wouldn't ship to their end-customers a product whose consent screens read "Pipedream wants access." Now Pipedream offers both: default-Pipedream-branded for fast onboarding, BYO-customer-branded for production.

**Implication for Lightbulb:** Lightbulb must register `client_id`/`client_secret` per provider for first-party-feeling consent. ("Lightbulb wants access to your Slack.") This is platform-tier infrastructure work, done once per provider, never per customer.

### 6.3 Marketplace templates work only when OAuth is platform-registered

Zapier templates ("Use this Zap") are a credential list disguised as a feature. The reason they work: each step's OAuth provider is already Zapier-registered, so the template adoption flow becomes "for each app in this Zap, click Connect or reuse your existing account."

The same flow on n8n (also has templates) requires the adopter to first complete the 11-step app-registration crawl *per provider* in the template, then run the template. **Templates without platform-registered OAuth are starter kits, not marketplaces.**

**Implication for Lightbulb:** the marketplace promise (one-click hire an agent) depends entirely on the credential layer's "one-click connect" promise. They are the same project.

### 6.4 The four UX failure modes documented in competitor research

1. **Forcing the user to leave the platform** to register an OAuth app, generate an API key, or accept a permission grant (n8n, Make HTTP, Anthropic MCP API).
2. **Consent screen branded by the wrong party** — confuses users about whose tool is asking for what (ChatGPT Apps SDK, Pipedream default for end-customer apps).
3. **Token expiry surfaces as a generic tool failure** rather than a connection-status warning (Lindy long-tail, Claude.ai pre-fix).
4. **Multi-account ambiguity** — user has two Gmails, the agent uses the wrong one (Gumloop team-credentials default footgun).

Lightbulb must avoid all four. The UX research has detailed examples of each in the appendix.

---

## 7. The end goal — acceptance criteria

Solutions will be evaluated against these. Each is testable.

### 7.1 For the end customer

1. **One-click connect per provider.** Customer clicks "Connect [Slack/Greenhouse/Google]" → sees consent screen branded "Lightbulb wants access" → clicks Authorize → green checkmark in Lightbulb.
2. **Never sees a token.** Customer never types, pastes, or even sees the string form of any credential.
3. **Never leaves Lightbulb to register anything.** No "go to Slack API console" step. Ever.
4. **One place to see connection status.** Customer can see all their connected providers, their freshness, and their connected accounts (if multiple) on one screen.
5. **Connection breakage is visible.** When a token expires/revokes, the customer sees a red badge before they see a tool failure. One-click reconnect.
6. **Disconnection works.** Customer can revoke any connection at any time. Affected agents stop trying.
7. **The marketplace deploy flow connects what's missing.** Deploying an agent that needs Greenhouse, and Greenhouse isn't yet connected, walks the customer through the connect flow as part of deploy — not as a separate prerequisite chore.

### 7.2 For the marketplace seller

8. **Manifest-declared credential requirements.** Seller writes `requires: [{provider: greenhouse}, {provider: slack, optional: true}]` and the platform handles the rest.
9. **Zero auth code in the bundle.** Seller's CLIs and MCP servers read standard env vars (e.g., `GREENHOUSE_API_KEY`) or use materializer files; never make OAuth calls themselves.
10. **Safety scanner rejects bundles** with hardcoded OAuth secrets, direct calls to OAuth endpoints, or undeclared provider env reads.
11. **Documented provider catalog.** Sellers can look up the canonical env var name for any supported provider in one place.

### 7.3 For the platform operator (Lightbulb team)

12. **One-time provider onboarding.** Onboarding a new provider to the platform — registering the OAuth app at the provider, storing `client_id`/`client_secret`, adding the catalog entry — happens once and benefits every customer thereafter.
13. **Platform-internal agents (Jarvis) bootstrap without per-deployment human action.** Jarvis's `admin@isol8.co` Google refresh token, GitHub PAT, AWS role are present in every Jarvis container at boot. Set up once at company founding, refreshed automatically.
14. **Auditable.** Every credential grant, revoke, refresh, and rotation is logged with timestamps, principals, and providers.
15. **Rotatable under incident.** Platform-wide rotation of the credential storage's master key, or any one provider's OAuth client, takes <2 hours of operator work.

### 7.4 For the agent at runtime

16. **Credentials at boot.** When OpenClaw starts in the customer's container, every tool the agent will invoke can find the credentials it needs in the place it expects them (env var named per the catalog, file at a documented path, etc.).
17. **Works for all four primitives.** MCP server, CLI, plugin, skill — same mechanism, no per-primitive special casing.
18. **Survives refresh.** Tools spawned after a token refresh see the new value. Long-running tools either refresh in place or restart cleanly.
19. **Multi-tenant blast radius is one customer.** Compromise of one customer's container exposes only that customer's credentials. Structurally — not by policy.

### 7.5 Non-goals (explicit)

- Not solving end-to-end audit at the API-call level (different workstream).
- Not solving cross-tenant credential sharing (e.g., a shared corporate Slack token across multiple Lightbulb-customer agents). Defer to v2.
- Not solving SCIM/enterprise SSO-driven auto-provisioning of customer connections.
- Not building our own OAuth implementation. We will adopt or buy.

---

## 8. The problem, decomposed

The "credential problem" is actually nine sub-problems, one per cell of this matrix.

### Three credential subjects (whose identity)

| Subject | Who provisions | Lifetime | Storage substrate (today) | Storage substrate (target) |
|---|---|---|---|---|
| **Platform** (Lightbulb) | Human Lightbulb employees, once | Years | AWS Secrets Manager | AWS Secrets Manager (unchanged) |
| **Agent identity** (Jarvis's own GitHub) | Human at agent-creation time, then refresh | Years; refresh tokens months | Nothing — has to live somewhere | TBD (candidate: a secrets vault) |
| **End-customer delegated** (customer's Slack) | Customer at deploy time | Refresh tokens months; access tokens hours-days | Nothing — has to live somewhere | TBD (candidate: an OAuth broker) |

### Three tool primitives × the lifecycle (where it ends up)

| Primitive | How it consumes credentials (verified) | Refresh handling needed |
|---|---|---|
| **MCP server** | spawn env (curated whitelist + explicit `mcp.servers[].env`) | restart on rotation |
| **CLI** | env vars OR config files OR keychain login state | tool-specific |
| **Plugin** | in-process via SecretRef resolver if used; else hand-rolled per-plugin (`extensions/telegram/src/token.ts` etc.) | in-process refresh |
| **Skill** | in-process via SecretInput on SkillConfig | in-process refresh |

### Three personas × the bootstrap moment (when it matters)

| Persona | Bootstrap event | Failure cost |
|---|---|---|
| Platform operator | Once per company founding | Manageable — they're technical |
| Marketplace seller | Once per agent publish | High — they leave the platform if it's hard |
| End customer | Once per agent deploy | Existential — they don't buy the product if it's hard |

**Each cell of the 3×4 matrix (3 subjects × 4 primitives) is a deliverable.** Some cells share a solution (e.g., "platform credentials reaching a CLI" and "platform credentials reaching a plugin" might both use the same env injection). Some cells are radically different (e.g., "end-customer Slack OAuth" vs "agent's own GitHub PAT" need different vendors).

Any solution proposal must:
- Identify which cells it covers
- Identify which cells it doesn't cover (and what does)
- Show that the covered cells, together with the uncovered cells, satisfy §7's acceptance criteria

---

## 9. Constraints (hard and soft)

### Hard

- **No fork of OpenClaw.** Build on the upstream image, add layers. Upstream PRs (#72548, #76493) may help eventually but cannot be on the critical path.
- **Self-hostable on AWS.** No SaaS in the critical path holds end-customer OAuth tokens.
- **License-compatible.** Apache 2.0 or MIT preferred. Elastic License 2.0 acceptable for infrastructure dependencies (carve-out allows embedding). AGPL/SSPL disqualifying.
- **Multi-tenant isolation must match the Paperclip pattern:** one shared service per env, application-layer isolation by `owner_id`.
- **Ships in weeks, not quarters.**
- **Works for all four primitives** (MCP, CLI, plugin, skill). Non-negotiable per existing user prompt.

### Soft (preferences, not blockers)

- Standardize on as few vendors as possible. Three vendors solving three different cells is fine; ten vendors is not.
- Prefer vendors with first-class machine-identity flows (AWS IAM auth on ECS task roles, etc.) over ones that require bootstrap secrets.
- Prefer Apache 2.0 over Elastic License 2.0 over MIT-SDK-with-closed-backend (Composio's posture).
- Prefer technologies with public production references at relevant scale.

### Architectural constraints surfaced by §4

- MCP children do **not** inherit arbitrary parent env (verified). Whatever delivers credentials to MCPs must either populate `mcp.servers[].env` explicitly, or wrap the MCP command, or push #76493 upstream first.
- OpenClaw's existing SecretRef resolver is local-disk-only (env/file/exec). It is *not* a remote-secret-store integration point — extending it requires either #72548 landing or our own plugin that doesn't depend on #72548.
- Channel plugins (Telegram, Discord, etc.) each implement their own credential resolution. We cannot retroactively standardize them without touching upstream. We can, however, populate the env vars they fall back to.

---

## 10. The solution space — categories, not vendors

Here we enumerate the *kinds* of solutions, with tradeoffs. Concrete vendor selection (Nango vs Klavis vs Composio; Infisical vs Vault) is deliberately deferred to a follow-on document, because vendor selection should be driven by which solution category we adopt.

### 10.1 For end-customer SaaS OAuth (the Slack/Greenhouse cell)

| Approach | What it is | Pros | Cons |
|---|---|---|---|
| **OAuth broker (managed token vault)** | A platform like Nango/Klavis/Composio handles the OAuth dance, stores tokens, refreshes them, exposes a per-customer fetch API | Production-ready; one-click connect; refresh handled; many providers built-in | Vendor dependency; license posture varies (Nango ELv2, Klavis Apache, Composio closed backend) |
| **Brokered HTTPS proxy** | All outbound HTTPS goes through a service that injects tokens; agent never sees tokens | Best blast-radius; built-in per-call audit | Breaks for non-HTTPS tools (CLIs that don't go through a proxy, gRPC, raw socket) — fails the four-primitive constraint |
| **Build our own** | OAuth dance + token vault as Lightbulb code | Full control; no vendor dependency | 6+ months of engineering; we'd be redoing what Nango/Klavis built |
| **Punt OAuth to the customer** | Customer registers OAuth apps themselves (n8n model) | Zero vendor dependency | Fails §7.1 acceptance criteria 1, 3; competitive UX disaster |

**Tradeoff:** OAuth broker is the only category that can satisfy §7.1 in weeks. Build-your-own is foreclosed by §9-hard ("ships in weeks"). Brokered proxy is foreclosed by §9-hard ("works for all four primitives"). Punt-to-customer is foreclosed by §7.1.

### 10.2 For agent-identity credentials (the Jarvis-GitHub-PAT cell)

| Approach | What it is | Pros | Cons |
|---|---|---|---|
| **Self-hosted secrets manager + machine identity** | Infisical or Vault stores agent credentials; ECS task assumes IAM role; sidecar fetches secrets at boot | Production-ready; machine-identity primitive matches Fargate exactly; supports dynamic secrets (per-tenant DB creds on demand) | Adds infrastructure (Postgres + Redis + service); operational complexity |
| **AWS Secrets Manager directly** | Just use AWS SSM/Secrets Manager — already in use for platform secrets | Zero new vendors; existing operations; AWS-native | No first-class agent-identity primitives; no UI for non-AWS people; harder to enforce per-tenant scoping; rotation is per-secret, not per-identity |
| **OAuth broker covers this too** | Stretch the OAuth broker to hold non-OAuth credentials | One fewer vendor | Wrong primitive; OAuth brokers are about per-end-user delegation, not per-agent identity; would be a category error |

**Tradeoff:** Secrets manager (Infisical-class) gives the cleanest fit but adds infrastructure. AWS Secrets Manager gives lowest ops cost but lacks the multi-tenant project/identity model. OAuth-broker-stretching fails the primitive shape.

### 10.3 For credential delivery to the four tool primitives (the runtime injection cell)

| Approach | What it is | Pros | Cons |
|---|---|---|---|
| **Wrapper shim per tool** | Every credential-needing tool is invoked through a small script that sources credentials at exec time | Primitive-agnostic; works today against current OpenClaw; we fully control it | All tools must go through the wrapper — minor inconvenience |
| **`mcp.servers[].env` interpolation** | Use OpenClaw's accidental `${VAR}` substitution to inject creds at config-load | Zero new mechanism | Not promised by schema; only fires at config-load (no refresh); doesn't help CLIs/plugins/skills |
| **Wait for upstream PRs** | Wait for #72548 + #76493 to land, then write a Nango SecretRef provider plugin | Most idiomatic | Timeline unknown (months+); doesn't ship Lightbulb in weeks |
| **Fork OpenClaw** | Patch credential injection in directly | Full control | Foreclosed by §9-hard |
| **Inject into `getDefaultEnvironment()` allowlist** | Modify the MCP SDK at the consumer level to allow more env through | Tempting | Breaks the MCP SDK's security model; not portable |

**Tradeoff:** wrapper shim is the only option that ships now, works for all four primitives, and doesn't fight upstream. It's also the least elegant — but elegance is not in the acceptance criteria.

### 10.4 For CLI standardization (a sub-problem of 10.3)

| Approach | What it is | Pros | Cons |
|---|---|---|---|
| **Wrap every CLI in our shim** | Treat heterogeneous CLIs as opaque binaries the shim populates env for | Works for any CLI (aws, gh, stripe, custom) | We don't get any structured introspection — CLIs are black boxes |
| **Adopt Printing Press for new CLIs** | Use Printing Press code-gen for first-party Lightbulb CLIs | Uniform env-var contract for our own CLIs | Doesn't help with third-party CLIs; not load-bearing |
| **Adopt OpenCLI spec** | Standardize CLI descriptions across the ecosystem | Future-facing | OpenCLI doesn't address auth at all (researched & verified); wrong layer |
| **Refuse CLIs in marketplace bundles** | Only allow MCP servers; no bundled CLIs | Smaller attack surface | User explicitly wants CLIs supported per existing constraints |

**Tradeoff:** wrapper-shim is the only approach that works for the current CLI ecosystem. Printing Press is interesting for *future* first-party CLIs but should not be load-bearing.

### 10.5 For Jarvis-style platform-internal agent bootstrap

| Approach | What it is | Pros | Cons |
|---|---|---|---|
| **One-time human OAuth setup per provider, then automated** | Human Lightbulb admin runs the Google Cloud Console steps once, drops `client_secret.json` and initial refresh token into the secrets manager, every Jarvis container reads it | Solves the Jarvis transcript pain | Only works for platform-internal agents — does not solve customer's experience |
| **OAuth-broker-with-Lightbulb-as-the-only-customer** | Treat Lightbulb-the-company as a single "customer" in the OAuth broker | Reuses the §10.1 mechanism | Conceptual contortion; mixes platform and customer credential subjects |
| **Pre-bake credentials into container image** | Encrypt and ship at build time | Simple | Rotation requires rebuild; image scanning may flag |

**Tradeoff:** the first option (one-time human setup → secrets manager) is the right shape. It only solves a slice of the larger problem, but that slice has a different solution from the customer-OAuth slice — they should not be conflated.

---

## 11. Open questions

These need to be resolved before solution selection.

1. **Does Lightbulb want to support BYO OAuth clients per customer?** Pipedream learned the hard way this was necessary for enterprise. Default position: ship platform-registered only in v1; add BYO in v2. Confirm.
2. **What's the SLA target for token-refresh latency?** Nango et al. don't publish SLAs. If we need <60s refresh for some provider, we'll need to layer our own freshness check.
3. **What's the marketplace seller's manifest format going to be?** OpenClaw has no marketplace concept upstream. We're inventing the manifest. Need a schema RFC.
4. **Where do connection-state events go?** Webhook from Nango → backend → DDB? Polling? Server-Sent Events to the customer UI?
5. **Does Jarvis count as a marketplace agent, or as a platform-internal special case?** Affects whether the platform-credentials path is exposed to non-Lightbulb sellers.
6. **What's the rollback story when a new provider integration ships broken?** Need a kill switch per provider.
7. **Multi-account-per-provider — v1 or v2?** Gumloop nails it; everyone else punts. Lightbulb's customers will eventually want "Atlas connects to my Sales team's Slack AND my Engineering team's Slack."
8. **Audit retention policy.** How long do we keep connection grant/revoke/refresh logs? Influences DB sizing.
9. **What is the failure mode for offline Nango (or chosen vendor) outage?** Need a runbook.
10. **Does the wrapper-shim approach satisfy the marketplace safety scanner's "no credential handling in seller code" rule?** Need to think through whether the shim being a wrapper makes seller-supplied scripts effectively co-credential-handlers.

---

## 12. What this report concludes

1. **The credential problem is three credential subjects × four tool primitives × three personas × lifecycle.** Not one problem, nine.
2. **OpenClaw is single-tenant by design**, upstream knows this, and the gap will not be closed by upstream in our timeline. We bolt on; we do not wait.
3. **The end customer's experience is the load-bearing acceptance criterion.** One-click connect with Lightbulb-branded consent screens. Everything else exists to support this.
4. **The platform must register OAuth clients at every supported provider.** This is not optional; it's the dividing line between "marketplace" and "starter kit." It's also a one-time platform-operator workflow, not a per-customer workflow.
5. **No single vendor solves all nine cells.** OAuth broker for customer-SaaS; secrets manager for agent-identity; AWS Secrets Manager for platform secrets; a wrapper shim for runtime delivery. Each has its own evaluation criteria.
6. **The Jarvis bootstrap problem is a different problem from the marketplace customer problem.** Conflating them led to confusion in the previous brief. They share infrastructure ideas (encrypted token storage) but have different personas, lifecycles, and acceptance criteria.
7. **Solution selection is the next document, not this one.** This document's job was to make the problem visible and testable. The acceptance criteria in §7 and the categories in §10 are the input to that next document.

---

## Appendix A — Sources

### Primary

- **Jarvis transcript** — supplied by the user, May 2026. Quoted verbatim in §5.A.
- **OpenClaw source** at `~/Desktop/openclaw`, HEAD `510628ca6f`, 2026-05-21. All file paths and line numbers cited inline; reproducible with the same checkout.
- **OpenClaw issues** verified live via `gh`:
  - [#71593](https://github.com/openclaw/openclaw/issues/71593) — SecretRef plugin parent issue
  - [#72548](https://github.com/openclaw/openclaw/pull/72548) — SecretRef plugin SDK seam (OPEN, no review decision)
  - [#82326](https://github.com/openclaw/openclaw/pull/82326) — Plugin manifest contract for SecretRef (OPEN, APPROVED)
  - [#76493](https://github.com/openclaw/openclaw/issues/76493) — SecretRef in `mcp.servers[].env`
  - [#60127](https://github.com/openclaw/openclaw/issues/60127) — Multi-tenancy parent issue
  - [#43903](https://github.com/openclaw/openclaw/issues/43903) — Multiple gateway tokens
  - [#26370](https://github.com/openclaw/openclaw/issues/26370) — Per-agent cron jobs
  - [#84294](https://github.com/openclaw/openclaw/issues/84294) — MCP OAuth 2.1 client_credentials

### Competitor UX research

- Anthropic Claude.ai Connectors: [claude.ai/settings/connectors](https://claude.ai/settings/connectors), [support.claude.com/en/articles/11176164](https://support.claude.com/en/articles/11176164-use-connectors-to-extend-claude-s-capabilities)
- Anthropic Managed Agents: [anthropic.com/engineering/managed-agents](https://www.anthropic.com/engineering/managed-agents)
- Anthropic MCP connector API: [platform.claude.com/docs/en/agents-and-tools/mcp-connector](https://platform.claude.com/docs/en/agents-and-tools/mcp-connector)
- OpenAI ChatGPT Apps / Connectors: [help.openai.com/.../10847137](https://help.openai.com/en/articles/10847137-chatgpt-apps-with-sync), [developers.openai.com/apps-sdk/build/auth](https://developers.openai.com/apps-sdk/build/auth)
- Lindy: [docs.lindy.ai/skills/popular-integrations/slack](https://docs.lindy.ai/skills/popular-integrations/slack)
- Gumloop: [docs.gumloop.com/core-concepts/credentials](https://docs.gumloop.com/core-concepts/credentials)
- n8n: [docs.n8n.io/integrations/builtin/credentials/slack/](https://docs.n8n.io/integrations/builtin/credentials/slack/)
- Pipedream Connect: [pipedream.com/docs/connect/managed-auth/quickstart](https://pipedream.com/docs/connect/managed-auth/quickstart), [pipedream.com/blog/bring-your-own-oauth-clients](https://pipedream.com/blog/bring-your-own-oauth-clients/)
- Zapier: [zapier.com/blog/solving-end-user-auth](https://zapier.com/blog/solving-end-user-auth/)
- Make: [help.make.com/connect-to-any-web-service-using-oauth-20](https://help.make.com/connect-to-any-web-service-using-oauth-20)
- Workato + Salesforce: [docs.workato.com/connectors/salesforce.html](https://docs.workato.com/connectors/salesforce.html)
- Sema4.ai env-injection precedent: [sema4.ai/docs/automation/control-room/vault](https://sema4.ai/docs/automation/control-room/vault)

### Tertiary references (cited for context, not load-bearing)

- Zapier consent-screen branding rationale: [zapier.com/blog/solving-end-user-auth](https://zapier.com/blog/solving-end-user-auth/)
- n8n credentials architecture deep-dive: [deepwiki.com/n8n-io/n8n/3.2-credentials-api-and-security](https://deepwiki.com/n8n-io/n8n/3.2-credentials-api-and-security)

## Appendix B — Glossary

| Term | Meaning in this document |
|---|---|
| **Platform operator** | Lightbulb the company / its engineering team |
| **Marketplace seller** | Author of a marketplace agent (Lightbulb team for seed agents, third parties later) |
| **End customer** | The buyer of a Lightbulb agent. Non-technical assumed. |
| **Agent** | An OpenClaw runtime instance dedicated to one end customer, possibly multiple per customer |
| **Platform-internal agent** | An agent serving the Lightbulb team itself, e.g., Jarvis. Not a customer's agent. |
| **OAuth broker** | A platform like Nango/Klavis/Composio that handles OAuth dance, stores tokens, exposes per-customer fetch API |
| **Secrets manager** | A platform like Infisical/Vault that stores arbitrary secrets, supports machine identity auth |
| **Tool primitive** | One of MCP server, CLI, plugin, skill — the four ways tools attach to an agent |
| **Wrapper shim** | Small launcher script that injects credentials at exec time before exec'ing the real tool |
| **Materializer** | A function that turns a credential into a file at a known path (for tools that need file-based auth) |
| **`SecretRef`** | OpenClaw's structured credential reference: `{source, provider, id}` |
| **`SecretInput`** | OpenClaw's "either string or SecretRef" type |
