# How the agent works through an idea, and what it does when something is missing

Date: 2026-10-15. Code read only. Sources: the agent's skill references (`apps/infra/hermes/skills-src/lightbulb/references/{ideas,integrations,boundaries,money,email,surfaces}.md`), the plugin tools (`apps/infra/hermes/plugins/lightbulb/__init__.py`), `docs/research/2026-10-15-ideas-execution-signins.md` and `2026-10-15-ideas-capability-map.md`. The Paperclip upgrade and parallel runs belong to another track and are not covered here.

## 0. The rule in one line

An idea is a request. The agent does the next step it can do, asks **once** for everything it cannot do, in **one place**, and never pretends a step happened.

## 1. The same five moves for every idea

1. **Read the request and the facts.** The DM message carries the title, the why and the three deliverables. `~/.openclaw/lightbulb-facts.json` lists `connected_integrations` (names and rails, never credentials). The agent never recites the catalog from memory; it reads `GET /api/v1/platform-info` live.
2. **Pre-flight.** Compare what the idea needs with what is connected. If something is missing, ask for all of it in one go, before any work. (Today nothing lists an idea's needs, so the agent discovers gaps one at a time, mid-run.)
3. **Work in units.** One invoice, one directory, one PR, one email. Each unit is a child task or a line on the task, with proof. After each unit, a one-line "next action" comment, so the next run (there will be one: turns are 30 minutes) picks up where this one stopped.
4. **Side effects get an approval first.** Sending, publishing, paying, submitting, deleting: `request_approval` puts a card in Needs you; approving it wakes the agent with the answer. Never a question in chat for a decision.
5. **Done means proof.** Links to the things that now exist. Then `done`.

## 2. Seven kinds of blocker, and the agent's move for each

| # | Blocker | Example | What exists today | What the agent should do | Gap |
|---|---|---|---|---|---|
| 1 | **Service not connected, but in the catalog** | Gmail, Stripe, QuickBooks, GitHub not yet connected | Boundaries table: say "Open Connections and click Connect next to <service>; I'll take it from there." `lightbulb-connect.sh link <provider>` mints the one-click link. The agent cannot connect anything itself. | Ask once, for every missing service at the same time, as a Needs-you item that names each service and why. Do the parts that need nothing in the meantime (research, drafts). | **Connecting does not wake the agent.** Nothing in `routers/connections.py` posts or wakes. "I'll take it from there" is only true if the person comes back and says so. |
| 2 | **Connected, but read-only or a scope is missing** | Microsoft 365 (no send, no event create), Shopify writes, Linear, Apollo; a Google API not enabled | `integrations.md`: for M365 "say so rather than trying"; for Google quote the error and say exactly what to enable, before any workaround. Shopify: a mutation needs the owner's confirm in that conversation. | Do the read half. Produce the write half as ready-to-use text (the six replies, the calendar invite details, the listing copy) and hand that step to the person, or ask for the scope (Shopify: edit the app's scopes and reinstall). | No idea says up front whether it needs a write. An Outlook owner starts "inbox triage" and finds out at the first send. |
| 3 | **No connector exists** | LinkedIn, Google Ads, a registrar, Product Hunt, job boards, Klaviyo, Google Business Profile, HubSpot | Nothing in the skill covers this case. The browser and the vault exist. | Web app with a UI: use the shared browser; ask the person to sign in once in the Browser panel (`browser_vault_save_login` if a password is wanted). No UI (phone, postal, a sealed app): prepare everything (script, text, amounts) and hand the step to the person as a task. | The agent improvises. It may try a browser route the site will block, or claim the step is done. |
| 4 | **Sign-in, code, CAPTCHA** | Intuit 2FA, LinkedIn "unusual login", a directory's CAPTCHA | Vault fill (`browser_vault_fill`), secure form for a new login, `browser_vault_enter_code` sends a person to the Browser panel and waits up to 390 s. Owner ruling 2026-10-07: codes and CAPTCHAs are typed by a person in the live browser, never in chat. | Call the vault tool; for a CAPTCHA post the line and end the turn; continue from a fresh snapshot on the next wake. | A Paperclip turn passes no room to the code ask even when it runs inside the bound DM (`turn.py:3804`), so the "go to the Browser panel" line reaches nobody (`no_room`). Delegated tasks have no binding at all. Verification emails with `Auto-Submitted` are dropped before storage. |
| 5 | **Money, identity, legal, phone** | Domain purchase, state tax portal, ad budget, a phone-only vendor, signing | `request_approval` for spend and sign-offs; Stripe Projects request for paid infrastructure (`money.md`); the owner's card in Activity. No phone tool. | Prepare everything, then one Needs-you item with the exact action, amount and link. For phone: the number, the script and what to ask for, as a task for the person. Never a workaround. | None in the mechanism. The gap is that nine ideas are mostly this kind of step and are offered like the others. |
| 6 | **The box cannot** | No local Chromium for a page scan; no bank-reconcile tool in the QuickBooks MCP; 50 mails a day; ~4 GB disk; 30-minute turns | `surfaces.md` and `platform-primitives.md` state the limits; `npx` can fetch tools; the AgentCore browser can load pages. | Use what is there (scan through the AgentCore browser; `npx` a crawler), split sends across days, keep work in units so a 30-minute turn is never a loss. Say what cannot be done. | The agent is not told the 30-minute budget or the daily mail cap in the Ideas instruction. |
| 7 | **Information missing** | Which help docs, the investor list, voice samples for posts, the billing calendar | Ask in the DM (that is how Muse works: it asks only when it needs to). | One message with every question, then wait. Never one question per turn. | None. |

Where the ask goes, always: **Needs you** for decisions, connects and person-steps; **the DM** for information; **the Browser panel** for codes and sign-ins; the `secrets` tool for any key. Never chat for a secret.

## 3. The 35 ideas by family: the path, the units, and where it stops

Verdict key: **Now** = runs with what exists once the services are connected. **Half** = the agent does the first half, a person or a missing write does the second. **Person** = mostly person steps; the agent prepares and tracks.

### A. Mail and calendar (Google)

Path: read with `gog` (Gmail, Calendar, Drive), draft, approve, send, log. Units: one email or one slot. Stops: Gmail not connected (1); Outlook owner (2, no send); first-contact sends need approval (5).

| Idea | Needs, in order | First stop | Verdict |
|---|---|---|---|
| inbox-triage | Google | Connect Google (BYO Cloud app); approve the first sends | Now |
| answer-leads-fast | Google, mailbox | Connect Google; approve the reply template | Now |
| answer-support-email | Google, Notion or Intercom | Owner points at the help docs | Now |
| interview-scheduling | Google Calendar + Gmail | Owner chooses the times | Now |
| meeting-recaps | a calendar invite to agents@, Meet bot | A member sends the invite; Zoom/Teams not supported | Now (Meet only) |
| monthly-receipts-package | Google, QuickBooks | Vendor-portal receipts need a sign-in (4) | Now |
| three-quotes-book-vendor | Parallel search, mailbox, Google | Phone-only vendors (5); owner picks | Now, Half for phone vendors |

Outlook owner: every row becomes Half (reads work; each send is handed over as text).

### B. Money (QuickBooks and Stripe)

Path: read invoices and charges, draft, approve, `send_invoice` or email with the pay link, match payments. Units: one invoice, one claim, one bill. Stops: QuickBooks or Stripe not connected (1); bank feed and portals need sign-in (4); phone-only vendors (5).

| Idea | Needs | First stop | Verdict |
|---|---|---|---|
| chase-overdue-invoices | QuickBooks, Stripe, Google | Approve each reminder | Now |
| send-invoices-on-time | QuickBooks, Stripe | Approve amounts | Now |
| claim-refund | Stripe, QuickBooks, Google, browser | Approve the claim; portal forms (4) | Now |
| cancel-or-renegotiate-bill | QuickBooks, Stripe, Google, browser | Vendor portal sign-in (4); phone-only vendor (5) | Half |
| unused-software-seats | QuickBooks, Stripe, Google, browser | Sign in to each tool's admin (4); approve each removal | Half |
| investor-update | Stripe, QuickBooks, Drive, Gmail | Approve numbers and text | Now (Sheets support not confirmed) |
| books-cleanup | QuickBooks, Google, browser | Bank feed is Intuit identity (4); no reconcile tool (6) | Half |
| sales-tax-registration | Stripe, Shopify, QuickBooks, browser | Identity checks, EIN/SSN, fees (5) | Person (the nexus analysis is Now) |

### C. Code (GitHub, Vercel, Stripe)

Path: clone, change, open one PR, prove it (CI, before/after). Units: one PR, one page, one dependency. Stops: GitHub not connected (1); site not in a repo (6); merge is the person's (5); DNS at a registrar we cannot reach (3).

| Idea | Needs | First stop | Verdict |
|---|---|---|---|
| seo-audit-fixes | GitHub, browser | Merge the PR | Now |
| dependency-prs | GitHub | Approve every merge | Now |
| stripe-subscriptions | GitHub, Stripe | Merge; switch on live keys | Now |
| accessibility-fixes | GitHub, browser | Scan needs a deployed preview (6); merge | Now, with a scan recipe |
| landing-page-checkout | GitHub, Vercel, Stripe, PostHog, Cloudflare | DNS write not confirmed (3); approve words and publish | Half |
| website-rebuild | GitHub, Vercel, Cloudflare, browser | DNS at the registrar (3); approve words | Half |
| soc2-readiness | GitHub, AWS, Google Docs, browser | Compliance platform signup and billing (5); auditor | Person (policies and code fixes are Now) |

### D. Store (Shopify)

Path: read the store with the skill; writes need the owner's confirm and write scopes on the owner's app. Stops: Shopify writes (2); flow editors are click-heavy (3); payments KYC (5).

| Idea | Needs | First stop | Verdict |
|---|---|---|---|
| product-listings-rewrite | Shopify write, Parallel | Grant write scopes; confirm the first 5 | Half until write scopes |
| shopify-store-setup | Shopify write, browser | Create the app with write scopes; payments KYC | Half |
| email-flows | Shopify read, browser (Klaviyo or Shopify Email), Cloudflare | Store tool sign-in (3/4); approve each email | Half |
| meta-ads-setup | Meta Ads (hidden in prod), Shopify, GitHub | Connector not available in prod (1); Business verification (5) | Person |

### E. Web with no API (the shared browser)

Path: the person signs in once in the Browser panel; the agent works the site with snapshots; a person types every code and CAPTCHA. Units: one directory, one post, one candidate. Stops: no connector (3), CAPTCHA (4), payments and verification (5).

| Idea | Needs | First stop | Verdict |
|---|---|---|---|
| directory-listings | browser, mailbox | CAPTCHAs; Google Business verification (postcard, phone or video) | Half |
| local-listings-reviews | browser, a customer list | Verification video (5); CAPTCHAs | Person |
| linkedin-ghostwriting | Google (read writing), browser (LinkedIn) | LinkedIn sign-in (4); approve each post | Half (drafting is Now; publishing is the risk) |
| launch-run | X, Google, browser | Product Hunt account and CAPTCHA; press-wire fee; every public word | Person |
| google-ads-search | GitHub, browser (Ads UI) | Billing, budget, launch (5); no Ads connector (3) | Person |
| hire-pipeline | Google, Apollo, browser (job boards) | Job board account and payment (5) | Person |
| crm-setup | Attio (or browser for other CRMs), Google, Apollo | Connect Attio; approve the import | Now for Attio, Half otherwise |
| deadline-sweep | Vercel, Cloudflare, AWS, Google, browser | Registrar and state portal sign-in; fees (5) | Person (the dated list and reminders are Now) |

### F. Multi-system

| Idea | Needs | First stop | Verdict |
|---|---|---|---|
| cold-email-engine | Cloudflare, a registrar (none), Apollo, Google, mailboxes at scale | Domain purchase and KYC (5); the shared agents@ address cannot send cold email (6) | Person |

Counts: Now 16, Half 10, Person 9. ("Now" assumes Google, not Outlook, and the named services connected.)

The nine Person ideas and the step only a person can do: sales-tax-registration (identity checks, EIN/SSN, signatures, state fees); soc2-readiness (compliance-platform signup and billing, choosing the auditor); meta-ads-setup (the connector is hidden in prod; Meta Business verification and budget); local-listings-reviews (Google's verification video, CAPTCHAs); launch-run (Product Hunt account and CAPTCHA, press-wire fee, every public word); google-ads-search (no Ads connector; billing, budget, launch); hire-pipeline (job-board accounts and payment, the interviews); deadline-sweep (registrar and state-portal sign-ins, renewal fees); cold-email-engine (domain purchase and KYC, mailboxes at scale; the shared agents@ address cannot send cold email). In each, the agent still does the research, the text, the list and the tracking.

## 4. What the agent needs from us to do this (small, ours)

1. **Needs per idea.** Each idea YAML gets `needs`: services (and whether a write is needed), browser sites, and the person steps. It goes into the Work item's description, where the agent reads it, and the Ideas instruction says: check needs against `connected_integrations` first; ask once for everything missing. The person's DM message stays a plain prompt.
2. **Connecting resumes the work, with no extra step** (owner, 2026-10-15). When a service is connected, the backend wakes the idea work that was waiting on it: a comment on the open task ("Gmail is connected") wakes its assignee through the normal path. The ask stays one plain sentence with the Connect link. No approval card for a connect.
3. **Codes from Work-started runs reach a person.** An issue started from a conversation is already bound to it, and its wakes already run in that conversation. The one gap is in the bridge: a Paperclip turn calls `website_logins.prompt_scope(agent_id, None, None, …)` (`turn.py:3804`) even when it runs inside the bound room, so the code line has no room to go to. Fix: pass the bound conversation's room id. What stays uncovered: a task delegated to another agent (the binding belongs to the agent that filed it) and bindings older than 14 days; those runs should hand browser steps back to the DM.
4. **"The person does this step" as a task.** A Paperclip task assigned to the person, with the prepared text, number or amount, that shows in their Inbox. The mechanism exists (`assigneeUserId`); the Ideas instruction does not use it.
5. **Done means per idea** (already proposed): a line a check can read, in the Work item, reviewed before `done`.
6. **Say what the box cannot do.** The Ideas instruction gets three lines: turns are 30 minutes, so work in units and leave a next-action note; 50 mails a day; no phone.
7. **Offer honestly.** A "Person" idea should say so on its card, or wait until the connector exists. (Owner decision: hide the eight, or label them.)

## 5. Three traces, as they would run after section 4

**inbox-triage (Now).** Start → task description says needs: Google (write). Facts show Google connected → read the last 24 h of mail with `gog`, sort into needs-you / routine / file → draft the routine replies → one approval card "Send these 6 routine replies" → approved, wake → send, label, post the short list in the DM → next-action note "tomorrow 08:00: same" → the task stays open as a daily job (a routine). Outlook owner: the same until "send", which becomes "here are the 6 replies to send from Outlook".

**chase-overdue-invoices (Now).** Start → needs: QuickBooks, Stripe, Google. QuickBooks not connected → one card "Connect QuickBooks to chase invoices; Stripe and Google are already connected", approve → wake → list overdue invoices (QuickBooks and Stripe) → one child task per invoice → draft each reminder → one approval card listing all reminders → approved → `send_invoice` with the pay link / email → mark each child done with the sent timestamp → the done check reads the Sent folder and the invoice statuses → `done`, DM line "3 reminders sent, links in Work".

**directory-listings (Half).** Start → needs: browser (each directory), mailbox for confirmation codes. → agent prepares the company text and the list of 40 directories → approval card "Approve the text and the list" → approved → for each directory (one child task each): open the site in the shared browser; sign-up form; a confirmation email arrives at agents@ (unless marked auto-submitted and dropped: gap 4) → CAPTCHA: the agent posts "go to the Browser panel", ends the turn → person types it → next wake continues → Google Business Profile: the verification is a postcard or video: a task for the person → 32 of 40 done by the agent, 8 handed over, each with a link.

## 6. Stripe and Stripe Projects: which ideas they serve

Two different rails (`money.md`):

- **Stripe MCP** (acting through the owner's connected Stripe account; connect once on Connections). Serves: chase-overdue-invoices, send-invoices-on-time, claim-refund (charges and evidence), investor-update (revenue numbers), stripe-subscriptions (plans in test mode), landing-page-checkout (checkout), cancel-or-renegotiate-bill and unused-software-seats (finding recurring charges), sales-tax-registration (sales by state). Nine ideas, all usable today once Stripe is connected. What the tools can write is set by Stripe, and the tool list is not in the repo; webhooks need a manual secret paste.
- **Stripe Projects** (Stripe's Provisioning API, private preview: Lightbulb creates a paid account or resource on the company's own Stripe identity, the owner approves once in Needs you, and the credentials arrive in the agent's shell as env vars). This is the rail for "a person has to pay for a service", which is the person step in: cold-email-engine (domains, mailboxes, warm-up), landing-page-checkout and website-rebuild (hosting, DNS, email sending), email-flows (an email-sending provider), directory-listings and launch-run (paid directories, press wire), hire-pipeline (job boards), soc2-readiness (compliance platform), deadline-sweep (renewal fees). Up to nine ideas move from Person or Half toward Now, limited to what Stripe's catalog carries (hosting, databases, email sending, analytics, monitoring per `money.md`; ad budgets and registrars are not confirmed).
  - State today: `STRIPE_PROVISIONING_ENABLED` defaults to false and the prod stack does not set it; and `provisioning_landing.land_credentials` refuses on AgentCore with `unsupported_on_agentcore` (`provisioning_landing.py:58-69`), so no workflow can reach `active` even with the switch on. The agent's instruction already tells it to ask through this rail and never sign up in a browser, which leaves no path while the rail is off. Finishing the AgentCore landing (write the env vars into the profile `.env`, the same way connector secrets land) is the unblocking work; it was parked in September.
  - Stripe made Projects "available to all" at Sessions 2026 (2026-04-30) after the preview our code was written against (`Stripe-Version: unsafe-development`), with 32 providers including Vercel, Cloudflare, Netlify, Render, Twilio, Sentry, WorkOS, Browserbase, Resend, OpenRouter, Clerk, Supabase and ElevenLabs; secondary sources cite a default cap of $100/month per provider. Our client may need the public API version. Twilio in the catalog means a phone number can be provisioned through this rail.
- **Issuing for agents** (the "agent wallet" for a business). A virtual Stripe Issuing card per agent, single-use cards scoped to one task, spend controls (per authorization, monthly, merchant category, country), a real-time authorization webhook (2 s to approve or decline every charge), full transaction visibility and disputes. Two forms: "cards for your own business" (apply in the Dashboard) and "cards for your platform" (we issue cards to our customers' agents, each customer sets its own limits; contact Stripe, embedded finance). This covers every purchase Projects does not: paid directories, a press wire, job boards, ad budgets, state fees, a registrar outside the catalog. With it, the money step in the Person ideas becomes agent-driven inside limits the owner set once; a charge above the limit, or at an unexpected merchant, goes to Needs you through the authorization webhook. Status: the docs page is live with an apply link; one third-party source (August 2026) called it a private preview.
- **Link for agents** (the consumer wallet, what Muse uses). A person lets their agent pay from their Link wallet; the agent gets a one-time card per task and the person approves each payment; the agent never sees real card details. Not the company rail, but the fallback when the owner wants to pay personally.
- Lightbulb is already enrolled in Projects as a platform (owner, 2026-10-15): our client uses the platform key with `Stripe-Context` to act on each connected company account, which is the platform shape. So no application step for Projects; the remaining work is the AgentCore landing, a check of the API version against the public release, and the prod switch.
- Order: Projects first (enrolled, code exists), then Issuing for the platform (an application to Stripe plus the authorization webhook wired to Needs you).
