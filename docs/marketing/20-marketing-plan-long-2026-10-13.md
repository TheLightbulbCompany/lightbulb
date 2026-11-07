# Part 2: Marketing plan

13 October 2026. Built with the marketing-plan method (thirteen sections, AARRR). Sources: the business plan (Part 1), the shared context (`.agents/product-marketing.md` v8), the founders' 72 decisions of 13 October (`docs/marketing/00-decisions.md`), the seventeen workstreams (`docs/marketing/01` to `17`), the build order (`18-build-order.md`), the vetting (`19-plan-vetting.md`) and the research under `docs/research/`. Numbers from PostHog, Stripe and Clerk are from 12 to 13 October. Open decisions are in section 13.

## 1. Executive summary

Lightbulb is a shared workspace where a team and its AI employees do the work together (§2). Two things are missing: a second person in each workspace, and a way to pay (§3). Today 33 of 38 outside workspaces have one person (research record, 12 Oct). Nobody pays list (Stripe, 12 Oct).

**Three big bets**

1. **Founders set up teams by hand, with a second person in the room.** The founder who sourced each team runs a 45-minute setup session (decision C1). The card goes on file, the first 30 days are free, and $50 charges on day 31 (A4). The customer answers the first approval card, never the founder. Outreach rises to 50 first touches a week from week 3 (A5).
2. **The product builds its own loops and doors, in fix-forward order, billing first.** Seat billing and the trial checkout ship before the first setup (A8). The first-session invite, Ask to join on session links and the new site follow (§5, §7).
3. **One honest inbound launch.** Show HN runs once, when its five-item gate is green (§9). The first comment gives our real numbers (B3). Product Hunt follows Show HN. A named founder signs every public word, and agents never publish.

**90-day priorities (14 Oct to 12 Jan)**

1. Ship the trial checkout before the first setup. Owner: Forge, directed by Prasiddha. The roadmap assumes 30 Oct (§9).
2. Ship the activation events and the staff exclusion list. Owners: Forge and Prasiddha (F1).
3. Send first touches from day one, at 50 a week from week 3. Owners: all three founders.
4. Run the setup sessions. Owner: Aden by default (C1). A team must be set up by 12 Dec to pay by 12 Jan (day-31 charge, §9).
5. Set up ten teams with a card on file by 4 Jan (A3).
6. Have about three teams paying at list by 12 Jan (A3).
7. Launch Show HN about 8 Dec if the gate is green. Owner: Prasiddha (§9).

**The 12-month outcome**

- Six teams pay at list by May 2027 (A3).
- The stage-2 gate is read monthly from 1 Feb (§10). It passes when a quarter of new teams reach a working first session and stay active in week four (§6). Sign-up opens wider only after it passes.
- A growth engineer joins after the $2M SAFE closes, to work the product loops (business plan, round). The start date waits on the gate read (§10).
- Paid acquisition starts only on a channel that already brings teams free (§4). Until then paid spend stays at $0.

The growth is linear and small: about $300 a month at list by May (§8).

**The one number on the wall**

The north star is teams with two or more people and a completed handoff (§13). Lightbulb staff count as neither the second person nor the handoff. Pulse reports it every Monday, with names (§9).

**Open decisions that block the most** (§13)

1. **The Forge date for the trial checkout.** No setup can be sold before it ships. Each week it slips costs about 1.2 setups (§9).
2. **The quadrant call and the fallback trigger.** The plan bets the problem is large and frequent. The evidence today reads small and frequent (§2). The proposed trigger is the 1 Mar read: fewer than three founding teams week-4 active and paying at list.
3. **Founder hours as the cost of a team.** Founder hours are the only acquisition cost today. Without an hourly figure, nobody can judge CAC or a paid test (§8).

## 2. Strategic frame

This page orients anyone who writes for Lightbulb. It lifts from product-marketing v8 and the business plan (§5 to §7) and adds no new positioning.

### What Lightbulb is, in one sentence

Lightbulb is a workspace where a team and its AI employees work in the same channels. People hand work to the agents, see what they do, approve it, and pick it up (plan §1).

### The category we're claiming

The shelf is team communication, the one Slack and Microsoft Teams own. The claim is what that shelf becomes when agents do the work: **the workspace for a company that runs on agents** (plan §5). The public word for the agents is "AI employees" (founder ruling, 13 Oct).

"People and agents together" is now Slack's own headline (checked 12 Oct). So the claim wins on specifics (plan §6). Agents are members with their own tools, memory and approval rules. The whole team sees and continues the work. Any model runs at one price.

Against Slack and Teams the contrast line is "Slack added agents to chat. Lightbulb built the company around them." (founder ruling, context file v8). Grok's team assistant is built per person, with private memory and a tick-to-share step. Consumer agents such as Meta's Muse serve one person, with no shared rooms or handoffs. Our claim starts where theirs ends (the Grok teardown of 9 Oct and the Muse walkthrough, both in `docs/research/`).

### Who we're for (ICP, distilled)

- **Who:** founder-led teams of 3 to 15 that run two or more AI agents for real work. One person routes context between people and agents by hand.
- **Who decides:** the founder decides, pays and sets up. The second person invited is the activation event.
- **Stated problem:** between cofounders "none of it syncs", and people keep "re-explaining the stack" to their agents every session (buyers on Reddit, Oct 2026, permalinks in the context file).
- **Real problem:** the founder is the message bus. Nobody else can see, approve or continue an agent's work.
- **Real competitor:** DIY glue, such as a shared markdown repo, Linear and copy-paste. It is free and good enough.

A founding team qualifies only if all seven tests hold (plan §5):

1. 3 to 15 people.
2. Two or more agents in recurring use across two functions.
3. Several people need the same agent context or output.
4. One person routes context by hand.
5. A missed handoff in the last 30 days.
6. No procurement gate.
7. Willing to invite a second person and connect one real workflow in the first session.

Not a fit: solo operators, pairs whose work does not overlap, cheaper-Slack shoppers and automation buyers. Cross-role teams come first (decision C5).

### The business model logic

Value grows with the agents' work and the people who see it. So the line is: "People are free up to five. You pay for the workspace and the work your agents do." (plan §6).

- **One plan:** $50 a month for up to five members, then $20 a member. Enterprise is custom (decision A9).
- **AI usage:** credits at the provider's price × 1.2, or the team's own ChatGPT account or key (plan §7).
- **Compounding:** seats grow as a team pulls in its company. Referral fees on services agents buy come later.

§8 carries the offer and the unit economics.

### The market-quality gate

| | Low frequency | High frequency |
|---|---|---|
| **Large problem** | | **The bet** |
| **Small problem** | | **The evidence today** |

Frequency is high. Each agent session starts from zero and every handoff is routed by hand (research record). Size is claimed large and not yet shown. One wedge team exists, nobody pays list, and two subscribed workspaces went idle within two weeks (plan §9).

So the plan bets on large × high, and the evidence today reads small × high. We plan as the bet and measure against the evidence. Week-4 activity and paying at list among the ten founding teams is the test. This stays an open decision in §13.

### Brand voice (the non-negotiables)

- **Tone:** plain, specific, calm. No hype, no exclamation marks. Say what is early.
- **Style:** short sentences and concrete nouns, such as invoice, PR and channel.
- **Proof:** every claim names a concrete act, such as an invoice sent or a PR opened.
- **Sign-off:** a named founder signs every public post, email and listing. Agents draft and never publish.

**Words to use:** your team, AI employees, channel, mention, hand off, continue, approve, "starts from zero", "who owns a change".

**The five banned words:** multiplayer, AI-native, orchestration, bot, prompt. Internal names (Paperclip, Hermes, AgentCore, Jev) stay out of public copy too. Hermes and Paperclip may appear only as "built on", never as the product's name (B10).

Email copy says "you decide who your agents can email" and never promises that each email waits for a person (decision B6). The line is "AI agents are members, not tools" (the homepage, context file v8). The September board message that ended in a banned word is retired by the 13 October ruling.

## 3. Current state

The starting line on 13 Oct 2026. Figures are the 12 to 13 Oct pulls from PostHog, Stripe and Clerk.

### Team composition (marketing surface area)

| Who | Role | Marketing surface area |
|---|---|---|
| **Prasiddha** | CEO, product and engineering | Strategic owner of marketing. Hacker News, Product Hunt, plugin listings, the written data answer, Search Console, the internal-workspace exclusion list. Directs Forge. |
| **Aden** | Cofounder | LinkedIn, setup sessions (default host), activation and churn interviews, setup requests answered within one business day, customer stories. |
| **Leo** | Cofounder | X (manual since the reply pipeline died on 11 Oct), Reddit (replies only until karma passes 100), author and sign-off for comparison pages. |
| **MKT385 interns** | Student team | Moving from Miami small-business research to finding technical founder teams. |
| **Pulse** | Growth agent | Researches, drafts posts and setup replies, writes the Monday readout. Never publishes. |
| **Forge** | Build agent | Builds the 35-task build order, including every site and billing change. |
| **Friday** | Agent | The materials give Friday no marketing duty. Question for the founders. |

There is no dedicated marketer. Prasiddha is T-shaped: deep in product and engineering, broad in go-to-market. He is also the builder, so every site change and both launches queue behind one person. Nobody owns lifecycle email, the website as a whole or analytics QA. The first hire is a growth engineer after the stage-2 gate, not a marketer (plan §8).

### Marketing budget (current)

- **Paid acquisition:** $0 a month, by design.
- **Tools:** PostHog, Clerk, Vercel and SES, all product tools that marketing rides on. Monthly cost by line: question for the founders.
- **X API credits:** lapsed. Whether to renew: question for the founders.
- **Research and gifts:** $50 gift cards for churn interviews (decision C7) and a light bulb posted after each first handoff (C14).
- **Headcount:** three founders. Founder pay: question for the founders. No retainers, agencies or advisors.
- **Blended CAC:** unknown. The real cost is founder hours, and nobody has priced them (§8).
- **Spend as % of ARR:** not meaningful at $0 list ARR.

This maps to Tier 1, pre-seed (funding-stage anchors). The 90-day plan must work with no paid lever at all.

### Phase of SaaS growth

Lightbulb is in Phase 1, $0 to $10K ARR, at $0 list ARR and $55 collected ever (Stripe, 12 Oct). The binding constraint is Activation: people arrive and stay solo. Revenue plumbing and measurement are hard prerequisites. Acquisition volume is not the constraint yet, because ten teams can be reached by hand.

### What's already done (acknowledge, then build on)

| Asset | Status | Marketing leverage |
|---|---|---|
| The 13 Oct marketing pass | Done | A marketing plan, the v8 context file, 17 workstreams with ready copy, 72 answered decisions, a 35-task build order and a claim-by-claim vetting. The ideas exist. |
| Product live since 6 Apr, reshaped 10 Sep, new domain 2 Oct | Live | About 1,359 PRs merged since 1 Sep. A real product to demo. |
| Anna's Frozen Treats | Active | Her CEO agent sends QuickBooks invoices (since 11 Oct). Her quote is cleared for use. |
| Second Catch | First wedge team | A plan waiting on a ~$5,000 approval. Named now (B1). |
| Email invites, public plugin repo, five research reports | Shipped 7 to 13 Oct | Doors for the second person and plugin users. Research behind the agent-voice work. |

### What's in flight (drafted but not shipped)

| Item | Status | Blocker |
|---|---|---|
| Seat billing, trial checkout, one plan (T04 to T06) | Not built | Forge. Blocks the first sellable setup. |
| Activation events (T01 to T03, T23) | Not built | Forge. The Monday numbers are hand-counted. |
| `/request-setup` and the homepage rewrite (T22, T28, T29) | Copy written, route 404 | Forge. Blocks the homepage's main button. |
| First-session invite, welcome and away emails (T07 to T15) | Specified | Forge. Setup sessions do it by hand meanwhile. |
| Ask to join for outsiders (T16 to T18) | Specified | Forge. Blocks Show HN. |
| Six comparison pages | Written, facts checked 13 Oct | Wait on `/pricing`. |
| Show HN (~8 Dec), Product Hunt (early Jan) | Copy and calendar written | Three of five gate items are red. |
| Plugin directory listings | Not submitted | No licence yet. Codex's directory bars hooks. |
| Anna case study, Second Catch story | Cleared in principle | One recording and a wording check. |

### What's stuck (and needs to unstick this quarter)

| Issue | Cost of inaction | Action |
|---|---|---|
| **Billing comes before the site.** Checkout still bills $20/$50/$150 with no trial. | No founding team can be sold. `/pricing`, comparison pages and the launch gate all wait. | Ship T04 to T06 first, by the fix-forward rule. |
| **Workspaces stay one person.** 33 of 38 outside workspaces are solo. | 24 of 50 accounts never returned after day one. | First-session invite (T07 to T10). Setup sessions do it by hand until then. |
| **No activation events.** No event for setup requested, team formed, first handoff or paid invoice. | The stage-2 gate cannot be read. Dev data swamps prod. | T01 to T03 and T23, then the Monday readout. |
| **The site tells the old story.** Sign-up says "Your AI company starts here". The `flickmeon.ai` root serves a launch page never set up. | About 8% of product-page visitors click sign up. The welcome email is wrong in three places. | T27 to T29 after T22 and T06. |
| **Search and the launch gate.** No ranking for any wedge query. Two namesakes win the brand query. | Inbound stays at about 10 visitors a day. | Search Console, comparison pages after `/pricing`, Ask to join before Show HN. |

### Audit rubric snapshot (scored from materials)

Scored 0 to 5 against the current-state rubric from the 13 Oct materials. Founders can push back where they have better data. Semicolons in the research notes became full stops, and quoted banned words are named as "banned words".

| # | Section | Score | Note |
|---|---|---|---|
| 1 | Positioning | 3 | Statement, three claims and contrast lines approved 13 Oct (PMM v8). But every machine-read surface still sells "AI agents for autonomous businesses" (05 A2). The vetting found several differentiation claims overstated against Slackbot, Grok's team assistant, Delta and Bloome |
| 2 | Customer research | 2 | One wedge customer. Three of six quoted pains are from competing builders (02). No reasons known for the 24 day-one churners (16). Interview guides written, not yet run |
| 3 | Homepage | 2 | H1 says "team" but every step after is solo, one "Get started free" button, no price, no FAQ, no named proof, banned words in AskAI. About 8% click to sign up (10) |
| 4 | Sales / product pages | 1 | `/pricing` returns 404 and nav points to an anchor. Plan cards show provider paths, not prices. Product page copy is written but blocked on billing (05 A1, 10) |
| 5 | Conversion pages | 1 | `/request-setup` 404. Ten thin, orphaned, off-wedge `[seoSlug]` pages due for deletion. `/launch` has no H1 or setup door (05 A4, A10) |
| 6 | Competitor comparison | 1 | Nothing live and `/ai-agents-for-slack` contradicts the planned Slack page. Six pages drafted with facts checked 13 Oct, held for `/pricing` (11) |
| 7 | Resources / content | 2 | Six blog posts from July to August with an organisation byline, internal names and banned words. Docs exist but carry stale facts. `llms.txt` stale (05 A3, A8, 13) |
| 8 | Onboarding | 2 | Flow exists but is single-player: invite card after connectors and admin-only, first run ends on the board, sign-up says "No shared data", welcome email wrong in three places. 10 of 20 trials got a first reply, 33 of 38 workspaces solo (08) |
| 9 | Email lifecycle | 2 | Daily digest is live and well built. Welcome is inaccurate. No trial-end, away or nurture email. Nine emails drafted from the CEO agent's address (12, 15) |
| 10 | Sales material | 3 | Battle card, nine objections, 45-minute run sheet, never-promise list, two asks and a sequence written 13 Oct. No deck by design. No case study yet (Anna page planned). Unused on a real call (03, 04) |
| 11 | Messaging | 3 | Voice, word lists and glossary documented. Live surfaces break them with banned words, "autonomous businesses" and "No shared data" (05, 10) |
| 12 | Pricing | 1 | The plan's price is decided but cannot be bought: checkout bills $20/$50/$150 unlimited with no trial. Promo codes open. 0 paying at list. LTV unknown. Seven pricing changes since April (15, Appendix B) |
| 13 | CRO | 1 | No tests ever run. PostHog wired but hero buttons fire no event, dev swamps prod, no setup or handoff events. One headline test planned (09, 10) |
| 14 | GTM launches | 1 | No structured launch since the April go-live. The launch competition was never set up. Show HN and Product Hunt planned with a five-item gate, three items red (07) |
| 15 | Ads (paid) | 0 | None, by design at this stage. Not a weakness |
| 16 | SEO | 1 | Technically sound (fast, server-rendered, sitemap, robots, llms.txt) but ranks for no wedge query, loses its own brand query to two namesakes, no third-party mentions (05) |
| 17 | Internationalization | 0 | US/English, USD only. Appropriate at this stage |
| | **Total** | **26 / 85 (31%)** | |

**The shape.** The strengths are on paper. Positioning, messaging and the sales kit score 3, and all three were approved in the last two days. Everything a customer touches scores 0 to 2. The gap is shipping, not ideas. By the fix-forward rule, almost every surface waits on two engineering items: seat billing with the trial checkout, and the activation events. So Activation and Revenue carry the plan. The first quarter is bedrock: billing, measurement, the second person, then the doors.

## 4. Acquisition

> *How do strangers become aware of Lightbulb?*

### Current state

Few strangers find Lightbulb today, and we cannot see how most of them arrive. The product pages drew about 227 visitors in 30 days (PostHog, to 12 Oct). About three quarters of all traffic is direct or unattributed (PostHog, 30 days, 70 to 75%). The named sources are small: Instagram 26, LinkedIn 16, Reddit 15 and Google 4 visitors (PostHog, 30 days).

Two older channels still send traffic by habit. Instagram brings a few visitors, but it is not where the wedge reads. Old `flickmeon.ai` links still circulate, and its root still serves an unset launch competition page (2,088 of 4,702 recent pageviews, research record). The site ranks for no wedge query. A search for our brand returns two unrelated companies with our name (05, one engine, 13 Oct). We have no listing in any directory, and the plugin repo has 0 stars (14).

Acquisition volume is not the constraint yet. Ten teams can be reached by hand. The constraint is that every door we would send them to is missing or broken. `/pricing` and `/request-setup` return 404 (curl, 13 Oct). Planned work fixes both doors first, and every move below waits on that order.

**What a stranger can be shown.** The product inventory moves here from the business plan (Appendix D). "Live" means merged on main, not checked in prod (vetting Part 4).

| Surface | State |
|---|---|
| Messaging workspace on web: channels, DMs, group chats, listen modes, one responder per message | Live since the 10 Sep pivot |
| Six AI employees per workspace, each with memory, skills, tools and an email address | Live |
| Shared Claude Code and Codex sessions, and the `lightbulb` plugin | Live. Only the Mac app shares sessions |
| Task board with approvals, routines and a nightly CEO run, readable over MCP | Live. MCP access added 11 Oct |
| Memory tab, voice in rooms, Google Meet through agent email | Merged 9 to 13 Oct |
| Shared cloud browser with saved sign-ins | Live |
| Mac app with consent given once at pairing | Shipped, gated per owner |
| iPhone app | TestFlight. App Store listing is T21 |
| Marketplace with a 15% platform fee | Built. Liquidity unproven |
| GitHub, Google, Stripe read-only key, agent email | Live |
| Slack, Discord and Telegram channels | Unverified on the current runtime. Off the homepage until Forge verifies them (E4) |

### The plan

The plan sends strangers to two doors on the website: **Request a guided setup** first and **Start free** second. The home hero, `/request-setup` and the six `/compare/*` pages are the conversion surface for every channel below. §5 (Activation) holds their copy and tests. This section only decides who sends people there, and how.

**Current channels**

| Channel | What it does today | Decision |
|---|---|---|
| Direct and unattributed | About three quarters of traffic (PostHog, 30 days) | Fix with UTMs and the source question (T22) |
| Instagram | 26 visitors in 30 days (PostHog) | Stop working it in stage 1 |
| Old `flickmeon.ai` links | Root serves the unset competition page | Redirect every path to the product site (T27, the /launch ruling). Old posts stay untouched, and every new link carries a UTM (E10) |
| LinkedIn, Reddit, X | Small, unplanned posts (16 and 15 visitors, PostHog) | Replace with the founder posts below |

**Planned channels**

| Channel | Thesis | Owner |
|---|---|---|
| Founder outreach, one person at a time | People who already said the pain in public reply to someone who read their words | All three |
| Founder posts in their own names | The category reads r/ClaudeAI, r/ClaudeCode, HN, X and LinkedIn, and the name accrues while we set up teams | Leo X and Reddit, Aden LinkedIn, Prasiddha HN and Product Hunt (C3) |
| Plugin listings in the Claude Code and Codex marketplaces | The wedge already installs plugins inside the tool it works in | Prasiddha |
| Show HN and Product Hunt | One launch, gated on product, sends inbound to a live session | Prasiddha |
| Six comparison pages | "X vs Y" queries are where buyers and AI answers shortlist products | Leo |
| SEO cleanup | Search and AI engines repeat the retired story until we fix it | Prasiddha |

**Skipped channels**

| Channel | Reason |
|---|---|
| Paid acquisition | No channel has proven itself free yet (plan §8) |
| Instagram | The wedge does not read it |
| Local small-business lists | Outside the wedge. Small businesses are the fallback only, in the business plan's Risks (plan §12) |
| Partnerships | Revisit at stage 2 (C6) |
| G2 and other review sites | They need `/pricing` and a beta-free site first (E14) |
| Paid AI directories | $49 to $497 per listing for generic traffic (14) |

**Move 1: founder outreach.** Each founder writes to one named person at a time. The note says what we noticed about them, names the problem in their words and offers one proof. The proof is Second Catch's plan or Anna's invoices, and nobody else is named until three teams pay (B2). The ask names the outcome: "Bring your cofounder and the agents you already use. We'll get your first plan, PR or invoice run approved by them this week." Touch one carries no link and no calendar ask. Two follow-ups stand alone in the same thread, with two- to four-word subject lines (03). Two asks are split by row and compared after 60 sends each (03). At this volume the comparison is a read, not a significant test (vetting). Ten first touches a week per founder is the floor. Aden and Leo go to twenty from week 3 if the week-2 reply and booking rates hold (A5). The lead sheet holds 28 companies: none hot, 9 warm and 19 cold. Six warm leads are worked by hand now, each through its own public route (02). Public signals give two to four setup calls at best, so most founding teams come from founder networks and inbound (02). When a call raises data, Prasiddha answers in writing within a day from the Privacy Policy's terms (B9). The data page is T33.

**Move 2: founder posts.** Each founder signs one post a week on his own channel (13). A DM goes out only after someone asks for help in the thread (C2). The mix is 60% searchable, 30% shareable and 10% experimental. The first three posts are the Claude Code handoff, the CEO's first ten minutes and why people are free up to five (13). Aden shares three past LinkedIn posts as the voice sample (C11). Reddit threads are read through the Chrome bridge, because Reddit's search does not return comments (founder ruling, context file v8). Leo posts only replies on Reddit until his karma passes 100 (C12). On HN the weekly unit is a disclosed comment, with our own writing submitted at most once every three weeks (13). Agents draft. Founders edit and publish by hand.

**Move 3: plugin listings.** Prasiddha submits the `lightbulb` plugin to Anthropic's directory and to the Codex catalog. The repo first gets an MIT licence and a README a stranger can follow (T34, E11). Codex bars plugins with hooks, so it may need a hooks-free build (14). The launch does not wait for the Codex listing (E13). The submission order is Anthropic's directory, our plugin repo, Codex, awesome-claude-code and the MCP registries. SaaSHub and AlternativeTo follow once `/pricing` and a comparison page exist. Every listing uses one tagline and its own UTM source (14).

**Move 4: Show HN and Product Hunt.** Both run once, in stage 1, when the five-item gate is green (07). The Show HN link is Prasiddha's live terminal on a public demo repo (B11). The first comment gives the real numbers (B3). The viewer cap rises only after a load test, and a fallback page is ready (B11). Prasiddha owns the Product Hunt maker account (E12). The gate has five items. They are the new site, seat billing with `/pricing` and Ask to join on session links. The last two are two named teams with a real handoff and a live setup form with UTMs. Plugins go in at D−14, and founders answer every comment by hand (07). The target dates are about 8 Dec for Show HN and about 5 Jan for Product Hunt (01).

**Move 5: comparison pages and SEO cleanup.** The cleanup starts in weeks 1 to 2 (T27, 05). The ten thin `[seoSlug]` pages go, each redirected to the nearest real page (E1). `/ai-agents-for-slack` redirects to the Slack comparison page (E2). Telegram, Discord and WhatsApp come off the homepage (E4). Every machine-read description moves to the approved positioning. One 40- to 60-word definition goes verbatim into the homepage, docs, `llms.txt`, the schema and every listing. JSON-LD covers home and `/pricing`, and Pulse checks 20 queries a month across the AI engines (05). The six comparison pages publish after seat billing, because they quote the price (T31, A8, E6). Bloome gets its own page, which makes six (E7). The order is Slack, session sharing, Slack alternatives, Grok, Teams and Bloome. Each prices an eight-person team at $110 a month and shows a "Checked on" date (11). Leo signs off every edit (E8). Authors are named: Prasiddha for product and engineering, Leo for comparisons, Aden for customer stories (E3).

### 90-day acquisition moves

| Move | AARRR stage | Owner | Skill | Measure |
|---|---|---|---|---|
| Ten first touches a week, rising to twenty for Aden and Leo from week 3 | Acquisition | All three | `prospecting`, `cold-email` | Touches, conversations and setups per source |
| Setup sessions on qualified teams | Acquisition into Activation | Aden by default | `sales-enablement` | Setups booked per send |
| One signed post a week per founder | Acquisition | Leo, Aden, Prasiddha | `content-strategy`, `social` | Setup requests per post (UTM) |
| SEO cleanup and schema (T27, T29) | Acquisition | Prasiddha | `seo-audit`, `schema`, `ai-seo` | Sitemap URLs indexed, monthly AI mention check |
| Plugin listings (T34) | Acquisition | Prasiddha | `directory-submissions` | Setup requests with a plugin source |
| Show HN and Product Hunt | Acquisition | Prasiddha | `launch` | Setup requests and teams formed from the launch week |
| Six comparison pages (T31) | Acquisition | Leo | `competitors` | Setup requests per page |

- **Weeks 1 to 2 (14 to 25 Oct):** first touches start. Plugin repo cleanup and the Anthropic submission go in (14). Search Console access is granted (E5). The SEO cleanup starts.
- **Weeks 3 to 4 (26 Oct to 8 Nov):** Aden and Leo move to twenty touches from week 3 if week-2 rates hold. Prasiddha stays at ten. The Codex submission goes in. `/request-setup` ships with the source question (T22).
- **Weeks 5 to 8 (9 Nov to 6 Dec):** the six comparison pages publish in weeks 7 to 8, after billing and `/pricing` (E6, A8). The gate review runs on 1 Dec, a week before Show HN.
- **Weeks 9 to 13 (7 Dec to 12 Jan):** Show HN goes out about 8 Dec if the gate is green. Outreach continues with no holiday pause (A6). Product Hunt follows Show HN, at the latest about 5 Jan. The exact date is open (§13).

The stage-1 goal is ten teams set up with a card on file by 4 Jan (A3). The pipeline model behind it is 375 touches, about 35 conversations and 11 setups (09). The week-2 check replaces those assumed rates with our own.

### 12-month acquisition outlook

- **Q1, Oct to Dec 2026 (pre-seed, stage 1):** founder outreach, weekly posts, plugin listings and Show HN. Ten teams set up by 4 Jan.
- **Q2, Jan to Mar 2027 (stage 1, round in progress):** Product Hunt and the comparison pages run. The Monday numbers show which source brings teams that stay.
- **Q3, Apr to Jun 2027 (stage 2, if the gate passes):** the gate needs a quarter of new teams to reach a working first session. They must also be active in week four. Sign-up opens wider. The growth engineer is hired to work the loops in §7. G2 and SaaSHub listings go in once the site gate passes.
- **Q4, Jul to Sep 2027 (stage 2, funded):** paid acquisition starts, but only on a channel that already brings teams free (plan §8).

### Skills + tools

- **Skills:** `prospecting`, `cold-email`, `sales-enablement`, `seo-audit`, `schema`, `ai-seo`, `launch`, `content-strategy`, `social`, `directory-submissions`, `competitors`.
- **Tools:** PostHog for first-touch source and the Monday numbers. Search Console and Bing Webmaster Tools for indexing. The Lightbulb agents draft: Pulse researches leads and drafts posts and replies, and Forge builds the site tasks. Agents never publish.

## 5. Activation

> *"Once a team tries Lightbulb, does a second person join and decide on an agent's work?"*

### Activation, defined

Activation at Lightbulb is a team event, not a sign-up. It has three parts, and each has an event that counts it (plan Appendix C):

1. **Team formed.** A new workspace has two or more people within 48 hours of its creation (`team_formed`, build order T02).
2. **First handoff.** A person decides on an agent's work in week one. That means an approval card answered, a session picked up, or an agent's task moved on. A rejection counts, because a person decided (00-decisions D12, `handoff_completed`, T03).
3. **Working first session.** The first session produces a first artifact and a first approval card that the customer answers inside that session.

The stage-2 gate reads off the same events. A quarter of new teams must reach a working first session and still be active in week four (plan §8).

### Current state

The funnel leaks after sign-up, and it leaks toward one person:

- 57 accounts (50 outside) and 46 workspaces (38 outside) exist (Clerk, 12 Oct).
- 33 of 38 outside workspaces have one person (plan §9, 12 Oct).
- 24 of 50 outside sign-ups never came back after day one (plan §9).
- 10 of 20 trials got a first agent reply in the last 30 days (plan §9).
- About 8% of product-page visitors click sign up, 18 of 227 (plan §9).

The first session is single-player. The CEO asks about the business, writes a plan and delegates the work. Nothing asks who else should see it (T08). The invite card copies a join link, is admin-only, and sits after three connector cards (08, vetting Part 4). The CEO's brief says who the person is, not what to deliver, so the first run has no fixed result (T13).

Sign-up sells a solo tool. Its headline says "Your AI company starts here", and its side panel says "No shared data" (08). The welcome email is wrong in three places and reaches invitees as if they had founded a company (08 §5). No event counts a team formed or a first handoff, so none of this is measurable today (08, finding 7).

One signal points the way, with a caveat. Second Catch, the one customer inside the wedge, first broke on "adding the cofounder" (plan §9). But the most active outside workspace is a one-person business (plan §9). So the second person is the bet for the wedge, not a proven law (vetting Part 4).

### The plan

**Move 1. The founder-run setup session is the stage-1 activation path.** For the ten founding teams, a founder does by hand what the product will do later (plan, Go To Market stage 1). The founder who sourced the team runs the 45-minute session, with Aden as the default (00-decisions C1). The session names the company, connects one real tool, wires one real workflow and invites the second person. The customer answers the first approval card, never the founder (C1). The call follows the battle card and the 45-minute run sheet: qualify, name the company, connect one tool, wire one workflow, invite the second person, answer the first card. The card holds nine objections. The call never promises a discount, a refund or a date (04). Every session opens with 15 minutes on how work moves today (16). The card goes on file in the session, through a Stripe checkout with a 30-day trial and a first charge on day 31 (A4).

The ask for the second person starts at booking, not in the session. If the founder still comes alone, the session goes ahead and the first approval card becomes the invite (C4). Aden answers every setup request within one business day from his own address, with a draft from the Growth agent (F3). The target is 10 of 10 founding teams formed within 48 hours, because a founder is in the room (08 §1). No setup can be sold until the trial checkout ships, under the fix-forward rule (00-decisions, standing rule).

**Move 2. The product does the setup session's work for teams that come alone.** Measurement (T01–T03) and billing (T04–T06) ship first (build order, standing rules). The changes below follow, in order of leverage. Forge builds them in dependency order, so T07 lands before T08 and T09.

| # | Change | Owner | Build task | Measure |
|---|---|---|---|---|
| 1 | After the first reply, the CEO posts "Who should see this with you?" with an email invite in the chat | Forge (Prasiddha directs) | T08 | Invites sent in session 1, `team_formed` |
| 2 | The first approval card offers "Ask someone else to decide" and invites the decider to that card | Forge | T09 | `team_formed`, split by invitations that carry a card |
| 3 | Members, not only admins, may invite (D5) | Forge | T07 | Invitations sent by non-admins, accepted ÷ sent |
| 4 | GitHub sign-in beside Google and email (D7) | Forge | T11 | GitHub share of sign-ups |
| 5 | The onboarding band puts "Invite your team" first, with quiet one-person reminders until someone joins | Forge | T10 | Invites sent after day 1 |
| 6 | The first run ends in the room with one finished document, one approval card and three offers written as outcomes. The CEO is the only voice in the welcome, and specialists appear when the CEO hands them work | Forge | T13 | Workspaces with a document and an approval in session 1 |
| 7 | The first real tool connects from a card inside the chat, not a settings page | Forge | T13 | Connectors connected in session 1 |
| 8 | The welcome email comes from the CEO agent's address, in the agent's voice, and replaces today's (D10) | Forge | T14 | Click-through to #general |
| 9 | The phone app ships to the App Store without in-app purchase | Forge | T21 | Installs from invitations |
| 10 | "A teammate you text": the CEO's DM is home. Agents message first in one line with one action, push reads as a named teammate, and outputs sit in Library | Forge | T25 | First approval from the phone, push → open rate |

Three details matter in this list. First, the backend posts the CEO's invite line as static text, so it fires every time and can be measured (08). Second, each offer names a finished thing and the person's one part: "I'll draft the three invoices due this week. You send them in a tap" (08 §3). Third, changes 9 and 10 assume the second person is usually invited by text, which no data confirms yet (vetting Part 4). The App Store build carries one risk. An app that shows plans must not steer US users to web checkout outside Apple's rules (vetting Part 4). Sign-up keeps the emailed code beside GitHub and Google (D6). Changes 6 and 7 borrow from Meta's Muse: setup inside the chat, and ideas written as finished promises (`docs/research/2026-10-13-muse-walkthrough.md`). Grok's team assistant wakes its computer when a person starts typing, so first-reply speed is part of activation (Grok teardown, 9 Oct).

The self-serve target for the end of December is 25% of new workspaces formed as teams (08 §1). The target for a first handoff in week one is 15%. Both are assumptions. They reset after four weeks of real data.

**Move 3. The conversion pages point founders at a setup and teams at each other.** The copy for every page is in `docs/marketing/10-website-copy.md`.

- **Sign-up, two doors.** The headline becomes "Bring your team." with "Create a workspace" and "Join your team" side by side (T11).
- **`/request-setup`.** Five fields: name, company, team size, the agents you use today and what you would hand off first. "How did you first hear about us?" is required on this form (D13, T22). A founder replies within a day. The page returns 404 today (research, 13 Oct).
- **Home hero.** Headline A is "Your team and your AI employees, in one place." It is split-tested against B, "AI employees your whole team can see." The primary button is "Request a guided setup" and the secondary is "Start free" (T28).
- **Subhead.** It reads "in your team's channels", so nobody reads it as "inside Slack" (B7).
- **Buttons.** "Request a guided setup" names the founding offer and its ten spots. "Start free" says no card and 30 days.
- **Seven sections, in order.** The problem in the buyer's words, how it works, the work itself and proof. Then any model at one price, the founding offer and a FAQ. The FAQ answers four objections: Slack, Claude alone, acting without asking, and data (10).
- **Proof.** Anna's quote, Second Catch's story by name, and the revenue band "$2M+ in combined annual revenue", which stays (founder ruling). Attach its source when it is reused (vetting).
- **`/pricing`.** It says "$50 a month for up to 5, then $20 a member" and names the 30-day trial. Managed AI costs the provider's price × 1.2, or the team brings its own key. A FAQ follows, and the page ships after billing (T30).
- **Self-serve sign-up stays open** as free data. No founder time goes to it in stage 1 (business plan, Go To Market).

Note one conflict. The field table in 10 marks the source question optional. The founders' answer D13 makes it required on the setup form, and D13 governs. At about 10 visitors a day, the headline test reads as directional only (10, Measures).

### 90-day activation moves

The dates that bind are fixed. Ten teams are set up with a card on file by 4 Jan, and about three pay by 12 Jan (A3). A team must be set up by 12 Dec to pay by 12 Jan (research, question 6). Forge has not committed dates, so the rows below are in order, not in weeks.

| Order | Move | Owner | Build task | Measure |
|---|---|---|---|---|
| 1 | Activation events carry `workspace_id` and `environment`, internal workspaces excluded | Forge, with Prasiddha owning the exclusion list (F1) | T01–T03 | Zero dev events tagged `prod`, `team_formed` matches Clerk two Mondays running |
| 2 | Source captured on the workspace form | Forge | T12 | Over 80% of new workspaces with a known source |
| 3 | `/request-setup` live, answered within a business day | Forge builds, Aden answers | T22 | Setup requests by source |
| 4 | Trial checkout with the card at setup | Forge | T04–T05 | Setups with a card on file |
| 5 | Setup sessions run for qualified teams | The sourcing founder, Aden by default | By hand | 10 of 10 formed in 48 hours, first handoff in week one |
| 6 | The invite moves in the first session | Forge | T07–T10 | `team_formed` share of new workspaces |
| 7 | The first run ends with one finished thing | Forge | T13 | Document and approval in session 1 |
| 8 | Sign-up two doors, GitHub sign-in, the new welcome email | Forge | T11, T14 (after T06) | `signup_started` → `trial_started` |
| 9 | Home hero with the A/B headline test | Forge builds, Prasiddha signs the copy | T28 | Setup requests and trial starts per visitor, per variant |
| 10 | Phone app in the App Store | Forge | T21 (after T06) | Installs from invitations |

### 12-month activation outlook

- **Q1 (Oct–Dec 2026).** Events are live and setup sessions run. The invite moves ship. Ten founding teams are set up by 4 Jan. Owners: Aden for sessions, Forge for the build.
- **Q2 (Jan–Mar 2027).** The self-serve first session does the setup session's work. The phone app is in the App Store, and "a teammate you text" ships. The first read of the 25% team-formed target lands. Owner: Prasiddha.
- **Q3 (Apr–Jun 2027).** If the stage-2 gate holds, sign-up opens wider (plan §8). The growth engineer, hired from 2027, takes over the activation loops (research, Team). Owner: Prasiddha until the hire.
- **Q4 (Jul–Sep 2027).** Activation is no longer the binding constraint. Focus moves to week-4 retention and the referral loops in Sections 6 and 7.

### Skills + tools

- **Skills:** `onboarding`, `signup`, `copywriting`, `cro`, `paywalls`.
- **Events (PostHog):** T01 event hygiene, T02 join events (`workspace_created`, `member_joined`, `team_formed`), T03 handoff events, T12 source on the workspace form, T22 `setup_requested`.
- **Tools:** Clerk (GitHub sign-in, emailed code), Stripe Checkout (30-day trial), SES (the CEO agent's welcome email).

## 6. Retention

> *"Once a team is working in Lightbulb, is it still working there in week four?"*

### Current state

Retention is thin, and most of it cannot be measured yet:

- About five outside workspaces are active in a given week, between 4 and 6 over the last four weeks (plan §9).
- Week-4 active is not measurable. No sweeper emits it. `ops/growth.py` only computes it in a report, from the wrong start date (vetting Part 4). T23 adds the daily pass.
- Two subscribed workspaces went idle within two weeks. One owner said he and his brother share no projects (plan §9).
- $55 has been collected ever, and nobody pays at list (plan §9).

The trial ends in a $0 cliff. The free trial carries no card, so day 31 collects nothing (15). Nothing is emailed on days 27 to 30 (15). The lapse copy says "Pick a plan to keep your agents", which threatens a loss that does not happen (15, finding 5). Cancellation is one click in the Stripe portal, with no reason asked (15).

The agents themselves are a retention risk. Their replies read as generated text. The founder put it in his own words on 13 Oct: "it feels like slop and stuff I won't do" (research, employee voice).

Off-app notice is thin. Web gets an in-tab toast and the daily digest. Phone push reaches members with the app, and the Mac app notifies natively (vetting Part 4).

One piece works well. The daily 08:00 "N things need you" digest sends nothing on an empty day and cannot send twice (12). Every new email copies its pattern.

### The plan

**Move 1. Tell people when the team needs them.** A team product fails quietly when the second person never learns that something happened (plan item 3). The away email follows three rules (00-decisions D8):

- It sends five minutes after a DM, an @mention or an approval, and only if the person has still not read it.
- It never sends between 22:00 and 08:00 in the person's time zone.
- It sends at most one email per room every 30 minutes.

Invited members get it too (D9). It has one setting and its own unsubscribe. AppSync has no disconnect signal, so "away" comes from the read cursor and live activity (T15). The measure is the median time from "needs you" to opened. Grok's team assistant keeps a needs-you state per task and pauses routines nobody reads. That is the bar for our away email and the stage-2 nudges (Grok teardown, 9 Oct).

**Move 2. A small lifecycle set, from the CEO agent's address.** Every email has one job and comes from the CEO agent's address (12). Stage 1 ships five:

| Email | Trigger | Who gets it | Owner | Build task |
|---|---|---|---|---|
| Welcome | Workspace ready, replaces today's welcome (D10) | Owner | Forge | T14 |
| Teammate welcome | A member joins | The new member, one line | Forge | T14 |
| Away | DM, @mention or approval unread after five minutes | Every member (D9) | Forge | T15 |
| Trial ending | Day 27, the day before the day-28 card | Owner and admins | Forge | T19 |
| Thursday check-in | Weekly, founding teams | The team, in its shared channel, by email only if absent that week | Drafted by the Growth agent, sent by the founder who runs the team (D11) | By hand |

The owner nudges wait for stage 2 (build order, "Not on this list"). These are the invite nudge, the first-handoff nudge, the week-2 summary and the week-4 question email. They run on the sweeper designed in 12, which copies the digest's claim rows and sends one lifecycle email per person per day. Each email is judged against a 10% holdout, not by open rate (12, Measures).

**Move 3. Trial to paid: show the record, then keep it safe.** The sequence replaces the cliff with five rules (15, T19):

1. **Day-28 recap card.** Every member sees the team's own numbers: handoffs, approvals, agents and people. If nothing finished, the card offers "Book 20 minutes" instead of a price.
2. **Reading-only from day 31.** Everything stays, and people can still message. Agents stop until an admin picks the plan.
3. **One pause per company.** A lapse about timing gets one 30-day pause (A12).
4. **90 days, then the deletion notice.** A reading-only workspace is kept 90 days, then the owner gets the standard deletion notice (A13).
5. **No refund policy.** There is no guarantee (A1). A duplicate or unexpected charge goes through the ordinary Stripe dispute route (15 §F).

Founding teams skip the cliff. Their card is on file from setup, and the day-27 email says the first $50 charges on that date (15 §E). The save is a founder check-in on four lapse signals, not an in-app offer (15 §F).

**Move 4. Learn why 24 people left after one day.** Aden emails each of the 24 day-one churners once, from his own address (C8). Each email carries the consent line. Calls run 20 minutes (C10), and each person gets a $50 gift card on the company card (C7). He books three a week until ten are booked, and five is enough (16 §5). Every call ends with "Who else carries context by hand?", and the answer becomes a lead (16).

Notes go into a Paperclip task within an hour, with no recordings (16 §4). The "very disappointed" share is reported separately for wedge teams and small businesses, with n beside each (C9). Under five answers in a segment, the readout says "too few" (16 §4).

**Move 5. Agents that communicate like employees.** Product quality is a retention lever, so it sits here. The founders ruled that the fix lives in the model's output, not in a wrapper (13 Oct). It covers every channel an agent writes on: room replies and DMs, Live calls, email, task comments and documents, and notification text. One set of rules, one grader and one eval cover all of them (plan item 8).

- Per-role reply rules in every agent's SOUL.md are the floor. The answer comes first, in one to four sentences, with one question at most. A reply never offers work the agent will not do (plan item 8).
- A blind bake-off rates 60 to 100 test messages per role across six to eight candidate models, scored by both founders. The managed default moves to the winner (plan item 8).
- The feedback loop follows the founder's words: "Jev decides and we run stop, we should learn from that." The run-routing and Stop ledger records who routed each run and who pressed Stop (T35).
- From that ledger come the later steps. A clean Stop raises that room's bar for unprompted replies. Three Stops in two weeks suggest Mentions mode. Every number here is untested (socially aware agents).
- Teams Copilot and Grok's team assistant answer only when addressed. Our Listening mode lets agents speak unprompted, so the bar has to rise per room (socially aware agents).
- People agree on when to stay silent only about half the time (κ = 0.49). So one Stop is noise, and the plan acts on counts (socially aware agents).
- No public dataset holds team chat with AI employees. Training data comes from public developer chat and code-review threads, founder-rated synthetic threads and our own edited or ignored replies. Licences are noted in the research files but are not a block (founder ruling, `docs/research/2026-10-13-messaging-datasets.md`).
- Muse credits a written list of over a hundred behaviours, each with its own eval. Instinct puts the point in the first 30% of a reply. Neither describes tone training (employee voice).
- Model work, from examples to a tuned model, comes later and only if the eval plateaus. Custom-model work on Bedrock is deferred (founder ruling, 13 Oct).
- Measures beyond the accept rate: reply and ignore rates, reply length, replies with bullets or headings, promises with no matching task, and Jev misses (plan item 8).
- Detail: `docs/research/2026-10-13-employee-voice-and-consumer-agents.md` and `2026-10-13-socially-aware-agents.md`.

### The week-4 question and the stage-2 gate

At week four, the founder who set up each team asks one question in the Thursday check-in. The question is "How would you feel if you could no longer use Lightbulb?" (plan, Go To Market). The share answering "very disappointed" sits beside week-4 active on the Monday readout. The benchmark is 40% (16, Measures).

The stage-2 gate opens when a quarter of new teams reach a working first session and stay active in week four (plan §8). Spend waits behind that gate. One caveat holds. A week-4 miss on ten teams is a signal, not a verdict (plan §12). A sample this small runs low for size reasons alone. What result moves the plan to the small-business fallback is an open decision for Section 13.

### 90-day retention moves

| Order | Move | Owner | Build task | Measure |
|---|---|---|---|---|
| 1 | Churn emails to the 24, then calls | Aden | By hand, Paperclip task template (16) | Calls completed, reasons by segment |
| 2 | Week-4 event from a daily pass, paid invoices counted at list | Forge | T23 | Paying at list equals Stripe |
| 3 | Run-routing and Stop ledger | Forge | T35 | 100% of room runs carry a routing source, stop rate after Jev-routed runs on the Monday readout |
| 4 | Reply rules in SOUL.md, then the blind bake-off | Prasiddha, both founders rate | Research track (plan item 8) | Share of replies a founder would accept from a hire |
| 5 | Away email | Forge | T15 | Median "needs you" → opened |
| 6 | Welcome and teammate welcome | Forge | T14 | Click-through to #general |
| 7 | Thursday check-ins, week-4 question at week four | Sourcing founder, Growth agent drafts | By hand | Share of founding teams heard from each Thursday, "very disappointed" share |
| 8 | Day-28 card, reading-only, pause, 90-day rule, trial-ending email | Forge | T19 (after T05, T06) | Card → checkout rate, reactivations after day 31 |

### 12-month retention outlook

- **Q1 (Oct–Dec 2026).** Churn calls are done and synthesised. The Stop ledger and the week-4 event are live. Away and welcome emails ship. Owners: Aden, Forge.
- **Q2 (Jan–Mar 2027).** The first founding teams reach week four. The trial-to-paid sequence runs on real cohorts. The stage-2 gate is read. Owner: Prasiddha.
- **Q3 (Apr–Jun 2027).** If the gate holds, the stage-2 owner nudges ship on the 12 sweeper. Model work starts only if the eval has plateaued. Owner: the growth engineer once hired.
- **Q4 (Jul–Sep 2027).** Retention is judged on paying teams at list. Focus moves to expansion and referral (Sections 7 and 8).

### Skills + tools

- **Skills:** `emails`, `churn-prevention`, `customer-research`, `paywalls`.
- **Design references:** the lifecycle sweeper in `docs/marketing/12-lifecycle-emails.md` (digest pattern, per-person claim rows, holdout testing). The run-routing and Stop ledger, T35.
- **Tools:**
  - SES and the agent-mail send door.
  - The Stripe Dashboard for pauses and cancellations, founders only.
  - Paperclip task templates for research notes.
  - PostHog for `week4_active` and the Stop events.

## 7. Referral

> *Do retained users bring more users, and at what cost?*

### Current state

Nobody refers anyone today, because almost nobody has a team to show. 33 of 38 outside workspaces have one person (research record, 12 Oct). Invites work, but only an admin can send one (06). A public session link shows a live terminal and then stops. The viewer sees no way in, and signed-out opens are not counted (06). The old `/launch` referral code cannot fire for any workspace owner (06).

### The plan

The plan builds four doors into the product, and each one starts with real work. No door pays anyone in stage 1.

| Door | How it brings people in | State |
|---|---|---|
| First-session invite | The CEO agent asks "Who should see this with you?" after its first reply | To build (T07 to T10) |
| Session link | A viewer asks to join, and the owner's Allow makes them a member | View-only today, to build (T16 to T18) |
| Agent email addresses | Every mail an agent sends carries `{agent}@{slug}.thelightbulb.company`, plus an opt-out footer on by default (D14) | Address live. Footer to build after checking it does not exist (T26) |
| Shared channel per founding team | A `lightbulb-<slug>` channel links each team to us | Planned for the ten teams |

**The session-link door.** A founder shares a session as "Anyone with the link". An outsider watches and taps Ask to join, which teammates already have today. The owner taps Allow, and that yes also makes them a workspace member with the session's controls (D1). The switch is on by default for these sessions, and the owner can turn it off (D2). A stranger gets a second button, "Start your own workspace", with the source recorded (T16). The cap of 20 viewers rises only after a load test, and a "Full / N watching" state ships regardless (D3, T18). The public session page stays the only no-account look at the product. Nothing more is built for launch (D4).

**The shared channel.** It is our line to each founding team (C15). It carries the Thursday check-in and the light bulb note after the first handoff (C14). Any introduction a team offers arrives there first.

**The referral credit waits for stage 2.** The planned offer is "Refer a team that pays, you both get $50", paid as a Stripe balance after the first full-price invoice (06). The founder ruled it late (A11, A14). The reason is simple: nobody pays at list yet, so there is nothing to reward. No competition coupon exists either, because the competition was never set up (A10).

**The launch competition is post-product work.** It runs after product items 1 to 9. Until then, every `flickmeon.ai` path redirects to the product site (T27). Today its root still serves the competition page (vetting). The live `/launch` page is checked for promises it cannot keep yet (A10, the /launch ruling).

### What to expect

These doors amplify the work. They will not make Lightbulb viral. Only the Mac app shares sessions, and a link shows a screen only while the session is live (plan item 4). We expect single-digit session-link joins a month in stage 1 (06, estimate). The session link is a launch story first and a loop second.

### 90-day referral moves

| Move | AARRR stage | Owner | Skill | Measure |
|---|---|---|---|---|
| `member_joined` with a `via` field (T02) | Referral | Prasiddha | `marketing-loops` | Joins by door |
| Count signed-out opens, add "Start your own workspace" (T16) | Referral | Prasiddha | `referrals` | New workspaces from a session page |
| Ask to join for outsiders (T17) | Referral | Prasiddha | `referrals` | Joins from session links a week |
| Full state, then a load test (T18) | Referral | Prasiddha | `launch` | Tested viewer ceiling |
| Shared channel per founding team | Referral | Aden | `referrals` | Introductions logged |

T17 and T18 are on the Show HN gate (07). Show HN is the first real test of the session door.

### Measures

- **Joins by door:** `member_joined` split by `via` (invitation, join link, session link).
- **New workspaces from a session page:** `workspace_created` with `source: session_link`.
- **First handoff by joiners:** the share of session-link joiners who approve, reject or continue work in week one.

The decision rule comes from 06. Fewer than one join per 20 shared links with an outside viewer makes the session page a demo, and we stop investing in it.

### 12-month referral outlook

- **Q1 to Q2 (stage 1):** the four doors ship and get measured. No rewards.
- **Q3 (stage 2, after the gate):** the growth engineer works the loops. The founders decide the credit's mechanism and reward.
- **Q4 (stage 2):** the credit runs only if a referred team costs under $100 and churns no faster than others (06).

### Skills + tools

- **Skills:** `referrals`, `marketing-loops`.
- **Tools:** the `member_joined` event and its `via` field (T02), `share_link_opened` for signed-out viewers (T16), and PostHog for the funnel.

## 8. Revenue

> *"What do we charge, who pays, and how does it compound?"*

### Current state

The price is decided and cannot yet be bought. The product still bills Base $20, Workspace $50 and Ultra $150, with unlimited members and no trial (billing_service, 13 Oct). Revenue to date is $55 (Stripe, 12 Oct). Six subscriptions run on our 100% internal coupon, and nobody pays list.

### Pricing and packaging, as decided

| Line | Price | Source |
|---|---|---|
| Team plan | $50 a month for up to five members | Decision A9 |
| Each member from the sixth | +$20 a month | Decision A9 |
| Enterprise | Custom | Plan §7 |
| Managed AI | Credits at the provider's price × 1.2, or the team's own ChatGPT account or key | Plan §7 |
| Marketplace | 15% of each paid agent sale | Plan §7 |
| Later | Referral fees from services that agents buy, every fee shown to the customer | Plan §7 |

Base and Ultra are retired fully (A9). Our own workspaces move to the one plan on the internal coupon. An 8-person team pays $110 a month, against $120 on Slack Business+ (plan §6, prices checked 13 Oct). There is no Pro price. Slack appears only in the battle card, to answer "we already have Slack" with that line (B8).

### The founding-teams offer

Ten founder-led teams, set up in person by 4 Jan 2027 (A3):

1. The card goes on file in the 45-minute setup session.
2. The first 30 days are free. The first charge, $50, lands on day 31.
3. The price is locked for two years.
4. There is no guarantee and no refund promise. The free month is the risk reversal (A1). The guarantee's terms are moot (A2).
5. There are no discounts and no coupons for outside teams.
6. The trial stays 30 days. There is no 90-day free period (founder ruling, 13 Oct). The credits markup stays at × 1.2 (founder ruling, Appendix B).

A common rule of thumb says discount-takers churn about twice as often. That is a heuristic, not our data. We hold the rule because a coupon anchors the product as cheap and muddies the number the round is judged on: teams paying list.

### What billing must change, in order

The fix-forward rule governs this list. Build the real thing first. No interim lines and no billing by hand (decision A8).

1. **Seat billing (T04).** A second subscription item at $20, quantity of members above five, prorated on join and leave. It ships before any team reaches a sixth member, so no interim seat rule is needed (A7).
2. **Trial checkout (T05).** Card at setup, first charge on day 31, a two-year price lock in metadata, promotion codes switched off. This ships before the first setup.
3. **One plan (T06).** One chooser block on web and iPhone. Base and Ultra gone.
4. **Trial end (T19).** The day-28 recap, reading-only, one pause, the 90-day clock.
5. **`/pricing` and every price on the site (T30).** Then the comparison pages and the "free up to five" post, which goes out the day `/pricing` goes live (E9).

Nothing that quotes a price ships before steps 1 to 3.

### Trial-to-paid mechanics (planned)

- **Day 27:** an email from the CEO agent's address. Founding teams read "Your first $50 is charged on {date}. Nothing to do."
- **Day 28:** a recap card with the team's own numbers: handoffs, approvals, agents and people. Value comes before the ask.
- **Day 31:** reading-only, not a wall. Every message and task stays, people keep talking, and agents stop.
- **One pause:** one 30-day pause per company, offered only when the lapse is about timing (A12).
- **90 days:** the workspace stays reading-only for 90 days, then the standard deletion notice (A13).
- **The save:** a founder check-in on a lapse signal, never a discount.

### Unit economics (estimates)

| Metric | Value | Inputs and source |
|---|---|---|
| Revenue per team, 5 people | $50 a month | Plan price. Credits excluded: five of six subscribed workspaces use their own ChatGPT account (plan §9) |
| Revenue per team, 8 people | $110 a month | $50 + 3 × $20 |
| Cost per workspace | $5 to $15 a month | AgentCore, $83 for 15 runtimes in 11 days (plan §11). The $5 floor is unmeasured |
| Margin after Stripe, 5 people | 66% to 87% | Stripe ~$1.75 on $50 (vetting) |
| Margin after Stripe, 8 people | about 83% to 92% | Stripe ~$3.49 on $110. Our arithmetic |
| Credits margin | about 9% of credit revenue | × 1.2 less OpenRouter 5.5% and Stripe (vetting) |
| Fixed cost | about $1.7k a month | AWS, dev and prod. October pace $1,784 (plan §9) |
| Breakeven | 40 to 54 teams at five people | Fixed cost ÷ $33 to $43 contribution (vetting) |
| Acquisition cost | Founder hours, unpriced | 45-minute setup, weekly check-ins, about 30 first touches per team set up (our arithmetic from the outreach quota) |
| Placeholder CAC | $600 | One year of the smallest plan (skill baseline). Not measured |
| Payback on $600 | 14 to 18 months at 5 people, about 6 at 8 | $600 ÷ monthly contribution |
| Annual retention, LTV | Unknown | No team has paid list. LTV/CAC above 3 needs about 42 to 54 months at five people |

The plain reading: at five people, a hand-set-up team pays back slowly. Seats from the sixth member and lower founder hours per team are what make the motion work.

### The 12-month revenue shape

| Date | Teams | MRR at list |
|---|---|---|
| 4 Jan 2027 | Ten set up, card on file | $0 |
| 12 Jan 2027 | About three paying | About $150 |
| May 2027 | At least six paying | At least $300, plus seats |
| Oct 2027 | Illustrative: 26 teams if stage 2 adds four a month from June | About $1.3k, still below breakeven |

The shape is linear and small. There is no hockey stick in this plan. The 7% weekly bar in plan §11 is the round's test, not this forecast. A team must be set up by 12 Dec to pay by 12 Jan.

### Open decisions for the founders

1. Do founder hours count in CAC, and at what hourly figure?
2. What date does Forge commit for T04 and T05?
3. On a member leaving mid-cycle, is the prorated amount credited?
4. Which failed-payment retries and dunning emails do we run?
5. Do we offer annual billing, and at what price?
6. What share of target teams pass five people? Seat revenue depends on it.

### Skills and tools

- **Skills:** `pricing`, `paywalls`, `churn-prevention`, `revops`, `emails`
- **Tools:** Stripe (subscriptions, quantities, trials, metadata), PostHog (`trial_converted` on paid invoices only), the Monday readout

## 9. 90-day roadmap

This roadmap runs from Wednesday 14 Oct 2026 to Tuesday 12 Jan 2027. Its goal is ten founding teams set up on the offer by 4 Jan (00-decisions A3). About three of them pay at list by 12 Jan (A3). Every move below names its AARRR stage and its owner. It also names the build tasks it waits on (T ids from 18-build-order) and its measure.

Owners are the three founders, Forge (the engineering agent), Pulse (the Growth agent), and the MKT385 interns. Agents research, draft and build. A founder signs and sends every public word (business plan §8).

### The order of work

The build order is fixed by the fix-forward rule (00-decisions A8). We build the real thing before any page or call depends on it. Nothing is done by hand in the meantime.

1. **Measurement first.** T01 to T03 make team formed and first handoff countable. Without them the Monday numbers are hand counts.
2. **Billing second.** T04 (seat billing) and T05 (trial checkout) come before the first setup session. T06 (one plan) follows them.
3. **The first setup waits on Forge.** The offer puts a card on file at setup. So no team can be set up before T05 ships. The roadmap assumes T05 is live by Friday 30 Oct (01 §B, week 3). That date is not yet committed (section 13, decision 1).
4. **Prices last.** `/pricing` (T30), the comparison pages (T31) and any copy that quotes a price wait for T04 to T06.

The deadline that matters is Saturday 12 Dec. A team set up after that date cannot reach its day-31 charge by 12 Jan (19-plan-vetting, §8 row 6). Each week T05 slips costs about 1.2 setups at the base rate (01 §A).

### Weeks 1–2: Unblock (14–25 Oct)

Start outreach on day one, because the ask books a call by reply and needs no product work (01 §C). Start the three measurement tasks and billing at the same time.

| Week | Move | Stage | Owner | Depends on | Measure |
|---|---|---|---|---|---|
| 1 | First 60 qualified names from HN and r/ClaudeAI threads, plugin users and founder networks | Acquisition | Pulse drafts, interns research, Leo reviews | none | 60 names with source by 18 Oct (01 §B) |
| 1 | Two asks and a three-touch sequence, with ask A and ask B picked by the founders | Acquisition | Pulse drafts, all three founders sign | none | Reply rate per ask |
| 1–2 | First touches: 15 in week 1, 30 in week 2 | Acquisition | Prasiddha, Aden, Leo | none | Touches and conversations per founder |
| 1 | Outreach sheet (touch, source, ask, stage) until events ship | Acquisition | Pulse | none | Every touch logged by Monday |
| 1–2 | Event hygiene, join events, handoff events | Activation | Forge, Prasiddha reviews | T01, then T02 and T03 | Zero `org_` persons and `team_formed` matching Clerk (18) |
| 1–2 | Seat billing, then the trial checkout | Revenue | Forge, Prasiddha reviews | T04, then T05 | A test setup ends with a card and a trial end date |
| 1 | Exclusion list: internal workspaces and Lightbulb staff user ids | Activation | Prasiddha | none | Readout excludes staff (F1, vetting) |
| 1–2 | Site cleanup: channels off, thin pages redirected, `flickmeon.ai` redirected | Acquisition | Forge | T27 | No 404s on old URLs |
| 1 | Contact Habits365 as the first setup candidate | Acquisition | Aden | none | Call booked |
| 1–2 | Record Anna and the Second Catch story | Acquisition | Aden | none | Recording done |
| 1–2 | Battle card and setup guide final | Activation | Pulse drafts, Aden owns | none | Used on the first call |
| 1–2 | Bios, LinkedIn page, Search Console, Reddit karma, accelerator contacts, Aden's three past LinkedIn posts | Acquisition | Each founder | none | Done by 25 Oct (F4, E5, C11, C12, C13) |

### Weeks 3–4: Foundation (26 Oct–8 Nov)

The first setup sessions start here, but only once T05 works end to end. The week-2 check sets the outreach volume for the rest of the quarter.

| Week | Move | Stage | Owner | Depends on | Measure |
|---|---|---|---|---|---|
| 3 | Week-2 quota check (Mon 26 Oct): 30 or 50 touches a week | Acquisition | Prasiddha, Aden, Leo | Outreach sheet | Under 7 conversations per 100 touches: go to 50. Under 4: rewrite the ask (01) |
| 3–4 | 50 touches a week: Aden and Leo at 20 each, Prasiddha at 10 | Acquisition | Prasiddha, Aden, Leo | Week-2 check | Conversations per founder (A5) |
| 3–4 | First setup sessions, run by the sourcing founder, Aden by default | Activation, Revenue | Aden | T05 | Card on file and a team formed within 48 h |
| 3 | One plan, with Base and Ultra retired | Revenue | Forge | T04, T05, then T06 | Chooser to Checkout start rate |
| 3 | Workspace form with the source question, then `/request-setup` | Acquisition | Forge, with Aden answering within a business day | T12, then T22 | Setup requests by source, 100% self-reported |
| 3–4 | Remaining Monday events, then the Monday readout | Revenue | Forge, then Pulse | T23, then T24 | Paying at list equals Stripe |
| 4 | `/pricing` with the plan's price | Revenue | Forge, Prasiddha signs | T30 | Pricing to setup or start free |
| 4 | Homepage hero: two buttons, headline test | Acquisition | Forge, Prasiddha signs | T28 | Setup requests per visitor, per variant |
| 3–4 | Away email | Retention | Forge | T15 | Median "needs you" to opened |
| 3–4 | Run routing and Stop ledger | Retention | Forge | T35 | 100% of room runs carry a routing source |
| 4 | Data page | Activation | Forge, Prasiddha signs | T33 | Calls that raise data and still book |
| 4 | Lifecycle emails drafted for stage 2: first week, week four, "needs you" | Retention | Pulse drafts, owner open | none | Signed by an owner (section 13, decision 5) |

### Weeks 5–8: Velocity (9 Nov–6 Dec)

This block carries the setups and the product doors the launch needs. The gate review on 1 Dec decides whether Show HN goes on 8 Dec.

| Week | Move | Stage | Owner | Depends on | Measure |
|---|---|---|---|---|---|
| 5–8 | 50 touches and one to two setups a week | Acquisition, Activation | Prasiddha, Aden, Leo | T05 | Cumulative setups against the pipeline table |
| 5–8 | Members invite, the CEO asks "Who should see this with you?", the first approval asks who decides, and the band puts the invite first | Activation | Forge | T07, T08, T09, T10 | `team_formed` share of new workspaces |
| 5–6 | One finished thing in the first run, and a welcome email from the CEO agent | Activation | Forge | T13, T14 | Working first sessions |
| 6–7 | Homepage sections, sign-up with two doors and GitHub, and the get-started doc | Acquisition, Activation | Forge, Prasiddha signs | T29, T11, T32 | Setup requests per visitor |
| 5–7 | Session page counts opens, Ask to join for outsiders, and "Full / N watching" with a load test | Referral | Forge, Prasiddha | T16, T17, T18 | A stranger reaches a seat in under two minutes (07 gate 3) |
| 6 | Plugin licence and README, then submissions on Tue 24 Nov (D−14) | Acquisition | Prasiddha | T34 | Listing live |
| 5–7 | Anna case study and the Second Catch story by name | Acquisition | Aden, Pulse drafts | none | Vetted by a founder (B4) |
| 7–8 | Six comparison pages | Acquisition | Forge, Leo signs | T31 | Setup requests per page |
| 6 | App Store build without in-app purchase | Activation | Forge, Prasiddha | T21 | Installs from invitations |
| 5–8 | Opt-out footer on agent mail | Referral | Forge | T26 | Sign-ups with `utm_source=agent_email` |
| 6 | First week-4 question to the earliest team | Retention | Sourcing founder, Aden by default | none | "Very disappointed" share, with n (C9) |
| 7 | Week-6 rate check (Mon 23 Nov): re-fit the pipeline | Acquisition | Pulse, Prasiddha | Readout | Rates replace assumptions |
| 8 | Gate review (Tue 1 Dec, D−7) on 07's five items | Acquisition | Prasiddha signs, all three attend | Gate items | Five green, or a new date |
| 8 | First day-31 charges | Revenue | none | T05 | `invoice_paid` with `at_list` |

### Weeks 9–12: Compound (7 Dec–12 Jan)

This block runs four weeks plus the week of 4 Jan, so the quarter closes on the 12 Jan read. There is no holiday pause (A6).

| Week | Move | Stage | Owner | Depends on | Measure |
|---|---|---|---|---|---|
| 9 | Show HN, Tue 8 Dec, 08:00 PT, if the gate is green | Acquisition | Prasiddha, with Aden and Leo on comments | 07 gate | 5 or more setup requests from launch sources in the week (07) |
| 9 | Product Hunt, Thu 10 Dec, 00:01 PT, self-hunted | Acquisition | Prasiddha | Show HN done | Setup requests tagged `producthunt` |
| 9–10 | Launch inbound answered within a business day | Activation | Aden, Pulse drafts | T22 | Requests answered on time |
| 9 | Last setups that can pay by 12 Jan: by Sat 12 Dec | Revenue | Aden | T05 | Setups dated by 12 Dec |
| 10 | Directories batch | Acquisition | Pulse, Prasiddha | T34 | Listings live |
| 9–12 | Touches and setups through the holidays | Acquisition | Prasiddha, Aden, Leo | none | Weekly touch floor met |
| 9–11 | Day-28 trial recap, reading-only lapse and workspace export | Revenue, Retention | Forge | T19, T20 | Card to checkout rate |
| 9–12 | Week-4 checks and save plays | Retention | Aden | Readout | Week-4 active, by name |
| 9–12 | A light bulb in the post after each first handoff | Referral | The sourcing founder | none | Teams thanked (C14) |
| 13 | Ten teams set up by Mon 4 Jan | Activation | Aden | T05 | Count with names |
| 13 | Quarter readout (Mon 11 Jan) and a draft of the stage-2 gate read for Mon 1 Feb | Activation | Pulse, Prasiddha | T24 | Readout delivered |
| 13 | About three paying at list by Tue 12 Jan | Revenue | none | T05, T23 | Stripe invoices at list |

### The launch window

Show HN and Product Hunt launch when all five of 07's gate items are green, not on a date (07 §A). Prasiddha signs off at the gate review.

1. The website brief has shipped: headline, "Request a guided setup" first, "Start free" second (T28, T29).
2. Seat billing and the trial checkout work, then `/pricing` exists (T04, T05, T06, T30).
3. A signed-out stranger goes from a session link to a seat in the workspace in under two minutes (T17, with T18's load test).
4. Two founding teams have a real handoff we may show: Anna and Second Catch, named (B2).
5. `/request-setup` is live with the source question, and UTMs reach the Monday readout (T12, T22).

The plan targets Show HN on Tue 8 Dec and Product Hunt on Thu 10 Dec (07 §F). If the gate is not green on 1 Dec, Show HN moves to the first Tuesday after it turns green. Product Hunt moves behind it, with Tue 5 Jan as the latest planned date (01 §B).

### The pipeline model

The pipeline runs touches → conversations → setups booked → set up → two or more people → first handoff → paying. The rates are 01's assumptions, each labelled low or medium confidence (01 §A):

- touch to conversation: 10%, one week later
- conversation to setup: 40%, one week later
- setup to team formed: 85%
- team formed to first handoff: 75%, the following week
- setup to paying at list: 50%, five weeks later

The table below drops 01's holiday reductions, because the founders ruled out a pause (A6). The base case is the 30-touch floor. The decided plan is 50 a week from week 3 (A5), shown in the last column.

| Wk | Mon | Touches | Conv. | Setups | Teams formed | First handoff | Paying | Cum. setups | Cum. paying | Cum. setups at 50/wk |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Wed 14 Oct | 15 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| 2 | 19 Oct | 30 | 1.5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| 3 | 26 Oct | 30 | 3 | 0.6 | 0.5 | 0 | 0 | 0.6 | 0 | 0.6 |
| 4 | 2 Nov | 30 | 3 | 1.2 | 1.0 | 0.4 | 0 | 1.8 | 0 | 1.8 |
| 5 | 9 Nov | 30 | 3 | 1.2 | 1.0 | 0.8 | 0 | 3.0 | 0 | 3.8 |
| 6 | 16 Nov | 30 | 3 | 1.2 | 1.0 | 0.8 | 0 | 4.2 | 0 | 5.8 |
| 7 | 23 Nov | 30 | 3 | 1.2 | 1.0 | 0.8 | 0 | 5.4 | 0 | 7.8 |
| 8 | 30 Nov | 30 | 3 | 1.2 | 1.0 | 0.8 | 0.3 | 6.6 | 0.3 | 9.8 |
| 9 | 7 Dec | 30 | 3 | 1.2 | 1.0 | 0.8 | 0.6 | 7.8 | 0.9 | 11.8 |
| 10 | 14 Dec | 30 | 3 | 1.2 | 1.0 | 0.8 | 0.6 | 9.0 | 1.5 | 13.8 |
| 11 | 21 Dec | 30 | 3 | 1.2 | 1.0 | 0.8 | 0.6 | 10.2 | 2.1 | 15.8 |
| 12 | 28 Dec | 30 | 3 | 1.2 | 1.0 | 0.8 | 0.6 | 11.4 | 2.7 | 17.8 |
| 13 | 4 Jan | 30 | 3 | 1.2 | 1.0 | 0.8 | 0.6 | 12.6 | 3.3 | 19.8 |

At the floor, 375 touches give about 35 conversations and 11 setups by the week of 28 Dec. That is about 12 setups by 4 Jan and 3.3 paying by 12 Jan (recomputed from 01 §A). At 50 a week, ten setups land by about 7 Dec and about five teams pay by 12 Jan (01 §A stretch). The offer has ten founding spots, so extra volume buys margin against slips, not more founding teams (business plan, The website).

Inbound from posts and the launch is not in the table. It is upside, reported in its own row. Week 3's setups assume T05 is live by 30 Oct. If it is not, shift every setup column right by the slip.

### The Monday readout

Pulse writes one page by 09:00 every Monday into the founders' channel, never the repo (01 §D, T24). Until T24 ships, Pulse fills it from the outreach sheet, Clerk and Stripe by hand.

```
Lightbulb · Monday readout · week of <date> · week <n> of 13
Source: PostHog + Clerk + Stripe. External workspaces only (Appendix C).
Lightbulb staff messages and members excluded.

THE FIVE NUMBERS          this wk  last wk  cum  target cum (pipeline table)
1 Setup requests            _        _       _      _
2 Teams formed (2+ in 48h)  _        _       _      _
3 First handoff in wk 1     _        _       _      _
4 Week-4 active             _ of _   _ of _  n/a    a quarter (stage-2 gate)
5 Paying at list            _        _       _      _
On the wall: teams with 2+ people and a completed handoff = _ (names)

TRAFFIC BY SOURCE   visitors → setup request → start free
OUTBOUND BY FOUNDER AND ASK   touches · conversations · booked · set up · 2+ · handoff · paying
ACTIVE SETUPS   team · trial day · 2+? · handoff? · week-4 answer · card on file?
VARIANCE   one line per number more than 30% off target, with the cause
DECISIONS NEEDED   three at most
```

Thursday's check-in reads the same page team by team (01 §E). Every number prints with the workspace names behind it (09 §6).

### If the quarter slips

Cut in this order, one line at a time, and only after the line above it is gone (01 §F).

1. A teammate you text on the phone (T25).
2. Product Hunt. Keep Show HN.
3. The homepage headline test. Ship the chosen headline.
4. Directories and comparison pages (T31).
5. Founder posts drop from one each a week to one company post a week.
6. The App Store listing (T21). Second people use the web.
7. Lifecycle emails beyond "needs you".
8. Show HN moves to January.

Never cut these, because they are the stage:

- the touch floor, setup sessions and Thursday check-ins
- the Monday readout
- the away email (T15) and billing (T04 to T06)
- the events (T01 to T03, T22, T23)

## 10. 12-month outlook

### Framing

**Budget method.** Neither method in the skill can run yet. Revenue-based budgeting needs ARR, and list ARR is $0 (business plan §9). Goal-based budgeting needs cash, burn and tool spend, and all three are blank (plan §11, research.md). Paid spend is $0 by design. The real currency is founder hours, and their cost per team is unpriced (section 13, decision 3).

**Growth pattern.** Plan for linear growth with one step. While founders do the selling, setups grow at about 1.2 a week at the floor (01 §A). The step comes when the stage-2 gate opens and sign-up opens more widely. From then the bar is paying teams growing 7% week over week (plan §11). That rate doubles the business about every ten weeks (plan §11). We do not plan on the step before the gate shows it.

**S-curve layering.** Each curve starts before the one before it plateaus (growth-patterns).

- **Channel curve 1, now:** founder outreach to named teams. It plateaus at founder capacity, about 50 touches a week (A5).
- **Channel curve 2, next:** product loops. These are the first-session invite, session links with Ask to join, the agent-mail footer and, in stage 2, a referral credit (06, A11).
- **Channel curve 3, last:** paid, only on a channel that already brings teams free (plan §8).
- **Market curve:** small teams first, then companies in stage 3 (plan §8).

Effort splits about 40/30/30 across current, next and experimental work, measured in hours, not dollars (research.md).

### The four quarters

| Quarter | Funding state | Focus | Milestones | KPI targets |
|---|---|---|---|---|
| Q4 2026 (Oct–Dec) | Tier 1: organic only, $0 paid, no hires | Stage 1: billing, measurement, ten founding teams | T01 to T06 shipped. Setups from late Oct. Show HN on 8 Dec if 07's gate is green. Plugin listed | 10 set up with a card by 4 Jan (A3). Teams formed at 85% or more of setups (01). First handoff at 75% or more of teams formed (01). Unattributed new workspaces under 20% (09) |
| Q1 2027 (Jan–Mar) | Tier 1 until the round closes | Read the stage-2 gate | About 3 paying by 12 Jan. First gate read Mon 1 Feb, then monthly. Fallback decision on the founding cohort. Growth engineer hired only if the round has closed and the gate has passed | 3 paying at list by 12 Jan (A3). About 5 by mid-Feb (01 §A). A quarter of new teams with a working first session and active in week four (plan §8) |
| Q2 2027 (Apr–Jun) | Partial Tier 2 if the $2M SAFE has closed | Stage 2: open sign-up more widely and let the loops sell | 6 paying by May. Sign-up opened more widely after the gate. Growth engineer on the loops. Lifecycle email live. Referral credit decided. Launch competition set up. A first paid test only on a proven free channel | 6 paying at list by 31 May (A3). Paying teams up 7% week over week once sign-up opens (plan §11). Week-4 gate holds on the self-serve cohort |
| Q3 2027 (Jul–Sep) | Partial Tier 2 | Stage 3 starts, if teams pull in their companies | SSO, admin permissions and approvals, a company plan. First service vendors signed. First sales hire | About 20 to 40 paying teams by 30 Sep, if the 7% bar holds from mid-May (computed: 6 to 10 teams × 1.07^20). First company plan sold. First vendor signed |

Q3's milestones are triggers, not dates. The plan has no dates or counts for stages 2 and 3 yet (19-plan-vetting, §8). Stage 3 starts when founding teams ask for SSO and admin controls, not on 1 Jul.

Operator-owner small businesses stay the fallback only, named in the business plan's Risks (plan §12). No outreach goes to them in stage 1. Existing small-business users are still interviewed and counted apart (C9). They become the main bet only on the trigger in section 13, decision 2.

### What changes when the round closes

The round is $2M on a SAFE at a $14M post-money cap, for 24 months (plan §11). Its close date is not in the materials.

- **Salaries and runway.** Three founders are paid, and the plan gets 24 months to reach the seed bar (plan §11).
- **The growth engineer.** This is the first hire, not a marketer. The hire works on the invite, session-link and referral loops (plan §8). It waits for the gate, so the earliest start is after the 1 Feb read (19-plan-vetting, §11 row).
- **Paid tests.** A Tier 2 test budget becomes possible after the gate passes (plan §8). It goes only to a channel that already brings teams free (plan §8).
- **Tools.** Spend can rise where a tool saves founder hours. No paid analytics add-ons are planned (F2).

### What does not change

- The ICP: founder-led teams of 3 to 15 that already run agents for real work (plan §5).
- Every public post, email and listing is signed by a named founder. Agents never publish (plan §8).
- No discounts, outside coupons or guarantee. The free month is the risk reversal (A1).
- Fix forward: the real thing ships before copy depends on it (A8).
- The five Monday numbers and the north star stay the scoreboard (section 13).
- No paid acquisition before the gate, whatever the bank balance (plan §8).
- The inbound launch and the product loops run in stage 1. The gate holds only what costs money (business plan, Go To Market).

## 11. Marketing operations stack

This section shows who does the work in §4 to §10, and with what. Lightbulb has no marketer and no paid budget. The plan still runs, because the founders do the work with agents and wired tools.

### The thesis

Three founders, the marketing-skills library, three Lightbulb agents and a set of wired tools can do the work of a larger team. The founders keep strategy, sign-off and every customer conversation. The agents research, draft, count and build. The skills give each task a tested method, so nobody invents a method under deadline.

The proof so far is one day of output. On 13 October, research agents ran 25 skills across 17 workstreams (`docs/marketing/01` to `17`). The founders answered 72 decisions the same day. The build order turned the result into 35 Forge-ready tasks (T01 to T35). That is planning output, not results. No skill-driven move has reached a customer yet.

Two rules limit the stack. Agents never publish, send or post in a founder's name. A founder signs every public word. The second rule is fix-forward: no copy, page or count depends on a feature before the feature ships.

| Part of the stack | Real today | Depends on |
|---|---|---|
| Three founders own the channels (Leo X and Reddit, Aden LinkedIn and setups, Prasiddha HN, Product Hunt, plugins) | Yes | Nothing |
| **Pulse** (Growth agent) researches, drafts outreach and replies, and writes the Monday readout | Drafting, yes. The readout is a hand count | T24 for an event-based readout |
| **Forge** (engineering agent) builds the site and product tasks from the Paperclip board | Yes. Founders and Forge merged about 1,359 pull requests since 1 September | The board loaded with T01 to T35 |
| **Friday** (the founders' assistant agent) prepares a dossier before each setup call (17 D3) | No. The materials give Friday no marketing duty yet. This plan proposes one | Founder confirmation |
| The five Monday numbers from events | No. None has an event yet | T01 to T03, T22, T23 |
| A sellable offer at the plan's price | No | T04 to T06 |
| MKT385 interns find technical founder teams | Starting | Founder briefing |

### Skills mapped to AARRR stages

Every skill below already produced a workstream, except where the row says otherwise.

| Stage | Skill | Workstream that used it | Move it executes |
|---|---|---|---|
| Acquisition | `prospecting` | 02 | The lead list: 28 companies, 9 warm, refreshed weekly by Pulse |
| Acquisition | `cold-email` | 03 | Two first-touch asks split-tested, then days 3, 8 and 16 |
| Acquisition | `content-strategy`, `social` | 13 | One founder-signed post a week each, drafted by Pulse on Fridays |
| Acquisition | `seo-audit`, `schema`, `ai-seo` | 05 | Site audit, JSON-LD for home and `/pricing`, AI-search baseline |
| Acquisition | `competitors` | 11 | Six `/compare/*` pages, published after `/pricing` (T31) |
| Acquisition | `launch` | 07 | Show HN about 8 December and Product Hunt in early January, behind the five-item gate |
| Acquisition | `directory-submissions` | 14 | The plugin in Anthropic's directory first, then community lists |
| Activation | `onboarding`, `signup` | 08 | The team-first first session and two sign-up doors (T07 to T13) |
| Activation | `copywriting`, `cro` | 10 | Homepage, `/pricing`, `/request-setup`, the headline test |
| Activation | `sales-enablement` | 04 | The 45-minute setup run sheet, battle card and nine objections |
| Retention | `emails` | 12 | Nine emails from the CEO agent's address (T14, T15) |
| Retention | `customer-research` | 16 | The week-4 question, churn calls, setup discovery |
| Referral | `referrals`, `marketing-loops` | 06 | Ask to join on session links (T16 to T18), the first-session invite |
| Revenue | `paywalls`, `churn-prevention` | 15 | Day-28 recap, reading-only day 31, one pause, the save script (T19) |
| Revenue | `pricing` | None yet | Planned for Q2: test the seat line once three teams pay |
| Cross-cutting | `analytics`, `attribution` | 09 | Tracking plan, source question, the Monday readout queries |
| Cross-cutting | `product-marketing` | Context file v8 | Positioning, ICP and voice that every draft reads first |
| Cross-cutting | `marketing-plan`, `marketing-ideas` | 01, 17, this plan | The 90-day calendar and the idea bank in §12 |

Each skill runs the same way. Pulse or a founder calls the skill, reads the context file first and follows the skill's method. The output is a draft, a workstream file or a Forge-ready task. A founder reviews it before anything leaves the building.

The stack does not fill the ownership gaps. Nobody owns lifecycle email, the website as a whole or analytics QA today. A skill can draft the nine emails, but a person must own whether they ship. §13 lists them as decision 5.

### MCPs and APIs mapped to stages

| Tool | Stage | What it does here | Wired now | To wire, and who |
|---|---|---|---|---|
| PostHog (project 380894) | Activation, Retention | Event store for four of the five Monday numbers | Yes. Counts run 6 to 15 times high without the data guards | T01 to T03, T23. Prasiddha owns the exclusion list |
| Stripe | Revenue | Paying at list, read from paid, undiscounted invoices | Yes | T04 to T06 to sell the plan. `invoice_paid` in T23 |
| Clerk | Activation, Referral | Membership truth for "team formed" | Yes | T02 emits the join events |
| Vercel | Acquisition | Hosting and a cookieless visitor count | Hosting yes. Web Analytics off | Workstream 09 task 10, Forge |
| Search Console and Bing | Acquisition | Indexing and query impressions | No | Prasiddha grants access (E5) |
| GitHub | Acquisition, cross-cutting | Site code, Forge's pull requests, the public plugin repo | Yes | T34: MIT licence and a README for a stranger |
| Lightbulb MCP | Cross-cutting | Founders read rooms and update tasks from Claude Code or Codex | Yes | Nothing |
| Paperclip board | Cross-cutting | Forge's queue, the Thursday fix list | Yes | A founder signs in to the hosted connection, clears the old board, then loads T01 to T35 (board ruling) |
| Chrome bridge | Acquisition | Reads Reddit threads and comments, which Reddit's search does not return | Yes | Nothing (founder ruling) |
| LiveKit and Recall (Meet) | Activation, Retention | Voice in rooms. Agents join Google Meet calls | Live in the product | No marketing use planned. A Q2 test: the CEO agent joins a setup call |
| Claude Code and Codex plugin marketplaces | Acquisition | Developers find the `lightbulb` plugin | No. Repo public, 0 stars, no licence, not listed | T34, then submit (14, week 2). Codex needs a hooks-free build (E13) |

No SEO data API is wired. Search Console covers stage 1. The plan adds one only if the comparison pages need keyword data.

The wired tools are only as good as their filters. Workstream 09 sets six data guards, and every count in the readout applies them. Each query keeps production events only. The `flickmeon.ai` launch site is a separate line. Coupon subscriptions never count as paying. Our own workspaces are excluded by one list of slugs. Counts use workspaces, not people. No percentage is printed on a base under ten.

### A concrete example: one week of the loop

This is the week the plan runs from week 1. The rows marked "by hand" become automatic when the named task ships.

| Day | Step | Who | Tool | State |
|---|---|---|---|---|
| Monday 09:00 | Write the one-page readout (01 §D): five numbers, named workspaces, pipeline by founder | Pulse | Clerk, Stripe, outbound sheet | By hand until T24 |
| Monday | Pipeline review, 30 minutes. At most three decisions | Three founders | The readout | Ready |
| Monday to Wednesday | Draft first touches from the lead list. Prasiddha sends 10. Aden and Leo send 10, then 20 from week 3 if week-2 rates hold | Pulse drafts. Founders send | Lead list (02), own inboxes | Ready |
| Daily | Thread alerts on the pain. Pulse drafts each reply into an approval card. The founder edits and posts by hand | Pulse, Leo, Prasiddha | Approval cards in our workspace | Planned (17 B1, D1) |
| Tuesday to Wednesday | Setup sessions, 45 minutes, with a dossier prepared before each | Aden by default. Friday drafts the dossier | Run sheet (04) | Waits on T05 for a card on file |
| Thursday | Founders' check-in (01 §E). Pulse drafts each team's message. The founder who runs the team sends it | Pulse, founders | Each team's `lightbulb-<slug>` channel | Ready once teams exist |
| Thursday | The fix list goes to Forge, ranked by "blocks a handoff", each with an owner | Forge, a founder | Paperclip board | Ready |
| Friday | Next week's three post drafts and a thread shortlist land in `#marketing-drafts` | Pulse | Our workspace (13 C4) | Planned |
| Friday | The weekly build log: one real handoff from our own workspace | Rotating founder | X, LinkedIn, Reddit | Planned (17 F1) |

The week costs founder hours, not dollars. Pulse and Forge take the counting, drafting and building. The founders keep the sends, the calls and the sign-offs, because those are what a founding team answers. Nobody has priced the founder hours yet, so blended CAC is unknown. §13 lists it as decision 3.

The week changes shape as tasks ship. After T24, a script prints the readout and Pulse adds the causes beside each number. After T22, setup requests arrive with a source answer, so the Monday review sees which channel feeds setups. After T26, every agent email a customer sends carries one footer line, and that source appears in the readout too.

The loop fails in a known place. If T05 slips, no setup can carry a card on file, and the Tuesday sessions have nothing to sell. The Monday readout reports that slip first.

### Capability unlocks by funding stage

| Stage | People | Tools | Channels live | Paid spend |
|---|---|---|---|---|
| **Now** (pre-seed, Tier 1) | Three founders, Pulse, Forge, Friday, MKT385 interns | Everything in the table above, plus the skills library | Founder outreach, Reddit, X, LinkedIn, HN, plugin listings, Show HN and Product Hunt behind the gate | $0. Tools and $50 research gift cards only |
| **After the $2M SAFE closes** (partial Tier 2) | Plus a growth engineer from 2027, titled Growth Engineer | Same tools, all wired | Same channels, plus the inbound launch | Still $0. The round pays salaries and runway first |
| **After the stage-2 gate** | The growth engineer works the loops | One ad account, for one channel | Wider sign-up, referral credit, newsletters, podcasts, App Store, launch competition | One paid test, on a channel that already brings teams free. Size to decide |
| **Stage 3: companies** | Plus the first sales hire | Single sign-on and admin controls in the product | Company plans, service vendors | Decided at the seed round |

The round buys time, not channels. It pays three founders and the runway to finish stage 1 properly. Paid acquisition stays at zero until a free channel proves it brings teams. That keeps the first ad dollar on a channel with a known close rate.

The stage-2 gate is a quarter of new teams reaching a working first session and staying active in week four. The first marketing title is Manager or Lead, never VP or CMO. The skill's model puts a strategist first. Lightbulb puts the growth engineer first on purpose. Strategy stays with the founders until the gate proves a motion worth handing over.

### Who owns what

The full RACI for every move sits in §13. In short, Prasiddha owns measurement, launches and the plugin listings. Aden owns setups, interviews and customer stories. Leo owns X, Reddit and the comparison pages. Pulse drafts for all three. Forge builds for all three.

## 12. Tactical idea bank

§4 to §8 say what Lightbulb is doing. This section maps what is possible. It places all 139 ideas from the `marketing-ideas` library, and the 32 ideas from workstream 17. A 17 idea that matches a library idea sits in that row. The rest appear as Lightbulb-specific rows numbered L1 to L23. Each idea sits once, under its main stage.

In "Maps to", numbers such as 05 are workstreams, codes such as C14 are founder decisions, and T-codes are build tasks.

### Status legend

- **Now**: in the 90-day plan, 14 October to mid-January 2027, or runs beside it at no new cost.
- **Q2**: January to March 2027, after billing, the events and the site gate.
- **Q3+**: after the SAFE closes or the stage-2 gate passes.
- **Q4+**: long game, stage 3 (companies).
- **Skip**: wrong for the voice, the model or this stage. The reason is in the row.

### 12.1 Acquisition

| # | Idea | Stage | Status | Reason or first step | Maps to |
|---|---|---|---|---|---|
| 1 | Easy Keyword Ranking | Acquisition | Now | Target "share Claude Code session" and other wedge queries with the three blog outlines | 13, T32 |
| 2 | SEO Audit | Acquisition | Now | Done 13 Oct. Record the Search Console baseline, then rerun each quarter | 05, E5 |
| 5 | Content Repurposing | Acquisition | Now | One real handoff becomes the build log, a LinkedIn post, an X post and a Reddit answer | 13 |
| 7 | Internal Linking | Acquisition | Now | Link home, `/docs` and the session-sharing page as the site is rebuilt | 05, T29, T32 |
| 10 | Parasite SEO | Acquisition | Now | Founder posts on Reddit, HN, LinkedIn and X carry the full name | 13, 17 E4 |
| 12 | Marketing Jiu-Jitsu | Acquisition | Now | Turn "free glue is good enough" into the contrast lines on calls and pages | 04, PMM v8 |
| 37 | Reddit Keyword Research | Acquisition | Now | Free keyword alerts on "handoff", "shared memory", "claude code team". Mine the language | 17 B1 (top 4), 02 |
| 38 | Reddit Marketing | Acquisition | Now | Leo replies in his own name, replies only until karma passes 100 | C12, 13 |
| 39 | LinkedIn Audience | Acquisition | Now | Aden posts weekly. Confirm the company page first | 13, E5 |
| 41 | X Audience | Acquisition | Now | Leo posts by hand while the reply pipeline is down | 13 |
| 44 | Comment Marketing | Acquisition | Now | Disclosed HN comments. Answer issues on public "shared memory for Claude Code" repos | 17 B5, 13 |
| 61 | Shared Slack Channels | Acquisition | Now | Each founding team gets a shared `lightbulb-<slug>` channel. A contractor room (17 A4) follows when guest access exists | 01 §E, 17 A4 (Q3+) |
| 78 | Product Hunt Launch | Acquisition | Now | Early January, behind Show HN and the five-item gate. Prasiddha is maker | 07, E12 |
| 81 | Early Access Pricing | Acquisition | Now | The founding offer: 30 days free, then $50 locked for two years. No discount | §8, T05 |
| 87 | Powered By Marketing | Acquisition | Now | An opt-out footer on customers' agent mail, on by default. Check it does not exist first | 17 E2, D14, T26 |
| 100 | Promo Videos | Acquisition | Now | Record the 60-second demo in week 2 | 14 |
| 102 | Social Screenshots | Acquisition | Now | Founder-workspace screenshots, each vetted by a founder before posting | B4 |
| 109 | Public Demos | Acquisition | Now | Show HN links Prasiddha's live terminal on a public demo repo. A watch-us-work link at each launch | 07, B11, 17 F3, T18 |
| 121 | Guerrilla Marketing | Acquisition | Now | Mail a light bulb after a team's first handoff, from the founder who set it up | 17 E3, C14 |
| 123 | Open Source as Marketing | Acquisition | Now | MIT licence on the plugin repo (E11) | T34 |
| 125 | App Marketplaces | Acquisition | Now | List the plugin in Anthropic's directory. Codex gets a hooks-free build, or launch without it | 17 B6 (top 3), 14, E13, T34 |
| 133 | Investor Marketing | Acquisition | Now | Each founder lists real accelerator contacts. Offer one batch a free setup | 17 B3 (top 7), C13 |
| L1 | One handoff, told in full | Acquisition | Now | A one-page teardown per founding team, with consent. Start with Second Catch | 17 C1 (top 8), 07 |
| L2 | Weekly build log: "three people, six agents" | Acquisition | Now | Post one real handoff from our workspace each Friday | 17 F1 (top 6), 13 |
| 6 | Proprietary Data Content | Acquisition | Q2 | A monthly "what our agents did" report from our workspace, aggregates only | 17 D4 |
| 8 | Content Refreshing | Acquisition | Q2 | Six July to August posts carry the retired voice. Refresh or retire them | T32 |
| 9 | Knowledge Base SEO | Acquisition | Q2 | Rewrite `/docs/get-started` around the team, then target its questions | T32 |
| 11 | Competitor Comparison Pages | Acquisition | Q2 | Six pages written and fact-checked. They publish after `/pricing` | 11, T31 |
| 35 | Community Marketing | Acquisition | Q2 | A founders' room inside Lightbulb. Confirm the vendor room can span workspaces | 17 C3 |
| 59 | Article Quotes | Acquisition | Q2 | Founders answer journalist requests on agents at work. No customer named beyond Anna and Second Catch | B2 |
| 65 | Live Webinars | Acquisition | Q2 | Office hours on a shared session, 30 minutes a fortnight | 17 F4 |
| 68 | Local Meetups | Acquisition | Q2 | A five-minute live demo of a real handoff at an AI Tinkerers chapter | 17 B2 |
| 82 | Product Hunt Alternatives | Acquisition | Q2 | BetaList and similar, after Product Hunt | 14 |
| 108 | Changelogs | Acquisition | Q2 | A public changelog that says who shipped it, agent or person | 17 F2 |
| 117 | Product Competitions | Acquisition | Q2 | A prize for the best team handoff at an agents hackathon. The prize is the founding offer, not cash | 17 B4 |
| 129 | Review Sites | Acquisition | Q2 | AlternativeTo and SaaSHub after the site gate, a founder submits each | 14 |
| L3 | Guest posts by founding teams | Acquisition | Q2 | Ask each paying team to write what it handed off | 17 C5 |
| 3 | Glossary Marketing | Acquisition | Q3+ | Wedge terms have little volume. The definition line in 05 covers stage 1 | 05 |
| 14 | Side Projects as Marketing | Acquisition | Q3+ | The public plugin already plays this role. Add nothing until it is listed | 14 |
| 15 | Engineering as Marketing | Acquisition | Q3+ | A free tool for the wedge, scoped by the growth engineer | Growth engineer |
| 16 | Importers as Marketing | Acquisition | Q3+ | An import from the shared markdown repo teams use today. Scope after stage 1 | None |
| 42 | Short Form Video | Acquisition | Q3+ | The 60-second demo seeds it. No cadence before stage 2 | 14 |
| 49 | Monthly Newsletters | Acquisition | Q3+ | An owned channel for stage 2, once there is a story a month | Stage 2 |
| 70 | Conference Speaking | Acquisition | Q3+ | Speak once the founding teams have handoffs to show | None |
| 74 | Press Coverage | Acquisition | Q3+ | Pitch after three teams pay, so the story has named customers | B2 |
| 75 | Fundraising PR | Acquisition | Q3+ | Announce when the SAFE closes. Close date not known | None |
| 88 | Free Migrations | Acquisition | Q3+ | The setup session does this by hand today. Productise after stage 1 | T13 |
| 98 | Template Marketing | Acquisition | Q3+ | Copyable workflows from real teams: routine, agents and approval rule in one step | 17 C4, 17 T4 |
| 101 | Industry Interviews | Acquisition | Q3+ | Borrowed podcasts and newsletters are stage-2 work | Stage 2 |
| 111 | Challenges as Marketing | Acquisition | Q3+ | The launch competition on `flickmeon.ai`, after the product work | A10, stage 2 |
| 115 | Curation as Marketing | Acquisition | Q3+ | A curated list of real team workflows, with the templates | 17 C4 |
| 126 | YouTube Reviews | Acquisition | Q3+ | Pitch reviewers once the session-sharing page and pricing are live | None |
| 127 | YouTube Channel | Acquisition | Q3+ | Build-log videos, if the written build log holds an audience | 17 F1 |
| 136 | Developer Relations | Acquisition | Q3+ | The growth engineer works with plugin users | Growth engineer |
| 138 | Podcast Tours | Acquisition | Q3+ | Borrowed channel in stage 2 | Stage 2 |
| 18 | Calculator Marketing | Acquisition | Q4+ | No data yet on hours lost to manual handoffs | None |
| 22 | Public APIs | Acquisition | Q4+ | The MCP and plugin are the developer surface until stage 3 | Stage 3 |
| 56 | Reseller Programs | Acquisition | Q4+ | Agencies reselling to companies is a stage-3 question | Stage 3 |
| 57 | Expert Networks | Acquisition | Q4+ | Certified implementers for company rollouts | Stage 3 |
| 66 | Virtual Summits | Acquisition | Q4+ | Needs an audience we do not have yet | None |
| 67 | Roadshows | Acquisition | Q4+ | Company-stage motion | Stage 3 |
| 71 | Conferences | Acquisition | Q4+ | Long game | None |
| 76 | Documentaries | Acquisition | Q4+ | Long game | None |
| 103 | Online Courses | Acquisition | Q4+ | Long game | None |
| 105 | Annual Reports | Acquisition | Q4+ | Builds on the monthly agents report | 17 D4 |
| 106 | End of Year Wraps | Acquisition | Q4+ | "Your team's year with its agents", December 2027 at the earliest | None |
| 116 | Grants as Marketing | Acquisition | Q4+ | Free workspaces for open-source teams, once cost per workspace is known | None |
| 131 | International Expansion | Acquisition | Q4+ | US and English only until companies arrive | None |
| 4 | Programmatic SEO | Acquisition | Skip | We are deleting ten thin template pages (E1). More templates repeat that mistake | T27 |
| 13 | Competitive Ad Research | Acquisition | Skip | No paid layer. The competitor watch (17 D5) tracks product changes instead | 17 D5 |
| 17 | Quiz Marketing | Acquisition | Skip | The qualification is a setup call, not a quiz | None |
| 19 | Chrome Extensions | Acquisition | Skip | Adds a surface to maintain. Web, Mac, phone and the plugin cover the team | None |
| 20 | Microsites | Acquisition | Skip | The name already collides twice. One site keeps one entity | 05 |
| 21 | Scanners | Acquisition | Skip | No fit with the product | None |
| 23 | Podcast Advertising | Acquisition | Skip | Rejected in 17 and not this stage: no spend before a free channel works | 17 rejection |
| 24 | Pre-targeting Ads | Acquisition | Skip | Not this stage: no paid spend before the stage-2 gate, then only on a channel that already brings teams free | None |
| 25 | Facebook Ads | Acquisition | Skip | Not this stage: no paid spend before the stage-2 gate, then only on a channel that already brings teams free | None |
| 26 | Instagram Ads | Acquisition | Skip | Not this stage: paid, and Instagram is not a stage-1 channel | None |
| 27 | Twitter Ads | Acquisition | Skip | Not this stage: no paid spend before the stage-2 gate, then only on a channel that already brings teams free | None |
| 28 | LinkedIn Ads | Acquisition | Skip | Not this stage: no paid spend before the stage-2 gate, then only on a channel that already brings teams free | None |
| 29 | Reddit Ads | Acquisition | Skip | Not this stage: no paid spend before the stage-2 gate, then only on a channel that already brings teams free | None |
| 30 | Quora Ads | Acquisition | Skip | Not this stage: no paid spend before the stage-2 gate, then only on a channel that already brings teams free | None |
| 31 | Google Ads | Acquisition | Skip | Not this stage: no paid spend before the stage-2 gate, then only on a channel that already brings teams free | None |
| 32 | YouTube Ads | Acquisition | Skip | Not this stage: no paid spend before the stage-2 gate, then only on a channel that already brings teams free | None |
| 33 | Cross-Platform Retargeting | Acquisition | Skip | Not this stage: no paid spend before the stage-2 gate, then only on a channel that already brings teams free | None |
| 34 | Click-to-Messenger Ads | Acquisition | Skip | Not this stage: no paid spend before the stage-2 gate, then only on a channel that already brings teams free | None |
| 36 | Quora Marketing | Acquisition | Skip | The wedge talks on Reddit and HN. Founder time goes there | None |
| 40 | Instagram Audience | Acquisition | Skip | Not this stage: about 26 visitors in 30 days and off-wedge | None |
| 43 | Engagement Pods | Acquisition | Skip | Off-voice and against community rules | None |
| 54 | Affiliate Discovery Through Backlinks | Acquisition | Skip | Not this stage: partnerships wait for stage 2 | None |
| 55 | Influencer Whitelisting | Acquisition | Skip | Not this stage: no paid spend before the stage-2 gate, then only on a channel that already brings teams free | None |
| 58 | Newsletter Swaps | Acquisition | Skip | Not this stage: no newsletter, and partnerships wait for stage 2 | None |
| 60 | Pixel Sharing | Acquisition | Skip | Not this stage: no paid spend before the stage-2 gate, then only on a channel that already brings teams free | None |
| 63 | Integration Marketing | Acquisition | Skip | Not this stage: "built on" stories with Clerk, Stripe and Vercel wait for stage 2 | 17 G1 |
| 64 | Community Sponsorship | Acquisition | Skip | Rejected in 17: paid sponsorship before a free channel works | 17 rejection |
| 69 | Meetup Sponsorship | Acquisition | Skip | Not this stage: no paid spend before the stage-2 gate, then only on a channel that already brings teams free | None |
| 72 | Conference Sponsorship | Acquisition | Skip | Not this stage: no paid spend before the stage-2 gate, then only on a channel that already brings teams free | None |
| 73 | Media Acquisitions as Marketing | Acquisition | Skip | Needs capital the round does not hold | None |
| 77 | Black Friday Promotions | Acquisition | Skip | The plan never discounts to acquire (§8) | None |
| 80 | New Year Promotions | Acquisition | Skip | No discounts (§8) | None |
| 83 | Twitter Giveaways | Acquisition | Skip | Off-voice. Attracts the wrong buyer | None |
| 84 | Giveaways | Acquisition | Skip | Attracts prize seekers, not teams | None |
| 85 | Vacation Giveaways | Acquisition | Skip | Off-voice | None |
| 86 | Lifetime Deals | Acquisition | Skip | Rejected in 17: discounts to acquire, wrong buyer, no team | 17 rejection |
| 89 | Contract Buyouts | Acquisition | Skip | Teams of 3 to 15 hold no contracts worth buying out | None |
| 97 | Playlists as Marketing | Acquisition | Skip | No fit | None |
| 99 | Graphic Novel Marketing | Acquisition | Skip | Off-voice | None |
| 104 | Book Marketing | Acquisition | Skip | Founder time is the scarce input | None |
| 107 | Podcasts | Acquisition | Skip | A show costs weekly founder time we do not have | None |
| 110 | Awards as Marketing | Acquisition | Skip | Off-voice at this size | None |
| 112 | Reality TV Marketing | Acquisition | Skip | Off-voice | None |
| 113 | Controversy as Marketing | Acquisition | Skip | The voice is calm. The contrast lines are enough | None |
| 118 | Cameo Marketing | Acquisition | Skip | Off-voice | None |
| 119 | OOH Advertising | Acquisition | Skip | Not this stage: no paid spend before the stage-2 gate, then only on a channel that already brings teams free | None |
| 120 | Marketing Stunts | Acquisition | Skip | Off-voice. The bulb in the post (#121) is the one physical play | None |
| 122 | Humor Marketing | Acquisition | Skip | The voice is plain and calm | None |
| 128 | Source Platforms | Acquisition | Skip | G2 (the review site) waits until the site gate passes (E14) | 14 |
| 130 | Live Audio | Acquisition | Skip | Founder time. Office hours (#65) cover live | None |
| L4 | Agents replying in threads on their own | Acquisition | Skip | Rejected in 17: agents never publish, communities ban it | 17 rejection |
| L5 | Own Discord or Slack community | Acquisition | Skip | Rejected in 17: a second place outside the product. #35 keeps it inside | 17 rejection |
| L6 | A public address that reaches our CEO agent | Acquisition | Skip | Not yet (C15). The shared channel per team is the line | 17 D2 |
| L7 | Local small-business lists | Acquisition | Skip | Not this stage. Small businesses are the fallback only (plan §12, Risks) | None |
| L8 | Open-source framework write-up | Acquisition | Skip | Not this stage: partnerships wait for stage 2. "Built on" only (B10) | 17 G2 |
| L9 | Accelerator perks listings | Acquisition | Skip | Not this stage: a standing partnership. Revisit if #133 lands a batch | 17 G3 |

### 12.2 Activation

| # | Idea | Stage | Status | Reason or first step | Maps to |
|---|---|---|---|---|---|
| 47 | Founder Welcome Email | Activation | Now | Aden answers each setup request within a day from his own address. Welcome from the CEO agent replaces today's | F3, D10, T14 |
| 51 | Onboarding Emails | Activation | Now | Ship the welcome and away emails from the CEO agent's address | 12, T14, T15 |
| 90 | One-Click Registration | Activation | Now | Add GitHub sign-in (D7) | T11 |
| 95 | Concierge Setup | Activation | Now | The 45-minute setup session, the founder who sourced the team runs it | 04, C1 |
| 96 | Onboarding Optimization | Activation | Now | Invite first, one finished thing, the CEO asks who should see it | 08, T08, T10, T13 |
| L10 | Two on the call | Activation | Now | Add "bring the person who should approve the first result" to booking and the run sheet | 17 A1 (top 1), C4 |
| L11 | The first approval goes to someone else | Activation | Now | The first approval card asks "Who should decide this?" and invites that person | 17 A2 (top 2), T09 |
| L12 | An invite that says what is waiting | Activation | Now | The CEO drafts invite text that names the task. The person sends it | 17 A3, T08 |
| L13 | A setup dossier before every call | Activation | Now | Read the prospect's public posts, pick the first workflow to wire | 17 D3 (top 9) |
| 124 | App Store Optimization | Activation | Q2 | List the phone app without in-app purchase, after the one plan ships | T21 |
| L14 | A real spots counter | Activation | Q2 | "Founding teams: 10 spots, 3 set up" on the homepage, updated by hand | 17 C2, T28 |
| 48 | Dynamic Email Capture | Activation | Skip | No newsletter to capture into. The setup form is the capture | None |

### 12.3 Retention

| # | Idea | Stage | Status | Reason or first step | Maps to |
|---|---|---|---|---|---|
| 46 | Reactivation Emails | Retention | Now | Aden emails the 24 day-one churners once, from his own address | C8, 16 |
| 50 | Inbox Placement | Retention | Now | Check authentication on the sending domain before T14 and T15 ship | 12 |
| 53 | Trial Reactivation | Retention | Now | Day-28 recap card, then reading-only from day 31 | 15, T19 |
| 94 | Offboarding Flows | Retention | Now | Replace one-click cancel with one reason, one pause per company, the save script | 15, A12, T19 |
| 135 | Support as Marketing | Retention | Now | Thursday check-ins. Fixes that unblock a handoff ship and are told to the team | 01 §E |
| 45 | Mistake Email Marketing | Retention | Q2 | Use it when something really breaks for a team. Never staged | None |
| 52 | Win-back Emails | Retention | Q2 | After the first teams lapse at list. Reading-only keeps the data 90 days | 15, T19 |
| 134 | Certifications | Retention | Q4+ | Company-stage implementer program | Stage 3 |

### 12.4 Referral

| # | Idea | Stage | Status | Reason or first step | Maps to |
|---|---|---|---|---|---|
| 93 | Viral Loops | Referral | Now | Ask to join on session links, the first-session invite, members may invite | 06, T07, T16 to T18 |
| L15 | "Who else" at week four | Referral | Now | Add "Who else carries context by hand?" to the week-4 script | 17 A5 (top 10), 16 |
| 79 | Early-Access Referrals | Referral | Q3+ | The referral credit for a team that brings a paying team, designed in stage 2 | 06, A11 |
| 137 | Two-Sided Referrals | Referral | Q3+ | Mechanism and reward decided in stage 2, not before | 06, A14 |
| 62 | Affiliate Program | Referral | Skip | Not this stage. The stage-2 referral credit replaces it (A11) | 06 |
| 92 | Newsletter Referrals | Referral | Skip | No newsletter | None |

### 12.5 Revenue

| # | Idea | Stage | Status | Reason or first step | Maps to |
|---|---|---|---|---|---|
| 91 | In-App Upsells | Revenue | Q2 | The +$20 seat past five and managed AI credits, shown when a team hits them | T04, 15 |
| 132 | Price Localization | Revenue | Q4+ | USD only until international | None |
| L16 | Marketplace liquidity | Revenue | Skip | Not this stage: it does not move a team from one person to two | None |

### 12.6 Cross-cutting

| # | Idea | Stage | Status | Reason or first step | Maps to |
|---|---|---|---|---|---|
| 114 | Moneyball Marketing | Cross-cutting | Now | Source tags on every link and the Monday readout pick the channels that work | 09, 01 §D |
| 139 | Customer Language | Cross-cutting | Now | Interview notes and thread language feed every draft | 16, 17 B1 |
| L17 | Every post through an approval card | Cross-cutting | Now | Route Leo's next five drafts through cards in our workspace | 17 D1 (top 5), 13 |
| L18 | Own the free namespaces | Cross-cutting | Now | `lightbulb` in the plugin directory, GitHub, X and LinkedIn, each with the 05 definition line | 17 E1, 14 |
| L19 | Say the full name in every first mention | Cross-cutting | Now | "Lightbulb, the team workspace for AI employees" in bios, listings and titles | 17 E4, 05 |
| L20 | Competitor and launch watch | Cross-cutting | Q2 | Pulse tracks named competitors' changelogs weekly. Internal only | 17 D5 |
| L21 | Buy a colliding domain or rename | Cross-cutting | Skip | Rejected in 17: costs money for a search problem copy can fix | 17 rejection |
| L22 | More agents in the starter roster | Cross-cutting | Skip | Not this stage: more agents do not bring a second person | None |
| L23 | Mac app features | Cross-cutting | Skip | Not this stage: the Mac does not move a team from one person to two | None |
### Idea-bank summary

| Group | Now | Q2 | Q3+ | Q4+ | Skip | Rows |
|---|---|---|---|---|---|---|
| 12.1 Acquisition | 24 | 13 | 18 | 13 | 56 | 124 |
| 12.2 Activation | 9 | 2 | 0 | 0 | 1 | 12 |
| 12.3 Retention | 5 | 2 | 0 | 1 | 0 | 8 |
| 12.4 Referral | 2 | 0 | 2 | 0 | 2 | 6 |
| 12.5 Revenue | 0 | 1 | 0 | 1 | 1 | 3 |
| 12.6 Cross-cutting | 5 | 1 | 0 | 0 | 3 | 9 |
| **Total** | **45** | **19** | **20** | **15** | **63** | **162** |

The 162 rows hold 139 library ideas and 23 Lightbulb-specific ones. Of the 139 library ideas, 35 are Now, 16 Q2, 20 Q3+, 15 Q4+ and 53 Skip.

No paid spend before the stage-2 gate removes 19 library ideas. Discounts, off-voice stunts and early partnerships remove most other Skips. Workstream 17's five rejections and the dropped D2 sit in the Skip rows.

Most Now rows cost founder hours, not money. Sixteen of them wait on a Forge task. The plan uses about a quarter of the library now, which fits three founders with no paid budget. This bank is the list to draw from as the gate passes.

## 13. Measurement, RACI, open decisions, appendix

### Measurement: the metrics that matter

**North star.** Teams with two or more people and a completed handoff (business plan, Go To Market). It is the one number on the wall. It captures the bet: Lightbulb earns its place only where people share work with agents and decide on it (§2).

The count uses external workspaces only. Lightbulb staff do not count as one of the two people, and their actions do not count as the handoff. Prasiddha owns that list (F1).

**The five Monday numbers.** Definitions follow Appendix C of the business plan, with staff excluded throughout (19-plan-vetting, Appendix C).

| Number | Definition |
|---|---|
| 1. Setup requests | A submitted guided-setup form, with its self-reported and tracked source |
| 2. Teams formed | A new workspace with two or more people, not counting Lightbulb staff, within 48 hours of creation |
| 3. First handoff | A person decided on an agent's work in the workspace's first week: an approval answered (approve or reject), a session picked up, or an agent's task moved on |
| 4. Week-4 active | Share of teams formed with a human message in their fourth week. Messages from Lightbulb staff and internal workspaces do not count |
| 5. Paying at list | A workspace with a Stripe invoice paid at full price. Coupon subscriptions do not count |

Two more definitions complete the set (Appendix C):

- **Working first session:** a new workspace with a first document and a first approval card answered by a member inside the first session.
- **External workspace:** any workspace except lightbulb, demo-lab, 10-06-test, adenprivatetest, external-review, starter-roster-audit-sep-12 and the MKT385 intern workspace.

### Leading indicators by AARRR stage

Each indicator comes from one event. Most of those events do not exist yet, so the task that builds each one is named (18).

| Stage | Indicator | Event | Task |
|---|---|---|---|
| Acquisition | Setup requests by source | `setup_requested` | T22 |
| Acquisition | Landing to setup click to request | `landing_viewed`, `setup_cta_clicked` | T23 |
| Acquisition | New workspaces with a known source (target over 80%) | `source_reported`, first-touch UTM | T12 |
| Acquisition | Conversations per 100 touches | Outreach sheet (no event) | none |
| Activation | Teams formed | `member_joined`, `team_formed` | T02 |
| Activation | First handoff in week one | `approval_answered`, `task_moved`, `handoff_completed` | T03 |
| Activation | Working first sessions | `approval_answered` plus a first Library document | T03, T13 |
| Retention | Week-4 active | `week4_active` from `room_message_sent` | T23 |
| Retention | Stop rate after Jev-routed runs | One run event with routing source and Stop time | T35 |
| Retention | "Very disappointed" share, wedge and small business apart, with n | Thursday check-in (manual) | none |
| Referral | Second people by door | `member_joined {via}` | T02 |
| Revenue | Founding teams with a card and a trial end date | Stripe read | T05 |
| Revenue | Paying at list | `invoice_paid {at_list}`, `trial_converted` | T23 |

The tracking plan sits in 09. One PostHog store is keyed on `workspace_id`, with no paid group analytics (F2). The identity rule ends `org_` distinct ids and dev pages tagged `prod` (T01). `trial_converted` fires only on a paid invoice, and room runs fire `agent_run_completed` (T01, T23). Vercel Web Analytics adds a cookieless visitor count (09). These replace today's activation metric, five messages on two days, which is single-player (plan item 7). Reactions carry no target message today, so no reply-quality number comes from reactions until they do (socially aware agents).

The data guards from 09 §6 apply to every query.

- Every query filters `environment = 'prod'` and the product hosts. Without that filter, counts run 6 to 15 times too high (09).
- `flickmeon.ai` traffic is its own line, never in the product funnel.
- Workspace numbers count `workspace_id`, never people.
- Internal workspaces are excluded by slug, resolved to ids on each run. Staff user ids are excluded as members and authors (planned rule, vetting).
- Every number prints with the workspace names behind it. No percentage on a base under 10, so the week-4 gate reads as "n of m" until then.

### Review cadence

- **Weekly:** Pulse's Monday readout by 09:00, then the founders' pipeline review. Thursday check-ins with each founding team (01 §D, §E).
- **Monthly:** the first Monday re-fits the pipeline rates on real data. It also moves work between current, next and experimental.
- **Quarterly:** the quarter readout on Mon 11 Jan, the gate read on Mon 1 Feb, then plan v2.

### RACI

| Domain | Responsible | Accountable | Consulted | Informed |
|---|---|---|---|---|
| This plan and the stage gates | Pulse drafts, Prasiddha | Prasiddha | Aden, Leo | Forge, interns |
| Lead list and research | Interns, Pulse | Leo | Aden, Prasiddha | Forge |
| Outreach sends | Prasiddha, Aden, Leo | Prasiddha | Pulse drafts | Interns |
| Setup requests and setup sessions | Aden, or the sourcing founder | Aden | Pulse, Friday (proposed) | Prasiddha, Leo |
| Thursday check-ins | The sourcing founder | Aden | Pulse drafts | Prasiddha |
| X and Reddit | Leo | Leo | Pulse | Aden, Prasiddha |
| LinkedIn | Aden | Aden | Pulse | Prasiddha, Leo |
| Show HN, Product Hunt, plugin listings | Prasiddha | Prasiddha | Aden, Leo, Pulse | Forge |
| Website and copy (T27 to T33) | Forge | Prasiddha | Pulse, Leo for comparisons | Aden |
| Billing and pricing (T04 to T06, T30) | Forge | Prasiddha | Aden | Leo |
| Events and readout (T01 to T03, T22 to T24) | Forge, Pulse | Prasiddha | Aden | Leo, interns |
| Exclusion list | Prasiddha | Prasiddha | Pulse | All |
| Analytics QA | Open (decision 5) | Prasiddha | Pulse, Forge | All |
| Lifecycle email (T14, T15, T19) | Forge builds, Pulse drafts | Open (decision 5) | Aden | Prasiddha |
| Proof: Anna, Second Catch | Aden | Aden | Pulse | Leo, Prasiddha |
| Stop ledger and agent voice (T35) | Forge | Prasiddha | Pulse | Aden, Leo |
| Product loops in stage 2 | Growth engineer, after the round | Prasiddha | Forge, Pulse | Aden, Leo |
| Growth engineer hire | Prasiddha | Prasiddha | Aden, Leo | Pulse |
| Round and investor narrative | Prasiddha | Prasiddha | Aden, Leo | Pulse |

No material assigns Friday a marketing role. The cell above is a proposal: Friday keeps the setup calendar and session notes.

### Open decisions blocking the plan

Ranked by what each one blocks.

1. **The Forge date for the trial checkout (T04, T05).** No setup can happen before it. The roadmap assumes 30 Oct. After 12 Dec, no founding team can pay by 12 Jan (19-plan-vetting).
2. **The quadrant call and the fallback trigger.** The plan bets on large and frequent. The evidence reads small and frequent (research.md). A proposed trigger: at the 1 Mar read, fewer than three founding teams are week-4 active and paying at list. Then the small-business fallback becomes the main bet in Q2.
3. **Founder hours in the cost of a team.** Hours are the only acquisition cost today. Without an hourly figure, CAC and any stage-2 paid test cannot be judged (research.md, question 4).
4. **Cash, burn, runway and the round.** All are blank (plan §11). They block the budget method in section 10 and the growth engineer's start date.
5. **Owners of lifecycle email and analytics QA.** Nobody owns them (research.md). The welcome, away and trial-end emails need a signer. The readout needs someone who checks it against Clerk and Stripe.
6. **Tool spend by line.** It is not broken out anywhere (research.md). It blocks the Tier 1 cost picture.
7. **Small items, due this week.** These are the three bios (F4), Search Console and Bing access (E5), and X API credits (research.md). Bios block launch copy. Search Console blocks the SEO baseline. Credits block X replies at volume.

**Founder calls from the consistency check (conflicts.md).** These stay open until a founder rules.

8. **Product Hunt date.** Thu 10 Dec beside Show HN (§9), or early January (§3, §4, §11, §12).
9. **Staff exclusion and the formed target.** Do Lightbulb staff ever count as the second person? If not, 10 of 10 formed (§5) becomes 85% (§9, §10).
10. **Growth engineer trigger.** SAFE close, or SAFE plus the stage-2 gate. Confirm growth engineer first, against the skill's strategist-first rule (§3, §10, §11).
11. **Friday's duty.** Setup dossier (§11), setup calendar and notes (this section's RACI), both or none.
12. **Capacity for Now.** Accept 45 Now ideas (§12), or retag what §9 does not schedule as Q2.
13. **Plugin submission timing.** Weeks 1 to 4 (§4, §11), or D−14 before Show HN (§9).
14. **Placeholder CAC.** Keep $600 as a labelled sensitivity in §8, or remove it until decision 3 prices founder hours.
15. **"Members, not" claim.** Settled: the 13 October banned-word ruling retires the September board message; copy uses "AI agents are members, not tools" (§2).
16. **Stage dates and the outer-year model.** Confirm stages 2 and 3 are gates, not quarters. Pick one forecast shape and one quarter scheme (§4, §8, §10).

**New from the finalize pass.**

17. **The eval's pass bar.** Item 8 sets no accept rate that blocks a fleet rollout of new reply rules (vetting Part 4). Muse uses a launch-blocking bar per behaviour.
18. **Source for the revenue band.** The "$2M+ in combined annual revenue" line stays on the homepage, but no source is attached yet (vetting).

### Appendix: deep-dive links

**In the repo, `docs/marketing/`:**

- `00-decisions.md`, the 72 founder answers (13 Oct)
- `01-marketing-plan.md`, the calendar, pipeline model, readout and cut order
- `02-prospecting.md`, `03-cold-email.md`, `04-sales-enablement.md`
- `05-seo.md`, `06-referrals-and-loops.md`, `07-launch.md`, `08-onboarding-and-signup.md`
- `09-analytics.md`, the tracking plan, identity rule, UTMs and HogQL
- `10-website-copy.md`, `11-competitors.md`, `12-lifecycle-emails.md`, `13-content-and-social.md`
- `14-directories.md`, `15-trial-to-paid.md`, `16-customer-research.md`, `17-ideas.md`
- `18-build-order.md`, the 35 tasks with dependencies and sizes
- `19-plan-vetting.md`, the claim-by-claim vetting (binding)
- `README.md`, the index and brief

**In the repo, `docs/research/`:** five reports. One is a competitor teardown (9 Oct). Four date from 13 Oct: employee voice and consumer agents, messaging datasets, the Muse walkthrough, and socially aware agents.

**Founder context:** `docs/business-plan.md` and `.agents/product-marketing.md` v8.

**Planner working files:** `research.md` and `materials/` in this plan's folder.
