# Business plan vetting (13 Oct 2026, evening)

Four agents checked every claim in `docs/business-plan.md` against its sources (the plan's links, `docs/research/`, the workstream files, the code on `origin/main` at 6249f614d, the live site, competitors' pages). First-party numbers from PostHog, Stripe and Clerk (12–13 Oct) were not re-pulled. Each section below gives purpose, whether the section does its job, a claim table with a status, contradictions, what is missing, what to cut and what to keep. The founders walk the plan section by section with this file open; the rewrite follows.

Status key: verified (source opened and agrees) · first-party (our own data, 12–13 Oct) · stale (true when written, changed since) · unverified (no source found) · wrong (source says otherwise).

| Part | Sections | Claims | Verified | First-party | Stale | Unverified | Wrong |
|---|---|---|---|---|---|---|---|
| 1 | Header, 1–4 | 55 | 29 | 4 | 2 | 17 | 3 |
| 2 | 5–7, Appendix B | 114 | 74 | 6 | 0 | 23 (4 Reddit permalinks not re-opened) | 7 |
| 3 | 8–13, Go To Market, The website, Appendix C | 179 | 112 | 35 | 3 | 17 | 12 |
| 4 | Engineering/Product 1–9, Appendices A, D, E | 95 | 64 | 7 | 4 | 10 | 6 (+4 overstated) |

Themes across all four: (1) planned changes written in the present tense read as facts (flickmeon.ai "redirects", "Request a guided setup" on the site, Vercel Web Analytics "on"); (2) competitor capabilities understated (ChatGPT group chats, Grok Team Bot, Delta cloud runners, Slackbot approvals, Muse and Instinct approvals); (3) numbers without a derivation (§5 market sizing, §11 costs and margin, item 8 training costs); (4) sources misattributed or dead (§2's 16% and the Salesforce link); (5) sections repeat each other (§2/§4/§5/§6 overlaps, "works alongside Claude Code" three times); (6) the metric definitions count our own messages.

---


# Part 1

# Vet 1: header, §1 Summary, §2 Problem, §3 Solution and product, §4 Why now

Vetted 13 Oct 2026 against: the plan's own links, docs/research/*.md, .agents/product-marketing.md (v8), docs/marketing/*.md, `origin/main` at 6249f614d (13 Oct 02:59), the live site, and public pages. Lens: marketing-skills:product-marketing. A positioning-bearing opening has to give the category, the target customer, the core problem with its cost, why the alternatives fall short, the differentiation, and the proof. The README sources rule also applies: open what you cite, and mark everything else "unverified".

## Header: title and subtitle (39 words)
- **Purpose:** Tells the reader what the document is, who wrote it, how current it is, and whether to trust it as final.
- **Does it do the job:** Partly. The date reads "Oct 11, 2026", but §5–7, Engineering/Product and Go To Market were rewritten on 12–13 Oct (Appendix A). "steps 0–8" is internal process language that a cold reader cannot decode.
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| H1 | "The Lightbulb Company — Business Plan (draft)" | Appendix A; CLAUDE.md legal entity | verified | §10 names the entity "Isol8 Inc., doing business as Lightbulb AI". "The Lightbulb Company" is a brand, not the legal name. |
| H2 | "Oct 11, 2026 · Prasiddha Parthsarthy" | Appendix A, Appendix B | stale | The content changed on 12–13 Oct (Appendix A: "rewritten on 12–13 October"). Use 13 Oct. |
| H3 | "Source of truth … the Google Doc is updated from this file" | product-marketing.md line 6 | unverified | The context file lists "the business plan (Google Doc, 11 Oct draft)" as a source, so the two copies already disagree on which is current. |

- **Contradicts:** The header gives 11 Oct. Appendix A says "Stripe, Clerk and PostHog figures added on 12–13 October", and Appendix B's newest row is 13 Oct.
- **Missing:** A version or last-updated date. Who the reader is (investor or recruit). A line saying confidential or not for distribution.
- **Cut:** "Working copy. Source of truth while…" (~25 words). This is process for us, not for a reader. Move it to Appendix A.
- **Must keep:** The company name, the author, and a current date.

## 1. Summary (230 words)
- **Purpose:** In 30 seconds, tells an investor or a joining founder what this is, who it is for, why it wins, how far along it is, and what is being asked.
- **Does it do the job:** Partly. It covers the what, who, price, traction and ask. It has no differentiation (the §6 positioning "unlike…" line is absent) and no proof beyond counts. It never uses the plan's own category word "AI employees".
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 1.1 | "a shared workspace where teams delegate work to AI agents" | origin/main messaging; CLAUDE.md "Messaging" | verified | |
| 1.2 | "agents write code, research, send email, browse the web" | Appendix D; agent_mail_send.py; shared browser | verified | Code runs in the AgentCore microVM shell and on the Mac app. |
| 1.3 | agents "take voice calls" | origin/main `voice_calls.py`, `livekit_huddles.py`, `meet_bridge.py` | verified (merged) | Appendix D says voice "Merged 9-11 Oct". Prod lags main by design, so "live" is unverified. |
| 1.4 | agents "run tasks on a shared board" | Paperclip task board, Appendix D | verified | |
| 1.5 | "every workspace starts with six of them, from a CEO to Customer Operations" | `paperclip_provisioning.py` lines 56–61 on origin/main | verified | This applies only to fresh workspaces. Legacy and founder rosters are not migrated (CLAUDE.md). |
| 1.6 | "running in its own cloud" | `hermes_provisioning`: one AgentCore runtime per workspace | verified | |
| 1.7 | "keep working when nobody's laptop is open" | nightly CEO heartbeat (`arm_nightly_ceo`); `IDLE_TIMEOUT_S = 30*60` | verified | The runtime parks after 30 min idle and wakes on a message or the heartbeat. Agents do not run continuously. |
| 1.8 | "works alongside Claude Code, GitHub and the team's messaging tools" | `lightbulb` plugin (session sharing); gh baked in the image; `routers/slack_app.py` | verified | Caveat: the Slack app's ingress docstring still describes container IPs (the OpenClaw era). Whether Slack forwarding works on Hermes is unverified. |
| 1.9 | "founder-led technical teams of 3 to 15 who already use agents" | §5; product-marketing.md Target Audience | verified (decision) | This is a founder ruling of 13 Oct, not evidence. |
| 1.10 | "hard part has moved from prompting to coordinating people and agents" | none | unverified | This is the thesis. §5's quotes support it, but nothing here cites them. |
| 1.11 | Team $50/mo flat up to 5; +$20 from the 6th; Enterprise custom | 00-decisions A9; §7; Appendix B | verified (decision); not billable | origin/main `config.py`: BASE 20 / WORKSPACE 50 / ULTRA 150, unlimited members. `/pricing` returns 404. The live homepage shows no price. The summary does not say the plan is unbuilt. |
| 1.12 | "Managed AI usage is billed on top as metered credits" | `openrouter_managed` path; settlement Lambda; §7 ×1.2 | verified | |
| 1.13 | "57 accounts across 46 workspaces" | §9 | first-party, 12–13 Oct, not re-pulled | |
| 1.14 | "about five outside workspaces active in a given week" | §9 weekly table (5/4/6/4) | first-party, 12–13 Oct, not re-pulled | |
| 1.15 | "no customer yet paying list price" | §9 ($55 ever; six subs on a 100% coupon) | first-party, 12–13 Oct, not re-pulled | |
| 1.16 | "first founding customers are being set up now" | §5 ("One customer inside the wedge"); §8 | unverified | No founding-team setup is recorded anywhere. Second Catch joined 6 Oct, before the offer existed. |
| 1.17 | "raising $2 million on a SAFE at a $14 million post-money cap" | §11; product-marketing.md Goals | first-party (founder decision) | No market comparable is cited in this section. §11's "in line with 2026 pre-seed rounds" has no link. |
| 1.18 | "to fund 24 months" | §11 | unverified | §11 leaves cash, burn and runway as blanks, so 24 months cannot be checked. |
| 1.19 | "the three stages set out in Go-to-market" | §8 | verified | |

- **Contradicts:**
  - "to fund 24 months" (§1) vs §11 "[cash in bank] … [monthly burn] … [months] of runway". The horizon is asserted with no burn figure.
  - Price table (§1) vs §7 "The product still bills Base $20 / Workspace $50 / Ultra $150 with unlimited members". The summary presents an unbuilt plan as the price.
  - §1 says "AI agents" throughout. product-marketing.md says "'AI employees' stays as the public word (founder, 13 Oct)", and the §6 positioning says "AI employees work alongside the people".
  - "first founding customers are being set up now" (§1) vs §9 "Inside the wedge: 1 (Second Catch)" and §12 "nobody has yet shown a 3-to-15-person technical team working through Lightbulb week after week".
- **Missing:**
  - Why us over Slack, Teams, Grok Bot and the session tools (the one-line differentiation from §6).
  - One proof point: Second Catch's plan on an approval card, or Anna's invoices.
  - Who the three founders are.
  - What the $2M buys, in milestones (ten teams by 4 Jan 2027, six paying by May, the week-4 gate).
  - What the round will be judged on (§11: 7% WoW growth, week-4 retention).
- **Cut:**
  - "It works alongside Claude Code, GitHub…" (~12 words). This repeats §3.
  - "Agents now do real company work…" (~17 words). This repeats §4's thesis.
  - The 3-row price table (~25 words). Replace it with one clause: "$50 a month for up to five people".
- **Must keep:**
  1. A shared workspace where the team delegates to AI agents and everyone sees and continues the work.
  2. Six agents per workspace, in their own cloud, working with nobody online.
  3. For founder-led teams of 3–15 already running agents.
  4. 57 accounts and about 5 active outside workspaces a week. Nobody pays list price yet.
  5. $2M SAFE at $14M post.

## 2. Problem (287 words)
- **Purpose:** Convinces a cold reader that a specific, costly, felt problem exists for the target customer, and that today's tools leave it open.
- **Does it do the job:** Partly. The team problem is concrete. But the section spends more than half its words on an enterprise problem the wedge does not have, and it cites statistics loosely. One of its links is broken. There is no cost and no customer voice, although §5 has both.
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 2.1 | "AI agents can now take real action" | none | unverified | This is framing, and it is plausible. Nothing is cited. |
| 2.2 | Muse and Instinct: "each gets its own cloud computer and browser" | docs/research/2026-10-13-muse-walkthrough.md; employee-voice research | unverified (Muse half) | Instinct: verified. Muse says "I have my own computer". Instinct "has a phone, an email address and a computer". A browser was not observed for Muse ("no browser view or sign-in was seen"). |
| 2.3 | Muse and Instinct "uses stored logins" | employee-voice research (Instinct "signs in and cancels subscriptions end to end") | unverified (Muse half) | Instinct: verified. Muse: connects Gmail and Calendar through cards "with your approval". Stored logins were not observed. |
| 2.4 | they "keep working after you leave" | Muse walkthrough ("get things done while you're away") | verified | |
| 2.5 | "Handing agent work … means copying prompts, writing summaries" | §5 Reddit/HN quotes; product-marketing.md Customer Language | verified (anecdotal) | This is supported by builder quotes and three buyer posts, all low-n. |
| 2.6 | "80% of people say it made them more productive (McKinsey)" | McKinsey State of AI 2026 (fm-magazine summary: "Eighty per cent of respondents reported that AI has improved individual productivity") | verified, with a wording fix | The figure is "respondents" (a survey of 1,719 organisation respondents), not "people". The McKinsey page timed out on WebFetch, so this was confirmed through the FM Magazine write-up and the search summary. |
| 2.7 | "share seeing any profit impact has been flat at 37% for two years" | FM Magazine on McKinsey 2026: "37 per cent … around the same as last year" | unverified ("two years") | The 37% is verified. Flat for **one** year is verified. "Two years" is unverified. No source is attached in the plan. |
| 2.8 | "half of deployed agents run in isolated silos (Salesforce)" | Salesforce 2026 Connectivity Benchmark (Feb 2026), per Enterprise DNA: "Fifty percent of enterprise AI agents currently operate in isolation" | verified (secondary); **link broken** | The plan's URL `salesforce.com/news/stories/connectivity-report-2026/` returns HTTP 404. Replace it with the report's own page. |
| 2.9 | "only 16% of firms have agents that work across teams" | Anthropic/Material "State of AI Agents" (9 Dec 2025): "16% running cross-functional processes across multiple teams" | verified, but unattributed and miscited | The sentence makes it read as Salesforce's figure. Its source is Anthropic's survey of 500+ US technical leaders, from Dec 2025. |
| 2.10 | Anthropic, OpenAI, Microsoft enterprise tools "come with admin controls and audit logs" | none opened | unverified | Probably true (Claude Enterprise, ChatGPT Enterprise compliance API, Purview). It needs a link. |
| 2.11 | "each gives an agent to a person, inside one vendor's product" | §6 table | wrong | ChatGPT has had group chats with up to 20 people since Nov 2025 (Tom's Guide and others). §6 says Copilot Business lists "LLM model choice". The "one person" half is overstated. |
| 2.12 | "No company has one place where its people and its agents, from any vendor, do the work together" | §6; product-marketing.md Competitive Landscape | unverified / contested | §6 and the context file name Bloome ("people and agents in one group chat, any model") and Nimbalyst, and Atlassian AMP is cross-vendor by protocol. "No company has" is too absolute. |
| 2.13 | "Who feels it": founder-led technical teams 3–15 using Claude Code | §5 | verified (decision) | |
| 2.14 | "suited towards small efficient vertical teams" | none | unverified | "Vertical" is undefined. It conflicts with "technical teams" (see Contradicts). |
| 2.15 | "Teams stitch the work together from an IDE, Slack and copy-paste" | §5 (markdown repo, Linear, n8n, copy-paste) | verified (anecdotal) | §5's list is more specific and better. |

- **Contradicts:**
  - §2: "No company has one place where its people and its agents, from any vendor, do the work together." vs §6: "'People and agents together' is the category's language now, not ours". The context file also lists Bloome as "people and agents in one group chat, any model".
  - §2: "The product is suited towards small efficient vertical teams." vs §5: "Founder-led teams of 3 to 15 that already run two or more AI agents" (technical). vs §12, where small back-office businesses are the *fallback*, not the target.
  - §2 leads with "The enterprise problem", but §8 puts companies at stage 3 and §5 excludes buyers with a "procurement gate".
  - The 16% figure is placed as if it were Salesforce's. The actual source is Anthropic's December 2025 report.
- **Missing:**
  - What the problem costs: hours, duplicated work, a missed handoff. §5's "solving the same problems twice" exists but is not used here.
  - How often the problem occurs, and evidence that the wedge (not enterprises) feels it enough to pay.
  - The visibility half of the core problem, from product-marketing.md: "the founder cannot see the whole company in one place".
  - The emotional tension: the founder as the human router or bottleneck.
- **Cut:**
  - "Consumer agents such as Muse and…" (~61 words, the whole paragraph after the first sentence). §4 repeats it almost word for word.
  - "**Who feels it.** First, founder-led…" (~45 words). This repeats §1 and §5.
  - "**Why today's tools don't fix…" (~25 words). It is thin, §6 does it with evidence, and the claim conflicts with §6.
  - "The product is suited towards…" (~9 words). It is vague and contradicts the wedge.
- **Must keep:**
  1. Agent work inside a company sits in one person's session.
  2. Handing it to a teammate or another agent means re-creating context, and ownership blurs.
  3. Individuals get the productivity gain but companies do not (80% vs 37%, correctly sourced).
  4. Nobody gives the team one place to see, approve and continue agent work. Claim this narrowly, not as "no company".

## 3. Solution and product (193 words)
- **Purpose:** Shows how the product removes the problem, in the user's terms: what a person does and what happens next. It also shows what is real today.
- **Does it do the job:** Partly. The delegate → see → step in → continue loop is clear and matches the code. It mixes built and unbuilt (the "larger companies" paragraph), it gives no example of a task end to end, and it does not mark what is live in prod versus merged.
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 3.1 | "delegates by mentioning an agent in a channel or DM" | CLAUDE.md "Listen modes"; `select_dispatch_targets` | verified | Any DM is addressed. In a channel, an @mention or the agent's name at the start of a message wakes it. |
| 3.2 | the agent uses "connected company context and the tools it is authorized to use" | `credentials/hermes_cli.project`; integrations | verified | |
| 3.3 | "work stays visible to the whole team while it happens" | async run frames published to room channels; live browser preview | verified | |
| 3.4 | "anyone can inspect it, redirect it or approve the next step" | abort/steer routes; Paperclip approvals | verified | |
| 3.5 | next person "picks it up with the full history, including shared Claude Code sessions" | session sharing (lightbulb plugin; Appendix D Mac app) | verified | Caveat: session sharing needs the Mac app (Engineering/Product item 4: "only the Mac app shares sessions"). Codex sessions are also shared but not mentioned. |
| 3.6 | "For larger companies … admin-set permissions and approvals" | origin/main: no SSO/SAML/SCIM or audit-log module found; Paperclip approvals exist; owner decides who agents can email (`agent_mail_send.py`) | wrong | Stated as present. Approvals exist. Admin permission sets and SSO do not. §8 lists them as stage-3 additions. |
| 3.7 | "with every action on a shared record" | room messages + Paperclip issues + run ledger | unverified | There is no unified audit log. Runs are not recorded with who routed them (Engineering/Product item 8: "today a run does not record whether Jev … started it, and a Stop does not record who pressed it"). |
| 3.8 | "a task board run by the CEO agent" | Paperclip; nightly CEO heartbeat; CEO as root of the org | verified | |
| 3.9 | Voice, memory, shared browser, Mac and iPhone apps, marketplace | Appendix D | verified with caveats | Memory and voice are "merged 9–11 Oct" (prod unverified). The iPhone app is TestFlight only. Mac execution is "gated per owner". The marketplace is "liquidity not yet proven". |
| 3.10 | "works alongside Claude Code, GitHub, IDEs and messaging apps" | as 1.8 | verified | The same Slack-on-Hermes caveat applies. "IDEs" has no integration of its own. |

- **Contradicts:**
  - §3: "For larger companies, the same workspace lets agents act on real systems under admin-set permissions and approvals" vs §8: "As teams pull in the rest of their company, we add what larger buyers need: single sign-on, admin permissions and approvals".
  - §3: "with every action on a shared record" vs Engineering/Product item 8: "a run does not record whether Jev, a fallback or an overrule started it, and a Stop does not record who pressed it".
  - §3 ranks "shared coding sessions" as core. Engineering/Product says "Not in this list, on purpose: … Mac features", and item 4 says "only the Mac app shares sessions". So the core feature depends on the Mac app, which is gated per owner.
- **Missing:**
  - One worked example: Second Catch's plan → an approval card, or Anna's invoice run.
  - What is live in prod versus merged.
  - What it does *not* do yet.
  - Why the architecture matters: an isolated runtime per workspace, and any model.
  - The approval gate as a feature in its own right (the context file's "human gates").
- **Cut:**
  - "Lightbulb gives the team one…" (~22 words). The first sentence is a near-copy of §1's first sentence.
  - "Lightbulb works alongside Claude Code,…" (~16 words). This is the third time the plan says it.
- **Must keep:**
  1. Mention an agent in a channel or DM and it does the work with the company's tools.
  2. The work stays visible, and anyone can inspect, redirect or approve it.
  3. The next person or agent continues with the full history, including shared coding sessions.
  4. The core is chat + shared sessions + a CEO-run task board. Everything else supports it.

## 4. Why now (152 words)
- **Purpose:** Explains why this company can win now and could not have three years ago: what changed in technology, behaviour or the market, with dated evidence.
- **Does it do the job:** No. It gives no dated evidence. The first paragraph repeats §2. The claim that "every tool that offers it is built around one person" is contradicted by the plan's own §6. The strongest why-now evidence (Slack's new headline, Atlassian AMP on 7 Oct, Zed Delta on 16 Sep, ChatGPT group chats, the McKinsey and Anthropic numbers) is elsewhere or missing.
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 4.1 | "AI agents have just crossed from answering questions to taking action" | none | unverified | There is no date and no source. "Just" is hard to defend: coding and computer-use agents have shipped since 2025. A dated marker would help, for example McKinsey's 27%→40% of large firms scaling agents. |
| 4.2 | Muse and Instinct "run whole tasks on their own computer, signed in to the user's accounts" | research files (as 2.2–2.3) | unverified (Muse half) | Verified for Instinct. Muse: own computer, yes. Signed in: only via app-connect cards. |
| 4.3 | they "keep working after the user walks away" | Muse walkthrough | verified | |
| 4.4 | "capability that used to need a developer is becoming something anyone can direct" | none | unverified | |
| 4.5 | "every tool that offers it is built around one person" | §6 table; ChatGPT group chats (Nov 2025) | wrong | §6 lists Atlassian AMP (agents in the shared Atlassian workspace), Slack ("All your people and AI agents working together"), Zed Delta (teammate picks up the task) and Grok Bot team admin controls. ChatGPT has had group chats since Nov 2025. |
| 4.6 | Claude Code, ChatGPT, consumer agents: "no shared view … no way for a teammate to pick up" | §6 session tools; ChatGPT group chats | stale | This is true of a solo Claude Code session. Delta, Lody and Egregore now provide pickup for Claude Code and Codex, and ChatGPT group chats share the view. |
| 4.7 | "more people inside a company start directing agents, including non-engineers" | none | unverified | This needs a figure. McKinsey's 80% individual-use figure, or Anthropic's report, could carry it. |
| 4.8 | "That gap is open now, and it is the one Lightbulb fills." | none | unverified / contested | §6 concedes the category language is shared. The open gap is narrower: agents as members with their own runtime, any vendor, beyond code. |

- **Contradicts:**
  - §4: "every tool that offers it is built around one person" vs §6: "Slack's homepage now reads 'All your people and AI agents working together', Atlassian calls it 'multiplayer', and Grok Bot's bots hand work to each other." Also product-marketing.md: "our thesis is now the incumbents' headline".
  - §4: "That gap is open now" vs §6: "'People and agents together' is the category's language now, not ours."
  - §4 paragraph 1 is a near-duplicate of §2: "Consumer agents such as Muse and Instinct show…".
- **Missing:**
  - Dated evidence of the shift: model and agent capability, cost per task, adoption numbers.
  - Why incumbents converging *helps* us. It validates the category, and every one of them is single-vendor or per-seat.
  - Why a 3-person company can build this now: agents write most of the code (§10), and AgentCore/Hermes off the shelf.
  - Why small teams first: they adopt agents fastest and have no procurement.
  - What closes the window, and when.
- **Cut:**
  - "AI agents have just crossed…" (~54 words, all of paragraph 1). It repeats §2 with no new evidence.
  - "That gap is open now,…" (~12 words). It is an assertion that §6 contradicts.
- **Must keep:**
  1. Agents now finish whole tasks on their own computers while the user is away.
  2. More people in a company now direct agents, so the bottleneck moves from capability to how a team sees, controls and continues the work.
  3. Incumbents are moving into "people and agents together", which proves the category, and each does it per person or per vendor. Rewrite it this way; it currently says the opposite.

## Summary
1. Status counts across 55 claim rows: verified 29, first-party 4, stale 2, unverified 17, wrong 3. Ten of the verified rows are founder decisions or anecdotes, not independent evidence.
2. Wrong, §2: "each gives an agent to a person, inside one vendor's product". ChatGPT has had group chats for up to 20 people since Nov 2025, and §6 says Copilot offers model choice.
3. Wrong, §3: larger companies get "admin-set permissions and approvals" (present tense). Approvals exist, but admin permission sets and SSO are not in origin/main, and §8 lists them as stage 3.
4. Wrong, §4: "every tool that offers it is built around one person". This contradicts §6 (Slack's H1, Atlassian AMP, Zed Delta, Grok Bot admin controls).
5. Stale, header: dated "Oct 11, 2026", but sections were rewritten 12–13 Oct. Stale, §4: "no way for a teammate to pick up". Delta, Lody and Egregore now do this for Claude Code and Codex.
6. Sourcing fixes in §2: the Salesforce link returns 404. The 16% is Anthropic/Material (Dec 2025), not Salesforce. McKinsey's 37% is flat for one year, not two. The 80% is "respondents", not "people".
7. Other contradictions: §1 "fund 24 months" against §11's blank burn. The §1 price table against §7's statement that the product still bills $20/$50/$150. §2 "small efficient vertical teams" against §5's technical-team wedge. §1 never uses "AI employees", the founder-ruled public word.
8. Largest single cut: §2's Muse/Instinct paragraph and §4's first paragraph say the same thing (~61 + ~54 words). Cut §4 ¶1 and keep one short version in §2.
9. Further cuts: "works alongside Claude Code…" appears three times. §2's "Who feels it" (~45 words) and "Why today's tools…" (~25 words) repeat §5 and §6.
10. Most missing: §4 has no dated evidence. Rewrite it around the incumbents' convergence (Slack's H1, AMP on 7 Oct, Delta on 16 Sep) as proof of the category, plus the narrow gap only Lightbulb fills: agents as members with their own runtime, from any vendor, beyond code. The Summary also needs one proof point and the one-line differentiation.


# Part 2

# Vet 2: business plan §5, §6, §7, Appendix B (13 Oct 2026)

Method: skills customer-research (§5), competitor-profiling + competitors (§6), pricing + offers (§7) invoked and applied. Sources opened today: IMARC, Menlo, SlashData, Stack Overflow 2025 AI survey, slack.com (/, /pricing, /features/slackbot, /ai-agents), cursor.com/pricing, docs.x.ai/grok-bot (+/faq, /teams-and-enterprises), zed.dev (introducing-delta, delta-public-beta), lody.ai/price, nimbalyst.com/pricing, egregore.xyz, finviz (Atlassian AMP release), HN item 47027197, openrouter.ai/docs/faq, thelightbulb.company (curl), origin/main @ 6249f614d. microsoft.com Copilot pages timed out twice: Microsoft figures rest on 11-competitors.md (opened in Chrome 13 Oct). Reddit permalinks not re-opened.

## 5. Customer and market (581 words)

- **Purpose:** tell a cold reader exactly who buys, what they do today, how we know, and how big the prize is.
- **Does it do the job:** partly. Who/what-today/qualification are sharp and evidence-labelled; but by customer-research's own bar (5+ independent buyer data points per segment, confidence labels) the wedge rests on 3 buyer quotes plus 2 vendor quotes and one in-wedge customer, and the market paragraph's two headline numbers ($6B, $920M) have no shown derivation and one has no source at all.
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 1 | "Founder-led teams of 3 to 15… two or more AI agents" | product-marketing.md (founder ruling 13 Oct) | verified | Definition, not a fact; matches context v8 |
| 2 | "the second person they invite is the activation event" | Appendix C, 08-onboarding | verified | Consistent with "Team formed" definition |
| 3 | DIY glue: markdown repo, Linear/Confluence, n8n, copy-paste | product-marketing.md v4 | unverified | No dated public source cited in the plan for this list |
| 4 | Egregore quote "no idea what your co-founder just spent four hours…" | HN 47027197 (opened) | verified | Feb 2026 "Ask HN: What are you working on?"; vendor voice, correctly labelled |
| 5 | Pairon quote "The hard part isn't 'can the agent write code?'" | r/ClaudeAI 1w7bx1r, 4 Sep 2026 | permalink in context file, not re-opened | Plan says "two developers"; 02-prospecting says "Pairon's developer" (singular). Vendor voice |
| 6 | "none of it syncs" (r/smallbusiness, 6 Oct) | 02-prospecting.md + context, dated | permalink in context file, not re-opened | |
| 7 | "re-explaining the stack… every session" (r/ChatGPTCoding, 6 Oct) | 02-prospecting.md + context | permalink in context file, not re-opened | |
| 8 | "pick up tickets overnight" (r/ChatGPTCoding, 9 Oct) | 02-prospecting.md + context | permalink in context file, not re-opened | |
| 9 | Qualification list (3–15, 2+ agents, missed handoff in 30 days…) | plan's own rule | verified | A decision, not a claim |
| 10 | One customer inside the wedge (Second Catch) | §9; decisions B1 | first-party, 12–13 Oct, not re-pulled | Naming cleared (B1 answer) |
| 11 | Second Catch: plan in week one; "first thing that broke was adding the cofounder" | §9 | first-party, 12–13 Oct, not re-pulled | |
| 12 | 50 outside sign-ups, 28 free-mail | §9 | first-party, 12–13 Oct, not re-pulled | |
| 13 | 33 of 38 outside workspaces have one person | §9 | first-party, 12–13 Oct, not re-pulled | Context's "36 of 46" is the all-workspace count; consistent |
| 14 | Team communication software $21.5B "today" → $55B by 2034 | IMARC (opened) | wrong (label) | Numbers verified (2025 value, 10.67% CAGR) but IMARC's market is **team collaboration software**, not "team communication"; and "today" = 2025 |
| 15 | Enterprise gen-AI spend $37B in 2025, 3× prior year | Menlo (opened) | verified | $37B vs $11.5B (3.2×); Menlo footnotes that 2024 excludes inference, so the 3× is not like-for-like |
| 16 | Every professional developer team = $6B/yr "at our pricing" | SlashData (opened) | unverified | SlashData gives 36.5M professional devs (early 2025). $6B only reproduces as 36.5M ÷ 8 × $1,320 (8-person team at $110/mo); the 8-person assumption is not stated |
| 17 | "700,000 small teams that already use AI every day" | Stack Overflow 2025 AI (opened) | unverified | Page has 51% of pros using AI daily and no team or "700,000" figure. Derivation absent |
| 18 | "worth about $920 million a year" | arithmetic | unverified | = 700,000 × $1,320; inherits #17 and the 8-person assumption |
| 19 | Referral fees "open the business-services budget, which dwarfs software" | none | unverified | No source; also a stage-3 bet presented as market size |

- **Contradicts:**
  - §5 "the shelf… Slack and Microsoft Teams own, team communication" + IMARC link vs the source's own name "team collaboration software" (includes conferencing).
  - §5 builds TAM on "every professional developer team", but §2 and §12 say the fallback pull is non-technical small businesses ("a one-person food business") and §5's wedge is cross-role (decisions C5: "Cross-role teams first"). Dev-only TAM undercounts the fallback and overstates the wedge's fit.
  - §5 calls Egregore and Pairon "founders who went on to build tools for it… itself the signal"; 02-prospecting.md: "three of the plan's six customer quotes are vendors describing their own market" and the relabel/swap is "Still open". The plan now labels them, but still leads with them.
- **Missing:**
  - Trigger events (what made a team look now): customer-research's extraction framework requires them; none listed.
  - Confidence labels and n per claim (buyer quotes: 3, all Reddit, all 6–9 Oct; Reddit skews technical — the skill's sample-bias check).
  - SAM/SOM bottom-up: how many teams match all qualification rules, and how many we can reach in stage 1 (02-prospecting found 28 qualifying companies in ~1,700 posts, none Hot — that is the real reachable number and it is absent).
  - Willingness to pay from any buyer (no quote about price).
  - What the 8 .edu and 28 free-mail sign-ups tell us about who the door is reaching.
- **Cut:**
  - "The people who describe the pain…" (~20 words): framing that vendor quotes are "itself the signal" is argument, not evidence; the label alone is enough.
  - "The lead list in docs/marketing/02-prospecting.md…" (~10): an internal path in an investor document.
  - "and enterprise spending on generative AI…" (~20): not the shelf or the wedge; carries no decision.
  - "the shelf we are building is…" (~20): repeats §6 category line.
  - "and the referral fees on the services…" (~20): stage-3 model, belongs in §7 and is unsourced.
- **Must keep:**
  1. The wedge definition and the founder-buyer / second-person activation event.
  2. The DIY-glue alternative and the three buyer quotes (dated).
  3. The qualification and not-a-fit lists.
  4. "One customer inside the wedge"; 33 of 38 one-person workspaces: the door admits individuals.
  5. Week-four retention is the open question the founding stage answers.

## 6. Competition and positioning (1,051 words)

- **Purpose:** show a cold reader the honest alternatives, where Lightbulb is genuinely different, and why a team would pay instead of staying with "good enough".
- **Does it do the job:** partly. The table, positioning statement and contrast lines are the right shape (competitor-profiling's structured, comparable rows; competitors' "help them decide"). But four cells are now contradicted by the competitors' own pages (Grok Bot has a Team Bot; Delta keeps working with the laptop closed; Lody's price is published; Slackbot has scheduled automations and approval), two named competitors (Bloome, Nimbalyst) are missing from the table, and no row says what a competitor does better (the competitors skill's core honesty rule; 11-competitors.md does this, §6 does not).
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 1 | Without us: glue, Slack/Teams bot, Grok Bot, session tool, nothing | context v8 | verified | |
| 2 | "All five are cheaper or already installed" | — | unverified | Grok Bot at $240+/8 and Copilot at $188/8 are dearer than $110; "already installed" covers them only for existing customers |
| 3 | "Slackbot, your personal AI agent" | slack.com/features/slackbot (opened) | verified | Page title "Personal AI Agent for Work"; page body says "your AI teammate for the whole team" |
| 4 | Slack: who sees = person who asked; reply can be shared to a thread | Slackbot page | verified | Interactions private; results can be shared to channels |
| 5 | Slack Business+ $15/user/month annual | slack.com/pricing (opened) | verified | $18 monthly |
| 6 | Full Slackbot only on Business+ and up | slack.com/pricing | verified | Free/Pro "limited trial experience" |
| 7 | Agentforce "available soon" | slack.com/ai-agents (opened) | verified | "will be available soon; talk to sales" |
| 8 | Slack can't: approval rules; work while nobody is online | Slackbot page | wrong (partly) | Page: agentic actions need human-in-the-loop approval; scheduled automations (weekly summary). "Own runtime, email" holds |
| 9 | Copilot Business $21, promo $18 through Dec 2026 | 11-competitors (Chrome, 13 Oct) | verified via 11, not re-opened (timeout) | |
| 10 | Business Standard with Copilot $23.50/seat | 11-competitors | verified via 11, not re-opened | |
| 11 | Copilot Business lists "LLM model choice" | 11-competitors | verified via 11, not re-opened | |
| 12 | Copilot Studio agents "metered through Azure" | none | unverified | Not in 11-competitors' sourcing table |
| 13 | Grok Bot: one cloud computer per person; bots share it | docs.x.ai teams-and-enterprises (opened) | verified | |
| 14 | "Cursor manages model selection" | same | verified | |
| 15 | "one user cannot reach another user's computer" | same | verified | |
| 16 | Grok Bot: who sees = "That person"; can't let a teammate see or continue | docs.x.ai/grok-bot/faq (opened) | wrong | FAQ: "To give the team one Bot they all talk to… publish a Team Bot." Also Bots "coordinate" with "direct handoffs" |
| 17 | Grok Bot included in paid Cursor plans | cursor.com/pricing, FAQ | verified | |
| 18 | Team admins get rules, secrets, review enforcement | teams-and-enterprises | verified (qualify) | Team Rules all plans; Secrets and Enforce Auto-review are Enterprise only |
| 19 | Grok Bot can't bring your own model or key | docs | unverified | Docs silent on BYOK; "Cursor manages model selection" implies it |
| 20 | Atlassian AMP 7 Oct 2026 | finviz/Business Wire (opened) | verified | Announced at Team '26 Europe, 6–8 Oct |
| 21 | AMP: owner, profile, scope; @mentioned in Jira, Confluence, Loom | release | verified | |
| 22 | "Run as User" or service account; full audit trail | release | verified | |
| 23 | Price not announced | release | verified | |
| 24 | "10 million" human-agent interactions a month | release | verified (qualify) | Release: "work together more than 10 million times a month"; unit undefined |
| 25 | Zed Delta free public beta since 16 Sep 2026 | zed.dev/blog/delta-public-beta (opened) | verified | "During the public beta, Delta is free." 11-competitors' "private beta" is stale |
| 26 | Session tools: private until shared, pick up, inline comments, `/handoff` | zed.dev, egregore.xyz | verified | `/handoff` is Egregore's, not Delta's |
| 27 | Lody price "not published" | lody.ai/price (opened) | wrong | Free to 3 members; Plus $8/seat/month yearly. Contradicts the plan's own "Lody $64" two paragraphs down |
| 28 | Egregore "an open install" | egregore.xyz | verified (qualify) | `npx create-egregore@latest`; licence not stated on page; enterprise inquiries offered |
| 29 | Session tools can't: agents that keep working | introducing-delta (opened) | wrong | Delta cloud runners keep running after the laptop closes |
| 30 | Session tools can't: non-code work | lody, nimbalyst, bloome | verified for Delta/Lody/Egregore | Nimbalyst (docs, trackers) and Bloome (both) are not code-only, but they are not in the row |
| 31 | Muse: free | muse-walkthrough research | unverified | Walkthrough records no price |
| 32 | Instinct "pays for things" | employee-voice research | verified (qualify) | Remembers card; founder claims >$1B/yr transactions (unaudited) |
| 33 | Consumer agents can't: permissions, approvals | research docs | wrong (partly) | Muse first-run: "With your approval, I can manage apps"; Instinct: separate monitor checks every action, access levels per person. "Shared record, a team" holds |
| 34 | Slack H1 "All your people and AI agents working together" | slack.com (opened) | verified | |
| 35 | Atlassian calls it "multiplayer" | release | verified | "integrated, multiplayer teamwork" |
| 36 | Grok Bot's bots hand work to each other | FAQ | verified | "direct handoffs" |
| 37 | "What none of them offer: … from any model vendor" | — | wrong (partly) | Bloome (any model, people and agents in one group chat, audit trail) and Copilot ("LLM model choice") overlap; Bloome is not in §6 at all |
| 38 | Any model: three provider paths | origin/main billing_repo `_VALID_PROVIDER_CHOICES` | verified | byo_key, chatgpt_oauth, openrouter_managed |
| 39 | "every agent from any vendor runs in the team's own isolated workspace" | CLAUDE.md, origin/main | unverified (overstated) | Models from any vendor: yes. Claude Code/Codex sessions run on members' own Macs, not the workspace runtime |
| 40 | Proof: six-agent roster, team email, approval cards | origin/main (paperclip_provisioning, agent_mail_send.py) | verified | B6: email is policy-gated, not per-message approval; keep "approval cards" off email |
| 41 | 8-person team $110, $13.75/seat | arithmetic on §7 | verified | 50 + 3×20; plan price not yet billable (§7) |
| 42 | Slack Business+ $120 for 8 | slack pricing | verified | annual |
| 43 | M365 Business Standard + Copilot $188 | 11-competitors | verified via 11, not re-opened | |
| 44 | Cursor Teams with Grok Bot $240 and up | cursor.com/pricing | verified | "$30+" |
| 45 | Nimbalyst $160 | nimbalyst.com/pricing (opened) | verified (qualify) | Teams "Free during Beta"; today it costs $0 |
| 46 | Lody $64 | lody.ai/price | verified | Yearly billing; free up to 3 members |
| 47 | Take from Delta: private until shared, anchored comments | zed.dev | verified | |
| 48 | Take from Egregore: handoff saves reasoning and open questions | egregore.xyz | verified | "decisions, trade-offs, open threads, and reasoning" |
| 49 | Take from Grok Bot: unread routines pause themselves | 2026-10-09-grok-bot-teardown.md | verified (teardown only) | Not on docs.x.ai FAQ; 11-competitors says teardown facts stay off public pages |
| 50 | Grok Bot: admin ceiling on what runs on a person's machine | — | unverified | |
| 51 | Slack: one entry point that routes to the right agent | — | unverified | Not on pages opened |

- **Contradicts:**
  - §6 table "not published (Lody)" vs §6 price line "Lody $64".
  - §6 positioning "the first five people are free" vs product-marketing.md positioning "so work assigned in chat gets finished and adding people is free", and its differentiator "one workspace price, no per-seat tax".
  - §6 "Slack Business+ is $120 ($15 a seat)" vs product-marketing.md "$13.75/seat vs $33 for Slack Business+ with Copilot" (11-competitors: "not 'about $33'").
  - §6 Grok Bot "a computer per person that nobody else sees" vs docs.x.ai FAQ Team Bot; product-marketing.md "Grok Bot… single-vendor models (Grok)" vs §6 "Cursor manages model selection".
  - §6 "Zed Delta (free public beta since 16 Sep)" vs 11-competitors.md "Zed Delta (12 Aug 2026): private beta" (11 is stale).
  - §6 contrast line vs decisions B8: "Slack appears in the battle card only to answer 'we already have Slack'" — §6's price paragraph uses Slack as the lead comparison; fine for an investor doc, but note the rule.
  - product-marketing.md "Human gates: approval cards for spend, deploys, sends and hires" and objection "Spend, sends… wait for a person" vs decisions B6 answer ("No per-message approval exists… never 'waits for a person'"). §6 claim 1 says "approval rules", which is safe.
  - 11-competitors.md lists "Corrections to the plan's §6… not answered; a founder still needs to approve them".
- **Missing:**
  - Is the positioning statement testable? Partly. "Work assigned in chat gets finished" and "the first five people are free" are checkable; "one shared record" is checkable in a demo. Nothing names the test (e.g. a setup-call question, or a week-4 metric that would prove "teams pick Lightbulb over Slackbot"). Add: "We are wrong if founding teams keep the work in Slack and use Lightbulb only for coding sessions."
  - What each competitor does better (11-competitors has it; §6 has none). An investor reading "what it can't do" for every row and "what it does better" for none discounts the whole table.
  - Bloome (closest in shape; own page by decision E7) and Nimbalyst (priced in the plan) are absent from the table.
  - Why incumbents cannot copy within 12 months (§12 says "they will"; §6 needs the moat line: neutrality across model vendors, not features).
  - Evidence that buyers compare us to these (no win/loss, no setup-call data yet; say so).
  - Dates: the table has no "checked on" per cell (competitor-profiling's "Current Data" principle); only the price line is dated.
- **Cut:**
  - "Slack's homepage now reads…" through "…not ours." (~45 words): context for us, already implied by the category sentence.
  - "Coding agents such as Claude Code…" (~20): repeats §3 and §12.
  - The whole "Contrast lines" paragraph (~85): copy for the site and battle card; an investor gets the same from the positioning statement.
  - "What we take from them" paragraph (~70): product-roadmap notes, not competition evidence.
  - "Business model: 'People are free…'" (~15): repeats §7 verbatim.
- **Must keep:**
  1. The alternatives list, including "do nothing" and DIY glue, and "the pitch has to beat 'good enough'".
  2. The comparison table, corrected and dated.
  3. "'People and agents together' is the category's language now": the honest admission that the claim must be more specific.
  4. The positioning statement and the three claims with proofs.
  5. The 8-person price comparison.

## 7. Business model (685 words)

- **Purpose:** show how Lightbulb makes money now and later, what one customer is worth, and that the offer is coherent and buildable.
- **Does it do the job:** partly. Packaging, price, value metric and the offer anatomy (core, risk reversal, real scarcity of ten, price lock, no discounting) are clear and match the founders' final answers. By the pricing skill's checklist it lacks willingness-to-pay evidence, unit economics (deferred to §11, which contradicts itself), payback, and expansion maths; the credits margin is stated but its derivation is not.
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 1 | Value scales with agents' work and people who see it | — | unverified | Assertion; no usage data cited |
| 2 | Slack and Teams "cannot follow without cannibalising seat revenue" | — | unverified | Opinion; Slack already gates Slackbot by seat tier |
| 3 | $50/month flat up to 5; +$20 per member from the 6th | decisions A9 answer, context v8 | verified | Plan price, not yet billable |
| 4 | One plan; Base and Ultra retired | decisions A9 answer | verified | Decision; code still has BASE/ULTRA (#13) |
| 5 | Enterprise custom pricing | plan | verified | Decision |
| 6 | 30-day free trial for every new team | origin/main billing_repo `_TRIAL_LOCAL_DURATION_SECONDS = 30 days` | verified | No-card trial exists; the card-on-file trial checkout does not |
| 7 | Self-serve adds a card when the month ends; founding team on file at setup | decisions A4 answer | verified | Decision |
| 8 | Credits at provider price × 1.2 | origin/main openrouter-pricing.json `"markup": "1.2"` | verified | |
| 9 | "leaves us about 10% after OpenRouter's and Stripe's fees" | openrouter.ai/docs/faq (5.5%, $0.80 min) + Stripe 2.9%+30¢ (not opened) | unverified (plausible) | 1.2 − 1.0 − 0.055 − 0.035 ≈ 0.11 → ~9% of revenue; minimums make small top-ups worse. Derivation not in plan |
| 10 | BYO ChatGPT/API key: inference costs us nothing | CLAUDE.md, origin/main provider paths | verified | Gate/runtime still cost (§11) |
| 11 | Marketplace takes 15% | origin/main config `MARKETPLACE_PLATFORM_FEE_PERCENT = 15` | verified | Liquidity unproven (Appendix D) |
| 12 | Ten founding teams set up by 4 Jan 2027 | decisions A3 answer | verified | |
| 13 | Price locked two years; shared channel; fixes within days | context v8 | verified | Decision |
| 14 | First charge $50 on day 31; no refund promise | decisions A1, A4 answers | verified | |
| 15 | "Ten teams is all three founders can set up by hand" | — | unverified | 01-marketing-plan models ~30 touches/week; capacity assumption not shown |
| 16 | Discount-takers churn ~2× full-price customers | marketing-skills offers (saas-offers.md heuristic) | unverified | No study cited; a skill heuristic is not a source |
| 17 | Six workspaces on 100% internal coupon; $55 lifetime revenue "are the evidence" | §9 | first-party, 12–13 Oct, not re-pulled | Numbers fine; logic wrong: internal coupons on our own workspaces are not evidence about discount-taker churn |
| 18 | Product bills Base $20 / Workspace $50 / Ultra $150, unlimited members | origin/main config.py lines 310–312 | verified | |
| 19 | Needs seat counting from the 6th | origin/main billing_service `"quantity": 1` | verified | No seat quantity in checkout today |
| 20 | Nobody outside pays for Base or Ultra | §9 | first-party, 12–13 Oct, not re-pulled | |
| 21 | Companies moving to buying finished work: Harvey, bookkeeping… | none | unverified | No source |
| 22 | Instinct earns from merchants its agent buys from | employee-voice research | unverified | Research has ">$1B/yr in transactions", not a merchant-fee model |
| 23 | Consumer agents collect 1 to 4% | none | unverified | |
| 24 | Business services "priced far higher per customer" | none | unverified | |
| 25 | "Engineering/Product item 5" | plan line 274 | verified | Exists: "Billing matches the plan." |

- **Contradicts:**
  - §7 credits margin "about 10%" vs §11 "gross margin is 70 to 90%" and §11 table "80–90%" (§11 contradicts itself; §7 gives no blended margin to reconcile them).
  - §7 "six workspaces on a 100% internal coupon… are the evidence" vs §9 "six active subscriptions, all on a 100% internal coupon" and Appendix C "Paying at list… coupon subscriptions do not count": the six are our own workspaces, so they evidence nothing about acquisition discounts.
  - §7 offer omits founder answers A12 ("Offer one 30-day pause per company") and A13 (90 days reading-only) that 15-trial-to-paid and context's flow carry; not a contradiction, but the offer is incomplete versus the decisions file.
  - product-marketing.md differentiator "no per-seat tax" vs §7's $20 per member from the 6th.
  - §1 "Managed AI usage is billed on top as metered credits" vs §7 "Two smaller streams sit on top": consistent, but §11 says credits are the only cost "that could run away", while §9 says "infrastructure, not inference, is today's main cost".
- **Missing:**
  - Unit economics: revenue per team (expected size × $50/$20 + credits), cost per workspace ($5–15 is in §11, not here), contribution margin per team.
  - Payback: CAC for a founder-run setup (three founders' hours per team), months to recover it at $50/month. Not stated anywhere.
  - Credits margin derivation (the 5.5% OpenRouter fee and $0.80 minimum; Stripe fees on top-ups) and what share of teams are expected to use managed credits vs their own key (§9: five of six on ChatGPT).
  - Willingness to pay: no Van Westendorp, no price objection from any setup call; the pricing skill's checklist item "Conducted willingness-to-pay research" is unchecked.
  - Expansion: seat revenue starts only at the 6th member, but 33 of 38 workspaces have one person; what share of target teams exceed five?
  - Annual billing and its discount (pricing skill default); not decided.
  - Churn assumption and what happens on day 31 without a card (15-trial-to-paid has it).
- **Cut:**
  - "That is the line Slack and…" (~30): repeats §6's business-model contrast line.
  - The second price table (~20): duplicates §1 and the sentence above it.
  - "Discount-takers churn at about…" through "…evidence." (~35): unsourced heuristic plus a non-sequitur; keep "We do not discount to acquire" and "The internal coupon is for our own workspaces only."
  - "Companies are moving from buying…" (~30): vision (§13 says it again).
  - "The subscription brings teams onto…" (~20): repeats §11's last paragraph.
- **Must keep:**
  1. The one plan: $50 up to five, $20 per member from the 6th, enterprise custom, credits at × 1.2 or bring your own key.
  2. The founding-teams offer: card at setup, 30 days free, $50 on day 31, two-year lock, no refund promise, ten teams.
  3. No discounts to acquire; internal coupon is internal only.
  4. What billing must change before the site quotes the price (fix-forward).
  5. Referral fees as a labelled long-term, experimental stream with every fee shown.

## Appendix B. Pricing history (281 words)

- **Purpose:** let a reader see how pricing moved and that today's plan is a deliberate decision, not drift.
- **Does it do the job:** partly. The dates mostly check out against origin/main, but it omits the first per-seat billing (10 Jul) and gives no reason for most changes, which is the one thing a pricing history is for.
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 1 | 13 Oct: credits stay × 1.2; "plus 3%" withdrawn | openrouter-pricing.json; context v8 | verified | |
| 2 | 13 Oct: 30 days free, card at setup, $50, two-year lock; week-4 refund dropped | decisions A1, A4 answers | verified | |
| 3 | 13 Oct: no 90-day free period; no coupons for outside teams | context v8 | verified | |
| 4 | 13 Oct: Base and Ultra retired; billing built before pricing page | decisions A9, A8 answers | verified | |
| 5 | 11 Oct: $50 up to 5, $20 from the 6th, enterprise; credits cost + 3% | context v6 | verified (decision record) | |
| 6 | 11 Oct: product still bills $20 / $50 / $150 | origin/main config.py | verified | Still true on 13 Oct |
| 7 | 22–23 Sep: Bedrock replaced by OpenRouter | origin/main 7cd5b07f0 (21 Sep), 0f1ceddd6 (23 Sep) | verified | |
| 8 | 22–23 Sep: credits × 1.2 (was about 3%) | openrouter-pricing.json note "Owner ruling 2026-09-23" | verified | "was about 3%" not checked |
| 9 | 22–23 Sep: trial 30 days at $0.25/day | 67c319d60, ed559dc03 (22 Sep); config 0.25 | verified | |
| 10 | (was 3 days at $3/day) | 162e586d9 (15 Jul, "true $3.00") | unverified | $3 cap verified; "3 days" not found |
| 11 | 22–23 Sep: Base plan $20 added | 7cd5b07f0 (21 Sep) "OpenRouter Free, Base" | verified | 21 Sep, a day before the row's range |
| 12 | 10 Sep: per-seat billing removed; unlimited members | 412340617, fc0da5251 (10 Sep) | verified | |
| 13 | 19 Aug: company marketplace and Ultra opened to buyers | 0866baeea (19 Aug) | verified | |
| 14 | 25 Jul: team billing $50 base + $20 per seat | #1477 base-plus-seats, merged 26 Jul | verified (date ±1 day) | Per-seat $20 Teams billing first shipped 10 Jul (#1213), not listed |
| 15 | 19 Jul: Ultra ($150) added | b8cf99ef7 (19 Jul) | verified | $150 not checked in that commit |
| 16 | 17 Jul: 14-day Stripe trial removed | a40f9491b (17 Jul) | verified | |
| 17 | 10–12 Jul: no-card free trial launched | 9619189e6 (10 Jul), 0f029ae87 (12 Jul) | verified | |
| 18 | 27 Apr: switched to flat monthly fee | 05fff0102, c3bae6611 (27 Apr) | verified | |
| 19 | 6 Apr: prod go-live with usage-based tiers | none found | unverified | |

- **Contradicts:** the 22–23 Sep row "managed credits at cost x 1.2 (was about 3%)" and the 11 Oct row "credits at cost plus 3%" read as a reversal the table never explains; the 13 Oct row withdraws it as "wording". Say it was a drafting error, or say it was a decision that was reversed.
- **Missing:** 10 Jul per-seat Teams billing ($20/seat, #1213); a "why" column (what each change learned); the trial model on Bedrock in July ($3 cap) vs OpenRouter in September.
- **Cut:** nothing material; the 13 Oct row (~70 words) can drop "a week-4 refund proposed the same morning was dropped" (~10) since the outcome is already stated.
- **Must keep:** 13 Oct row (current plan and offer); 10 Sep row (seat-free pivot that today's plan reverses); 27 Apr row (flat fee origin).

## Summary

1. Counts (114 rows): verified 74 (incl. qualified and via 11-competitors), first-party 6, Reddit permalink not re-opened 4, stale 0 (11-competitors' Delta "private beta" is the stale source), unverified 23, wrong 7.
2. Wrong: §5 IMARC market is "team collaboration software", not "team communication".
3. Wrong: §6 Grok Bot "that person" sees the work / can't share — docs.x.ai FAQ offers a Team Bot "they all talk to".
4. Wrong: §6 Lody price "not published" — $8/seat yearly, and §6's own price line says $64.
5. Wrong: §6 session tools can't keep working — Delta's cloud runners do.
6. Wrong (partly): §6 Slack can't do approvals/offline work — Slackbot has human-in-the-loop approval and scheduled automations.
7. Wrong (partly): §6 consumer agents lack permissions/approvals — Muse asks approval, Instinct has an action monitor; and "none of them offer… any model vendor" ignores Bloome and Copilot's model choice.
8. Wrong (logic): §7 cites six internal-coupon workspaces as evidence that discount-takers churn.
9. Largest single cut: §6 "Contrast lines" paragraph (~85 words), site/battle-card copy that repeats the positioning statement.
10. Most missing: §7 unit economics and payback (revenue per team, cost per workspace, founder-setup CAC, months to payback, blended margin with credits), plus a stated derivation for §5's $6B / 700,000 / $920M.


# Part 3

# Vet 3: business plan §8–13, Open questions, Go To Market, The website, Appendix C

Vetted 13 Oct 2026 against: `docs/business-plan.md` (working copy), `.agents/product-marketing.md` v8, `docs/marketing/README.md`, `00-decisions.md` (founder answers final), workstreams 01, 07, 09, 10, 11; `origin/main` at 6249f614d; the live site (curl + fetch, 13 Oct); public pages (Carta via search, Stripe via search). Skills read first: marketing-plan, analytics, launch, copywriting, cro.
"First-party" = PostHog/Stripe/Clerk/prod reads of 12–13 Oct, not re-pulled.

---

## 8. Go-to-market (338)
- **Purpose:** show a cold reader how the first customers are reached, what each stage must prove, and who does what.
- **Does it do the job:** partly. It has stages, gates, owners and channels, as the marketing-plan method asks. It has no pipeline numbers (touches, close rate), no budget per stage and no dates after January. The stage-2 gate uses a measure ("a working first session") that is not defined anywhere.
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 1 | "three stages, each unlocked by what the one before proves" | GTM section | verified | Internal consistency only |
| 2 | "ten founder-led teams of 3 to 15 people" | §5, 00-decisions A3 | verified | |
| 3 | "30 days free with a card on file, then $50" | §7, A4 | verified (as a decision) | Not billable today: trial checkout and seat billing are unbuilt (§7, item 5). The first setup is gated on them (A4, fix-forward). §8 does not say this |
| 4 | "the price locked for two years" | §7 | verified | |
| 5 | "ten set up by 4 January 2027" | A3, PMM v8 | verified (as a decision) | 01's own "What we know" still says 31 Jan (stale) |
| 6 | "about three ... paying by 12 January" | 01 §A pipeline | verified (model) | Base case gives 3.3 paying by 12 Jan, every rate labelled "assumption, low". Needs setups by ~12 Dec (day 31) |
| 7 | "at least six paying by May" | A3 | verified (as a decision) | 01 models 50% setup→paying (5 of 10); the plan's 6/10 = 60% is above the model |
| 8 | "Request a guided setup" on thelightbulb.company | live site | wrong (as a present-tense fact) | No such button; `/request-setup` returns 404 (curl 13 Oct). Planned, not live |
| 9 | "first session ends with a second person ... one real handoff" | GTM setup session | verified (as intent) | |
| 10 | "Leo on X and Reddit, Aden on LinkedIn, Prasiddha on HN and PH" | C3 | verified | |
| 11 | "Every public post ... signed off by a named founder" | B4, C2 | verified | |
| 12 | "Once a quarter of new teams ... week four, we open sign-up more widely" | GTM stage 2 | verified (as a decision) | "Working first session" undefined (Appendix C). Sign-up is already open (GTM: "Self-serve sign-up stays open") |
| 13 | "The first hire is a growth engineer" | §10, §11 | verified | |
| 14 | "Paid acquisition starts only after that" | GTM stage 2 | verified | |
| 15 | "single sign-on, admin permissions and approvals" (stage 3) | 11-competitors | verified | SSO not shipped (11) |
| 16 | "sign the first service vendors ... first sales hire" | §12 | verified | No date or trigger number |

- **Contradicts:**
  - §8 "we open sign-up more widely" and GTM "Self-serve sign-up stays open as free data" vs §12 "before we open sign-up" and PMM Goals "then open sign-up". Sign-up is open today (`/sign-up` 200).
  - §8 names stage 2 "a repeatable motion for small teams"; GTM names it "Stage 2: spend".
  - §8 "six paying by May" vs `docs/marketing/README.md` goal "Ten ... paying $50 a month at list by May 2027".
  - §8/§12 "4 January 2027" vs 01 "Stage 1 = ten ... by 31 Jan 2027" and 01's "stage-2 gate read for 31 Jan".
- **Missing:** the funnel numbers behind "ten by 4 Jan" (30 touches/week → ~30 conversations → ~10 setups, from 01); what stage 1 costs (founder hours); dates or target counts for stages 2 and 3; that the first setup waits on the trial checkout; what "working first session" means.
- **Cut:** "The detail, owners and weekly ..." (~12 words, pointer only); "The milestones for each stage ..." (~11 words, pointer only, and Risks does not give stage-3 milestones).
- **Must keep:** three gated stages; ten teams set up by 4 Jan with a card on file, ~3 paying by 12 Jan, ≥6 by May; the stage-2 gate (a quarter of new teams active in week four); founders own named channels and sign every post; no paid acquisition until a channel works for free.

## 9. Traction (657)
- **Purpose:** show honestly where the company is: users, usage, revenue, cost, and the signal in them.
- **Does it do the job:** partly. Honest and dated, and it names the decisions the data feeds (analytics lens). The AWS figure conflicts with §11. "Active" and "with 2+ humans" do not exclude Lightbulb staff. The funnel's denominators do not line up.
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 1 | "57 accounts (50 outside the team), 46 workspaces (38 outside)" | Clerk 12 Oct | first-party | Matches §1, PMM |
| 2 | "33 of them with one person" | Clerk | first-party | PMM says "36 of 46 workspaces have one member" (different denominator; not a contradiction, but the sentence reads as 33 of 46) |
| 3 | "About five outside workspaces are active in a given week" | PostHog | first-party | Week table: 4–6 |
| 4 | "2,740 messages sent by people since September" | prod store | first-party | PMM says "2,740 human messages all time". 09: 2,709 prod `room_message_sent` in 30 days |
| 5 | "82% of them from our own workspace" | prod store | first-party | |
| 6 | "$55 collected ever" | Stripe | first-party | Matches §7 |
| 7 | "six active subscriptions, all on a 100% internal coupon" | Stripe | first-party | Some are outside workspaces (Santi's idle workspace, ¶ below); §7 says the coupon is "for our own workspaces only" |
| 8 | "227 product-page visitors → 18 clicked sign up → 20 trials" | PostHog 30 days | first-party | 20 trials > 18 clicks: the steps are not one funnel (trials also arrive by join link and direct `/sign-up`). Say so |
| 9 | "10 got a first agent reply → about 5 ... still active" | PostHog | first-party | |
| 10 | "peaked at 50 visitors a day around 30 September", "about 10 a day now" | PostHog | first-party | |
| 11 | "three quarters arrives direct" | PostHog; 09 | first-party | 09: direct 212 of ~302 first-touch visitors ≈ 70% |
| 12 | "Instagram about 30 people in 60 days" | PostHog | first-party | 09: `ig` 26 in 30 days by first touch, so 60 days is likely more |
| 13 | "Five of the six subscribed ... own ChatGPT account" | prod rows | first-party | |
| 14 | "28 free-mail, 14 company domain, 8 .edu" | Clerk | first-party | Sums to 50 |
| 15 | "24 of 50 never returned after day one" | PostHog | first-party | |
| 16 | "Inside the wedge: 1 (Second Catch)" | §5 | first-party | |
| 17 | Readout "every Monday ... an agent writes" | 09 §11 | unverified | Script does not exist on origin/main (09 build task 11) |
| 18 | "Vercel Web Analytics is on" | 09 | wrong (as present tense) | `@vercel/analytics` not in package.json (09). Planned |
| 19 | "every product event carries the workspace id" | 09 | wrong (as present tense) | Billing events carry 0 `workspace_id`; 1,583/1,588 run events org-keyed (09). Planned |
| 20 | Weekly table, Sep 14–Oct 11 (4/6/4/5 active; 3/3/3/4 with 2+) | PostHog/prod | first-party | "With 2+ humans" may count Lightbulb staff in a customer's workspace; not stated |
| 21 | AWS Aug $1,627, Sep $1,670, Oct 1–11 $633 | Cost Explorer | first-party | Oct pace = $633/11×31 ≈ $1,784 |
| 22 | "ECS $145, AgentCore $83, Secrets Manager $80, Aurora $78" | Cost Explorer | first-party | |
| 23 | "one-person food business ... sending invoices since 11 October" | PMM, memory of #3857 | first-party | PMM confirms |
| 24 | "Second Catch ... joined on 6 October" | first-party | first-party | |
| 25 | "about $5,000 of spend" | PMM | first-party | |
| 26 | "first thing that broke was adding the cofounder" | PMM | first-party | |
| 27 | "Two of the subscribed workspaces went idle after two weeks" | first-party | first-party | |
| 28 | "AWS runs at about $1,700 a month" | Cost Explorer | first-party | Conflicts with §11 "$1.3–1.4k" |
| 29 | "infrastructure, not inference, is today's main cost" | rows 13, 21 | verified (follows) | |
| 30 | "CEO of Habits365 has asked for a demo" | PMM | first-party | |

- **Contradicts:**
  - §9 "AWS runs at about $1,700 a month" vs §11 table "Fixed cost (AWS, dev and prod) | $1.3–1.4k a month".
  - §9 "2,740 messages ... since September" vs PMM "2,740 human messages all time".
  - §9 "six active subscriptions, all on a 100% internal coupon" (some outside) vs §7 "The internal coupon is for our own workspaces only".
  - §9 heading text "on 12 October 2026" vs Open questions "added to Traction on 12–13 October".
- **Missing:** whether "active" and "2+ humans" exclude Lightbulb people; the AWS cost per workspace (only the total is given); the trend of the five Monday numbers (none exists yet, so say "zero by construction"); cohort retention for the 20 trials.
- **Cut:** "Lightbulb is early, and this section says so plainly." (~9 words, says nothing the numbers don't); "What those users show is ..." sentence (~30 words; restates the risks fallback, not evidence); the "What we measure from here" paragraph (~95 words) belongs in Appendix C / item 7, it is plan, not traction.
- **Must keep:** 50 outside accounts, 38 outside workspaces, 33 with one person; ~5 active a week; 0 paying at list, $55 ever; the funnel; AWS ~$1.7k a month against no list revenue; Second Catch as the one wedge team.

## 10. Team (127)
- **Purpose:** tell an investor or a prospective founder who the founders are and why they can win.
- **Does it do the job:** no. Names and roles are placeholders; there is no background, no ownership split, no time commitment and no equity/vesting note.
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 1 | "built by three founders and the agents they run" | PMM, GTM owners | first-party | |
| 2 | "Most of the engineering is done by AI agents" | none | unverified | Plausible from repo history; no measure given (e.g. share of PRs by agent sessions) |
| 3 | "ships a messaging workspace, six agents, a task board, a Mac app and an iPhone app" | Appendix D, CLAUDE.md | verified (partly) | iPhone app is TestFlight only; say so |
| 4 | "Isol8 Inc., doing business as Lightbulb AI" | CLAUDE.md | verified | CLAUDE.md: entity rename not yet effective; this is the only allowed legal name. Title says "The Lightbulb Company" |
| 5 | "first hire is a growth engineer" | §8, §11 | verified | |
| 6 | "first sales hire follows once companies arrive" | §8 stage 3 | verified | |

- **Contradicts:** none found.
- **Missing:** who each founder is (role, background, prior work, why this problem); who is full-time; equity split and vesting; advisors; the MKT385 interns and the agents (Forge, Pulse) as the actual labour model; 00-decisions F4 asked each founder for a 60-word bio "this week".
- **Cut:** "Between them they cover product ..." (~22 words; unsupported until the bios exist).
- **Must keep:** three founders with named channels and roles; agent-run engineering as the reason three people ship this much; legal entity name; first hire is a growth engineer.

## 11. Financials and the ask (380)
- **Purpose:** state cash, burn, runway, unit economics and the raise with its use of funds.
- **Does it do the job:** no. Cash, burn and runway are placeholders. There is no use-of-funds breakdown. Two cost figures conflict with §9 and with each other. The per-workspace cost counts only AgentCore.
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 1 | "a live workspace costs us $5 to $15 a month" | Cost Explorer | first-party, unverified range | $83 / 15 runtimes / 11 days ≈ $15.3 a runtime-month before the park. The $5 floor has no measurement. Secrets Manager ($80 in 11 days ≈ $218/month) also grows with workspaces and is not counted |
| 2 | "AgentCore billed $83 for 15 runtimes over the first 11 days" | Cost Explorer | first-party | |
| 3 | "before the 30-minute idle park shipped" / "shipped 9 Oct" | origin/main | stale | #3987 merged 9 Oct 22:24 EDT (`IDLE_TIMEOUT_S = 30 * 60`). Merged ≠ in prod (prod is promoted by hand); deploy not checked. The park took effect on 9–11 Oct at the earliest, so the $83 is mostly before it |
| 4 | "almost nothing per extra person" | architecture | verified (qualitative) | |
| 5 | "$50 a month and $20 per seat past five" | §7 | verified (decision) | Not billable yet |
| 6 | "gross margin is 70 to 90%" | arithmetic | unverified | ($50 − $15)/50 = 70%, ($50 − $5)/50 = 90%, before Stripe (~$1.75) and per-workspace secrets: ~66–87% |
| 7 | "On AWS cost alone ... breaks even at 30 to 40 paying teams" | arithmetic | wrong | $1.3–1.4k / ($50 − $1.75 − $5..15) = 30–42 teams. With §9's $1,700 (Oct pace $1,784), it is 40–54 teams |
| 8 | Table "Runtime cost per workspace $5–15" | as row 1 | first-party | |
| 9 | Table "Cost per human seat about $0" | architecture | verified (qualitative) | |
| 10 | Table "Gross margin ... 80–90%" | none | wrong | Prose above says 70–90% |
| 11 | Table "Fixed cost (AWS, dev and prod) $1.3–1.4k" | none | wrong | §9 table: $1,627 Aug, $1,670 Sep, ~$1,784 Oct pace |
| 12 | "$2 million on a SAFE at a $14 million post-money cap" | §1 | verified (decision) | |
| 13 | "in line with 2026 pre-seed rounds for AI companies" | research "under business-plan/research in the project files" (Appendix A), not in repo | unverified | Secondary summaries of Carta: median cap ~$12–13M for $2M raises (Aug 2025), ~$15M for the $1–2.5M band (2025). $14M is plausible; Carta's Q2 2026 page returned 403. Cite it |
| 14 | "to fund 24 months" | none | unverified | Implies ~$83k a month average burn; no lines given |
| 15 | "salaries ... growth engineer from 2027, infrastructure ... paid acquisition" | none | unverified | No amounts. AWS alone at $1.7k ≈ $41k over 24 months |
| 16 | "7% week over week, which doubles the business every ten weeks" | arithmetic | verified | ln2/ln1.07 = 10.2 weeks |
| 17 | "ten founding customers to several hundred paying teams within a year" | arithmetic | verified | 10 × 1.07^52 ≈ 337 |
| 18 | "the bar ... the best early-stage companies are measured against" | none | unverified | YC's 5–7% weekly growth guidance; cite it |
| 19 | "managed usage is sold as metered credits and never bundled" | §7 | verified (decision) | |

- **Contradicts:**
  - §11 "gross margin is 70 to 90%" vs §11 table "80–90%".
  - §11 table "Fixed cost ... $1.3–1.4k a month" vs §9 "AWS runs at about $1,700 a month".
  - §11 "falls with the 30-minute idle park (shipped 9 Oct)" vs CLAUDE.md "main IS AHEAD OF PROD BY DEFAULT"; "shipped" in Appendix A is defined as "merged".
  - §11 implies a growth engineer "from 2027"; GTM puts the hire after the stage-2 gate (week-4 data no earlier than Feb 2027). Consistent only if "2027" means "after the gate".
- **Missing:** cash in bank; monthly burn by line (salaries, AWS, vendors such as OpenRouter, Clerk, Vercel, PostHog, Stripe fees, LiveKit/Recall); runway; use of funds by line and by month; when salaries start; the AWS cost that does not scale with workspaces vs the part that does; a 24-month headcount plan; what milestone the seed is raised on, with a date; source for the $14M cap.
- **Cut:** "We are not writing a five-year forecast." (~8 words, keep only if a 24-month budget replaces it); "The bar we hold ourselves to is the one the best early-stage companies are measured against" (~17 words, replace with a cited benchmark); "At $50 a team that is still a small revenue line, and we say so" (~15 words; correct, but repeats §13).
- **Must keep:** the raise ($2M SAFE, $14M post, 24 months); per-workspace cost and the margin it gives; breakeven teams on fixed cost (once corrected); managed AI as credits, never bundled; the 7%-a-week bar and what the seed is priced on.

## 12. Risks and what we're testing (498)
- **Purpose:** name what could kill the plan and the test, with a threshold, that would show it.
- **Does it do the job:** partly. The wedge risk has a test and a fallback. The incumbent and spend risks have no threshold or date. One product-risk claim (email sign-off) is wrong per the founders' own ruling.
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 1 | "customers who have stuck so far came from outside it" | §9 | first-party | |
| 2 | "ten teams set up by 4 January 2027, six paying by May" | A3 | verified (decision) | |
| 3 | "a quarter of new teams still active in week four before we open sign-up" | GTM stage 2 | verified (decision) | Sign-up is already open (see contradictions) |
| 4 | "the fallback ... small businesses running back-office work" | PMM | verified | |
| 5 | "a week-4 miss on ten teams is a signal, not a verdict" | | verified (reasoning) | n=10; 25% = 2.5 teams |
| 6 | "Slack, Microsoft, Anthropic and OpenAI can each add agents" | §6 | verified | |
| 7 | "none of them will build a neutral place" | none | unverified | Opinion; Slack says "your people and AI agents" (§6) |
| 8 | "Stripe's agent wallet and Issuing for agents give an agent a one-time card" | web search (secondary) | verified (medium) | Link wallet for agents launched 29 Apr 2026, built on Issuing for agents; each spend needs consumer approval. Cite Stripe's own page |
| 9 | "spending limits to follow" | same | stale | Secondary sources say per-authorization and period limits exist now; auto-approval is what is "coming" |
| 10 | "no charge goes out without a person's sign-off" | approvals rail | verified (partly) | True for spend approvals; not shown for every paid tool call |
| 11 | "the first service vendor signed once companies are buying through Lightbulb" | §8 stage 3 | verified (decision) | |
| 12 | "every outside action runs in the team's own isolated runtime" | CLAUDE.md | unverified | Browser is a separate AgentCore Browser session; agent mail is sent by the backend (`agent_mail_send`), not the runtime |
| 13 | "high-stakes actions wait for a person" | approval cards | verified (partly) | Spend/hire yes; outbound email no (row 14) |
| 14 | "every public post, email or charge needs human sign-off" | 00-decisions B6 | wrong | B6 (final): no per-message approval; `send_to_anyone` defaults to True (`agent_email_repo.py:40`); "never 'waits for a person'". The GTM website text says the same |
| 15 | "sold as metered credits, never bundled" | §7 | verified | |
| 16 | "AWS AgentCore, OpenRouter and two open-source projects we fork" | hermes-version.json, CLAUDE.md | wrong | Paperclip is a fork. Hermes is the upstream image `nousresearch/hermes-agent` pinned by digest, not a fork |
| 17 | "images are pinned and promoted to production by hand" | CLAUDE.md, hermes-version.json | verified | |
| 18 | "the plan's first hire is a growth engineer" | §8 | verified | |
| 19 | "runbooks and agent memory are written so the system does not live in one head" | docs/runbooks | verified (partly) | Runbooks exist; "agent memory" as continuity is unmeasured |

- **Contradicts:**
  - §12 "every public post, email or charge needs human sign-off" vs 00-decisions B6 answer "No per-message approval exists ... never 'waits for a person'" and the Website section "it never promises that mail waits for a person".
  - §12 "before we open sign-up" vs GTM "Self-serve sign-up stays open as free data".
  - §12 "two open-source projects we fork" vs `hermes-version.json` `"base": {"image": "nousresearch/hermes-agent@sha256:…"}`.
- **Missing:** a threshold and date for the incumbent test ("drop it when those tools add a feature" — measured how?); the security risk's test (prompt-injection incidents, approval coverage); vendor-concentration plan (AgentCore/OpenRouter price or outage); key-person risk on Prasiddha (sole HN/PH/data owner and admin); legal-entity rename risk; the fallback's own trigger (what number on 4 Jan / May switches to the small-business fallback).
- **Cut:** "That is how a team this size ships this much" (~10 words, repeats §10); "and it matches the rule we already run under" clause (~10 words, and the rule as stated is wrong).
- **Must keep:** the wedge test with its numbers and fallback; the incumbent risk with a measurable test; agents that spend, held to stage 3; the product risks (injection, heavy managed usage, vendor pins) stated correctly; founder bandwidth.

## 13. Vision (261)
- **Purpose:** say what the company becomes if it works, and why that is large.
- **Does it do the job:** partly. Clear end-state and business model; no sourced market size, no five-year milestones.
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 1 | "In five years most of the work a company buys will be done by AI services" | none | unverified | Forecast; label it as one |
| 2 | "Today they work inside a dozen vendors' products, each one a silo" | §2 Salesforce | verified (partly) | §2: "half of deployed agents run in isolated silos" |
| 3 | "That is the role Slack and Microsoft Teams play for people today" | §5 | verified | |
| 4 | "the tool a company runs its work in is the tool it keeps" | none | unverified | Assertion; no retention data cited |
| 5 | "providers that win that business pay us" | §7 | verified (decision) | Unbuilt |
| 6 | "every fee shown" | §7 | verified (decision) | |
| 7 | "a market that is already measured in trillions" | none | unverified | No source; §5 gives $21.5B and $37B figures |

- **Contradicts:** §13 "a market ... measured in trillions" vs §5's sourced sizes ($21.5B team-communication software, $37B enterprise gen-AI). Not wrong, but no line connects them.
- **Missing:** what year-five Lightbulb looks like in numbers (teams, revenue mix of subscription vs referral fees); the source for "trillions" (business-services spend).
- **Cut:** "A company on Lightbulb gets more done with fewer tools ..." sentence (~38 words, restates the sentence before); "Every company will have a staff of agents alongside its people, and the question ..." (~25 words, rhetoric).
- **Must keep:** the neutral place for every vendor's agents with one record; Slack/Teams' role as the prize; referral fees from services agents buy, shown to the customer.

## Open questions for Prasiddha (44)
- **Purpose:** list what the plan cannot answer without a founder.
- **Does it do the job:** partly. It names two gaps; there are more.
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 1 | "Two things remain to fill in" | plan | wrong | Also open: use of funds; the AWS fixed-cost figure (§9 vs §11); the Bedrock upstream ruling item 8 needs; B9 data answer ("Founder unsure"); the $14M cap source |
| 2 | "Stripe, Clerk and PostHog figures were added ... on 12–13 October" | Appendix A | first-party | §9 says "12 October" |

- **Contradicts:** "12–13 October" vs §9 "on 12 October 2026".
- **Missing:** the items in row 1; the advisor list; whether "with 2+ humans" excludes our staff.
- **Cut:** none.
- **Must keep:** founders' backgrounds; cash, burn, runway.

## Go To Market (long section: stages 1–3) (1,061)
- **Purpose:** the executable stage-1 plan: sources, message, cadence, owners, setup session, proof, rhythm, launch; then stage 2 and 3 channels.
- **Does it do the job:** partly. Strong on owners, rhythm and ORB channels (launch and marketing-plan lenses). Weak on numbers: no touch → setup → paying targets in the plan (they live in 01), the A/B ask test is underpowered at this volume, the launch gate is shorter than 07's, and two present-tense facts are not true today.
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 1 | "ten founding teams set up by 4 January 2027" | A3 | verified (decision) | |
| 2 | Sources: r/ClaudeAI and HN threads, plugin users, founder networks | 02 | verified (plan) | |
| 3 | "listing the `lightbulb` plugin in both marketplaces" | 07 | verified (plan) | In neither directory today (07); Codex submission status unverified |
| 4 | "Habits365 inbound, Second Catch's circle, accelerator batches" | PMM, C13 | first-party | |
| 5 | "Not this stage: Instagram, local SMB lists, paid acquisition" | 01 | verified | |
| 6 | "one proof (Second Catch's plan, Anna's invoices)" | B1, B2, PMM | verified | Both cleared |
| 7 | "Two asks are written and split-tested" | 03 | unverified (method) | At ~30 touches a week and ~10% reply, a winner after weeks is noise; say it is a read, not a test |
| 8 | "Three to five touches, each standing alone" | 03 | verified | 03 has day 3/8/16 sequence (four touches) |
| 9 | "a direct message only after someone asks for help in the thread" | C2 | verified | |
| 10 | "Ten first touches a week per founder is the floor" | A5, 01 | verified | |
| 11 | "Aden and Leo go to twenty from week 3" | A5 | verified | |
| 12 | "No pause over the holidays" | A6 | verified | 01 still models 10-touch holiday weeks |
| 13 | "One founder-signed post a week each" | 13 | verified | |
| 14 | "Only a team that passes the §5 qualification list" | §5 | verified | |
| 15 | "Cross-role teams first" | C5 | verified | |
| 16 | "the first approval card is the invite" | C4 | verified | |
| 17 | "Forty-five minutes ... Aden by default" | 04, C1 | verified | |
| 18 | "the card goes on file in the session" | A4 | verified (decision) | Needs the unbuilt trial checkout; the stage cannot start until it ships (fix-forward). Not stated here |
| 19 | "the X reply pipeline has been dead since 11 October" | none | first-party, unverified | No source file |
| 20 | Aden answers setup requests "within one business day" | F3 | verified | |
| 21 | "Prasiddha: ... the data question answered in writing within a day" | B9 | verified | B9 answer is "provisional" |
| 22 | "MKT385 interns move from Miami SMB research" | none | first-party | |
| 23 | "The Anna case study (her consent, one 60-second recording ...)" | PMM, 01 | verified (plan) | |
| 24 | "no other team is named until three teams are paying" | B2 | verified | |
| 25 | "Every number and screenshot ... vetted by a founder" | B4 | verified | |
| 26 | "A light bulb goes in the post ..." | C14 | verified | |
| 27 | Monday pipeline stages | 01 §D | verified | |
| 28 | Thursday check-ins "drafted by the Growth agent" | D11 | verified | |
| 29 | "How would you feel if you could no longer use Lightbulb?" | 16, C9 | verified | Sean Ellis test; C9 wants it split wedge vs SMB with n — not stated here |
| 30 | "Self-serve sign-up stays open as free data" | live `/sign-up` 200 | verified | |
| 31 | Show HN/PH "as soon as the website brief has shipped and a session link brings a viewer into the workspace" | 07 | verified (partly) | 07's gate has five items (adds `/pricing` after billing, two named handoff teams, `/request-setup` with source). The plan lists two |
| 32 | "Prasiddha's live terminal on a public demo repo" | B11 | verified | |
| 33 | "viewer cap raised after a load test" | `session_links.py:270` | verified | Cap is `MAX_LINK_VIEWERS = 20` today |
| 34 | "the launch does not wait for the Codex plugin directory" | E13 | verified | |
| 35 | Stage 2 gate "a quarter of new teams reach a working first session ... week four" | §8 | verified (decision) | "Working first session" undefined in Appendix C |
| 36 | Stage 2 ORB lists: owned/rented/borrowed/loops | 07 §B | verified | |
| 37 | "a referral credit ... decided in this stage, not before" | A11, A14 | verified | |
| 38 | "until then `flickmeon.ai` redirects to the product site" | live | wrong | `flickmeon.ai/` returns 200 with a "Launch Competition" page, H1 "Your business is ready when you are."; only other paths 308 to the product. Decided (A10, /launch), not done |
| 39 | "paid acquisition only on a channel that already brings teams for free" | §8 | verified | |
| 40 | Stage 3: SSO, admin permissions, company plan, first vendors, first sales hire | §8 | verified (plan) | No trigger number |

- **Contradicts:**
  - GTM "No pause over the holidays" vs 01 pipeline "lower in Thanksgiving week (20) and the two holiday weeks (10 each)".
  - GTM launch gate (two conditions) vs 07 §A "all five must be green", and 07's own SLC note "seat billing ... does not hold the launch" vs its gate item 2 (billing then `/pricing`).
  - GTM "Self-serve sign-up stays open" vs §12 "before we open sign-up" and PMM Goals "then open sign-up".
  - GTM "flickmeon.ai redirects" vs CLAUDE.md "its root serves `/launch`" and the live page.
  - GTM stage 1 vs 01 "What we know": "Stage 1 = ten ... by 31 Jan 2027" (01 is stale on the date it cites from the plan).
- **Missing:** the numbers behind ten by 4 Jan (01's 325 touches → ~30 conversations → ~10 setups → ~3 paying), and a kill/adjust rule if week-2 rates come in under them; when the first setup can happen (after the trial checkout ships); launch date range (01: Show HN ~8 Dec, PH ~5 Jan); per-stage cost; stage-2 and stage-3 entry dates or counts; who runs setups when Aden is the default for all three founders' leads.
- **Cut:** "they cost founder time, not money, and they produce teams, which is what this stage needs" (~17 words, argument already made); "The session does by hand what Engineering/Product items 1 and 2 will do" (~16 words, true but not a decision); "The MKT385 interns move from Miami SMB research to finding technical founder teams" is a decision, keep; the stage-2 "Owned/Rented/Borrowed" list (~60 words) carries no owner, number or date — either give owners or cut to one line.
- **Must keep:** where the ten come from and the ask; touch floor and the week-2 check; qualification and the setup-session script; owners per channel; proof rules (Anna and Second Catch only); Monday/Thursday rhythm and the one number on the wall; launch for inbound gated on the session door; stage-2 gate and the "paid only on a proven free channel" rule.

## The website (734)
- **Purpose:** what each page must do in stage 1 and the copy that does it.
- **Does it do the job:** partly. By the copywriting and CRO lenses it is good: one primary action, customer-language problem, objections in an FAQ, team-first steps. Gaps: the A/B test cannot reach significance at today's traffic, proof is thin (one quote, one story, an unsourced revenue band), and the setup form's field count is inconsistent.
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 1 | "the homepage headline says 'team'" | live | verified | H1 "Your team and your agents, in one place." |
| 2 | "every step after it is solo" | live | verified (partly) | Steps are "Make a channel / Add your agents / Mention one"; no invite step |
| 3 | sign-up says "Your AI company starts here" | live `/sign-up` | verified | |
| 4 | "plans section shows three provider paths labelled as plans" | live | verified | "Choose your plan": ChatGPT / own key / Lightbulb-managed |
| 5 | "`/pricing` returns 404" | curl | verified | |
| 6 | "no founding-teams offer and no way to request a setup" | live, curl | verified | `/request-setup` 404 |
| 7 | price copy waits for seat billing | A8 answer | verified | |
| 8 | `/pricing`: "$50 ... up to 5, +$20 a member after, 30-day trial, × 1.2" | §7 | verified (decision) | |
| 9 | `/sign-up` headline "Bring your team." | 10 | verified (plan) | |
| 10 | `/request-setup` "Five fields" | 10, D13 | wrong (count) | D13 makes "How did you first hear about us?" required on this form: six fields; 01: "Five fields ... plus" the source question |
| 11 | "A founder replies within a day" | F3 | verified | F3 says "one business day", Aden |
| 12 | `/docs/get-started` rewrite | live | verified (need) | Live page still lists Workspace $50 "unlimited members" and Ultra $150 |
| 13 | "/compare/* Six pages (the five in workstream 11 plus Bloome)" | 11 | stale | 11 already holds six pages including Bloome (E7) |
| 14 | "each signed off by Leo" | E8 | verified | |
| 15 | Headline "Your team and your AI employees, in one place." | PMM Goals | verified (decision) | |
| 16 | A/B "for the first month, judged on setup requests and trial starts per visitor" | none | unverified (method) | At ~10 visitors/day (~300/month) and ~6% CTA rate, ~9 conversions per arm: no test can detect a difference. Ship the pick; read it qualitatively |
| 17 | Subhead "A CEO, Engineering, Finance and Growth agent in your team's channels" | B7 | verified | |
| 18 | "Request a guided setup (founding teams, ten spots)" | §7 | verified (decision) | |
| 19 | "Start free, no card, 30 days" | live, §7 | verified | Live: "Start free for 30 days — no credit card." |
| 20 | Problem quote "Your agent remembers what you did..." | PMM | verified | It is Egregore's founders (a competitor) on HN; on our homepage it reads as a customer quote. Attribute it or use a buyer's line |
| 21 | Proof: "Anna's quote, Second Catch's ... by name" | B1, PMM | verified | |
| 22 | "the revenue band as is" ("$2M+ in combined annual revenue") | live, PMM | unverified | No source; PMM: "attach the source when reused" |
| 23 | "$50 a month for up to five people" | §7 | verified (decision) | Must wait for billing (row 7) |
| 24 | FAQ objections | PMM Objections | verified | |
| 25 | Words to use / avoid | PMM | verified | |
| 26 | "Telegram, Discord and WhatsApp come off it" | E4 | verified (decision) | Live today lists them |
| 27 | "the owner decides who agents can email, which the server already enforces" | `agent_email_repo.py`, B6 | verified | `send_to_anyone=True` by default; owner can close to an allowlist |
| 28 | Named authors per page type | E3 | verified | |
| 29 | "The ten thin landing pages go, with redirects" | E1 | verified (decision) | |
| 30 | "`/ai-agents-for-slack` redirects to the Slack comparison page" | E2 | verified (decision) | Comparison pages publish after billing (E6), so the redirect target does not exist until then |
| 31 | "flickmeon.ai ... Redirect to the product site until then" | live | wrong (today) | Root serves the launch-competition page (200) |

- **Contradicts:**
  - Website "Five fields" vs §9 "the setup form ... ask 'How did you first hear about us?'" and 00-decisions D13 "required on the setup form".
  - Website "the five in workstream 11 plus Bloome" vs 11 "Build six pages ... 6 | Lightbulb vs Bloome".
  - Website "`/ai-agents-for-slack` redirects to the Slack comparison page" (now) vs E6 "comparison pages publish after seat billing ships".
  - Website "flickmeon.ai | Redirect to the product site until then" vs live flickmeon.ai (launch page, 200).
- **Missing:** current conversion rates per page as the baseline (227 → 18 is the only one); the page the session link lands on for launch traffic; mobile/no-JS checks; where the "$2M+" number comes from; what happens to `/docs/get-started`'s old plan list before billing ships (it quotes prices today, against the fix-forward rule).
- **Cut:** "Words to use: people, team, ..." list (~35 words; duplicates PMM, keep the pointer only); "Every page carries the two events ..." (~25 words; belongs in item 7 / Appendix C).
- **Must keep:** one job for the site (request a guided setup, free trial second); the page table; the headline and subhead; section order with proof and FAQ; price copy only after billing ships; email copy states the owner rule, never a per-message approval.

## Appendix C. Metric definitions (191)
- **Purpose:** define every number the plan and the Monday readout are judged on, precisely enough to compute.
- **Does it do the job:** partly. Five Monday numbers are defined and computable (09 has the HogQL). The analytics lens fails on three points: staff activity is not excluded, the stage-2 gate's "working first session" has no definition, and two of nine metrics cannot be computed today.
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 1 | Setup request = submitted form with self-reported and tracked source | 09 `setup_requested` | verified (plan) | No emitter on origin/main |
| 2 | Team formed = 2+ people within 48 h | 09 | verified (plan) | "People" can include a Lightbulb founder added during setup; exclude our staff |
| 3 | First handoff = approval answered / session picked up / task moved, in week one | 09, D12 | verified | Only `session_picked_up` exists today (09) |
| 4 | Weekly active = at least one human room message | 09 | verified | Founders' Thursday check-ins in a team's shared channel count as human messages; also counts one-person workspaces |
| 5 | Week-4 active = share of teams formed with a human message in week four | 09 | verified | 09 uses days 21–28 (8 days); same staff issue as row 4 |
| 6 | Paying at list = Stripe invoice at full price; coupons do not count | 09 Stripe query | verified | |
| 7 | Agent answered rate (2 minutes) | none | unverified | No query in 09; not one of the five numbers |
| 8 | Stop rate after Jev runs (60 s) | item 8 | unverified | Item 8: runs do not record who routed them and Stop does not record who pressed it; not computable today |
| 9 | External workspace = all but the seven listed | 09 F1 | verified (partly) | "the MKT385 intern workspace" has no slug; 09: the list "is not encoded anywhere"; 09 also excludes workspaces created by platform admins, which C omits |

- **Contradicts:**
  - Appendix C defines "Week-4 active" over "teams formed"; the stage-2 gate (§8, GTM) reads "a quarter of new teams reach a working first session and are still active in week four" — a different denominator and an undefined step.
  - Appendix C "Team formed ... two or more people" vs §5 "the second person they invite is the activation event" (invite vs join).
  - Eng/Product item 7 "week-4 active (emitted by the existing sweeper)" vs 09 "a sweeper event for week-4 active" rejected in favour of a readout query.
- **Missing:** "working first session"; exclusion of Lightbulb staff as authors and members inside customer workspaces; the "very disappointed" share (C9: split wedge vs small business, with n); pipeline terms used every Monday (touch, conversation, setup booked, set up); "account", "trial", "active subscription" as §9 uses them; the cohort rule for "new teams"; environment = prod and host filters (09 finds counts 6–15× too high without them).
- **Cut:** none; it is already terse.
- **Must keep:** the five Monday definitions; paying-at-list excluding coupons; the external-workspace rule.

---

## Summary
- Status counts (179 claim rows): verified 112 (many are founder decisions, not facts), first-party 35, unverified 17, stale 3, wrong 12.
- Wrong: §12 "every ... email ... needs human sign-off" (B6: no per-message approval; `send_to_anyone` on by default).
- Wrong: §12 "two open-source projects we fork" (Hermes is the pinned upstream image; only Paperclip is a fork).
- Wrong: §11 breakeven "30 to 40 paying teams", table margin "80–90%" (prose 70–90%), fixed cost "$1.3–1.4k" (§9: ~$1.7k; Oct pace $1,784 → 40–54 teams).
- Wrong: GTM and Website say flickmeon.ai "redirects" (root serves the launch-competition page); §8 "Request a guided setup on thelightbulb.company" and §9 "Vercel Web Analytics is on", "every product event carries the workspace id" are planned, not live; the Website's "Five fields" (six with the required source question); Open questions "Two things remain".
- Stale: "idle park (shipped 9 Oct)" is merged, not shown in prod; "spending limits to follow" (Stripe has them now); "/compare: five in workstream 11 plus Bloome" (11 has six); 01's "31 Jan" date.
- Cross-section conflicts: sign-up "open" vs "before we open sign-up"; README "ten paying by May" vs "six"; GTM "no holiday pause" vs 01's reduced holiday weeks; plan's two-item launch gate vs 07's five.
- Largest single cut: §9's "What we measure from here" paragraph (~95 words), which is a plan in the traction section and repeats item 7 and Appendix C.
- Most missing: §11's numbers: cash, burn by line, runway and use of funds for the $2M. Without them the ask cannot be judged. Founder bios (§10) are a close second.
- Metric risk: Appendix C does not exclude Lightbulb staff, so the founders' own Thursday messages in a founding team's channel make it "active" and can pass the week-4 gate.


# Part 4

# Vet 4: Engineering/Product (intro, items 1–9), Appendix A, D, E

Checked 13 Oct 2026 against origin/main = 6249f614d (the same commit 18-build-order.md used), docs/research/*.md, docs/marketing/*.md, .agents/product-marketing.md v8, CLAUDE.md, the live site and vendor pages. Methods applied: onboarding and signup (items 1–2), marketing-loops and referrals (item 4), paywalls and churn-prevention (item 5), analytics (item 7). Word counts are measured from the file.

## Engineering/Product intro (98)
- **Purpose:** state the gap (first session is single-player) and the rule for ordering items 1–9.
- **Does it do the job:** partly. The gap is clear. But "in the order the ten founding teams need them" is not the build order: 18-build-order puts measurement (item 7) and billing (item 5) first.
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 1 | "nothing asks who else should be in the room" | 18 T08 (`appLinks.ts` has no invite kind) | verified | |
| 2 | "invite card copies the join link, is admin-only" | `packages/shared/src/messaging/onboardingSteps.ts` | verified | `adminOnly: true`, detail "Share the workspace join link" |
| 3 | invite card "comes last" | same file | wrong | Order: starter-model, Stripe, GitHub, Notion, **invite**, name-agents, context. It comes after the connectors, not last. 18 T10 says "puts connectors first", which is the accurate wording |
| 4 | "older Get started card was removed in #2544" | git log 6ecfc88ad | verified | 18 T10: `getStartedConfig.ts` still exists, now dead |
| 5 | "off-app notification for web users is a daily digest" | `core/services/daily_digest.py` | verified | Daily at 08:00 local |
| 6 | "activation event is the second person invited" | §5, PMM line 145 | verified | But PMM and Appendix C define activation as 2+ people within 48 h, which is a *join*, not an *invite*. Use one definition |

- **Contradicts:** intro "in the order the ten founding teams need them" vs 18 "Order: measurement first, then billing (before the first setup), then items 1–9".
- **Missing:** that the numbering is not the build order. Who owns the items (Forge? Prasiddha?) and by what date, relative to the 4 Jan stage-1 gate.
- **Cut:** "(the older Get started card was removed in #2544)" (~10 words). It is history, not a decision.
- **Must keep:** the first session is single-player; activation is the second person; the items close that gap.

## 1. The first session ends with a second person in the room (141)
- **Purpose:** turn the first session into a team-forming moment.
- **Does it do the job:** partly. Today, Change and Measure are present. There is no baseline for the measure (share of workspaces with 2+ people in 48 h today), no target, and six changes are bundled into one with no order. The onboarding method asks for the aha moment and one goal per session. The signup method asks for a completion baseline before the GitHub sign-in is added.
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 1 | "33 of 38 outside workspaces have one person" | Traction §9 | first-party, 12–13 Oct, not re-pulled | |
| 2 | "Second Catch's first break was adding the cofounder" | §5, §9 | first-party, 12–13 Oct, not re-pulled | |
| 3 | "every retained customer is a workspace where a second person showed up" | §9 | wrong (internal contradiction) | §9: "The most active is a one-person food business". The retained customer with the most activity is solo |
| 4 | "any member can invite, not only admins" (implies admin-only today) | `routers/orgs.py` `_require_admin_of` | verified | |
| 5 | GitHub sign-in is not on today | 18 T11 ("GitHub is off in the Clerk instance") | unverified | It is Clerk config, not in code; the build order asserts it |
| 6 | Cites 08-onboarding-and-signup.md, 17-ideas.md | docs/marketing | verified | Both files exist |

- **Contradicts:** Why vs §9 (claim 3). 18 T11 makes GitHub sign-in depend on T06 (retiring Base and Ultra). The item presents it as a first-session change.
- **Missing:** the baseline share today (the Traction week table gives 4 of 5 active workspaces with 2+ humans, but that does not count new workspaces in 48 h). A target. The order in which the six changes ship (18 has T07 → T08 → T09 → T10 → T11).
- **Cut:** "since the wedge lives there" (5 words). It is unsupported, and the signup method says to test it.
- **Must keep:** the CEO asks "Who should see this with you?" with an invite; the first approval invites the decider; any member can invite; the measure is 2+ people in 48 h.

## 2. The first ten minutes produce one finished thing, not a tour (206)
- **Purpose:** define the first-session result (artifact + approval + outcome-shaped offers).
- **Does it do the job:** mostly. It meets the onboarding method well (do, don't show; one goal; connect from the chat). The measure has no baseline. The welcome-email change is a separate job.
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 1 | "the result lands on the board, which a new person has not opened" | 18 T13 | unverified | 18's Today is different: the brief "says who the person is, not what to deliver; connect links are plain URLs" |
| 2 | "replaces today's [welcome email]" | 18 T14 (`welcome_email.py`, `EMAIL_FROM_WELCOME`) | verified | |
| 3 | Muse: "setup inside the chat", "an Ideas list where every item is a finished promise" | docs/research/2026-10-13-muse-walkthrough.md | verified | |
| 4 | "too many complications" (Santi) | none in sources | first-party, not re-pulled | Not in docs/research or docs/marketing; source it or drop the name |
| 5 | Shafil: workspace "reached ready and was never typed in" | none in sources | first-party, not re-pulled | |
| 6 | "24 of 50 sign-ups gone after day one" | §9 | first-party, 12–13 Oct, not re-pulled | |

- **Contradicts:** Today vs 18 T13 Today (claim 1).
- **Missing:** a baseline for "first artifact and first approval in session one". What happens when the person has no tool to connect. Whether the approval is real spend or a token decision.
- **Cut:** "(Both borrowed from Muse: … never has to think up a prompt.)" (~25 words). Provenance belongs in research. Also "The CEO stays the only voice…" can be folded into the contract sentence (~15 words).
- **Must keep:** the first run ends in the room with one document, one approval and three outcome offers; connect from a card in the chat; the welcome email comes from the CEO agent's address; the measure.

## 3. People hear about the team's work when they're away (124)
- **Purpose:** get the second person back when something needs them.
- **Does it do the job:** mostly. Today, Change, Why and Measure are specific. The measure has no baseline and no target.
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 1 | "web gets an in-tab toast" | `components/notifications/MessageNotifications.tsx`, `RunNotifications.tsx` | verified | |
| 2 | "daily needs-you digest" | `daily_digest.py` | verified | |
| 3 | "mobile push works for members with the app" | `daily_digest.py` (Expo push), CLAUDE.md | verified | 18 T25: unverified whether approvals push |
| 4 | "the desktop app notifies natively" | CLAUDE.md (Desktop, `notify.rs`) | verified | |
| 5 | 5 min / 1 per room per 30 min / 22:00–08:00 | 00-decisions D8 | verified | |

- **Contradicts:** none.
- **Missing:** how "not connected" is detected. AppSync has no disconnect callback (CLAUDE.md); 18 T15 names read cursor + live activity. The current baseline for "needs you" → opened.
- **Cut:** none material.
- **Must keep:** an away email after 5 minutes, only when not connected, with rate and quiet limits; invited members included; the measure.

## 4. A shared session is a door into the workspace (247)
- **Purpose:** turn the one public surface into an acquisition loop.
- **Does it do the job:** partly. Today and Change are precise. Against the loops/referrals method: trigger, share action and conversion are named, but there is no baseline (session-link opens are not counted for signed-out viewers, per 18 T16), no conversion target, and no stop condition. The item says so honestly ("amplification rather than virality").
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 1 | "live terminal, view only" | `app/s/page.tsx:68` | verified | |
| 2 | "at most 20 viewers" | `session_links.py:270` `MAX_LINK_VIEWERS = 20` | verified | 21st sees "Could not reach this session" (18 T18) |
| 3 | "teammate can already ask to join … one tap" | `routers/messaging.py:482,488` join-requests | verified | |
| 4 | join link "admits anyone without approval" | `routers/orgs.py:135` "No admin approval step" | verified | |
| 5 | on by default, owner can turn off per session | 00-decisions D1, D2 | verified | |
| 6 | "only the Mac app shares sessions" | CLAUDE.md; `lightbulb:share` needs the Mac app | verified | |
| 7 | "one `member_joined` event for every door" (implied absent) | 18 T02 | verified | Absent today |
| 8 | cites 06-referrals-and-loops.md | docs/marketing | verified | |

- **Contradicts:** none with 18 (T16–T18 match). The Go To Market Show HN plan needs "the viewer cap raised after a load test" before launch. This item ties the raise to "launch week only". Consistent, but the item does not say the cap gates Show HN.
- **Missing:** today's link-open count (none for signed-out viewers; that is T16). Where the signed-out "Start your own workspace" source is stored. A stop/abuse rule for strangers asking to join (spam requests to the owner).
- **Cut:** "The public session page is the only no-account look at the product; nothing more is built for launch." (~20 words). It restates D4; keep only if a reader would ask.
- **Must keep:** outsiders get Ask to join, and the owner's yes makes them a member; on by default for Anyone links; "Start your own workspace" with the source; a full state and a load test before raising the cap; the measure.

## 5. Billing matches the plan (172)
- **Purpose:** make the product bill the §7 price before anything quotes it.
- **Does it do the job:** partly. Today and Change are specific. "Why: §7" is a pointer, not a reason. The paywalls and churn methods ask for the trial-end moment (recap, what happens on day 31, lapse state) and involuntary-churn handling. The item has neither, though 18 T19 and T20 (day-28 recap, reading-only, one pause, 90 days, export) are filed under item 5. The measure ("paying workspaces at list") has a baseline of 0 but no target here; the targets sit in §8.
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 1 | "$20 / $50 / $150, unlimited members, no seat counting" | CLAUDE.md Billing; 18 T04 (`quantity: 1`) | verified | |
| 2 | "`/pricing` … returns 404 today" | live fetch 13 Oct | verified | |
| 3 | "old `flickmeon.ai/pricing` links land on it" | curl: 308 → thelightbulb.company/pricing | verified | |
| 4 | "nobody outside pays for either" (Base, Ultra) | §9 (0 paying at list) | first-party, not re-pulled | |
| 5 | "the phone app sells one product" (change) | `PaywallScreen.tsx` lists three today (18 T06) | verified as today's gap | |
| 6 | "`flickmeon.ai` redirects to the product site until…" | live: `flickmeon.ai/` returns 200 "Lightbulb Launch Competition" | wrong if read as today | Only non-root paths redirect today. The root redirect is task T27. PMM line 13 makes the same present-tense claim |

- **Contradicts:** PMM v8 line 13 "the domain redirects to the product site until then" vs live site (root serves the competition). 18 T05 adds two things the item omits: `allow_promotion_codes=False` and the 3-day docstrings in `trial_cap.py`/`provision_gate.py`.
- **Missing:** what happens at day 31 for self-serve teams without a card (18 T19: reading-only, 90 days, one pause). Failed-payment handling (Stripe Smart Retries/dunning). Apple-billed rows (T04 excludes them; the item does not say). Proration rule when a member leaves mid-cycle (credit or not).
- **Cut:** "`flickmeon.ai` redirects … after items 1 to 9." (~25 words). It is site work (T27), not billing.
- **Must keep:** seat quantity from the 6th member, prorated; trial checkout with the card at setup and the first charge on day 31; Base and Ultra retired; billing ships before any page that quotes a price.

## 6. The phone app is in the App Store (60)
- **Purpose:** remove the TestFlight wall for a teammate invited by text.
- **Does it do the job:** yes, briefly. It needs a baseline (TestFlight installs from invites) and one risk.
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 1 | "TestFlight only" | CLAUDE.md, PMM line 12 | verified | |
| 2 | "in-app purchase waits on Apple's paid-apps agreement" | CLAUDE.md (`APPLE_IAP_SELLING` off in prod) | verified | |
| 3 | "the second person is usually invited by text" | none | unverified | No data in sources; it is also the premise of item 9 |

- **Contradicts:** 18 T21 depends on T06 (one plan). Not stated here.
- **Missing:** App Review risk. An app that shows plans but sells nothing in-app must not steer US users to web checkout outside the allowed rules; CLAUDE.md notes the web-checkout link is US-only. Target date relative to the 4 Jan gate.
- **Cut:** none.
- **Must keep:** ship without in-app purchase; subscriptions stay on the web; measure installs from invites.

## 7. The product emits the numbers the plan is judged on (121)
- **Purpose:** make the five Monday numbers countable.
- **Does it do the job:** partly. It is the closest item to the analytics method (events, properties, UTM, source question). It has **no Today line**, unlike every other item. One of its premises is wrong (see claim 2). Its measure ("the Traction section") is circular; 18 T23/T24 give real checks.
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 1 | "today's activation metric (5 messages on 2 days)" | `core/services/ops/growth.py:54-56` | verified | Also a 7-day window (`ACTIVATION_WINDOW_DAYS = 7`) |
| 2 | "week-4 active (emitted by the existing sweeper)" | 18 T23: "none exists" | wrong | `ops/growth.py:378` *computes* week-4 retention in a report but emits no event; no sweeper does. It also counts from the activation week, not from team formed |
| 3 | `trial_converted` should fire "only for paid invoices" (fires on status today) | `stripe_webhook_handlers.py:280,511`; 18 T23 | verified | |
| 4 | `agent_run_completed` not firing for room runs | `messaging.py:4268` returns `ignored` before `schedule_run_completed`; 18 T01 | verified | |
| 5 | Vercel Web Analytics off | 18 T23 (`@vercel/analytics` not installed) | verified | Not in `apps/frontend/package.json` |
| 6 | source question required on setup form, optional at sign-up | 00-decisions D13 | verified | |

- **Contradicts:** claim 2 vs 18 T23. Handoff definition: item 7 counts "approved, rejected or continued" (D12), but PMM line 145 says "approved or continued" (rejected missing).
- **Missing:** a Today line. The identity rule (18 T01: `org_` distinct ids and dev pages tagged `prod`, which corrupt every count today). An exclusion list owner (F1: Prasiddha). A data-quality check (analytics method: validate before trusting).
- **Cut:** none; it is tight.
- **Must keep:** the five events with `workspace_id` + `environment`; `trial_converted` on paid invoices only; room runs counted; UTM + source question; the Monday readout.

## 8. Agents that communicate like employees, not like a model (785; the brief said 900)
- **Purpose:** decide how agent replies stop reading as generated text, and how we will know.
- **Does it do the job:** partly. The decision (rules + blind eval first, then model choice, then a tuned speaker) and the measures are there. But the item is a research plan written as a product item. About half its words are method detail. Its "How" (founder rulings: fix it in the model's output) contradicts its own Change ("No fine-tuning"), and both stand in one paragraph.
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 1 | "it feels like slop and stuff I won't do" (founder, 13 Oct) | research employee-voice, line 3 | verified | |
| 2 | "generated SOUL.md carries only a name, title and capabilities" | `hermes_profiles.build_soul_md` | verified | Plus a "You are X at Y" line and a CEO line; nothing on how to reply |
| 3 | "the one style guide we ship (Paperclip's comment style)" | `skills-src/lightbulb/references/chat-vs-document.md`, `voice.md` | wrong | Two more ship: chat-vs-document ("chat gets a short summary") and voice.md ("short, plain sentences, no markdown"). The research file makes the same error while listing chat-vs-document as context |
| 4 | comment style "requires headings and bullets" | `skills-src/paperclip/references/comment-style.md` | partly wrong | It requires "a short status line" and bullets; a heading appears only in its example |
| 5 | "no clean, commercially usable corpus of workplace chat" | research employee-voice; messaging-datasets | verified | messaging-datasets finds three CC BY sets of developer chat; none is a company Slack |
| 6 | Muse is Meta; Instinct is Noah Shinn's startup | research employee-voice line 8 | verified | |
| 7 | "neither … trained on chat for tone" | research: "Neither video describes tone training" | overstated | The research says neither *described* it. Write "neither describes training on chat" |
| 8 | Muse: "over a hundred behaviours … launch-blocking bar" | research (13:22, 22:09) | verified | |
| 9 | Instinct: point "in the first 30%", learns from "lower response rate" | research (50:30, 1:19:14) | verified | |
| 10 | Run does not record Jev/fallback/overrule; Stop not attributed | research socially-aware line 11; 18 T35 | verified | |
| 11 | 30-minute follow-up, "three in two weeks", style note, log-only pre-send check | research socially-aware Part 3 | verified as proposals | The research flags every number as untested (line 213) |
| 12 | Candidates "GPT-5.6, Claude, DeepSeek, Qwen3, Kimi, GLM" | none | unverified | GPT-5.6 is not in any source |
| 13 | "3–10k turns … a few thousand preference pairs" | none | unverified | Not in the research files |
| 14 | Bedrock "about $80 per training hour" | aws.amazon.com/bedrock/pricing (RFT) | verified | Only for gpt-oss-20b and Qwen3 32B. A Claude or GPT candidate cannot be tuned this way |
| 15 | "roughly $1–2k per full run" | none | unverified | Derived; no hours estimate given |
| 16 | "Bedrock inference was retired in September" | CLAUDE.md (#3051, #3056); Appendix B 22–23 Sep | verified | |
| 17 | OpenRouter private routing is Enterprise-only | openrouter.ai/docs/guides/routing/private-models | verified | Invite-only beta for Enterprise |
| 18 | "OpenRouter itself trains nothing" | openrouter.ai privacy page | unverified | The page does not describe OpenRouter's own training; it offers no fine-tuning product, which is probably the intended meaning |
| 19 | KTO needs only good/bad labels | research socially-aware line 100 | verified | |
| 20 | Live calls "run on the chat path, so the same model and rules apply" | git log #3995 (9 Oct), #4061 (12 Oct) | verified | |

- **Contradicts:** Change "No fine-tuning" vs How step 3 (LoRA + RFT) in the same item. The research ladder says "Rung 3: a tuned style model or LoRA (not yet)" and puts the rules "in the bundled `lightbulb` skill (fleet-wide), with one role line in `build_soul_md`". The item puts them "in every agent's generated instructions". 18 lists item 8 as "a research track" except T35. The item reads as committed work.
- **Missing:** owner and time box; the one decision the founders must make (the Bedrock re-entry ruling) stated as a question; the eval pass bar (Muse's "launch-blocking" threshold, e.g. 80% founder-accept); cost of the eval rounds in founder hours.
- **Cut (method detail → research appendix, ~430 words):** "How (founder rulings 13 Oct…" through "…regardless of which model writes." (the four-step model-work ladder, ~330 words); "No fine-tuning: the research…" through "…when it runs long." (Muse/Instinct provenance, ~75 words; keep one clause); "Fireworks/Together plus OpenRouter's…" (~25 words). In the item, keep: the rules, the blind eval, the Stop ledger (T35), one line "model choice, then a tuned speaker only if the eval plateaus; Bedrock re-entry needs a ruling", and the measures.
- **Must keep:** per-role reply rules in every agent's instructions; a blind founder-rated eval per role; record who routed each run and who pressed Stop (the label source); model choice and tuning only if rules plateau; the measures (accept rate, stop rate after Jev runs, length, promises with no task).

## 9. A teammate you text (176)
- **Purpose:** make the phone app the second person's home.
- **Does it do the job:** partly. Change is specific. The measure is four milestones with no baseline or target, and "still there in week three" is not one of the plan's metrics (week-4 is).
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 1 | "the phone app mirrors the web workspace" | CLAUDE.md Mobile; 18 T25 | verified | |
| 2 | Instinct "has no app at all: a contact you text, that texts you first" | research employee-voice line 16 | partly | "There is no app. It has a phone…"; it "rarely phones" (3:25). "Texts you first" is not in the research |
| 3 | "Instinct's trust ladder" | research (trust proxies: first card, first password, "two or three weeks in") | partly | The ladder in the item is adapted, not Instinct's |
| 4 | "the second person is invited by text" | none | unverified | Same premise as item 6 |

- **Contradicts:** Measure "week three" vs Appendix C "Week-4 active". 18 T25 says "unverified whether approvals push"; item 3 implies push works.
- **Missing:** a baseline for phone use by invited members; what "agents message first" costs in runs; how it interacts with item 3's quiet hours.
- **Cut:** "Borrowed from Instinct, which has no app at all…" (~15 words), as provenance. "Not in this list, on purpose…" (~20 words) belongs in the intro.
- **Must keep:** the CEO's DM is home; push reads as a named teammate with one action; outputs in Library.

## Appendix A. How this draft was built (158)
- **Purpose:** tell a reader what the numbers and claims rest on.
- **Does it do the job:** partly. Two facts are wrong and the research list is incomplete.
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 1 | "the 50 PRs merged since 1 Sep" | `git log origin/main --since=2026-09-01 --until=2026-10-12` | wrong | 1,359 squash-merged PR commits |
| 2 | "as of 11 Oct 2026" | Sections were rewritten 12–13 Oct from origin/main | stale | Say 13 Oct, 6249f614d |
| 3 | "anything marked live was checked against the deploy model…, not against prod" | CLAUDE.md | verified as method | But Appendix D says "Live" without that caveat |
| 4 | "research files under business-plan/research in the project files" | repo | unverified | No such path in the repo; say where it lives |
| 5 | docs/research list (three files) | `ls docs/research` | stale | Omits muse-walkthrough and grok-bot-teardown, both cited elsewhere |
| 6 | DB / Cost Explorer reads 11–12 Oct; Stripe, Clerk, PostHog 12–13 Oct | none | first-party, 12–13 Oct, not re-pulled | |

- **Contradicts:** Appendix A "as of 11 Oct" vs Appendix E "written on 13 October" and 18 "checked against origin/main at 6249f614d".
- **Missing:** the commit hash; who ran the reads; that docs/marketing (Appendix E) is also a source.
- **Cut:** "This is a first pass…" (~6 words) once the draft is final.
- **Must keep:** sources and dates; "shipped" means merged, not live in prod.

## Appendix D. Product inventory (309)
- **Purpose:** one table of what exists and its state.
- **Does it do the job:** partly. Rows are accurate on main. "Live" is not checked against prod (Appendix A). The core surface named in §3, shared coding sessions, has no row.
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 1 | Messaging: listen modes, one responder per message | CLAUDE.md Messaging (Jev) | verified | |
| 2 | Six-agent roster with memory, skills, email, tools | CLAUDE.md; `onboardingSteps.ts` | verified | |
| 3 | Runtime "parks after 30 min idle" | `agentcore_runtime.py:117` `IDLE_TIMEOUT_S = 30 * 60`; #3987, 9 Oct | verified | CLAUDE.md still says 4 h; it is stale there |
| 4 | Task board "MCP access added 11 Oct" | #4029, 11 Oct | verified | |
| 5 | Memory "Merged 10-11 Oct" | #3988 (10 Oct), #4021 (11 Oct) | verified | |
| 6 | Voice "Merged 9-11 Oct" | #3995 (9 Oct) … #4061 (12 Oct), #4064 (13 Oct) | stale | Work ran to 13 Oct; in-app Live moved to the Meet pipeline and GPT-Live was deleted on 12 Oct |
| 7 | Shared cloud browser, people click in | CLAUDE.md (Browser v2) | verified | |
| 8 | Mac app "Shipped, gated per owner" | CLAUDE.md (`DESKTOP_EXECUTION_OWNER_ALLOWLIST`) | verified | |
| 9 | iPhone on TestFlight; IAP off in prod | CLAUDE.md | verified | |
| 10 | Marketplace 15% fee | `config.py:445` `MARKETPLACE_PLATFORM_FEE_PERCENT = 15` | verified | |
| 11 | Integrations "Slack/Discord/Telegram channels … Live" | `routers/slack_app.py` exists; no Discord/Telegram router; 00-decisions E4 "Forge verifies" | unverified | `slack_app.py` still documents a "container's Bolt receiver"; Discord/Telegram working on Hermes is unproven |
| 12 | GitHub, Google, Stripe read-only key, agent email | CLAUDE.md | verified | |

- **Contradicts:** E4 (founder) "Telegram, Discord and WhatsApp come off the homepage now… stay in product and docs if Forge verifies them" vs "Live" here. Appendix A "not against prod" vs "Live".
- **Missing:** rows for shared coding sessions + the Claude Code/Codex plugin (core product per §3 and item 4), Library, Jev, Meet/voice calls via agents@, the session-link public page. A "prod?" column.
- **Cut:** none; add rows instead.
- **Must keep:** the table, with an honest state column.

## Appendix E. Execution (273)
- **Purpose:** point to the execution layer and record what it changed in the plan.
- **Does it do the job:** yes, with one stale omission.
- **Claims:**

| # | Claim | Source | Status | Note |
|---|---|---|---|---|
| 1 | files 01–17 written 13 Oct, from plan, origin/main, site, research | docs/marketing/README.md | verified | |
| 2 | "72 founder decisions … the founders' answer of 13 October" | 00-decisions.md (A14+B11+C15+D14+E14+F4 = 72; "All 72 numbered items answered") | verified | |
| 3 | overlapping tasks "are built once" | README; 18-build-order.md | verified | |
| 4 | file list ends at (17) | `ls docs/marketing` | stale | 18-build-order.md, the Forge board source, is not named |
| 5 | the guarantee was dropped | 00-decisions A1 | verified | |
| 6 | §5 quotes now attributed to builders of competing tools | §5 text | verified | |
| 7 | "build the real thing before the site depends on it" | 00-decisions standing rule (A8) | verified | |

- **Contradicts:** none.
- **Missing:** 18-build-order.md and its order rule ("measurement first, then billing, then items 1–9"), which the Engineering/Product intro contradicts.
- **Cut:** "Two facts the execution pass corrected in this plan…" (~55 words). It is changelog, already in Appendix B and 00-decisions.
- **Must keep:** where the execution files are, that founders answered all 72 decisions, the fix-forward rule.

---

## Summary (10 lines)
1. Counts (95 rows): verified 64, first-party 7, stale 4, unverified 10, wrong 6, partly wrong or overstated 4.
2. Wrong: intro, the invite card "comes last" (it sits after the connectors, before name-agents and context).
3. Wrong: item 1, "every retained customer is a workspace where a second person showed up" (§9's most active customer is a one-person business).
4. Wrong: item 5 / PMM v8, "flickmeon.ai redirects" (the root serves the Launch Competition today; only /pricing-type paths 308).
5. Wrong: item 7, week-4 "emitted by the existing sweeper" (none exists; `ops/growth.py` only computes it in a report).
6. Wrong: item 8, "the one style guide we ship" (chat-vs-document.md and voice.md also ship). Partly wrong: comment style "requires headings" (bullets yes, heading only in the example). Overstated: "neither trained on chat for tone".
7. Wrong: Appendix A, "50 PRs merged since 1 Sep" (1,359). Stale: Appendix A "as of 11 Oct" and its three-file research list; Appendix D voice "Merged 9-11 Oct" (ran to 13 Oct); Appendix E omits 18-build-order.md.
8. Unverified, worth fixing: Appendix D "Slack/Discord/Telegram … Live"; item 8's GPT-5.6, "3–10k turns" and "$1–2k per run" (the $80/h Bedrock RFT rate holds only for gpt-oss-20b and Qwen3 32B); "invited by text" (items 6, 9).
9. Largest cut: item 8's "How" ladder plus provenance (~430 of 785 words) moves to a research appendix. Also, "No fine-tuning" contradicts its own step 3.
10. Most missing: baselines and targets on the measures (items 1–5, 9), plus the build order itself. The intro says "in the order the teams need them", but 18 ships measurement and billing first, and item 7 has no Today line.

