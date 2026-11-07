# 10. Website copy: home, /pricing, /sign-up, /request-setup

## Purpose
Moves the stage-1 goal: ten founding teams of 3 to 15, each with a second person in 48 hours. Serves Go To Market "The website" (plan §8 and the website brief), with §5 (who), §6 (claims) and §7 (price and offer).

## What we know

| Fact | Source | Confidence |
|---|---|---|
| Live H1 is "Your team and your agents, in one place."; the only hero button is "Get started free" to `/sign-up` | [thelightbulb.company](https://thelightbulb.company/), fetched 13 Oct; `Hero.tsx` on `origin/main` 6249f61 | high (first-party) |
| Section order today: Hero, ProofBand, HowItWorks ("Three steps between you and your next hire"), WorkspaceBand ("Everything you need to run the company"), PricingThreeCard ("One workspace. Three ways to power it." / "Choose your plan", no price), AskAI, Footer ("Your business is ready when you are." / "Start your business") | `app/page.tsx` and components on `origin/main` | high |
| `/pricing` and `/request-setup` return 404; nav and footer "Pricing" go to `/#pricing` | curl, 13 Oct; `Navbar.tsx`, `Footer.tsx` | high |
| AskAI's question text says "AI-native" and "multiplayer"; its button says "Copy prompt" | live HTML, 13 Oct | high |
| Sign-up H1 "Your AI company starts here."; pills "Private by design / No shared data", "Free for 30 days / No credit card. Nothing to cancel.", "Extensible skills"; page title "Lightbulb — AI Agents for Autonomous Businesses" | live `/sign-up`; `sign-up/[[...sign-up]]/page.tsx`, `SignupPlanPill.tsx` | high |
| Create-workspace form: "Workspace name", "Agent email address", helper "Your CEO will email as ceo@{slug}.{domain}", button "Create workspace" | `app/onboarding/page.tsx` | high |
| Hero and WorkspaceBand "Get started free" fire no event; nav and pricing buttons fire `signup_cta_clicked`. No setup-request event exists | `Hero.tsx`, `WorkspaceBand.tsx`, `lib/signupFlow.ts`; `git grep setup_requested` empty | high |
| Checkout still bills Base $20 / Workspace $50 / Ultra $150, unlimited members. Base and Ultra are retired: one plan, $50 for up to 5 people, $20 a member from the 6th. Seat billing and the trial checkout ship before any page names the price | plan §7; 00-decisions A8, A9 | high |
| ChatGPT and own-key paths open only after billing is active ("Choose a provider after workspace billing is active"); the trial runs on managed AI with a daily allowance | `lib/providerCards.ts`; CLAUDE.md billing | high |
| Approval cards exist for a CEO plan, a new hire, and spend/provisioning; I found no rule that gates outbound email | `skills-src/paperclip/references/api-reference.md`, `marketplace_hire.py`, `skills-src/lightbulb/references/money.md` | medium (code read, not tested) |
| "We do not sell this content, and it is not used by us to train AI models"; each workspace is an "isolated" AWS environment | [Privacy Policy](https://thelightbulb.company/privacy) | high (first-party) |
| Library is a live panel (artifacts) | `workspace/panelRegistry.ts` | high |
| Funnel, 30 days to 12 Oct: 227 visitors → 18 sign-up clicks (6%) → 20 trials → 10 first reply | plan §9 | high (first-party) |
| Slack Business+ is $15 per user a month annual, $18 monthly; Slack's H1 is "All your people and AI agents working together" | [slack.com/pricing](https://slack.com/pricing), [slack.com](https://slack.com/), 13 Oct | medium (one source) |

## Findings (CRO audit, ranked by impact)

| # | Dimension | Finding | Component to change |
|---|---|---|---|
| 1 | CTA hierarchy | One button, "Get started free", everywhere. The stage-1 action (request a guided setup) has no door. The plan makes it primary. | `Hero.tsx`, `WorkspaceBand.tsx`, `PricingThreeCard.tsx`, `Footer.tsx`, `Navbar.tsx`; new `app/request-setup` |
| 2 | Value proposition | The H1 says "team", but every step after it is solo: "your next hire", "run the company", "Start your business", "Your AI company starts here". A founder with a team cannot see their situation after the first line. | `HowItWorks.tsx`, `WorkspaceBand.tsx`, `Footer.tsx`, sign-up page |
| 3 | Objections / price | No price on the page. The pricing block shows three provider paths styled as plans ("Recommended", "Choose your plan"), and `/pricing` 404s. "What does it cost for my team" has no answer. | `PricingThreeCard.tsx`, new `app/pricing` |
| 4 | Trust | Proof is one revenue sentence with no person or team behind it. Anna's cleared quote is not on the site. | `ProofBand.tsx` (keep the line), new proof block |
| 5 | Objections | No FAQ. The four objections from the plan (Slack, Claude, acting without asking, data) are unanswered. | new FAQ section in `app/page.tsx` |
| 6 | Voice | Public banned words: "AI-native" and "multiplayer" in the AskAI question, "prompt" on its button; "Autonomous businesses" in the footer and sign-up title; "No shared data" contradicts a team product. | `askAiPrompt.ts`, `AskAI.tsx`, `Footer.tsx`, sign-up page metadata |
| 7 | Measurement friction | Two of the busiest buttons (hero, band) fire no event, so landing → start free undercounts. | `Hero.tsx`, `WorkspaceBand.tsx` |
| 8 | Scannability | The pricing headline names the mechanism ("Three ways to power it") instead of the answer ($50 a month). The band mock shows "128 runs / 41 needed a person", which reads as a stat we cannot back. | `PricingThreeCard.tsx`, `WorkspaceBand.tsx` |
| 9 | Headline | "Your team and your agents" passes the "Now you can" test weakly: true, but it is now Slack's own claim. "AI employees" is our more specific word (plan §6). | `Hero.tsx` |

## Recommendation
Rebuild the homepage in the plan's seven-section order with two buttons: **Request a guided setup** (primary) and **Start free** (secondary). Put the price on the page and on a real `/pricing`. Every step talks about the team, and every claim is one we can show in the product today.

I kept the fixed headline, subhead and buttons word for word. I left out three things on purpose:
- "Sends wait for a person": I could not find a rule that gates email.
- "Pick up a session" for viewers outside the workspace: that is item 4 and not built yet.
- Any per-seat comparison against Microsoft: I could not check that price today.

Alternatives I rejected:
- Keeping "Get started free" as the primary button. It fills the funnel with single people (33 of 38 outside workspaces have one person).
- A demo video in the hero. It does not exist yet, and the setup session is the demo.
- Removing the provider cards altogether. The choice of model is claim 3, so it stays as one line with three options.

## Deliverable

### A. Homepage, in the plan's order

**Hero** (`Hero.tsx`)

| Element | Copy |
|---|---|
| Headline A (control, fixed) | Your team and your AI employees, in one place. |
| Headline B (split test, first month) | AI employees your whole team can see. |
| Subhead (fixed) | A CEO, Engineering, Finance and Growth agent in your team's channels. Mention one, it does the work, and the result comes back where everyone can see it, approve it, or pick it up. |
| Primary button | Request a guided setup |
| Microcopy under primary | For founding teams of 3 to 15. Ten spots. A founder replies within a day. |
| Secondary button | Start free |
| Microcopy under secondary | 30 days free. No card to start. |
| Hero mock (keep) | Growth · needs you: "Launch post is drafted. Want to read it before it goes out?" [Approve] [Edit] |

**1. The problem** (new `ProblemBand`, after the hero; the revenue line moves to Proof)

| Element | Copy |
|---|---|
| Eyebrow | Sound familiar |
| H2 | Your agent remembers what you did. It has no idea what your cofounder just spent four hours figuring out. |
| Body | So you carry it. You paste last week's decisions into a new session, write the summary for your cofounder, and check three tools to learn what the agents did. Every handoff starts from zero, and the company waits on whoever holds the context. |

**2. How it works, team first** (`HowItWorks.tsx`)

| Element | Copy |
|---|---|
| Eyebrow | How it works |
| H2 | Bring the team first. Then the agents. |
| Step 1 title | Invite your team |
| Step 1 body | Send your cofounder the join link. People join the rooms, and the first five are free. |
| Step 2 title | Add agents where the work is |
| Step 2 body | Put the CEO in #general and Engineering in #launch. An agent only sees the rooms you add it to. |
| Step 3 title | Mention one and go |
| Step 3 body | It reads the thread above it, does the work, and posts the result in the room. Anyone can approve it, redirect it, or pick it up. |
| Step 1 art overlay | Invite to Lightbulb · Mara Reyes · Jonah Pike · [Send invite] |

**3. See the work** (`WorkspaceBand.tsx`, three captions under one screenshot each)

| Element | Copy |
|---|---|
| Eyebrow | See the work, not a summary |
| H2 | Everything an agent does, where your team can see it. |
| Caption 1: shared sessions | **Shared sessions.** Watch a teammate's Claude Code or Codex session live from the Mac app, and pick it up where they stopped. |
| Caption 2: approval cards | **Approval cards.** When an agent needs a yes (a plan, a spend, a new hire), it asks in the room and waits for a person. |
| Caption 3: Library | **Library.** Every document an agent makes is kept in one place the whole team can find. |
| Mock change | Replace "128 runs / 41 needed a person" with "Needs you · 2". |

**4. Proof** (`ProofBand.tsx` keeps its line; new quote and story above it)

| Element | Copy |
|---|---|
| Quote | "I'm really impressed with how fast it works and how helpful it can be for us now and moving forward when it comes to freeing up more time." |
| Attribution | Anna, Anna's Frozen Treats. Her CEO agent drafts and sends her invoices from QuickBooks. |
| Story (pending the founders' ruling) | **A two-founder team** asked their CEO agent for a plan to win new sellers. In week one it delivered the plan and filed a spend of about $5,000 for the founders to approve. Nothing went out until they did. |
| Revenue band (unchanged) | Teams that communicate through Lightbulb do $2M+ in combined annual revenue. |

**5. Any model, one price** (`PricingThreeCard.tsx`)

| Element | Copy |
|---|---|
| H2 | Any model. One price. |
| Price line | $50 a month for your workspace, up to five people. |
| Sub-line | Then $20 a month for each person from the sixth. 30 days free, no card to start; founding teams put a card on file at their setup session and the first charge is on day 31. |
| Option 1 | **Lightbulb-managed.** Nothing to set up. Billed as credits at the model provider's price plus 20%. |
| Option 2 | **Your ChatGPT account.** Sign in once; your agents run on your ChatGPT plan. |
| Option 3 | **Your own key.** OpenAI or Anthropic. Your provider bills you directly. |
| Link | See pricing for your team → `/pricing` |

The three options are a plain row with no "Recommended" pill, no separate buttons and no art. Publish order: seat billing and the trial checkout first, then this block and `/pricing` (Build tasks 3 and 4).

**6. Founding teams** (new `FoundingTeams`)

| Element | Copy |
|---|---|
| Eyebrow | Founding teams |
| H2 | Ten teams. Set up with you, by a founder. |
| Body | In 45 minutes, one of us sits with you and does four things: connect one real tool, wire one real workflow, invite your second person, and answer your first approval card. |
| What you get (list) | 30 days free, then $50 a month for up to 5 people and $20 a member from the 6th · Your price locked for two years · A shared channel with us inside your workspace, and fixes within days · A say in what we build next |
| Terms | No discounts and no coupons. The free month is the offer. |
| Who it's for | Teams of 3 to 15 already running two or more agents, such as Claude Code or Codex, for real work. |
| Button | Request a guided setup |

**7. FAQ** (new `HomeFaq`, also emitted as FAQ structured data)

| Question | Answer |
|---|---|
| We already have Slack. | Keep it. Lightbulb is where your agents do the work, and your team joins the rooms where something is happening. |
| Claude already does this for me. | For one person, it does. Lightbulb earns its place when a second person needs to see the work, continue it, or approve it. Your Claude Code and Codex sessions come with you, shared with the team. |
| Will it do things without asking? | Plans, spending and new hires come to you as approval cards, and the agent waits for your answer. Each agent sees only the rooms you add it to, and its work stays on the team's record. You decide who your agents can email. |
| What happens to our data? | Each workspace runs in its own isolated cloud environment on AWS. We do not sell your content or use it to train AI models ([Privacy Policy](/privacy)). Ask us to close your account and we deactivate or delete it and your data. |

**Ask AI** (`AskAI.tsx`, `askAiPrompt.ts`; after the FAQ)

| Element | Copy |
|---|---|
| H2 | Still not sure? |
| Body | Ask your own AI what Lightbulb is and whether it fits your team. The question is already written. |
| Button | Copy the question |
| Question text | Go to thelightbulb.company and read the site. Lightbulb is a team workspace where AI employees are members of the company: they sit in channels, people mention them like colleagues, and the work comes back where the whole team can see it, approve it or continue it. Every workspace runs in its own cloud with its own agents, memory and task board. What does a team of 3 to 15 get out of working this way, and who is it a good fit for? |

**Footer** (`Footer.tsx`)

| Element | Copy |
|---|---|
| H2 | Bring your team. Your AI employees are already in the room. |
| Buttons | Request a guided setup (primary) · Start free (secondary) |
| Product column | The workspace · Pricing (`/pricing`) · Marketplace · Docs · Download for Mac |
| Base line | © {year} Isol8 Inc., doing business as Lightbulb AI. All rights reserved. · Your team and your AI employees, in one place. |

**Navbar** (`Navbar.tsx`): Product · Docs · Blog · Pricing (`/pricing`) · Download · Log in · **Start free** (text) · **Request setup** (button). The singular "Blog" fixes the "Blogs" label.

### B. /pricing (new `app/pricing/page.tsx`)

| Element | Copy |
|---|---|
| H1 | One price for the workspace. The first five people are free. |
| Sub | Your team pays for the workspace and the work its agents do, not for each seat. |

| Plan | Price | Includes |
|---|---|---|
| Team | **$50 a month**, flat | Up to 5 people · six AI employees (CEO, Engineering, Finance, Growth, Analytics, Customer Operations) · channels, DMs, task board, Library, shared browser, team email · Mac app; iPhone app on TestFlight |
| Each person from the 6th | **+$20 a month** | The same workspace, one more person |
| Enterprise | Talk to us | Custom pricing for special cases |

| Element | Copy |
|---|---|
| Inference line | Your agents' AI is billed separately, your choice: **Lightbulb-managed** at the model provider's price × 1.2, as prepaid credits, or **bring your own**: your ChatGPT account or an OpenAI or Anthropic key, billed by them. |
| Trial | **30 days free.** No card to start; add one when the month ends. Founding teams put a card on file at their setup session, and the first charge is on day 31. Your agents run on Lightbulb-managed AI with a daily allowance meanwhile. |
| Founding offer | **Founding teams: ten spots.** A founder sets your team up with you, and your price is locked for two years. [Request a guided setup] |
| Buttons | Request a guided setup · Start free |

| FAQ | Answer |
|---|---|
| Why $50 flat? | What your team gets grows with the work its agents do, not with headcount. So you pay for the workspace and the agents' work, and people up to five cost nothing. An eight-person team pays $110 a month. |
| What happens at the 6th person? | Your bill goes up by $20 a month for each person from the sixth, prorated from the day they join. When someone leaves, it goes back down. |
| What does managed AI cost? | The model provider's price plus 20%, drawn from prepaid credits you top up. Usage shows in the workspace, and nothing runs past your balance. |
| Can we bring our own key? | Yes. Sign in with your ChatGPT account, or add an OpenAI or Anthropic key, after your plan is active. Your provider bills you directly and we charge nothing for inference. |
| What if it doesn't stick? | Cancel any time. We help you take your work with you. |

### C. /sign-up and create workspace (`sign-up/[[...sign-up]]/page.tsx`, `JoinTeamBlock.tsx`, `onboarding/page.tsx`)

| Element | Copy |
|---|---|
| Eyebrow | Get started |
| H1 | Bring your team. |
| Door 1 title | Create a workspace |
| Door 1 body | For the person setting up the team. 30 days free, no card. |
| Door 2 title | Join your team |
| Door 2 body | Got an invite? Open the email on this device. Have a join link? Paste it here. |
| Door 2 field / button | Join link · Join |
| Pill 1 (keep `SignupPlanPill`) | Free for 30 days · No card. |
| Pill 2 | Your team in the same rooms · The first five people are free. |
| Pill 3 (replaces "No shared data") | Your own cloud · Each workspace runs in its own isolated environment. |
| Pill 4 (replaces "Extensible skills") | Agents that ask first · Plans, spending and new hires wait for a yes. |
| Under the form | Setting up a team of 3 to 15? Request a guided setup. |
| Create-workspace H1 | Name your workspace |
| Sub | Use your company's name. Your team and your agents will work here. |
| Field 1 / placeholder | Company or team name / Acme |
| Field 2 | Agent email address |
| Helper (keep logic) | Your CEO will email as ceo@{slug}.{domain} |
| Button | Create workspace |
| Next-step line (after create) | Next: invite the person you work with most. |

### D. /request-setup (new `app/request-setup/page.tsx`)

| Element | Copy |
|---|---|
| H1 | Request a guided setup |
| Sub | One of the founders sets your team up with you in 45 minutes: one real tool, one real workflow, your second person in the room. Ten spots. |

| Field | Label | Type | Helper text | Required |
|---|---|---|---|---|
| 1 | Your name | text | | yes |
| 2 | Work email | email | We reply here within a day. | yes |
| 3 | Company | text | | yes |
| 4 | Team size | radio: Just me · 2 · 3–5 · 6–15 · 16+ | How many people would use it? | yes |
| 5 | Agents you use today | checkboxes: Claude Code · Codex · ChatGPT · Cursor · Other (text) | Pick all you use for real work. | yes |
| 6 | What would you hand off first? | textarea, 3 lines | One job. For example: "weekly investor update" or "triage new GitHub issues". | yes |
| 7 | How did you first hear about us? | select + text | | no |

How-heard list: Hacker News · Reddit · X · LinkedIn · Product Hunt · A shared Claude Code or Codex session · The Claude Code or Codex plugin marketplace · A friend or colleague · An email from a founder · Search engine · Asked an AI (ChatGPT, Claude, Perplexity) · Other (tell us).

| Element | Copy |
|---|---|
| Button | Request my setup |
| Under button | A founder reads every request. |
| Confirmation H1 | Thanks, {first name}. |
| Confirmation body | One of us will email you within a day to book 45 minutes. Bring the person you'd hand work to first. |
| Confirmation for "Just me" or "2" | Thanks, {first name}. The guided setups are for teams of three or more, but you can start your 30 days today and invite people as you go. [Start free] |

The founding-team list is for teams, so "Just me" and "2" go to the second confirmation.

### E. /docs/get-started rewrite outline
1. **Before you start**: who it is for (a team that shares work) and what you'll have at the end: one finished result, approved by a second person.
2. **Create the workspace**: the name, the agents' email address, 30 days free.
3. **Brief your CEO**: the one-screen brief. The example handoff stays.
4. **Invite your second person**: the join link or an email invite, and why first.
5. **Connect one real tool**: GitHub, Gmail or QuickBooks, from the chat.
6. **Put agents in rooms**: listen modes in two lines, and agents see only their rooms.
7. **Hand off one job and approve the first card.**
8. **Share a session (optional, Mac app)**: watch, join, pick up.
9. **Plans and providers**: link to `/pricing`.

Delete "private AI company", the Marketplace-hire step from "first wins", and the provisioning internals ("the gateway connects").

### F. Meta titles and descriptions

| Page | Title | Description |
|---|---|---|
| Home | Lightbulb — Your team and your AI employees, in one place | A team workspace where AI employees are members. Mention one in a channel; the work comes back where everyone can see it, approve it, or pick it up. |
| /pricing | Pricing — Lightbulb | $50 a month for your workspace, up to five people, then $20 a person. Managed AI at provider price × 1.2, or bring your own. 30 days free. |
| /sign-up | Create your workspace — Lightbulb | Bring your team. Create a workspace in a minute, or join the one your teammate set up. 30 days free. |
| /request-setup | Request a guided setup — Lightbulb | Founding teams of 3 to 15: a founder sets up your workspace with you in 45 minutes. Ten spots. |
| /docs/get-started | Get started — Lightbulb Docs | From a new workspace to a finished result your teammate approved, in one sitting. |

## Build tasks

| # | Title | Today | Change | Why | Measure |
|---|---|---|---|---|---|
| 1 | Two-button hero | `Hero.tsx`: H1 "agents", one "Get started free", untracked | Section A hero copy; primary to `/request-setup`, secondary to `/sign-up`; both fire `signup_cta_clicked` with `ctaId` `hero_setup` / `hero_start_free` and `headline_variant` | Stage-1 primary action | landing → setup request, landing → start free |
| 2 | Headline split test | none | A/B on the H1 via a cookie-stable variant (PostHog flag or Vercel Flags); store `headline_variant` on every landing event and on the setup form | Plan's first-month test | Measures 1–3 per variant |
| 3 | Price on the page | `PricingThreeCard.tsx` shows provider cards as plans, no price | Section A.5: one price, three-option row, no per-card buttons. Publishes after seat billing and the trial checkout ship (plan item 5): billing first, then the block | Objection: cost | clicks to `/pricing` |
| 4 | `/pricing` page | 404 | Publishes after billing. New `app/pricing/page.tsx` with Section B; nav and footer point there; the home `#pricing` anchor stays for old links | Plan website table | `/pricing` → setup or start free |
| 5 | `/request-setup` page and endpoint | 404 | New page with Section D; POST stores the request (DynamoDB row or the founders' channel) and emails the founders; emits `setup_requested` {team_size, agents, source, headline_variant, utm_*} | The founding-teams door | setup requests by source |
| 6 | Team-first steps and band | "your next hire", "run the company", fake stat | Sections A.2 and A.3 copy; replace mock stat; add three screenshots (session, approval card, Library) | Finding 2 | scroll depth past steps |
| 7 | Problem, Proof, Founding teams, FAQ sections | none | New `ProblemBand`, proof quote block above `ProofBand`, `FoundingTeams`, `HomeFaq` (+FAQPage JSON-LD in `lib/seo.tsx`) in the plan's order in `app/page.tsx` | Findings 4–5 | setup requests per visitor |
| 8 | Voice fixes | "AI-native", "multiplayer", "Copy prompt", "Autonomous businesses", "Start your business", "Blogs" | Section A Ask AI, footer and nav copy; `HOME_TITLE`/`HOME_DESCRIPTION` in `lib/seo.tsx`; sign-up metadata | Banned words in public copy | none (hygiene) |
| 9 | Sign-up two doors | "Your AI company starts here." | Section C in `sign-up/[[...sign-up]]/page.tsx`, `JoinTeamBlock.tsx`, `onboarding/page.tsx`; add the how-heard question after create (optional) | Second person is the activation event | sign-ups via join link vs create |
| 10 | Get-started doc | solo first session | Section E outline into `docs/get-started` content | Matches the setup session | doc → start free |
| 11 | Hero and band tracking | no event on two buttons | `captureSignupCta` on `Hero.tsx` and `WorkspaceBand.tsx` | Finding 7 | complete landing → start free |

## Measures
- **Landing → setup request:** `setup_requested` ÷ unique landing visitors, weekly, by source (UTM and how-heard). This feeds the Monday number "setup requests by source".
- **Landing → start free:** `signup_cta_clicked` ÷ unique visitors. The baseline is about 6% (18 of 227, plan §9). Report it next to trials started, so a rise in clicks that does not become trials shows up.
- **Per headline variant:** both rates above, plus trial → team formed in 48 hours, for A against B. Run it for four weeks or about 1,000 visitors per arm, whichever comes later. At about 10 visitors a day today, that is too little traffic for significance. Read the result as directional, and say so in the readout.
- **Qualified share:** the share of setup requests from teams of 3–15 that run two or more agents. If most requests come from "Just me", the page is attracting the wrong people.
- **Downstream:** teams formed and first handoff in week one for guided setups against self-serve sign-ups.

## Founder decisions (13 Oct 2026)
- B1: Name Second Catch now. The story block names them.
- A1/A2: No guarantee. Removed from the founding block, `/pricing` and the FAQ; "week-4 active" stays as a metric only.
- A4: founding teams put a card on file at the setup session (a Stripe checkout with a 30-day trial, first charge on day 31), built before the first setup. Self-serve starts without a card, as today, and adds one when the month ends.
- A8: Seat billing first, then `/pricing` with the plan's price. No interim line.
- A9: Base and Ultra retired. One plan, one price everywhere.
- B6: No send gate. Copy says "you decide who your agents can email", never "waits for a person".
- B7: Subhead is "in your team's channels".
- B8: No Slack Pro price; Slack's price appears only where a page answers "we already have Slack".
- E4: Telegram, Discord and WhatsApp are off the homepage. The homepage lists the workspace on web, desktop and phone, Claude Code and Codex sessions, email, Meet.
- F3: Aden replies to setup requests within one business day from his own address; the Growth agent drafts.

## Sources
- `docs/business-plan.md` §5, §6, §7, §8, §9, Go To Market "The website"; `.agents/product-marketing.md` v7.
- `origin/main` 6249f61: `apps/frontend/src/app/page.tsx`, `components/landing/{Hero,HowItWorks,WorkspaceBand,ProofBand,proofBandCopy,PricingThreeCard,Footer,Navbar,AskAI,askAiPrompt}.tsx/ts`, `app/sign-up/[[...sign-up]]/page.tsx`, `components/SignupPlanPill.tsx`, `components/onboarding/JoinTeamBlock.tsx`, `app/onboarding/page.tsx`, `lib/providerCards.ts`, `lib/signupFlow.ts`, `lib/seo.tsx`, `workspace/panelRegistry.ts`, `apps/infra/hermes/skills-src/`.
- Live pages, 13 Oct 2026: https://thelightbulb.company/, /sign-up, /docs/get-started, /privacy, /pricing (404), /request-setup (404).
- https://slack.com/pricing and https://slack.com/, 13 Oct 2026.
- Skills: `marketing-skills:copywriting` (the "Now you can" test, the Human Action Model), `marketing-skills:cro` (the seven-dimension framework, form guidance).
