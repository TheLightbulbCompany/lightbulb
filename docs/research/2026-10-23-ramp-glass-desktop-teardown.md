# Ramp Glass desktop — teardown (v0.9.3)

Date: 2026-10-23. Method: REA (`analyze_javascript_application`) + direct read of
the app's own source. Target: `/Applications/Glass.app` =
`com.ramp.glass.desktop` v0.9.3 (from `Glass-0.9.3-universal.dmg`).

## TL;DR

Glass's desktop download is a **thin Electron host**, not the product. The Glass
product (the agent, spend-management UI) is a **hosted web app** the shell loads
from `https://glass.ramp.com` (prod), with `glass-qa` / `glass-demo` variants.
The server-side agent engine is referred to throughout as **"Core."** What the
native host adds is the **agentic-browser substrate**: an embedded browser the
agent drives through a sandboxed Playwright API, plus the ability to import the
user's real Chrome logins into a server-side "cloud browser." It directly
parallels Lightbulb's own AgentCore Browser v2 + Mac agent-Chrome story.

Note: Glass ships its **own unminified TypeScript** in `app.asar` `/src` (41
files, ~5.8k lines, real names, doc comments). No decompilation was needed; REA
unpacked the asar and the source read straight. The giant dependency graph
overflowed the MCP response budget, so it's retained as a REA evidence bundle at
`scratchpad/glass-rea/evidence.json`.

## Stack (root `package.json`)

`@glass/desktop`, author Ramp, `main: src/main.ts`, ESM. Deps:
- `playwright-core` 1.58.0 — real browser automation over CDP
- `quickjs-emscripten` 0.32.0 — sandbox for agent-authored JS
- `@sentry/electron`, `electron-log` — crash/telemetry
- `tldts` — registrable-domain grouping for cookies
- `ws`, `zod`
Updates feed: `https://electron-updates.ramp.engineering` (Squirrel).

## What the native host actually does

### 1. Embedded "native browser" (`native-browser-manager.ts`, 558 ln)
A separate Electron session partition with `<webview>` guests: own user agent,
`file://` reads hard-denied for the whole partition, popup handling drawn into a
canvas slot, find-in-page, and a cross-window **owner-claim** fence
(`native-browser-owner.ts`). This is the browser the agent operates.

### 2. Browser Functions runtime (`browser-automation/`) — the core mechanism
The web app (via Core) ships JavaScript **programs** to the host over IPC
(`glass:native-browser:run-code`). Each runs in a **QuickJS sandbox**
(`quickjs-emscripten`, 64 MiB VM cap, 60 s deadline) inside an Electron
**utility process**, one per tab (`functions-runtime.ts`, `functions/host.mjs`,
`functions/runner.mjs` ~2.1k ln). The sandboxed program is handed a
Playwright-shaped API — `page.getByRole/getByText/getByLabel/fill/click/goto/
snapshot/waitForURL`, chainable `Locator`, `browser.tabs/newTab/selectTab`,
`expect().toBeVisible`, plus a `site` global of per-domain helper "function
packs." None of it touches the host directly: every call marshals over a
host-call bridge (`__browserCall`) to **Playwright** (`chromium.connectOverCDP`)
driving the real tab. So the chain is: agent-authored JS → QuickJS sandbox →
marshalled Playwright → the embedded tab. The result envelope
(`{ok, value, current_url, retry_safe, ...}`) is "the same envelope Core's
RunCode returns to the agent."

### 3. Chrome cookie / profile import (the notable capability)
`chrome-cookie-source.ts` (header: "derived from ramp/agents@440f6e9c…")
discovers the user's Chrome profiles, copies the cookie SQLite DB read-only
(`node:sqlite`), and decrypts cookie values with a **bundled native helper**
`Contents/Resources/native/glass_keychain` (`keychainBinaryPath`) — i.e. it
reads Chrome's Safe Storage key from the macOS Keychain. The user picks which
sites (`browser-cookie-sites.ts`; empty/malformed selection never means "all").
Selected cookies are then **X25519 + AES-256-GCM sealed** to a server-supplied
public key ("Core's profile key") and uploaded to
`POST /v1/browser-profile/cookie-import` (`browser-profile-upload.ts`, header:
"Session-bound upload of browser cookies to the user's Glass cloud browser").
**Purpose:** seed the server-side cloud browser with the user's real logins so
the agent can act as the signed-in user on third-party sites.
**Guardrail (`environments.ts`):** cookie import is allowed only to reviewed
hosted origins (`isTrustedBrowserCookieImportDestination`) — never a preview
build, "a preview runs unreviewed PR code and supplies the encryption key, so it
could decrypt a user's real browser logins."

### 4. Auth plumbing
Ramp sign-in via external-browser OAuth (`ramp-sign-in.ts`), connector OAuth
(`connector-auth.ts`), custom app-link schemes (`app-links.ts`), and **Okta
FastPass** (`okta-fastpass-permissions.ts`): grants `loopback-network` /
`local-network-access` and the `com-okta-authenticator:` launch, but only to
real Okta org URLs (`*.okta.com`, `oktapreview`, `okta-emea`, `okta-gov`,
`okta.mil`).

### 5. Permission / origin model (solid)
`glass-app-permissions.ts` is deny-by-default: only Glass's own origin gets
microphone (audio only), plus the Okta exceptions; everything else denied. Every
IPC handler is gated by `requireGlassSender` — sender must be the main window
**and** its frame origin must equal `appUrl.origin`. `app.enableSandbox()` on.

### 6. Standard desktop plumbing
Squirrel auto-update, Sentry, environment switcher (QA/Demo/Prod + preview links
over custom URL schemes), single-instance lock, tray/menus/notifications,
zoom/fullscreen IPC.

## Why it's relevant to us

Glass is solving the same problem as Lightbulb's agentic browser, a different
way. Lightbulb: cloud browser (AgentCore Browser v2) with workspace-shared
saved sign-ins, plus Mac agent-Chrome. Glass: **import the user's real Chrome
cookies → sealed upload → server-side cloud browser**, and drive it with a
QuickJS-sandboxed Playwright surface fed by programs the backend agent writes.
Worth comparing against our Mac-browser cookie handling and the "every session
is shared" sign-in model.

## Limits of this teardown

Desktop host only. The product logic (agent behaviour, prompts, spend features)
lives in the web app at `glass.ramp.com` and in "Core," neither of which is in
this binary. To see those, the next step is a runtime capture of the loaded web
app (`capture_electron_scenario` / `inspect_web_network_capture`) against a
signed-in session — which needs a Ramp login and should be a separate, explicit
step.

---

## AI spend management — how Glass does it

**Two different things both get called "AI spend management" at Ramp. Keep them apart.**

### A. Glass managing *the AI's own* spend (its novel angle)
Glass is an **agent workspace / IDE**, not a cards dashboard. The web client's
route model (chunk names) is `chat`/`sessions`/`spaces`/`canvas`/`library`/
`drafts` plus `automations`, `connector`/`plugins`, `models`, `policy`,
`limits`, `schedule`, `deployment`/`installation`. There is **no**
`transaction`/`card`/`bill`/`expense` route in the client — so Glass itself does
not render spend; it runs agents and governs what those agents cost.

How, from RE + Ramp's public description (public = medium confidence):
- Built on **Anthropic's Claude Agent SDK** (public); supports multiple models.
- **Ramp Router**: each agent request is routed to the cheapest adequate model.
  Corroborated by client chunks `models` + `model` + `router`-style routing.
- **Per-project cost meter + spend limits**: every project/session carries a
  live cost meter with a cap. Corroborated by the `limits` route chunk. This is
  the actual "spend management" lever — you budget an agent the way you budget a
  card.
- **Headless mode + scheduled automations**: Glass can run without a prompt as a
  local server and run scheduled jobs (e.g. "pull spend anomalies each morning,
  post to Slack"). Corroborated by `schedule`, `automations`, `deployment`,
  `installation` chunks.

### B. Agents managing *the company's* spend (Ramp's core product)
This is server-side ("Core") and Ramp's existing platform, reached by Glass
agents via **connectors/plugins/MCP** and via the **cloud browser** (see the
desktop teardown above — cookie import seeds the agent's browser with the user's
real logins so it can act on vendor sites). Public Ramp pieces describe:
- **Policy Agent**: reads the company expense policy, evaluates every card
  transaction and reimbursement, recommends approve / reject / escalate.
- **Agent cards**: tokenized virtual cards issued directly to AI agents via API
  or MCP, with Ramp's normal card controls/limits applied to the agent.

### The mechanism in one line
Glass = a Claude-Agent-SDK workspace where agents run inside **cost-metered,
spend-limited projects** (Router picks cheap-enough models), and those agents
act on real spend by driving a **cloud browser seeded with your logins** and by
calling Ramp/third-party systems through connectors — while server-side agents
(Policy Agent) and agent-issued virtual cards enforce policy on the money side.

### Confidence / evidence
- High (RE): desktop is a browser-automation + cookie-import shell; web client
  is an agent workspace with the route model above; no spend dashboard in-client.
- Medium (public blogs/press): Router, cost meter, Policy Agent, agent cards,
  Claude Agent SDK. The client chunk names corroborate Router/limits/automations
  but the decisioning lives server-side in Core and was not reverse-engineered.
- Not done (needs a Ramp login, deliberately not crossed): authenticated runtime
  capture of the live API to see the real request/response shapes and tool defs.

Sources: ramp.com/blog/ramp-agents-announcement, ramp.com/blog/ai-agent-spending-controls,
ramp.com/blog/ai-expense-management, blog.buildbetter.ai/what-is-ramp-glass.

---

## Access (2026-10-23)

- Desktop app installs and runs; `glass.ramp.com` is live (HTTP 200, public SPA).
- The client bundle ships `login`, `signup`, **and `waitlist`** route chunks, so
  there is a self-serve signup AND a waitlist gate — access may be gated. The SPA
  serves `index.html` for every path, so route probing can't tell which is active;
  confirming needs loading the page in a real browser (JS-rendered).
- Public coverage: Glass is built on Anthropic's Claude Agent SDK and the
  workspace itself is "now free." Not yet confirmed whether signup is open or
  invite/waitlisted. Creating an account is an outward action — not done without
  the owner.

## Lightbulb overlap + what's takeable

### Where we already match Glass (overlap)
- **Multi-agent workspace + chat — HIGH, we're arguably ahead.** Lightbulb is a
  multiplayer messaging workspace (rooms/channels/DMs shared by humans + AI
  employees); Glass is sessions/spaces. Same category; our multiplayer model is
  richer.
- **Cloud browser with saved logins — HIGH, near parity.** AgentCore Browser v2
  (shared sessions, workspace-shared sign-ins) + Mac agent-Chrome cookie/profile
  import is the same substrate Glass's cookie-import + cloud browser provides.
- **Managed inference with metering + caps — HIGH on plumbing.** The
  openrouter-gate already: validates `input.model` against a **`policy.models`
  list** (multi-model-ready), routes modality (`vision_model`), reserves each
  request at priced-ceiling × markup against the Metronome fast-gate, settles at
  real OpenRouter cost, and enforces daily caps. This is most of "Ramp Router"
  minus the routing decision.
- **Automations — MEDIUM.** Nightly CEO loop, feed routines, tasks-in-own-session
  vs Glass's headless scheduled automations.
- **Marketplace of agents + iOS app — we have, Glass doesn't.**

### Gaps (Glass has, we don't) and how takeable

1. **Ramp Router: multi-model "cheapest adequate" routing + per-project budget
   and live cost meter. → ADJACENT / takeable.** The gate already lists allowed
   models, meters, reserves and caps. Missing: (a) a selection step that picks the
   cheapest model good enough for the task, (b) a per-room/per-project budget +
   live meter surfaced to the user. ~80% of the infra exists. Highest-leverage
   win and it's on our own money path.

2. **The financial angle — split it in two, they are NOT the same lift:**
   - *Spend management FOR the AI* (budget/meter/limit what agents cost) = the
     same Router win above. **Takeable.**
   - *Spend management AS a fintech* — Policy Agent evaluating real card
     transactions (approve/reject/escalate) and **agent virtual cards** (tokenized
     cards issued to agents). This is Ramp's regulated moat: they are a card
     issuer with the transaction platform. Agent cards need card issuing (Stripe
     Issuing), KYC, compliance — **not a quick win.** A lighter slice is buildable:
     our Finance agent + the existing **read-only company-Stripe binding** could
     power a "spend policy advisor" that reads connected Stripe/accounting data and
     flags transactions against a policy the owner writes — decision support, not
     money control, no issuer lift. **Medium effort, real differentiation.**

3. **Headless/scheduled automations as a first-class surface. → EASY-ish.** We
   have the pieces (routines); productizing "schedule a job, run headless" is
   packaging, not new infra.

### The crux
Don't conflate the two "AI spend" meanings. **Metering/routing/budgeting agent
runs is our lane and mostly already built** (the gate) — lean in. **Issuing cards
and policing real transactions is Ramp's fintech moat** — expensive and
regulated; at most take the read-only "policy advisor" slice over connected
Stripe. The owner's instinct on Router is right and cheap; the card side is the
part to scope carefully before committing.

---

## Live Stripe test — 2026-10-23 (the blocker is cleared, proven)

Ran against the **live platform account** `acct_1TBLi5I54BysGS3r`, key from
Secrets Manager `lightbulb/prod/stripe_secret_key`, pinned to the gated version
**`2026-09-16.preview`** (the one Rami gave us in Slack). Reads only, plus
empty-body write probes that create nothing.

**All previously-500ing calls now succeed:**
| call | Sept 3 | now |
|---|---|---|
| GET eligibility | 200 | 200 (`is_eligible:false` — direct/platform mode, expected) |
| GET features | **500** | **200** (`uppsala.fabric.onboarding_rework`, `require_payment_method_for_all_resources`; min plugin 0.40.0) |
| GET identity | **500** | **200** (account_owner adenkim04@gmail.com) |
| GET catalog/providers, catalog/services | 200 | 200 |
| POST projects/prepare | **500** | **200 `{"is_eligible": true}`** |

**The re-flag worked.** Our account is eligible and the write path is live.

**But the API shape changed** under `2026-09-16.preview`, so our
`stripe_provisioning.py` (built on the old spec) is partly stale:
- Still present: `eligibility`, `features`, `identity`, `catalog/*`,
  `provider_connections`, `payment_profile`, `projects`, `resources`,
  `projects/prepare`.
- **Gone/renamed (404 now):** `account_requests`, `payment_method_requests`,
  `accounts`, `account_links`, `provider_accounts`. Account creation moved to
  Accounts v2 core + **embedded onboarding** (the `onboarding_rework` flag;
  Rami's "updated account creation with email verification OTP").

**So unblocking is real work, not a flag flip:** update the client to the new
surface (`projects`/`resources` centric), move company-account creation to
Accounts v2 + embedded onboarding (with the OTP flow — note our
[[otp-codes-browser-only]] ruling), validate, then flip
`STRIPE_PROVISIONING_ENABLED`. CLAUDE.md's "every state-changing call 500s …
leave the rail OFF" note is now empirically false and should be corrected with
this evidence.

## Path to Ramp parity — three workstreams

1. **Router (meter + route + budget).** Have: openrouter-gate (per-owner key,
   model-policy `models` list, price ceilings, per-request reservation × markup,
   real-cost settlement, daily caps, vision routing). Deltas: cheapest-adequate
   model selection; a per-room/project budget object + live meter in UI; expose
   model choice. Incremental, no Stripe.
2. **Provisioning rail (services for the company).** Now UNBLOCKED (above).
   Deltas: new-surface client + Accounts v2 embedded onboarding + enable flag.
3. **Real-money angle (the moat).** Fork, unresolved: **Link Agent Wallet**
   (consumer-funded, OAuth, budget-a-task, agent pays via shared payment token —
   Stripe pitched us, `github.com/stripe/link-cli`) vs **Issuing for agents**
   (we issue cards + real-time auth decisioning; program-manager + funding/float;
   preview access). Plus a Policy-Agent layer (read policy → approve/reject/
   escalate) over whichever rail.
