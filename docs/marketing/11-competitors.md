# 11 — Competitor and comparison pages

## Purpose

Moves the stage-1 goal (ten founding teams by 31 Jan 2027) by catching founders who are already comparing tools and sending them to "Request a guided setup". Serves plan §6 (competition and positioning) and the website brief in §8.

## What we know

Every competitor fact was opened on the competitor's own page on 13 Oct 2026 unless marked.

| Fact | Confidence |
|---|---|
| Slack's homepage H1 is "All your people and AI agents working together". | High (first-party) |
| Slack Business+ is $15/user/month annual, $18 monthly. Ongoing Slackbot access is Business+ and Enterprise+; Free and Pro get "a limited sample". | High |
| Slackbot calls itself "your personalized AI"; "your interactions are private". Its page names no model. | High |
| Agentforce in Slack: the AI-agents page says it "will be available soon"; no price or plan shown. The plan's "sold separately" is not on Slack's page today. | High (first-party), plan claim unverified |
| Microsoft 365 Business Standard with Copilot is $23.50/user/month paid yearly, up to 300 users. Copilot Business add-on is $21, promo $18 from 1 Jul to 31 Dec 2026, first year only. Copilot Business lists "LLM model choice". | High (opened in Chrome; WebFetch timed out) |
| Grok Bot: one microVM per user; "one user cannot reach another user's computer". All of a user's Bots share it. "Cursor manages model selection." Background turns and routines keep running when the app is closed. Audit logs are Enterprise only. Included in paid Cursor plans; Cursor Teams is "$30+/user/month". | High (docs.x.ai, cursor.com). **x.ai/bot returned 403.** |
| Zed Delta (12 Aug 2026): private beta; Claude Code first; threads private until shared; share by link, open in a browser; comments anchored to code; cloud runners keep working after you close the laptop. Price not published. | High |
| Lody: shared coding sessions teammates can continue; Claude Code, Codex, Grok Build, Kimi Code and any ACP agent; per-member usage. Free up to 3 members; Plus $8/seat/month billed yearly. | High |
| Egregore: `/handoff` saves "the reasoning", `/invite` shares memory; Claude Code examples only; install with `npx create-egregore@latest`. Price not published. | High |
| Nimbalyst: "The visual workspace for Codex and Claude Code"; MIT open source; free for individuals; Teams $20/user/month, free during beta; shared docs, trackers and team chat. No session-sharing feature listed. | High |
| Bloome: group chat "where people and agents work together"; @mentions; Claude Code, Codex, Gemini CLI, OpenCode and many models; audit trail. Price: "Free to try", no tiers published. | High (bloome.im) |
| Atlassian AMP (Business Wire, 7 Oct 2026): agents with an owner and profile, "Run as User" or a service account, full audit trail, @mentioned in Jira and Confluence. Availability and price not announced. | High |

**Corrections to the plan's §6** (founders only): Microsoft's cheapest Teams + Copilot bundle is $23.50 a seat, not "about $33"; Copilot lists model choice, so its gap is "outside Microsoft 365", not "other vendors"; Grok Bot's docs say Cursor picks the model, not "Grok only"; Delta keeps working with the laptop closed; Grok Bot posting to Teams and iMessage comes from our teardown, not a public page, so it stays off public pages.

**Our own facts** (from the plan and origin/main): `/pricing`, `/request-setup` and any `/vs` or `/compare` route return 404 today. Checkout still bills Base $20 / Workspace $50 / Ultra $150 with unlimited members; Base and Ultra are retired and the one plan is $50 for up to five plus $20 a member from the 6th. Seat billing is built first. SSO is a stage-3 item, not shipped. The iPhone app is on TestFlight only.

## Findings (ranked by impact)

1. **"We already have Slack" is the first setup-call objection** (website brief FAQ), and Slack's homepage now uses our category sentence. The Slack page is the one most likely to be read before a call.
2. **The wedge searches for session sharing.** Its own words are sessions that "start from zero". Delta, Lody, Egregore, Nimbalyst and Bloome all answer that query; one honest page covering all five catches it.
3. **The competitors are better than our old lines say** (Delta's cloud runners, Copilot's model choice, Lody's agent range). Our true difference is narrower: one set of AI employees the whole team sees, doing non-code work too, on one record.
4. **Price wins only at the plan's price.** For 8 people: Lightbulb $110 (plan), Slack Business+ $120, Microsoft 365 with Copilot $188, Cursor Teams $240+. Pages publish after seat billing ships, with the plan's price.
5. **Search volume is unverified** (no keyword tool used). The order follows objections and the wedge's words; reorder after 60 days of data.
6. **Bloome is closest in shape** (people and agents in one group chat, any model). It gets its own page (founder decision E7), as well as its place on pages 2 and 3.

## Recommendation

Build six pages, in this order, on one route with one data file. All six publish after seat billing ships, with the plan's price. Leo signs off every edit:

| # | Page | Why first |
|---|---|---|
| 1 | Lightbulb vs Slack AI / Slackbot | Objection #1 on setup calls; Slack is the shelf we sit on |
| 2 | Sharing Claude Code sessions with a team (Delta, Lody, Egregore, Nimbalyst, Bloome) | The wedge's own query; technical founders |
| 3 | Alternatives to Slack for teams that use AI agents | Earlier-stage searchers; lists real options |
| 4 | Lightbulb vs Grok Bot | Closest product; technical teams on Cursor |
| 5 | Lightbulb vs Microsoft Teams + Copilot | Real but off-wedge; Microsoft shops are rarely founder teams of 3–15 |
| 6 | Lightbulb vs Bloome | Closest in shape; own page by founder decision |

Rejected: a page per session tool (five thin pages, each out of date within weeks); an Atlassian AMP page (not available, no price, no search demand yet; keep it in the alternatives list); "Slack vs Teams" third-party pages (off-wedge). Every page carries "Checked on <date>" under the table.

## Deliverable

Shared rules for all six pages: byline Leo (comparisons); publish after billing ships; buttons are **Request a guided setup** (to `/request-setup?source=compare-<slug>`) then **Start free** (to `/sign-up?source=compare-<slug>`). The Lightbulb price cell reads "$110 a month ($50 for up to five people, $20 each after), plus AI usage or your own key". Offer line: 30 days free with a card on file, first charge on day 31; no discounts, no coupons. Slack's price appears only on page 1, which answers "we already have Slack" (Business+ only, never Pro). The Lightbulb column is the same on every page:

| Row | Lightbulb |
|---|---|
| Who it's for | Founder-led teams of 3–15 already running AI agents |
| Where the agent lives | A member of your channels, running in your workspace's own cloud |
| Who sees the work | Everyone in the room; one shared record |
| Any model | Lightbulb-managed, your ChatGPT account, or your own API key |
| Price for 8 people | $110 a month, plus AI usage or your own key |
| Approvals and record | Approval cards for spend, sends, deploys and hires; who did what and who approved it |
| Works while nobody is online | Yes: routines, email and a nightly CEO run |
| Code and non-code work | Both: shared Claude Code and Codex sessions, plus email, books and the task board |

### Page 1 — Lightbulb vs Slack AI

- **URL:** `/compare/slack`
- **Title:** Lightbulb vs Slack AI and Slackbot | Lightbulb
- **Meta:** How Lightbulb and Slack's Slackbot differ for small teams using AI agents: who sees the work, which models, approvals, and price for 8 people.
- **H1:** Lightbulb vs Slack AI: an assistant for each person, or AI employees for the team
- **Intro (60 words):** Slack is where most teams already talk, and Slackbot now answers questions and takes actions across your connected apps. Lightbulb is a team workspace where AI employees are members of your channels, with their own tools, memory, email and approval rules. If you are choosing where your people and your agents should work together, this page compares the two plainly.

| Row | Slack Business+ with Slackbot |
|---|---|
| Who it's for | Every company size |
| Where the agent lives | Slackbot, a personal AI in Slack; third-party agent apps |
| Who sees the work | The person who asked; answers can be shared to a channel |
| Any model | Not published |
| Price for 8 people | $120/month annual ($15 × 8); Agentforce price not published |
| Approvals and record | Not published for Slackbot |
| Works while nobody is online | Not published |
| Code and non-code work | Non-code, through connected apps |

**What Slack does better.** Slack is the place your team already talks, with thousands of apps, Slack Connect with other companies, huddles and years of polish. If your team is larger than 15 or works with many outside partners, Slack will serve the conversation better than we will today.

Slackbot is included in Business+ and knows your whole Slack history from day one. Lightbulb's agents know what happens in Lightbulb and in the tools you connect; they do not read your Slack.

Slack has enterprise controls, mobile apps in every store and a long security record. We are early: the iPhone app is in TestFlight and SSO is not shipped. Slack added agents to chat. Lightbulb built the company around them.

**FAQ**
- *Can we keep Slack?* Yes. Many teams keep Slack for talk and run their AI employees in Lightbulb. Your team joins the rooms where agents are working.
- *Is Slackbot an AI employee?* Slackbot is a personal assistant; its interactions are private to you. In Lightbulb the agents are shared members with their own email, tools and approval rules.
- *What does it cost for 8 people?* Slack Business+ is $120 a month billed yearly. Lightbulb is $110 a month for the workspace, plus AI usage or your own key.

### Page 2 — Sharing Claude Code sessions with a team

- **URL:** `/compare/shared-claude-code-sessions`
- **Title:** Share Claude Code sessions with your team: Delta, Lody, Egregore, Nimbalyst, Bloome, Lightbulb
- **Meta:** Five ways to let a teammate watch, pick up and continue a Claude Code or Codex session, compared honestly, with prices for an 8-person team.
- **H1:** How to share Claude Code sessions with your team
- **Intro (60 words):** Every Claude Code session starts from zero, and your cofounder cannot see what yours just figured out. Several tools now fix that: Zed's Delta, Lody, Egregore, Nimbalyst and Bloome. Lightbulb does it too, inside a team workspace where the same history also covers email, books and approvals. Here is what each one does, and who each fits.

| Row | Zed Delta | Lody | Egregore | Nimbalyst | Bloome |
|---|---|---|---|---|---|
| Who it's for | Engineers on Claude Code | Teams running coding agents | Claude Code teams | Builders using Claude Code and Codex | Teams of people and agents |
| Where the agent lives | Your terminal, synced to a thread; cloud runners | Your machines via a daemon | Your Claude Code session | Your desktop app | Group chat; sandbox per task |
| Who sees the work | People you share the link with | Teammates you share with | Invited teammates | Team org (shared docs) | Chat members |
| Any model | Claude Code first | Claude Code, Codex, Grok Build, Kimi, ACP agents | Claude Code shown | Claude Code, Codex; others in alpha | Many models and coding agents |
| Price for 8 people | Not published (beta) | $64/month yearly ($8 × 8) | Not published | $160/month ($20 × 8), free in beta | Not published |
| Approvals and record | Comments on code and thread | Permission requests; usage by member | Handoffs with reasoning | Change history on shared docs | Audit trail; per-tool permissions |
| Works while nobody is online | Yes, cloud runners | On your own always-on machine | Not published | Not published | Not published |
| Code and non-code work | Code | Code | Code | Code plus docs, mockups, trackers | Both |

**What they do better.** Delta's comments anchor to a line of code and move with it, and a teammate can open a thread in the browser without installing anything. That is the best review loop on this page, and we took the "private until shared" idea from it.

Lody runs more coding agents than we do, on your own machines, and shows cost per teammate. Egregore's handoff saves the reasoning and open questions, not just the state. Nimbalyst is open source and free for one person. Bloome puts many models in one group chat, close to how Lightbulb works.

If your team only needs to share code sessions, one of these may be enough. Lightbulb fits when the same team also hands off email, books and approvals. They share the coding session. We share the company.

**FAQ**
- *Can my teammate continue my Claude Code session?* In Lightbulb, yes: watch it, join it, or pick it up with the full history. Delta and Lody also support continuing a session.
- *Do I need to switch from Claude Code or Codex?* No. Lightbulb works alongside them and makes their sessions shareable with your team.
- *Which is cheapest?* Lody's paid plan is $8 a seat. Egregore installs free; Delta and Bloome have not published prices.

### Page 3 — Alternatives to Slack for teams that use AI agents

- **URL:** `/compare/slack-alternatives-for-ai-agents`
- **Title:** Slack alternatives for teams that use AI agents (2026) | Lightbulb
- **Meta:** Six options for a small team that wants AI agents in its chat: Slack itself, Microsoft Teams, Lightbulb, Bloome, Grok Bot and Atlassian. Who each fits.
- **H1:** Alternatives to Slack for teams that use AI agents
- **Intro (60 words):** If your team runs AI agents, the question is no longer only where people talk. It is where the agents work, who can see what they did, and who approves it. Here are six honest options, Slack included, with what each does well. We make Lightbulb, so we put it first and say where others fit better.

| Row | Lightbulb | Slack + Slackbot | Teams + Copilot | Bloome | Grok Bot | Atlassian AMP |
|---|---|---|---|---|---|---|
| Who it's for | Founder teams 3–15 | Every company | Microsoft 365 customers | Teams of people and agents | Cursor users | Jira and Confluence companies |
| Where the agent lives | Your channels; workspace cloud | Personal AI in Slack | Microsoft 365 apps | Group chat | One cloud computer per person | Jira, Confluence, Loom |
| Who sees the work | The room | The person who asked | Your tenant | Chat members | That person | Your Atlassian workspace |
| Any model | Yes | Not published | "LLM model choice" | Yes | Cursor picks | Not announced |
| Price for 8 | $110 + usage | See Slack's page | $188 | Not published | $240+ (Cursor Teams) | Not announced |
| Approvals and record | Approval cards; one record | Not published | Not published | Audit trail | Auto-review; audit logs on Enterprise | Full audit trail |
| Works offline | Yes | Not published | Not published | Not published | Yes | Not announced |
| Code and non-code | Both | Non-code | Mostly non-code | Both | Both | Both |

**What the others do better.** Slack and Teams have the deepest app catalogues and the security reviews large customers ask for. If your company lives in Microsoft 365, Copilot sits in Word, Excel and Outlook, where Lightbulb does not. Atlassian's agents can run as a named user with a full audit trail, inside the tools Jira teams already use. Grok Bot gives each person a capable cloud computer that keeps working with the laptop closed.

**FAQ**
- *What is the best Slack alternative for a small team with AI agents?* If the agents should be shared members of the team, Lightbulb or Bloome. If you only want a personal assistant in chat, Slackbot or Copilot.
- *Do we have to leave Slack?* No. Keep Slack for talk and add Lightbulb where agents work.
- *Which is cheapest for 8 people?* Compare the Price row. Lightbulb is $110 a month; Bloome has not published a price.

### Page 4 — Lightbulb vs Grok Bot

- **URL:** `/compare/grok-bot`
- **Title:** Lightbulb vs Grok Bot: AI for each person, or for the team | Lightbulb
- **Meta:** Grok Bot gives each person a cloud computer and a set of Bots. Lightbulb gives the team one set of AI employees everyone can see. An honest comparison.
- **H1:** Lightbulb vs Grok Bot
- **Intro (60 words):** Grok Bot, from xAI and Cursor, gives each person their own Bots on a persistent cloud computer, with routines that keep running when the laptop is closed. Lightbulb gives a team one set of AI employees in shared channels, with one record and approval rules. Both do real work in real tools. The difference is who the agents belong to.

| Row | Grok Bot |
|---|---|
| Who it's for | Individuals and Cursor Teams/Enterprise |
| Where the agent lives | One cloud computer per person, shared by that person's Bots |
| Who sees the work | That person; templates share setup, not history |
| Any model | Cursor manages model selection |
| Price for 8 people | Included in Cursor Teams, $30+/user/month: $240+ |
| Approvals and record | Auto-review can ask first; audit logs are Enterprise only |
| Works while nobody is online | Yes |
| Code and non-code work | Both |

**What Grok Bot does better.** Each person gets a full computer with a browser, terminal and files, on desktop and mobile apps, and it keeps working while they are away. Admin controls are deep: network policy, team rules, team secrets and SIEM export on Enterprise.

If your team already pays for Cursor, Grok Bot costs nothing extra. Routines that nobody reads pause themselves, a rule we took. Grok Bot gives each person a team of Bots. Lightbulb gives the team one set of AI employees everyone can see.

**FAQ**
- *Can a teammate see my Grok Bot's work?* Its docs say users cannot reach each other's computers, and a shared template does not include conversation history. In Lightbulb the room sees the work.
- *Which models?* Grok Bot's docs say Cursor manages model selection. Lightbulb runs Lightbulb-managed models, your ChatGPT account or your own key.
- *Can we use both?* Yes. Many teams use a personal agent and a shared workspace.

### Page 5 — Lightbulb vs Microsoft Teams + Copilot

- **URL:** `/compare/microsoft-teams-copilot`
- **Title:** Lightbulb vs Microsoft Teams with Copilot | Lightbulb
- **Meta:** Microsoft 365 with Copilot vs Lightbulb for a small team using AI agents: where agents live, who sees their work, approvals, and price for 8 people.
- **H1:** Lightbulb vs Microsoft Teams with Copilot
- **Intro (60 words):** Microsoft Teams with Copilot brings AI into Word, Excel, Outlook and Teams, with prebuilt agents and Copilot Studio for building your own. Lightbulb is a team workspace where AI employees are members of your channels and do work the whole team can see and approve. If your company does not live in Microsoft 365, the choice is simpler than it looks.

| Row | Microsoft 365 Business Standard with Copilot |
|---|---|
| Who it's for | Microsoft 365 customers, up to 300 users |
| Where the agent lives | Copilot in Microsoft 365 apps; agents via Copilot Studio |
| Who sees the work | Your Microsoft 365 tenant |
| Any model | "LLM model choice" |
| Price for 8 people | $188/month paid yearly ($23.50 × 8) |
| Approvals and record | Not published on the pricing page |
| Works while nobody is online | Not published |
| Code and non-code work | Mostly documents, email and meetings |

**What Microsoft does better.** Copilot works inside the documents, spreadsheets and email your team already uses, and the price includes Office apps, email hosting and 1 TB of storage per person. For a company on Microsoft 365, nothing else is that close to the work.

Microsoft has the identity, compliance and admin tooling large buyers require. Lightbulb does not have SSO yet.

**FAQ**
- *Does Copilot work outside Microsoft 365?* Copilot Business requires an eligible Microsoft 365 plan. Lightbulb connects to the tools you use, Google or Microsoft.
- *What does it cost for 8 people?* $188 a month for Business Standard with Copilot, paid yearly. Lightbulb is $110 a month plus AI usage.
- *Can we use both?* Yes. Lightbulb agents can work alongside a Microsoft 365 company.

### Page 6 — Lightbulb vs Bloome

- **URL:** `/compare/bloome`
- **Title:** Lightbulb vs Bloome | Lightbulb
- **H1:** Lightbulb vs Bloome
- **Intro:** Bloome is a group chat "where people and agents work together", with @mentions, many models and coding agents, and an audit trail. Lightbulb is close in shape. This page says where each fits. Table: the shared Lightbulb column against Bloome's cells from page 3 (price "Free to try", no tiers published). Leo writes the rest from bloome.im; every cell is reopened and dated.

### JSON-LD (FAQPage)

One block per page, built from the same FAQ data the page renders, so they cannot drift. Example for page 1; the other five follow the same shape with their three questions:

```json
{"@context":"https://schema.org","@type":"FAQPage","mainEntity":[
 {"@type":"Question","name":"Can we keep Slack?","acceptedAnswer":{"@type":"Answer","text":"Yes. Many teams keep Slack for talk and run their AI employees in Lightbulb. Your team joins the rooms where agents are working."}},
 {"@type":"Question","name":"Is Slackbot an AI employee?","acceptedAnswer":{"@type":"Answer","text":"Slackbot is a personal assistant; its interactions are private to you. In Lightbulb the agents are shared members with their own email, tools and approval rules."}},
 {"@type":"Question","name":"What does it cost for 8 people?","acceptedAnswer":{"@type":"Answer","text":"Slack Business+ is $120 a month billed yearly. Lightbulb is $110 a month for the workspace, plus AI usage or your own key."}}]}
```

Google shows FAQ rich results only for a few site types now, so expect no rich snippet; the markup still helps AI answers read the page (see workstream 05).

### Update rule

- **Monthly** (first Monday): the Growth agent reopens every Source URL and drafts changes; Leo signs off before anything ships (plan §8).
- **Within a week of:** the Copilot promo ending (31 Dec 2026), Delta leaving beta, Agentforce shipping in Slack, Bloome, Egregore or AMP publishing prices, or any change to our own price.
- **On the page:** a cell we cannot reopen on the vendor's page becomes "Not published"; "Checked on" moves only when every cell was reopened. Quarterly: full refresh and page order.

## Build tasks

1. **Seat billing and `/pricing` before any page.** *Today:* checkout bills $20/$50/$150 with unlimited members; `/pricing` is 404. *Change:* seat billing and `/pricing` ship first; the six pages publish after, with the plan's price. *Why:* a price we don't bill fails the first check. *Measure:* `/pricing` returns 200 with the plan's numbers, then the six pages return 200.
2. **One data file, one route.** *Today:* SEO pages come from `seoLandingPages` in `apps/frontend/src/lib/seo.tsx` via `app/[seoSlug]/page.tsx`, a fixed kicker/bullets shape. *Change:* add `apps/frontend/src/content/compare/index.ts` (per-page copy, per-competitor cells, a source URL and `checkedOn` per cell), `apps/frontend/src/app/compare/[slug]/page.tsx` (`dynamicParams = false`) and `app/compare/page.tsx` as the hub; reuse `pageMetadata`, `JsonLd`, `breadcrumbJsonLd`, `Navbar`, `Footer`. *Why:* one source of truth per competitor. *Measure:* six pages return 200, each with a Leo byline; a test fails if any cell lacks a URL or date.
3. **FAQPage from the same array the page renders** (pattern in `app/docs/page.tsx`). *Measure:* Rich Results Test parses each page.
4. **Sitemap and llms.txt.** Add `/compare` and each `/compare/<slug>` (`lastModified` = `checkedOn`) to `app/sitemap.ts` and `app/llms.txt/route.ts`. *Measure:* seven new URLs in `/sitemap.xml`.
5. **Source on the buttons.** `/request-setup?source=compare-<slug>` and `/sign-up?source=compare-<slug>`, carried into workstream 09's events; depends on `/request-setup` (workstream 10). *Measure:* setup requests per page.
6. **Redirect `/ai-agents-for-slack`** (it says Lightbulb runs "behind the scenes" of Slack) to `/compare/slack`. *Measure:* one Slack story on the site.
7. **Internal links.** Homepage FAQ "We already have Slack" and "Claude already does this for me" link to pages 1 and 2; the footer links to the hub.

## Measures

| Measure | Target by 31 Jan 2027 | Monday number it feeds |
|---|---|---|
| Organic visits per compare page | Baseline in the first 30 days, then reorder pages by it | Landing visitors |
| Setup requests with `source=compare-*`, per page | 3 total | Setup requests |
| Trial starts with `source=compare-*` | 10 total | Trial starts |
| Setup calls where the founder mentions a compare page | Asked on every call | Qualitative, logged in the call notes |
| Cells older than 45 days | 0 | Hygiene, not a Monday number |

## Founder decisions (13 Oct 2026)
- E6: Pages publish after seat billing ships, with the plan's price. No "See pricing" interim.
- E7: Bloome gets its own page. Six pages.
- E2: `/ai-agents-for-slack` redirects to the Slack comparison page.
- E8: Leo signs off every edit.
- E3: Authors are Prasiddha (product, engineering), Leo (comparisons), Aden (customer stories).
- B8: No Slack Pro price. Slack's price appears only on the "we already have Slack" page.
- Open: the §6 corrections to the plan were not answered; a founder still needs to approve them.

## Sources

All checked 13 Oct 2026.

| Claim | URL |
|---|---|
| Slack H1, subhead | https://slack.com/ |
| Slack plan prices; Slackbot by plan; free tier 90 days | https://slack.com/pricing |
| Slackbot "personalized AI", private interactions, plans | https://slack.com/features/slackbot |
| Agentforce "will be available soon", no price | https://slack.com/ai-agents |
| Copilot Business $21/$18 to 31 Dec 2026; Business Standard with Copilot $23.50; 300 users; "LLM model choice" | https://www.microsoft.com/en-us/microsoft-365/copilot/business (redirects to /copilot/solutions/business; read in Chrome) |
| Grok Bot overview, routines, closed-laptop work | https://docs.x.ai/grok-bot |
| Grok Bot per-user microVM, admin controls, "Cursor manages model selection" | https://docs.x.ai/grok-bot/teams-and-enterprises |
| Grok Bot plans; background turns continue; templates exclude history | https://docs.x.ai/grok-bot/faq |
| Grok Bot auto-review; audit logs Enterprise only | https://docs.x.ai/grok-bot/security-faq |
| Grok Bot drafts a Slack message for the user to send | https://docs.x.ai/grok-bot/chat-and-collaboration |
| Grok Bot included in Pro, Pro+, Ultra, Teams, Enterprise | https://cursor.com/help/grok-bot/plans |
| Cursor Teams "$30+/user/month" | https://cursor.com/pricing |
| x.ai/bot | https://x.ai/bot — **403, not read** |
| Delta: beta, Claude Code first, link sharing, anchored comments, cloud runners | https://zed.dev/blog/introducing-delta |
| Lody features and agents | https://lody.ai |
| Lody Free and Plus $8/seat | https://lody.ai/price/ |
| Egregore commands, install | https://egregore.xyz |
| Nimbalyst features, MIT, free individual | https://nimbalyst.com |
| Nimbalyst Teams $20/user, free in beta | https://nimbalyst.com/pricing/ |
| Bloome group chat, models, "Free to try" | https://bloome.im |
| Atlassian AMP, 7 Oct 2026, run as user, audit trail | https://finviz.com/news/399638/atlassian-introduces-amp-the-agentic-multiplayer-protocol-to-power-human-ai-collaboration (Business Wire release) |
| Lightbulb positioning, claims, contrast lines, price | docs/business-plan.md §6–§8 |
| Lightbulb product facts, objections | .agents/product-marketing.md |
| Existing SEO route, sitemap, FAQPage pattern | origin/main: apps/frontend/src/lib/seo.tsx, app/[seoSlug]/page.tsx, app/sitemap.ts, app/docs/page.tsx |
| Live 404s for /pricing, /request-setup, /vs/slack | https://thelightbulb.company (curl) |
