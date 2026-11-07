# The Lightbulb Company — Business Plan (draft)

Oct 14, 2026 · Prasiddha Parthsarthy
Working copy, rewritten on 12–13 October. Source of truth while sections are being rewritten; the Google Doc is updated from this file when the pass is done.

## 1. Summary

Lightbulb is a shared workspace where teams delegate work to AI agents, see what they are doing, step in, and continue the work together. The agents write code, research, send email, browse the web, take voice calls and run tasks on a shared board, and every workspace starts with six of them, from a CEO to Customer Operations, running in its own cloud so they keep working when nobody's laptop is open. It works alongside Claude Code, GitHub and the team's messaging tools.

It is built for founder-led technical teams of 3 to 15 who already use agents for real work. Agents now do real company work, so the hard part has moved from prompting to coordinating people and agents.

| Plan | Price |
|---|---|
| Team (up to 5 members) | $50 per month, flat |
| Each member from the 6th | +$20 per month |
| Enterprise | Custom pricing for special cases |

Managed AI usage is billed on top as metered credits, and billing for this plan is being built before the site quotes it. Today Lightbulb has 57 accounts across 46 workspaces, about five outside workspaces active in a given week, and no customer yet paying list price; the first founding customers are being set up now. We are raising $2 million on a SAFE at a $14 million post-money cap to fund 24 months and the three stages set out in Go-to-market.

## 2. Problem

AI agents can now take real action, but companies can't use that power together. Consumer agents such as Muse and Instinct show an agent can do the whole task for you: each gets its own cloud computer and browser, uses stored logins, and keeps working after you leave. Inside a company, that kind of work still sits in one person's session.

**The team problem.** Handing agent work to a teammate or another agent means copying prompts, writing summaries and explaining status. Ownership blurs, and the next person rebuilds context instead of continuing.

**The enterprise problem.** Companies have bought AI, and it works for individuals: 80% of respondents say it made them more productive ([McKinsey](https://www.mckinsey.com/capabilities/quantumblack/our-insights/the-state-of-ai)). It has not worked for the company: the share seeing any profit impact has been flat at 37% for a year, half of deployed agents run in isolated silos ([Salesforce](https://www.salesforce.com/news/stories/connectivity-report-announcement-2026/)), and only 16% of firms surveyed run agents across multiple teams ([Anthropic and Material, December 2025](https://claude.com/blog/how-enterprises-are-building-ai-agents-in-2026)). Enterprise AI tools from Anthropic, OpenAI and Microsoft come with admin controls and audit logs, but each gives its own vendor's agents inside its own product. Group use exists (ChatGPT has had group chats since November 2025), but none runs agents from any vendor as members of a company's own workspace. No company has one place where its people and its agents, from any vendor, do the work together, where everyone can see what an agent did and who is responsible for it.

**Who feels it.** First, founder-led technical teams of 3 to 15 who already use Claude Code or similar tools for real work. Later, larger companies that want agents taking action but need control and visibility first. The product is suited towards small efficient vertical teams.

**Why today's tools don't fix it.** Teams stitch the work together from an IDE, Slack and copy-paste, and personal agents are built for one user.

## 3. Solution and product

Lightbulb gives the team one shared place to delegate work to agents, see what they're doing, step in, and continue the work without starting over.

A person delegates by mentioning an agent in a channel or DM, and the agent takes the work using connected company context and the tools it is authorized to use. The work stays visible to the whole team while it happens, so anyone can inspect it, redirect it or approve the next step. When the work moves on, the next person or agent picks it up with the full history, including shared Claude Code sessions, instead of rebuilding context from scratch.

For larger companies, the same workspace lets agents act on real systems under approvals, with the work on a shared record; admin-set permissions and single sign-on come in stage 3.

The core product is the shared workspace where people and agents talk, shared coding sessions, and a task board run by the CEO agent. Voice, memory, a shared browser, Mac and iPhone apps and an agent marketplace support it (full list in Appendix D).

Lightbulb works alongside Claude Code, GitHub, IDEs and messaging apps rather than replacing them.

## 4. Why now

AI agents have just crossed from answering questions to taking action. Consumer agents such as Muse and Instinct now run whole tasks on their own computer, signed in to the user's accounts, and keep working after the user walks away. The capability that used to need a developer is becoming something anyone can direct.

Companies want that capability, and the incumbents are moving toward it (Slack, Atlassian AMP, Zed Delta, Grok Bot's team admin controls), but each does it inside its own product or for code only. A Claude Code session or a consumer agent still lives in one individual's session; session tools such as Delta, Lody and Egregore now let a teammate pick up coding work, and ChatGPT group chats share the view, but only for code or inside one vendor's product. As more people inside a company start directing agents, including non-engineers, the hard part stops being what an agent can do and becomes how a team sees, controls and continues the work together. The gap still open is narrower (agents as members with their own runtime, from any vendor, beyond code), and it is the one Lightbulb fills.

## 5. Customer and market

**Who we sell to.** Founder-led teams of 3 to 15 that already run two or more AI agents for real work, with Claude Code, Codex or similar, where one person routes context between people and agents by hand. The founder decides, pays and sets it up; the second person they invite is the activation event.

**What they do today.** They glue it together themselves: a shared markdown repo every teammate clones and runs their agent from, Linear or Confluence as the bus between sessions, n8n for the deterministic parts, and copy-paste for the rest. The people who describe the pain best are founders who went on to build tools for it, which is itself the signal: "your agent remembers what you did. It has no idea what your co-founder just spent four hours figuring out. We kept solving the same problems twice, or discovering decisions the other had made only when something broke" (the founders of Egregore, a session-sharing tool, on Hacker News, February 2026). "The hard part isn't 'can the agent write code?' It's deciding what context should carry across sessions, who owns a change, and which actions should stop for human approval" (the two developers behind Pairon, on Reddit, September 2026). Buyers say it more plainly: two cofounders on separate accounts, "none of it syncs" (r/smallbusiness, 6 October 2026); a team "re-explaining the stack in their prompts every session" (r/ChatGPTCoding, 6 October 2026); a five-person team wanting its coding agent to "pick up tickets overnight" (r/ChatGPTCoding, 9 October 2026). The lead list in docs/marketing/02-prospecting.md carries the links.

**What qualifies a founding team.** All of: 3 to 15 people; two or more agents in recurring use across two functions; several people need the same agent context or output; one person routes context by hand; a missed handoff in the last 30 days; no procurement gate; willing to invite a second person and connect one real workflow in the first session. Not a fit: a solo operator whose tasks one ChatGPT tab handles; a pair whose work doesn't overlap; a team shopping for a cheaper Slack; a deterministic-automation buyer.

**What we know and don't.** One customer inside the wedge so far (Second Catch: a plan produced in week one, "the first thing that broke was adding the cofounder"). Of 50 outside sign-ups, 28 came from free-mail addresses and 33 of 38 outside workspaces have one person: the door admits individuals, and the product is built for teams. Whether a team in the wedge keeps using Lightbulb in week four is the question the founding-customer stage exists to answer; the small businesses that have pulled so far are covered in Risks.

The shelf we sit on is the one Slack and Microsoft Teams own, team collaboration, and the shelf we are building is what that becomes when agents do the work: the workspace for a company that runs on agents. The market is large and growing fast. Team collaboration software was worth $21.5 billion in 2025 and heads to $55 billion by 2034 ([IMARC](https://www.imarcgroup.com/team-collaboration-software-market)), and enterprise spending on generative AI reached $37 billion in 2025, three times the year before ([Menlo Ventures](https://menlovc.com/perspective/2025-the-state-of-generative-ai-in-the-enterprise/)). At our pricing, every professional developer team in the world is about a $6 billion a year market by our estimate (36.5 million professional developers in teams of eight at $110 a month, [SlashData](https://slashdata.co/post/global-developer-population-trends-2025-how-many-developers-are-there)). We start with the small teams that already use AI every day (51% of professional developers do, [Stack Overflow](https://survey.stackoverflow.co/2025/ai)), and the referral fees on the services their agents buy open the business-services budget, which dwarfs software.

## 6. Competition and positioning

**What a team would do without us.** Keep gluing agents together with a markdown repo, Linear and copy-paste; add a bot to Slack or Teams; give each person their own agent (Grok Bot); share coding sessions with a session tool (Zed Delta, Lody, Egregore); or do nothing. All five are cheaper or already installed, so the pitch has to beat "good enough".

| Who | Built for | Where the agent lives | Who sees its work | Price | What it can't do |
|---|---|---|---|---|---|
| Slack + Slackbot / Agentforce | Every company; small teams on Free and Pro | "Slackbot, your personal AI agent" plus third-party agent apps embedded in chat | The person who asked; a reply can be shared to a thread | Business+ $15/user/month annual (full Slackbot access only on Business+ and up); Agentforce "available soon" per Slack's page | Give an agent its own runtime, tools and email; approvals and offline work are limited to Slackbot's human approval for actions and scheduled automations |
| Microsoft Teams + Copilot | Microsoft 365 customers | Copilot in Teams; agents built in Copilot Studio, metered through Azure; Copilot Business lists "LLM model choice" | The Microsoft 365 tenant | Copilot Business $21/user/month (promo $18 through Dec 2026) on top of a Microsoft 365 seat; Business Standard with Copilot $23.50 per seat | Teams that don't live in Microsoft 365; agents as members of a small team's rooms |
| Grok Bot (xAI on Cursor) | Individuals; Cursor Teams and Enterprise | One cloud computer per person; all that person's bots share it; "Cursor manages model selection" | That person ("one user cannot reach another user's computer"); a team can publish one Team Bot "they all talk to" | Included in paid Cursor plans; team admins get rules, secrets, review enforcement | Let a teammate see or continue a personal bot's work; bring your own model or key |
| Atlassian AMP (7 Oct 2026) | Jira and Confluence companies | An agent with an owner, a profile and a scope, @mentioned in Jira, Confluence and Loom | The Atlassian workspace; "Run as User" or a service account, full audit trail | Not announced; "10 million" human-agent interactions a month claimed | Anything outside Atlassian; founder teams that don't run on Jira |
| Session tools: Zed Delta (free public beta since 16 Sep 2026), Lody, Egregore | Engineers using Claude Code or Codex | A shared coding session: private until shared, a teammate picks up the task, inline comments, `/handoff` | Invited teammates | Free beta (Delta); $8 a seat a month billed yearly (Lody); Egregore is an open install | Non-code work (email, books, board), approvals |
| Consumer agents: Muse, Instinct | Individuals | A cloud computer and browser per person, signed into their accounts; Instinct pays for things | The person | Free (Muse) | A shared record, a team (Muse asks for approval and Instinct checks every action, but for one person) |

**Where we stand.** The category, said the same way everywhere: the workspace for a company that runs on agents, competing with Slack and Teams for the place a team does its work. Slack's homepage now reads "All your people and AI agents working together", Atlassian calls it "multiplayer", and Grok Bot's bots hand work to each other. "People and agents together" is the category's language now, not ours. What none of them offers in one product (Copilot offers model choice, and Bloome puts people and agents from any model in one group chat): an agent that is a member of the team, with its own runtime, memory, email, tools and approval rules, from any model vendor, in the rooms the team already uses, with one record of who did what and who approved it, for the whole company's work and not only code. Coding agents such as Claude Code and Codex stay partners: Lightbulb works alongside them and makes their sessions shareable.

**Positioning.** For founder-led teams of 3 to 15 already running two or more AI agents, who are the human router between their people, agents and tools, Lightbulb is the team workspace where AI employees work alongside the people: in the same rooms, with their own tools, memory and approval rules. Unlike Slack and Teams (an assistant added to people-chat, sold per seat), Grok Bot (a computer per person, plus an optional shared Team Bot), the session tools (code only) or the glue teams build themselves, every agent from any vendor runs in the team's own isolated workspace with one shared record, so work assigned in chat gets finished and the first five people are free.

**Three claims, each with its proof.**
1. *Agents are members, not tools.* A name, a job, memory, an email address, tools and approval rules, in the rooms you choose. Proof: the six-agent roster, team email, approval cards.
2. *The whole team sees the work and can continue it.* Every run, session and decision on one record; a teammate picks up where anyone left off. Proof: shared sessions, Inbox, Library; Second Catch's plan waiting on an approval.
3. *Any model, one price.* Lightbulb-managed, your ChatGPT account or your own key; one workspace price, the first five people free. Proof: the three provider paths; $50 flat.

**Contrast lines.** vs Slack and Teams: "Slack added agents to chat. Lightbulb built the company around them." vs Grok Bot: "Grok Bot gives each person a team of bots. Lightbulb gives the team one set of AI employees everyone can see." vs session tools: "They share the coding session. We share the company." vs DIY glue: "Stop wiring it together. Bring the agent, get the team." Business model: "People are free up to five; you pay for the workspace and the work your agents do."

**Price.** An 8-person team pays $110 a month on Lightbulb, $13.75 a seat. Slack Business+ is $120 ($15 a seat, agents "available soon"); Microsoft 365 Business Standard with Copilot is $188 ($23.50 a seat); Cursor Teams with Grok Bot $240 and up; Nimbalyst $160; Lody $64 (prices checked 13 Oct 2026, docs/marketing/11-competitors.md).

**What we take from them.** Delta: threads private until shared, comments anchored to the conversation. Egregore: a handoff that saves the reasoning and open questions, not just the state. Grok Bot: routines nobody reads pause themselves; an admin ceiling on what runs on a person's machine. AMP: an agent identity that can "run as user" or as a service account. Slack: one entry point that routes to the right agent, which is what our CEO agent and Jev already do.

## 7. Business model

**What we charge for.** The value a team gets scales with the work its agents do and the people who can see it. So we charge a flat workspace price, meter the agents' work, and keep the first five people free. That is the line Slack and Teams cannot follow without cannibalising seat revenue: "People are free up to five; you pay for the workspace and the work your agents do."

A team pays $50 a month flat for up to 5 members, and each member from the 6th on adds $20 a month. That is the one plan: the Base and Ultra tiers are retired. Larger companies with special needs get custom enterprise pricing. Every new team starts with a 30-day free trial; a self-serve team adds a card when the month ends, a founding team has one on file from its setup session.

| Plan | Price |
|---|---|
| Team (up to 5 members) | $50 per month, flat |
| Each member from the 6th | +$20 per month |
| Enterprise | Custom pricing |

Two smaller streams sit on top. Teams that use our managed AI pay for it as credits at the model provider's price times 1.2, which leaves us about 10% after OpenRouter's and Stripe's fees; or they bring their own ChatGPT account or API key, and inference costs us nothing. The agent marketplace takes 15% of each paid agent sale.

**The founding-teams offer** (ten teams set up by 4 January 2027). A founder sets the team up in person: wires one real workflow and gets the second person invited in the first session. The team gets a shared channel with us inside its own workspace, fixes within days, the price locked for two years, and a say in what we build next. The card goes on file in the setup session; the first 30 days are free and the first charge, $50, is on day 31. There is no refund promise: the free month is the risk reversal. Ten teams is all three founders can set up by hand; after that it is self-serve.

We do not discount to acquire. A pricing rule of thumb we use, not a study, holds that discount-takers churn at about twice the rate of full-price customers and that a coupon anchors the product as cheap; we have no outside discount-takers to test it on, since the six workspaces on a 100% internal coupon were given it by us, not offered it to sign up. The free month moves the risk the way a discount would without anchoring the price, so the number the round is judged on stays clean: teams paying list. From now on, the internal coupon is for our own workspaces only.

**What the product bills today, and what has to change.** The product still bills Base $20 / Workspace $50 / Ultra $150 with unlimited members. To bill this plan it needs seat counting from the 6th member (workspace membership to Stripe quantity, prorated when someone joins or leaves), a checkout with the 30-day trial so a founding team's card goes on file at setup, Base and Ultra retired (nobody outside pays for either; our own workspaces move to the one plan on the internal coupon), and a pricing page that says so. Billing ships first and the pages that quote it after: no interim lines on the site and no manual billing steps (Part 2, section 4).

The long-term/experimental model is referral fees from the services agents buy. Companies are moving from buying software their staff operate to buying finished work from AI services: legal work from Harvey, bookkeeping, recruiting, support. On Lightbulb, the CEO agent will choose and pay for those services on the company's behalf, and the service provider will pay Lightbulb for bringing it the customer, the same way Instinct earns from the merchants its agent buys from. Consumer agents collect 1 to 4% of what flows through them, and business services are priced far higher per customer. Lightbulb will show every such fee to the customer, and agents will choose on fit and price, never on what pays us. The subscription brings teams onto the platform; the services their agents buy are where the business can grow largest.

## 8. Go-to-market

Lightbulb goes to market in three stages, each unlocked by what the one before proves.

First, founding customers: ten founder-led teams of 3 to 15 people, each set up in person by one of us on the §7 offer (30 days free with a card on file, then $50 a month, the price locked for two years), who tell us where the product holds and where it breaks: ten set up by 4 January 2027, about three past their free month and paying by 12 January, at least six paying by May. The entry point will be "Request a guided setup" on thelightbulb.company, and the first session ends with a second person in the workspace and one real handoff completed. The founders own the channels where these teams already are: Leo on X and Reddit, Aden on LinkedIn, Prasiddha on Hacker News and Product Hunt. Every public post, email and listing is signed off by a named founder; agents research, draft and prepare, and never publish on their own. The detail, owners and weekly rhythm are in Part 2, the marketing plan (sections 6 and 7).

Second, a repeatable motion for small teams. Once a quarter of new teams reach a working first session and keep using Lightbulb in week four, we open sign-up more widely and let the product do the selling: a team that invites a second person, gives its agents company email addresses or shares a coding session pulls the rest of its company in. The first hire is a growth engineer who works on those loops. Paid acquisition starts only after that, and only where a channel already brings us teams for free.

Third, companies. As teams pull in the rest of their company, we add what larger buyers need: single sign-on, admin permissions and approvals, and a plan priced for them. That is when we sign the first service vendors whose agents our customers' agents buy from, and make the first sales hire. The milestones for each stage are in Risks and what we're testing.

## 9. Traction

Lightbulb is early, and this section says so plainly. Figures below are from Clerk, Stripe, PostHog and the production message store on 12–13 October 2026.

57 accounts (50 outside the team), 46 workspaces (38 outside), 33 of them with one person. About five outside workspaces are active in a given week. 2,740 messages sent by people since September, 82% of them from our own workspace. Revenue: $55 collected ever; six active subscriptions, all on a 100% internal coupon, so no customer has yet paid list price. The 30-day trial funnel: 227 product-page visitors → 18 clicked sign up → 20 trials → 10 got a first agent reply → about 5 workspaces still active. Traffic peaked at 50 visitors a day around 30 September and is about 10 a day now; three quarters arrives direct, Instagram about 30 people in 60 days. Five of the six subscribed workspaces run on their own ChatGPT account, so inference costs us close to nothing.

| Metric (outside workspaces only) | 12 Oct 2026 |
|---|---|
| Accounts | 50 outside (28 free-mail, 14 company domain, 8 .edu); 24 of 50 never returned after day one |
| Workspaces | 38 outside; 33 with one person |
| Paying at list price | 0 (six subscriptions on an internal coupon) |
| Active in a given week | about 5 |
| Inside the wedge | 1 (Second Catch) |

**What we measure from here** (definitions in Appendix C), read every Monday from a one-page readout an agent writes from PostHog, Clerk and Stripe: setup requests by source; teams formed (two or more people within 48 hours); first handoff in week one; week-4 active; paying at list. In front of them, the traffic funnel: landing → setup request or start free, by source. To make those readable, every product event will carry the workspace id and an environment, Vercel Web Analytics is to be switched on, every link we post will carry a UTM, and the setup form and sign-up will ask "How did you first hear about us?" (a short list plus free text), because three quarters of today's traffic is unattributed.

| Week | Active external workspaces | With 2+ humans |
|---|---|---|
| Oct 5-11 | 5 | 4 |
| Sep 28-Oct 4 | 4 | 3 |
| Sep 21-27 | 6 | 3 |
| Sep 14-20 | 4 | 3 |

| AWS cost, whole account (dev + prod) | Aug | Sep | Oct 1-11 |
|---|---|---|---|
| Total | $1,627 | $1,670 | $633 |
| Largest lines in Oct | ECS $145, AgentCore $83, Secrets Manager $80, Aurora $78 | | |

The customers who stuck are not yet the technical teams we are going after. The most active is a one-person food business whose CEO agent runs its books in QuickBooks: profit and loss, invoice drafts, marking checks paid, and since 11 October sending invoices. What those users show is that an agent doing back-office work across a company's tools, and still working when nobody is logged in, is worth paying for. Second Catch, a two-founder team that joined on 6 October, is the first customer inside the target: in its first week the CEO agent produced a seller-acquisition plan that now waits on the founders' approval of about $5,000 of spend, and the first thing that broke was adding the cofounder. Two of the subscribed workspaces went idle after two weeks; one owner told us he and his brother have no shared projects, which is the point: the product earns its place only where people share work.

The hard signal is cost against revenue. AWS runs at about $1,700 a month for the whole account against no revenue at list price, so infrastructure, not inference, is today's main cost, and the per-workspace figure has to fall as the fleet grows. One inbound lead, the CEO of Habits365, has asked for a demo.

## 10. Team

Lightbulb is built by three founders and the agents they run. Prasiddha Parthsarthy [role; background an investor should know]. Aden [role; background]. Leo [role; background]. Between them they cover product and engineering, go-to-market, and the hands-on customer work the founding customers need. Most of the engineering is done by AI agents and shared coding sessions directed by the founders, which is why a team of three ships a messaging workspace, six agents, a task board, a Mac app and an iPhone app: the company is its own first customer. The legal entity is Isol8 Inc., doing business as Lightbulb AI. The first hire is a growth engineer, as set out in Go-to-market, and the first sales hire follows once companies arrive. [Advisors, if any.]

## 11. Financials and the ask

Lightbulb has [cash in bank] in the bank and spends about [monthly burn] a month, [months] of runway at today's pace. The unit economics are good: a live workspace costs us $5 to $15 a month to run (AgentCore billed $83 for 15 runtimes over the first 11 days of October, before the 30-minute idle park merged on 9 October) and almost nothing per extra person, against $50 a month and $20 per seat past five, so gross margin is about 66 to 87% after Stripe's fee while customers bring their own AI account, and every seat on a bigger team is nearly pure margin. On AWS cost alone (about $1,700 a month) the company breaks even at about 40 to 54 paying teams. The one cost that could run away is managed AI for a heavy team, so managed usage is sold as metered credits and never bundled into the plan.

| | Now (Oct 2026) | How it moves |
|---|---|---|
| Runtime cost per workspace | $5–15 a month | falls with the 30-minute idle park (merged 9 Oct; prod not yet confirmed) |
| Cost per human seat | about $0 | |
| Gross margin per paying workspace | about 66–87% | |
| Fixed cost (AWS, dev and prod) | about $1.7k a month (October's pace $1,784) | |
| Breakeven on AWS | about 40–54 paying teams | |

We are raising $2 million on a SAFE at a $14 million post-money cap, in line with 2026 pre-seed rounds for AI companies, to fund 24 months: salaries for three founders, a growth engineer from 2027, infrastructure and vendors, and paid acquisition once a channel has proven itself for free. We are not writing a five-year forecast. The bar we hold ourselves to is the one the best early-stage companies are measured against: once sign-up opens, paying teams grow 7% week over week, which doubles the business every ten weeks and would take ten founding customers to several hundred paying teams within a year. At $50 a team that is still a small revenue line, and we say so: the subscription is how teams come in, and the round is judged on that growth rate, on week-four retention, and on the first companies and service vendors paying, which is what the seed round after this one will be priced on.

## 12. Risks and what we're testing

The first risk is that the small-team wedge is wrong. The customers who have stuck so far came from outside it, and nobody has yet shown a 3-to-15-person technical team working through Lightbulb week after week. That is what the founding-customer stage tests: ten teams set up by 4 January 2027, six paying by May, and a quarter of new teams still active in week four before we open sign-up more widely. If those numbers do not come, the fallback is in our own data: small businesses running back-office work through an agent, which is where the pull has been. One caveat we hold ourselves to: a week-4 miss on ten teams is a signal, not a verdict; a product this new and this small shows low retention for size reasons alone, which is why reach keeps running in parallel and the ten are a qualified sample rather than whoever answered.

The second is the incumbents. Slack, Microsoft, Anthropic and OpenAI can each add agents to what they already sell, and they will. Our answer is that none of them will build a neutral place where people and agents from every vendor work together under one set of controls, because each wants the agent to be theirs. The test is whether teams keep using Lightbulb alongside Claude Code and their messaging tool, or drop it when those tools add a feature.

The third is timing on agents that spend. Companies will let agents buy on their behalf, and the rails are already here: Stripe's agent wallet and Issuing for agents give an agent a one-time card for a purchase a person has approved, with spending limits already available on agent cards ([Stripe](https://docs.stripe.com/issuing/agents)) and purchases without per-spend approval to follow. That is how we will build it, and it matches the rule we already run under, that no charge goes out without a person's sign-off. What we cannot control is how fast finance teams get comfortable, and the referral model in Business model only earns once they do. We hold it to its stage: the first service vendor signed once companies are buying through Lightbulb, not before.

Three risks sit in the product. Agents that send email, browse the web and run code can be tricked by what they read, so every outside action runs in the team's own isolated runtime, high-stakes actions wait for a person, every public post or charge needs human sign-off, and the owner decides who agents can email. Managed AI for a heavy team could cost more than its plan, so it is sold as metered credits, never bundled. And we run on AWS AgentCore, OpenRouter and two open-source projects (Hermes, the upstream image pinned by digest, and Paperclip, which we fork), so images are pinned and promoted to production by hand.

Finally, the company is three founders and the agents they direct. That is how a team this size ships this much, and it is also the thinnest point: the plan's first hire is a growth engineer, and the runbooks and agent memory are written so that the system does not live in one head.

## 13. Vision

In five years most of the work a company buys will be done by AI services rather than by people using software: legal work, bookkeeping, recruiting, support, research, engineering. Every company will have a staff of agents alongside its people, and the question will not be whether to use them but where they work. Today they work inside a dozen vendors' products, each one a silo. Lightbulb is the place a company's people and all of its agents work together, whoever makes the agents and whoever supplies the service, with one record of who did what and who approved it. That is the role Slack and Microsoft Teams play for people today, and it is the customer base we intend to take, because the tool a company runs its work in is the tool it keeps.

From that seat the business grows past the subscription. When a company's agents choose and pay for the services the company needs, Lightbulb is where that choice is made and where the money moves, and the providers that win that business pay us for bringing it to them. A company on Lightbulb gets more done with fewer tools and fewer vendors to manage, and the services that do its work compete for it on fit and price in the open, with every fee shown. If we get there, Lightbulb is not a messaging product with agents in it but the operating layer of a company that runs on them, taking a small share of a market that is already measured in trillions.

## Open questions for Prasiddha

Two of the things that remain to fill in (use of funds and the $14 million cap's source are also open): the founders' backgrounds and any advisors in Team; cash in bank, monthly burn and runway in Financials and the ask. The Stripe, Clerk and PostHog figures were added to Traction on 12–13 October.

## Appendix A. How this draft was built

This is a first pass built from the shipped code on main of the lightbulb monorepo as of 11–13 Oct 2026 (`origin/main` at 6249f614d), including the 1,359 PRs merged from 1 Sep to 11 Oct and the repo's own growth docs. Prod lags main by design, so "shipped" here means merged; anything marked live was checked against the deploy model in CLAUDE.md, not against prod.

Traction and cost numbers come from read-only reads of the production database, AWS Cost Explorer and the usage counters on 11 and 12 October 2026, with Stripe, Clerk and PostHog figures added on 12–13 October. Market, go-to-market and round-size claims are sourced in the research files under business-plan/research in the project files. Sections 5–7, the product plan and go-to-market sections (now Part 2) were rewritten on 12–13 October from `origin/main`, production data and the research reports under docs/research/ (employee voice and consumer agents; socially aware agents; messaging datasets; the Muse walkthrough; the Grok Bot teardown), with the shared marketing context in `.agents/product-marketing.md`.

## Appendix B. Pricing history

| Date | Change |
|---|---|
| 13 Oct 2026 | Managed credits stay at provider price × 1.2 (the "plus 3%" wording of 11 Oct is withdrawn). Founding-teams offer set: 30 days free with a card on file at setup, then $50 with a two-year price lock; a week-4 refund proposed the same morning was dropped (the free month is the risk reversal); no 90-day free period; no coupons for outside teams. Base and Ultra retired: one plan. Billing is built before the pricing page. |
| 11 Oct 2026 | Plan pricing set for this plan: $50 a month flat for up to 5 members, $20 a month per member from the 6th, custom enterprise pricing; managed credits at cost plus 3%. The product still bills the seat-free $20 / $50 / $150 plans until billing is changed. |
| 22-23 Sep 2026 | Bedrock replaced by OpenRouter; managed credits at cost x 1.2 (was about 3%); trial 30 days at $0.25/day (was 3 days at $3/day); Base plan $20 added |
| 10 Sep 2026 | Pivot to multiplayer messaging; per-seat billing removed, every plan covers unlimited members |
| 19 Aug 2026 | Company marketplace and Ultra opened to buyers |
| 25 Jul 2026 | Team billing: $50 base plus $20 per seat |
| 19 Jul 2026 | Ultra tier ($150) added |
| 17 Jul 2026 | 14-day Stripe trial removed; checkout bills immediately |
| 10-12 Jul 2026 | No-card free trial launched |
| 27 Apr 2026 | Switched to a flat monthly fee |
| 6 Apr 2026 | Prod go-live with usage-based pricing tiers |

## Appendix C. Metric definitions

| Metric | Definition |
|---|---|
| Setup request | A submitted guided-setup form, with its self-reported and tracked source |
| Team formed | A new workspace with two or more people within 48 hours of creation |
| First handoff | A person decided on an agent's work (an approval card answered, approve or reject; a session picked up or continued; a task an agent did that a person moved on) within the workspace's first week |
| Weekly active workspace | At least one human room message that week; messages from Lightbulb staff and from the internal workspaces do not count |
| Week-4 active | Share of teams formed with a human message in their fourth week; messages from Lightbulb staff and from the internal workspaces do not count |
| Working first session | A new workspace with a first artifact and a first approval card answered by a member inside the first session (Part 2, section 5) |
| Paying at list | A workspace with a Stripe invoice paid at full price; coupon subscriptions do not count |
| Agent answered rate | Share of agent-addressed human messages answered within 2 minutes |
| Stop rate after Jev runs | Share of agent runs Jev started that a person stopped within 60 seconds |
| External workspace | Any workspace except lightbulb, demo-lab, 10-06-test, adenprivatetest, external-review, starter-roster-audit-sep-12 and the MKT385 intern workspace |

## Appendix D. Product inventory

| Surface | What it does | State |
|---|---|---|
| Messaging workspace (web) | Channels, DMs and group chats shared by people and agents; listen modes; one responder picked per message so agents don't pile on | Live, core product since the 10 Sep pivot |
| AI employees | Six-agent starter roster per workspace, each with its own memory, skills, email address and tools | Live |
| Isolated runtime | One cloud runtime per workspace (Hermes on AWS Bedrock AgentCore); parks after 30 min idle, wakes on the next message | Live |
| Task board (Paperclip) | Issues, approvals, routines and a nightly CEO run; agents and coding sessions can read and update tasks | Live; MCP access added 11 Oct |
| Memory | Private and team memory scopes; a Memory tab to see, delete and share what agents remember | Merged 10-11 Oct |
| Voice | Spoken turns with agents in rooms, sharing the chat path's tools and recall | Merged 9-11 Oct |
| Shared cloud browser | Agents and people use one live browser with saved sign-ins; people can click in while agents work | Live |
| Mac app (Tauri) | Agents can run shell, browser, files and screen on a paired Mac with consent given once at pairing; live coding sessions viewable by teammates | Shipped, gated per owner |
| iPhone app (Expo) | Full chat, teams and control surfaces; Apple subscriptions | On TestFlight; App Store IAP off in prod until Apple's paid-apps agreement is active |
| Marketplace | Sellers publish agents; buyers deploy a copy; paid listings via Stripe with a 15% platform fee | Built; liquidity not yet proven |
| Integrations | Slack channel, GitHub, Google, Stripe read-only key, agent email in and out (Discord and Telegram: unverified, off the homepage) | Live |

## Appendix E. Execution: Part 2

The operating plan is Part 2 of this document, the marketing plan. It has nine sections and three appendices: the summary, who we sell to and how we talk, where we are today, the product track (35 tasks, in order, with a date), the measurement track (the five Monday numbers and the readout), the outreach track (named leads, the copy, a week-by-week table per founder, the setup session, the launch, the directories), the calendar, what happens after 12 January, and the open decisions. It absorbs the product plan, the go-to-market stages and the website brief that earlier drafts of this document carried.

Behind Part 2 sit the files in the repository: `docs/marketing/README.md` (the brief and index), `00-decisions.md` (72 founder decisions, answered 13 October), `01` to `17` (one file per workstream), `18-build-order.md` (the 35 engineering tasks with their specs), `19-plan-vetting.md` (the claim-by-claim check of this document), `20-marketing-plan-long-2026-10-13.md` (the earlier 13-section version, kept for its idea bank), the research under `docs/research/`, and the shared context in `.agents/product-marketing.md`.
