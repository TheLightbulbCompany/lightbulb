# Lightbulb marketing plan (14 Oct 2026 to 12 Jan 2027)

## 1. Summary

Lightbulb's AI employees do real work: invoices, pull requests, email, research, in the team's real tools. They work across the whole team, in its channels, and anything that spends, sends or deploys waits for a person's yes. And they work with the agents the team already runs: a Claude Code or Codex session on one person's laptop becomes part of the team's work, shared, watched and picked up. Today most outside workspaces have one person in them, and nobody pays. This plan fixes both.

**The target**

- Ten teams set up with a card on file by 4 January 2027.
- About three paying $50 a month by 12 January.
- Six paying by May.

**What each of us does until 12 January**

- **Prasiddha:** ships all 35 product tasks by Thursday 15 October, billing and events first. Runs Show HN on Thursday 15 October and Product Hunt on Tuesday 20 October.
- **Aden:** hosts the setup sessions, answers setup requests within a day, runs the Thursday check-ins, writes the customer stories.
- **Leo:** owns the lead list, X and Reddit, and signs the comparison pages.
- **All three:** send first touches from day one, 50 a week between us from week 3.

**The one blocker.** No team can be set up until billing and the trial checkout ship. Due Thursday 15 October. Setup sessions start the week of 19 October.

**The number we watch.** Teams with two or more people and a completed handoff, reported every Monday. Staff do not count as either.

## 2. Who we sell to and how we talk

**Who qualifies.** A lead is a fit when all seven hold:

1. A founder-led team of 3 to 15 people.
2. Two or more AI agents in recurring use, across two functions.
3. Several people need the same agent context or output.
4. One person routes context between people and agents by hand.
5. A missed handoff in the last 30 days.
6. No procurement gate.
7. Willing to invite a second person and connect one real workflow in the first session.

Not a fit: solo operators, pairs whose work does not overlap, people shopping for a cheaper Slack, automation buyers.

**What they pay.** $50 a month for up to five members, then $20 a member. AI usage is billed at the provider's price times 1.2, or they use their own ChatGPT account or API key. Founding teams get the first 30 days free with a card on file. The first charge is on day 31. No discounts, no coupons.

**How we talk.** Plain, specific, calm. Short sentences and concrete nouns: invoice, pull request, channel. Every claim names a real act, such as an invoice sent or a PR opened. No hype and no exclamation marks. Say what is early.

- **Words we use:** AI employees, does the work, in your real tools, your whole team, the agents you already run, channel, mention, approve, continue, "who owns a change". "Handoff" is a number we count, not a word we sell with.
- **Words we never use in public:** multiplayer, AI-native, orchestration, bot, prompt. Internal names (Paperclip, Hermes, AgentCore, Jev) stay out of public copy. Hermes and Paperclip may appear only as "built on".
- **Email copy** says "you decide who your agents can email". It never promises that a person reads each email first.
- **A named founder signs** every public post, email and listing. Agents draft and never publish.

## 3. Where we are today

**The numbers** (read 12 and 13 October 2026)

| What | Figure | Source |
|---|---|---|
| Outside workspaces with one person in them | 33 of 38 | Clerk |
| Accounts that never came back after sign-up day | 24 of 50 | Clerk |
| Trials in the last 30 days that got a first agent reply | 10 of 20 | PostHog |
| Visitors a day | about 10 | PostHog |
| Product-page visitors who click sign up | about 8% | PostHog |
| Money collected, ever | $55 | Stripe |
| Teams paying at list | 0 (six active subscriptions, all on a free coupon) | Stripe |

**What exists**

- The product, live since April, reshaped in September, on the new domain since 2 October.
- Anna's Frozen Treats: her CEO agent sends QuickBooks invoices. Her quote is cleared for public use.
- Second Catch: the first wedge team, waiting on a ~$5,000 approval. May be named in public.
- Email invites and a public plugin repo, both shipped in the last week.
- Written and waiting on billing: the homepage, pricing page, six comparison pages, nine lifecycle emails, the setup run sheet, the objection list and the Show HN post.

**The audit in one line.** 26 of 85 on the current-state scorecard. Positioning and sales material score well on paper. Everything a customer touches scores 0 to 2. The gap is shipping, not ideas. The full scorecard is in Appendix A.

**Money.** No paid acquisition. The only cost is founder hours, which nobody has priced yet.

## 4. Product track

**The rule.** Build the real thing before any page, email or call depends on it. Nothing is done by hand in the meantime. No page quotes a price until billing works.

**Owner and date.** Prasiddha. All 35 tasks ship by Thursday 15 October, in the order below. The spec for each (what exists today, the change, the measure) is in `docs/marketing/18-build-order.md`.

| # | Task | What it does | Needs first |
|---|---|---|---|
| 1 | T01 | Events carry the right environment and person, and room runs count | |
| 2 | T04 | Seat billing: $20 a month from the sixth member | |
| 3 | T05 | Trial checkout: card at setup, first charge on day 31 | T04 |
| 4 | T06 | One plan. Base and Ultra retired. One price constant everywhere | T05 |
| 5 | T02 | Events for workspace created, member joined, team formed | T01 |
| 6 | T03 | Events for the first handoff: approval answered, task moved | T01 |
| 7 | T12 | Workspace form asks "How did you first hear about us?" and keeps the first-touch source | T01 |
| 8 | T22 | The setup request page, with the source question required | T12 |
| 9 | T23 | The remaining events: week-4 active, invoice paid at list, landing viewed, setup clicked | T02, T05, T22 |
| 10 | T24 | The Monday readout script | T23 |
| 11 | T27 | Site cleanup: channels off the homepage, thin pages redirected, flickmeon.ai redirected | |
| 12 | T30 | The pricing page. Every price on the site reads one constant | T06 |
| 13 | T28 | Homepage hero: two buttons, headline test | T22 |
| 14 | T29 | Homepage sections, metadata, voice. Anna and Second Catch named | T28 |
| 15 | T07 | Any member may invite. Invitations land on a page that explains | |
| 16 | T08 | After the first run, the CEO asks "Who should see this with you?" | T07 |
| 17 | T09 | The first approval asks who should decide | T07, T03 |
| 18 | T10 | Onboarding band puts the invite first. Quiet one-person reminders | T08 |
| 19 | T13 | The first run ends with one finished thing | |
| 20 | T14 | Welcome email from the CEO agent's address, for owners and teammates | T06, T07 |
| 21 | T11 | Sign-up with two doors and GitHub sign-in | T06 |
| 22 | T15 | Away email: a DM, mention or approval unseen for five minutes, sent when the person is away | T01 |
| 23 | T35 | Each run records who routed it and who pressed Stop | T01 |
| 24 | T16 | Session page counts opens and offers "Start your own workspace" | T12 |
| 25 | T17 | Ask to join for outsiders on a public session. Allow makes them a member | T02 |
| 26 | T18 | "Full: N watching", then a load test before the viewer cap rises | |
| 27 | T33 | The data page | |
| 28 | T34 | Plugin repo: MIT licence and a README for a stranger | T22 |
| 29 | T32 | Blog bylines and the get-started doc rewritten around the team | T22, T13 |
| 30 | T31 | Six comparison pages, Leo's byline | T30, T22, T17 |
| 31 | T26 | Opt-out footer on agent mail with a link back to us | |
| 32 | T19 | Day-28 recap card and email, reading-only after lapse, one pause, 90-day window | T05, T06 |
| 33 | T20 | Workspace export | |
| 34 | T21 | Phone app in the App Store without in-app purchase | T06 |
| 35 | T25 | Phone: a teammate you text | T07 |

**Three things that take longer than the code**

- **The load test** behind the viewer cap (T18). A few hours, after the code.
- **Apple review** for the App Store build (T21). Days, outside our control.
- **Plugin directory submissions** (after T34). The directories decide their own timing.

**What Thursday unlocks**

- Setup sessions start Friday 16 October. A team set up that day pays on 16 November.
- Four of the five launch gate items are code and turn green when Thursday's work is in production. The fifth is two named teams with a real handoff. Anna has one. Second Catch has one once its founders answer the $5,000 card.
- Show HN runs Thursday 15 October at 08:00 PT, if production is deployed and dry-run by Wednesday night. Otherwise Tuesday 20 October. Product Hunt runs Tuesday 20 October (Thursday 22 October if Show HN slips).

## 5. Measurement track

**The one number on the wall.** Teams with two or more people and a completed handoff. Lightbulb staff count as neither the second person nor the handoff. Prasiddha keeps the list of internal workspaces and staff ids that every query excludes.

**The five Monday numbers**

| Number | Definition | Counted by machine from | Until then |
|---|---|---|---|
| 1. Setup requests | A submitted setup form, with its source | T22 | The outreach sheet |
| 2. Teams formed | A new workspace with two or more people within 48 hours, staff excluded | T02 | Clerk, by hand |
| 3. First handoff | In the workspace's first week, a person answered an approval, picked up a session or moved an agent's task on | T03 | Not counted |
| 4. Week-4 active | Share of teams formed with a human message in their fourth week, staff excluded | T23 | Not counted |
| 5. Paying at list | A workspace with a Stripe invoice paid at full price. Coupons do not count | T23 | Stripe, by hand |

**Two more definitions**

- **Working first session:** a new workspace with a first document and a first approval card answered by a member inside the first session.
- **Outside workspace:** any workspace except our own, the demo lab, the test and review workspaces, and the intern workspace.

**The Monday page.** The readout script posts one page to the founders' channel by 09:00 every Monday. Prasiddha owns it. Never in the repo. It carries:

1. The five numbers: this week, last week, cumulative, target.
2. The number on the wall, with the team names behind it.
3. Traffic by source: visitors, setup requests, free starts.
4. Outbound by founder and by ask: touches, conversations, booked, set up, two or more people, handoff, paying.
5. Each founding team: trial day, second person yes or no, handoff yes or no, week-4 answer, card on file.
6. Variance: one line per number more than 30% off target, with the cause. Then at most three decisions needed.

**The rhythm**

- **Monday 09:00:** the readout, then the founders' pipeline review.
- **Thursday:** the sourcing founder checks in with each founding team, reading the same page team by team.
- **First Monday of the month:** re-fit the pipeline rates on real data.
- **Monday 11 January:** the quarter readout.
- **Monday 1 February:** the stage-2 gate read.

**Six data rules**

1. Every query filters to production and the product hosts. Without that, counts run 6 to 15 times too high.
2. flickmeon.ai traffic is its own line, never in the product funnel.
3. Workspace numbers count workspaces, never people.
4. Internal workspaces and staff are excluded as members and as authors.
5. Every number prints with the workspace names behind it.
6. No percentage on a base under ten. The week-4 gate reads "n of m" until then.

## 6. Outreach track

**How it works.** A founder sends a first touch. A reply becomes a conversation. A conversation books a 45-minute setup session. In the session the team puts a card on file, connects one tool, finishes one piece of work, invites a second person and answers its first approval card. The first charge is on day 31. The founder who sourced the team runs its session and its Thursday check-ins.

**Volume.** 15 touches this week (5 each). 30 next week (10 each). From the week of 26 October, 50 a week: Aden 20, Leo 20, Prasiddha 10. Every touch goes in the outreach sheet the same day: company, source, ask variant, stage.

**Who owns which lead.** Leads reached by email or a contact form: Aden. Leads reached through GitHub or Hacker News: Prasiddha. Reddit and X threads: Leo. Inbound setup requests go to Aden within one business day.

### The leads

Researched 13 October. "Hold" means the company sells something adjacent and is neither a customer nor a partner yet.

| # | Company | Person | Why they fit | Reach them | Owner |
|---|---|---|---|---|---|
| 1 | CABLE, Sydney | Theo | Six people, engineers and GTM staff all shipping with coding agents, hiring now | theo@cable.energy | Aden |
| 2 | XYZ by FORMATION, Berlin | Jilles van Gurp, CTO | Wrote on HN that he wants to "invite a colleague to some ongoing coding session" | Inquiry form on formationxyz.com | Aden |
| 3 | Brainwaves | grrowl (HN) | Built our product's shape themselves: Claude Code and Codex inside a Slack bot, Linear as the bus | hello@brain-waves.io | Aden |
| 4 | Airntell | Inusha De Silva | 14 people, "teammates using their own Codex accounts and local machines" | Pilot form on airntell.com, or reply on codex issue 48339 | Aden |
| 5 | Gem Wallet | none named | Six committers, an AGENTS.md that defines a hand-built handoff routine | GitHub org, X @GemWallet | Prasiddha |
| 6 | Fazeshift | Chris, VP Engineering | About 11 engineers, "we use coding agents heavily ourselves", hiring | fazeshift.com, HN thread 49923384 | Prasiddha |
| 7 | Pagelove | samj (HN) | Founders only, first hire open, Claude Code and Codex do "substantial implementation work" | Careers page, HN thread 49529763 | Prasiddha |
| 8 | Proximal Energy | skrhee | Engineers disagreeing over a shared AGENTS.md | GitHub org | Prasiddha |
| 9 | Sunspell, NYC | dscarp23 | "We are six people", Claude Code daily, hiring | Careers page | Prasiddha |
| 10 | TrakPro, Ireland | none named | Team of five, Claude Code or Codex daily | HN thread 48000122 | Prasiddha |
| 11 | Autumn (YC S25) | johnyeocx | Five people, three agents across the team | Discord only | Prasiddha |
| 12 | Mondrio, Brazil | none named | Four people, "all-in on Claude Code" | mondrio.com/contact | Aden |
| 13 | Merge | harshithl1777, cofounder | Five founders, reviewing code another engineer's AI wrote | Contact page on mergeoa.com | Aden |
| 14 | Keystroke (YC W24) | dallinbentley, cofounder | Four people, Cursor plus an in-house Slack agent | Contact link on keystroke.ai | Aden |
| 15 | TesterArmy (YC 2026) | okwasniewski, cofounder | Six people, Claude Code in their demo | tester.army/contact | Aden |
| 16 | HyperProbe (YC 2026) | shailendraht, cofounder | Eight people, product works with Cursor | Book a demo on hyperprobe.co | Aden |
| 17 | CircleIt | mintone | "The bottleneck has moved to feedback from the rest of the team" (9 October) | No readable address. Reply in HN thread 50020545 | Prasiddha |
| 18 | Meridian, NYC | vipshek | "Most coding agent sessions are user-local" | HN thread 49277029 | Prasiddha |
| 19 | Campus Tours Live | none named | Five committers, one agent setup shared across three tools | GitHub org | Prasiddha |
| 20 | Modelence (YC S25) | Aram Shatakhtsyan, CEO | Three people, Claude Code plugins | support@modelence.com | Aden |
| 21 | Dojo Coding Labs | Daniel Bejarano, CEO | "Agents write, senior engineers review" | hello@dojocoding.io | Aden |
| 22 | Cauchy | Maria Vechtomova, cofounder | "Agent code is production code" | cauchy.io/contact | Aden |
| 23 | VOYGR (YC W26) | Yarik, CTO | Claude Code and Cursor, hiring a founding engineer | HN thread 49925202 | Prasiddha |
| 24 | JCODE Agency, Belgium | founder (u/MyKMDBag34) | Four agents passing work through files, "my manual review caught it" | contact@jcode.store | Leo |
| 25 | VarynForge | founder (u/fresh_squeezed_code) | "A git repo of markdown that every teammate clones" | hello@varynforge.com | Leo |
| 26 | GPTree / Foremerge | Nick | Several coding agents across one team, one repo | Hold | |
| 27 | Driver AI | Adam Tilton, cofounder | 15 people, Claude Code and Codex, a handoff-docs folder | Hold | |
| 28 | Feyn Labs | Shreyash | "Harder to loop people in on the impact of a PR" | Hold | |

Also: **Habits365**, an inbound demo request, is Aden's first setup candidate. Call them this week.

**Threads where someone described the problem but no company is visible.** Reply in the thread as yourself, no pitch, no link. The product comes up only if they ask.

| Where | Date | What they said | Who replies |
|---|---|---|---|
| HN 49745346 | 17 Sep | A homegrown skill to merge colleagues' sessions | Prasiddha |
| HN 49368260 | 19 Aug | "I use Codex, my non-technical co-founder uses Claude" | Prasiddha |
| HN 48959477 | 18 Jul | A team of three, a Codex seat per member | Prasiddha |
| HN 48997035 | 21 Jul | "Getting the humans and agents to talk together is really fragmented" | Prasiddha |
| HN 49279530 | 12 Aug | "People on my team tend to work more in isolation with agents" | Prasiddha |
| r/smallbusiness 1wzbc1q | 6 Oct | Two cofounders, separate accounts, "none of it syncs" | Leo |
| r/ChatGPTCoding 1wysqo1 | 6 Oct | "Re-explaining the stack in their prompts every session" | Leo |
| r/ChatGPTCoding 1x1ks1u | 9 Oct | Team of five, coding agent picks up tickets overnight | Leo |
| r/ChatGPTCoding 1wz72y2 | 6 Oct | "20 or so PRs that nobody on my team actually wrote" | Leo |
| r/smallbusiness 1wzbc1q (TheCryptoBillionaire) | 7 Oct | "Six people all living in their own ChatGPT/Claude bubbles" | Leo |
| r/AI_Agents 1wz5soi | 6 Oct | Six agents, one shared file, one line each per night | Leo |
| r/ClaudeAI 1wdf533 | 11 Sep | Linear issues so Claude and ChatGPT talk "without me having to copy paste" | Leo |
| r/startups 1vf8m2e | 4 Aug | A sales team built around an agent | Leo |

### The words we send

**The three qualifying questions**, in the first reply to anyone who answers:

1. Who on your team runs an agent today, and for what?
2. When one person's agent work has to reach someone else, how does it get there now? Tell me about the last time it went wrong.
3. If we set it up together, would you bring one teammate and one real piece of work to the first session?

**First touch.** Subject: `handoffs`. Under 90 words. The observation line quotes their own post, ten words or fewer, never invented. Variant A asks for a call. Variant B asks for a reply. Each founder sends half A, half B, and the sheet records which.

> Hi {first},
>
> {observation}
>
> When two people run agents, the founder ends up carrying context between them.
>
> Second Catch, a two-founder team, set up in Lightbulb last week. Their CEO agent wrote their seller plan. It is done, and the $5,000 it needs waits on an approval card both founders can see.
>
> A: Bring your {teammate} and the agents you already use, and we'll get your first {plan / PR / invoice run} approved by them this week. 45 minutes, I run it.
> B: Reply with one handoff that still goes through you, and we'll set it up so your {teammate} approves the {work} from a channel this week. No call needed.
>
> {founder}
> Lightbulb, thelightbulb.company

**Day 3**, same thread:

> {first}, one more angle. The hard part with agents on a team isn't the code. It's who owns a change, and which actions stop for a person's yes.
>
> In Lightbulb the agents sit in the same channels as your people, and spend, sends and deploys wait for an approval you can see.
>
> A: Bring your {teammate} and one agent you use, and your first {work} gets approved this week. / B: Name one handoff that still goes through you and we'll wire it this week.

**Day 8**, same thread:

> {first}, a food business asked its CEO agent to match photographed checks to open invoices in QuickBooks. It marked them paid and has sent invoices since 11 October, asking first. The owner told us: "I'm really impressed with how fast it works."
>
> Same shape for your {work}: the agent does it in the real tool, and a person says yes.
>
> A: Want us to set up yours with your {teammate} this week? / B: Which handoff would you want it to do first?

**Day 16**, the last one. Honour it.

> {first}, I'll stop here. If handoffs between your people and your agents aren't a problem right now, ignore this and good luck with {their project}.
>
> If it becomes one, reply "handoff" and we'll get your first {work} approved by your {teammate} that week.

**Reddit or X reply** (public, under 60 words, no link, Leo in his own name):

> Same here: the agent remembers my work, not my cofounder's. What helped was putting the agents in the same channel as the people, so a handoff is a mention and anything that spends money waits for a yes. Curious what you do when a change needs an owner.

**LinkedIn message** (Aden, after a connection is accepted):

> Hi {first}, I saw your post on {topic}. I'm Aden, one of the founders of Lightbulb. We put a team's people and its AI agents in the same channels. If you and your {teammate} run agents, I'll get your first {work} approved by them this week. Want it?

### Prasiddha, week by week

Channels: Hacker News, Product Hunt, plugin directories. Ten touches a week to the GitHub and HN leads above. Three HN comments a week in handoff and agent-memory threads, each with the line "I work on Lightbulb". Never ask anyone to upvote.

| Week of | Touches | Post | Launch and listings |
|---|---|---|---|
| 14 Oct | 5 | Two HN comments in the agent-memory threads above | Wed: prod deployed, public session page dry-run, Second Catch asked to answer its card. Thu 08:00 PT: Show HN, first comment within a minute, in the thread until 18:00 PT. Thu: plugin submitted to the Anthropic directory, the Codex catalog and awesome-claude-code |
| 19 Oct | 10 | HN comment on session handoff: what a teammate needs besides the transcript | Tue 00:01 PT: Product Hunt live, maker comment, replies all day. Wed: launch post-mortem in the founders' channel. Fri: thank-you email to everyone who engaged |
| 26 Oct | 10 | Submit blog P1 to HN: "Handing a Claude Code session to a teammate" | Chase the plugin listings |
| 2 Nov | 10 | HN comments in "AI employees for small teams" threads | MCP registries |
| 9 Nov | 10 | HN comments in per-seat pricing threads, pointing at the pricing post | |
| 16 Nov | 10 | Submit blog "Deciding when an agent should speak in a team chat" | |
| 23 Nov | 10 | One HN comment | |
| 30 Nov | 10 | Submit blog "No public dataset has a team chatting with AI coworkers" | |
| 7 Dec to 28 Dec | 10 | HN comments, year-end "what are you working on" threads | |
| 4 Jan | 10 | | Mon 11 Jan: quarter readout. Draft the 1 Feb gate read |

### Aden, week by week

Channels: LinkedIn, setup sessions, check-ins, interviews, customer stories. Twenty touches a week to the email and form leads above, by email or LinkedIn message. One LinkedIn post a week, first person, one scene, link in the first comment, every comment answered in the first hour. Answer every setup request within one business day.

| Week of | Touches | Post (hook line) | Sessions and calls |
|---|---|---|---|
| 14 Oct | 5 | Wed: "Your agent remembers what you did. It has no idea what your cofounder figured out yesterday." Thu 10:00 PT: launch post, link to the homepage, never to the HN thread | Call Habits365. Ask Second Catch to answer its card and clear its story. Get Anna's screenshot vetted. Block Thu and Fri for setup calls. Fri: first setup sessions |
| 19 Oct | 10 | "A one-person food business sends invoices from QuickBooks through an agent now." | Setup sessions from launch inbound. Tue: share the Product Hunt page, no vote ask. Record Anna's 60-second story. Start the day-one churn interviews: the 24 who never came back, $50 gift card each |
| 26 Oct | 20 | Document post: "The handoff is where agent work gets lost." Four screenshots from a recorded handoff | One or two setups. Thursday check-ins begin |
| 2 Nov | 20 | "I'd judge a new hire by their first ten minutes. So I timed our CEO agent's." | Setups. Check-ins. Submit G2 and the LinkedIn company page |
| 9 Nov | 20 | "We charge for the work, not the people. Here's why five people cost the same as one." | Setups. Check-ins. First week-4 question to the earliest team: "How would you feel if you could no longer use it?" |
| 16 Nov | 20 | "Most AI in group chats speaks only when tagged. We think a good colleague does better than that." | Setups. Check-ins. First day-31 charges land |
| 23 Nov | 20 | "We gave our first customers 100% off. Here's what that taught us about price." | Setups. Save play for any team not using the product |
| 30 Nov | 20 | "The first thing that broke was adding the cofounder. Here's what it looks like now." (Second Catch) | Setups. Check-ins |
| 7 Dec | 20 | "We asked our first teams one question in week four." | Setups. Check-ins |
| 14 Dec | 20 | "We end every setup session the same way: someone invites a teammate." | Setups. Check-ins. Crunchbase and Wikidata |
| 21 Dec | 20 | "33 of 38 workspaces that signed up have one person in them. We built for teams." | No holiday pause |
| 28 Dec | 20 | | Setups. Week-4 checks |
| 4 Jan | 20 | "Two teams, two different jobs, the same pattern: the agent does it, a person says yes." | Ten teams set up by Mon 4 Jan. Mail a light bulb to each team after its first handoff |

### Leo, week by week

Channels: X and Reddit. Twenty touches a week to the Reddit and X leads and threads above. Three to five helpful replies a day in the wedge's threads, link-free, affiliation disclosed. Reddit posts only as replies until karma passes 100. One X thread or post a week, screenshots from our own workspace only. Author and sign-off for the six comparison pages.

| Week of | Touches | Post (hook line) | Also |
|---|---|---|---|
| 14 Oct | 5 | Wed: reply in the newest r/ClaudeAI "two devs, several sessions" thread. Thu 10:00 PT: X thread "Three founders, six agents, one workspace. What we built and what's still rough.", homepage link in the last post | Check Reddit karma. Read each sub's rules again |
| 19 Oct | 10 | X thread: "Our agents send email and open PRs. Here is what we decide first: who they may email, and what waits for a yes." with a real approval card. Tue: share the Product Hunt page, "the best thing you can do is try a session link and tell us what broke" | r/ClaudeCode follow-up: "The most common question was 'who owns the change?'", only if it was |
| 26 Oct | 20 | r/ClaudeCode showcase post (if karma allows, else the megathread): "Every new Claude Code session starts from zero. Here is how we stopped re-explaining to each other." | Submit AlternativeTo (as an alternative to Slack, Teams, Grok Bot) and SaaSHub |
| 2 Nov | 20 | X thread: "We made a new workspace and wrote down everything the CEO agent did before anyone typed again." | |
| 9 Nov | 20 | r/ClaudeAI showcase: "A plugin that lets a teammate watch your Claude Code session and pick it up." | Comparison pages reviewed and signed |
| 16 Nov | 20 | X: "When someone stops an agent mid-reply, that is the most honest feedback we get." | |
| 23 Nov | 20 | r/startups "I will not promote" post: what we let agents do alone in a three-person company, no product name, no link | |
| 30 Nov | 20 | X thread: "We went looking for real team chat with AI coworkers in it. None exists." | |
| 7 Dec to 21 Dec | 20 | X: "This quarter our agents opened N PRs and filed N tasks for the three of us." (our workspace's counts only) | |
| 28 Dec to 4 Jan | 20 | One X post a week | |

### The setup session

Forty-five minutes. The session ends with one thing finished, not a tour. You drive, they click. Ask for the second person at booking. If they insist on coming alone, book anyway, and the first approval card is the invite.

| Minutes | Step | What happens | If it fails |
|---|---|---|---|
| 0 to 5 | Qualify | The three questions. Say what the next 40 minutes produce | A disqualifier: stop, thank them, offer the free sign-up |
| 5 to 12 | Name the company | They sign up, name the workspace, add the card (30 days free, first charge day 31), say what the company does in one sentence. Watch the CEO agent answer | Slow to be ready after 5 minutes: reprovision and note it |
| 12 to 20 | Connect one tool | The tool they named: QuickBooks, Gmail, GitHub | Try the next tool. If none connects, use email and log the failure |
| 20 to 32 | Wire one workflow | One real task: "draft the three invoices due this week". Hand it to the right agent in a room | The agent stalls: restate it as one outcome. Still bad: do it by hand beside it and say so |
| 32 to 40 | Invite the second person | By email, on the call, while the first result is visible. Keep the room open until they appear | They cannot join live: send the join link to their phone and book a 10-minute slot within 48 hours |
| 40 to 45 | First approval card | The agent's output becomes a card. The customer answers it, never the founder | No card appears: ask the agent to propose the next step that needs a yes. Still none: file a bug before ending |

Close: agree the week-4 check, confirm the shared channel, write down what broke, record setup done, second person in, handoff done.

**Never promise:** a discount, coupon, extra free month or refund; SOC 2, uptime or response-time terms; features not built (SSO, Delta-style inline comments); that an agent acts without approval or never errs; revenue or growth results; a roadmap date; a word from the banned list.

### The launch

**Show HN: Thursday 15 October, 08:00 PT.** Product Hunt: Tuesday 20 October, 00:01 PT. If production is not deployed and dry-run by Wednesday night, Show HN moves to Tuesday 20 October and Product Hunt to Thursday 22 October.

**The gate.** Prasiddha signs off Wednesday evening.

| # | Must be true | How to check |
|---|---|---|
| 1 | Homepage shipped: headline, "Request a guided setup" first, "Start free" second, team-shaped steps, FAQ | Open it in a private window |
| 2 | Billing and the trial checkout work, and the pricing page is live | A test setup ends with a card and a trial end date |
| 3 | A signed-out stranger goes from a session link to a seat in the workspace in under two minutes, and the viewer cap has been load-tested | Do it from a fresh browser |
| 4 | Two named teams with a real handoff: Anna, and Second Catch once its card is answered | A screenshot each, vetted by a founder |
| 5 | The setup request page is live with the source question, and a test request with `utm_source=hn` shows in the readout | Submit one |

If Second Catch has not answered its card by Wednesday night, the first comment names Anna alone and says so.

**The countdown**

| When | Prasiddha | Aden | Leo |
|---|---|---|---|
| Wed 14 Oct | Deploy prod. Pick the clean public repo. Dry-run the public session page for the evening. Test UTMs end to end. Preload replies | Ask Second Catch to answer its card and clear its story. Vet Anna's screenshot. Block Thu and Fri for setup calls | Draft the X thread. Warm list: message people who asked about handoffs, sharing the problem, not a vote |
| Thu 15 Oct | 08:00 PT Show HN. First comment within a minute. In the thread until 18:00 PT. Submit the plugin to the directories | 10:00 PT LinkedIn post with the homepage link. Run setup calls from inbound | 10:00 PT X thread with the homepage link, no HN link. Reply to every X reply |
| Fri 16 Oct | Reply to late comments. Email every setup request | Setup sessions | r/ClaudeAI follow-up only if the Wed reply earned it |
| Mon 19 Oct | Monday readout: launch cohort by source. Prepare the Product Hunt draft, self-hunted | Prepare the week's setup calendar | |
| Tue 20 Oct | 00:01 PT Product Hunt live, maker comment, replies all day | Share the page on LinkedIn, no vote ask | Share the page on X, no vote ask |
| Wed 21 Oct | Post-mortem in the founders' channel: sources, setup requests, objections heard | Setup calls | |
| Fri 23 Oct | Thank-you email to everyone who engaged. Chase the listings | | "What we learned from launch week" thread |

The Show HN title, body, first comment, the Product Hunt copy and the reply playbook for comments are in Appendix B.

### Directories

All free. Listings quote our own numbers only, vetted by a founder.

| Place | Owner | When |
|---|---|---|
| Anthropic plugin directory | Prasiddha | Thu 15 Oct |
| Codex plugin directory | Prasiddha | Thu 15 Oct, if the portal accepts a package without hooks |
| awesome-claude-code | Prasiddha | Thu 15 Oct |
| Our plugin repo as a marketplace: topics, licence, first README line | Prasiddha | Thu 15 Oct |
| Product Hunt | Prasiddha | Tue 20 Oct |
| MCP registries (official registry, Glama) | Prasiddha | Week of 2 Nov |
| G2 and Capterra | Aden | Week of 2 Nov, once the pricing page is live |
| LinkedIn company page | Aden | Week of 2 Nov |
| AlternativeTo (alternative to Slack, Teams, Grok Bot) | Leo | Week of 26 Oct |
| SaaSHub | Leo | Week of 26 Oct |
| Crunchbase, Wikidata | Aden | Week of 14 Dec |
| App Store | Prasiddha | When Apple approves the build |

Skipped on purpose: paid AI directories, BetaList and the minor launch sites, press-release sites, any directory with no editorial gate.

## 7. Calendar

| Date | What happens | Who |
|---|---|---|
| Wed 14 Oct | Production deployed. Public session page dry-run. Second Catch asked to answer its card. Gate signed off in the evening | Prasiddha, Aden |
| Thu 15 Oct | All 35 product tasks shipped. Show HN 08:00 PT. Plugin submitted to the directories. LinkedIn and X launch posts 10:00 PT | All three |
| Fri 16 Oct | First setup sessions from launch inbound | Aden |
| Mon 19 Oct | First Monday readout: launch cohort by source | Prasiddha |
| Tue 20 Oct | Product Hunt 00:01 PT | Prasiddha |
| Wed 21 Oct | Launch post-mortem in the founders' channel | Prasiddha |
| Mon 26 Oct | Outreach rises to 50 touches a week. Thursday check-ins begin | All three |
| Mon 2 Nov | First monthly re-fit of the pipeline rates. G2 and the LinkedIn company page submitted | Prasiddha, Aden |
| Mon 9 Nov | First week-4 question to the earliest team | Aden |
| Mon 16 Nov | First day-31 charges land. First "paying at list" on the readout | |
| Mon 7 Dec | Monthly re-fit | Prasiddha |
| Mon 4 Jan | Ten teams set up with a card on file | Aden |
| Mon 11 Jan | Quarter readout. Draft of the gate read | Prasiddha |
| Tue 12 Jan | About three teams paying at list | |
| Mon 1 Feb | Stage-2 gate read, then monthly | Prasiddha |

## 8. After 12 January

**The gate.** Stage 2 opens when a quarter of new teams reach a working first session and are still active in week four. It is read on the first Monday of each month from 1 February. Sign-up opens wider only after it passes. Until then everything stays as it is in this plan: founder outreach, hand-run setups, no paid spend.

**What passing it unlocks**

- Sign-up opens more widely, and the product loops (the first-session invite, session links with Ask to join, the agent-mail footer) carry more of the selling.
- A growth engineer joins to work those loops, once the $2M SAFE has closed. The hire is an engineer, not a marketer. The earliest start is after the 1 February read.
- One paid test, only on a channel that already brings teams free.
- The referral credit is decided. The launch competition on flickmeon.ai is set up.

**What starts stage 3.** Founding teams asking for single sign-on, admin permissions and a company plan. Not a date. Then: a company plan, the first service vendors, the first sales hire.

**Targets**

- About five teams paying at list by mid-February.
- Six paying by 31 May.
- Once sign-up opens, paying teams grow 7% week over week. If that holds from mid-May, 20 to 40 paying teams by 30 September.

**The fallback.** If at the 1 March read fewer than three founding teams are both week-4 active and paying at list, the wedge is not paying. Then operator-owner small businesses, today the fallback named in the business plan's risks, become the main bet for the second quarter.

**What does not change, whatever happens**

1. The customer: founder-led teams of 3 to 15 that already run agents for real work.
2. A named founder signs every public post, email and listing. Agents never publish.
3. No discounts, no outside coupons, no guarantee. The free month is the risk reversal.
4. Fix forward: the real thing ships before copy depends on it.
5. The five Monday numbers and the number on the wall are the scoreboard.
6. No paid acquisition before the gate, whatever the bank balance.

## 9. Open decisions

Each one names what it blocks.

1. **The price of a founder hour.** Hours are the only acquisition cost today. Without a figure, nobody can judge the cost of a team or a paid test.
2. **The fallback trigger.** Confirm the 1 March rule above, or set another.
3. **Cash, burn, runway and the round's close date.** All blank in the business plan. They set the growth engineer's start date.
4. **Who signs the lifecycle emails and who checks the readout against Clerk and Stripe.** Nobody owns either today.
5. **Tool spend by line.** Not broken out anywhere.
6. **Three bios, Search Console and Bing access, X API credits.** Bios block launch copy. Search Console blocks the search baseline. Credits block X replies at volume.
7. **The growth engineer trigger.** The SAFE close alone, or the SAFE plus the gate.
8. **Stages 2 and 3 as gates, not quarters.** Confirm, and pick one forecast shape.
9. **The pass bar for new agent reply rules.** No accept rate blocks a fleet rollout today.
10. **The source for the "$2M+ in combined annual revenue" line on the homepage.** It stays up with no source attached.
11. **Four billing details.** A member leaving mid-cycle: credit the prorated amount or not. Which failed-payment retries and emails. Annual billing and its price. How many target teams pass five people, which sets seat revenue.

## Appendix A. The current-state scorecard

Scored 0 to 5 from the 13 October materials. Push back where you have better data.

| # | Area | Score | Why |
|---|---|---|---|
| 1 | Positioning | 3 | Statement and claims approved 13 Oct. Every machine-read surface still sells "AI agents for autonomous businesses" |
| 2 | Customer research | 2 | One wedge customer. Three of six quoted pains are from competing builders. No reasons known for the 24 day-one churners |
| 3 | Homepage | 2 | Headline says "team" but every step after is solo. One button, no price, no FAQ, no named proof |
| 4 | Product and pricing pages | 1 | Pricing page returns 404. Plan cards show provider paths, not prices |
| 5 | Conversion pages | 1 | Setup request page 404. Ten thin, orphaned pages due for deletion |
| 6 | Competitor comparison | 1 | Nothing live. Six pages written, held for the pricing page |
| 7 | Content | 2 | Six blog posts with an organisation byline, internal names and banned words. Docs carry stale facts |
| 8 | Onboarding | 2 | Single-player: invite card after connectors and admin-only. 33 of 38 workspaces solo |
| 9 | Email lifecycle | 2 | Daily digest live. Welcome inaccurate. No trial-end, away or nurture email |
| 10 | Sales material | 3 | Battle card, objections, run sheet written 13 Oct. Unused on a real call |
| 11 | Messaging | 3 | Voice and word lists documented. Live surfaces break them |
| 12 | Pricing | 1 | Decided but cannot be bought. Checkout bills $20/$50/$150 with no trial. Seven pricing changes since April |
| 13 | Conversion testing | 1 | No test ever run. Hero buttons fire no event. Dev data swamps prod |
| 14 | Launches | 1 | No structured launch since April. The launch competition was never set up |
| 15 | Paid ads | 0 | None, by design |
| 16 | Search | 1 | Technically sound. Ranks for no wedge query. Loses the brand query to two namesakes |
| 17 | Internationalisation | 0 | US, English, USD. Fine at this stage |
| | **Total** | **26 / 85** | |

## Appendix B. Launch copy

### Show HN

**Title** (76 characters): `Show HN: Lightbulb – AI employees for your team and its Claude Code sessions`

**URL:** a live "Anyone with the link" session page that Prasiddha runs on the clean public plugin repo all day. It opens with no sign-in. Before posting, check it for secrets, `.env` files and customer data.

**Body** (in the text field if the form accepts text with a URL, else the first paragraph of the first comment):

> Hi HN, I'm Prasiddha. My cofounders and I run most of our company through AI agents: they open our PRs, send our email and do our research, and a customer's agent sends her QuickBooks invoices. The work was real. The problem was that each piece of it sat in one person's chat, and I was the one relaying it.
>
> Lightbulb is our fix. The agents are members of a team workspace: a CEO, Engineering, Finance, Growth and others, each with its own memory, email and tools. You mention one in a channel, it does the work in the real tool, and the result comes back where the whole team can see it. Spend, sends and deploys wait for a person's yes, and the record of who did what stays in one place.
>
> It also works with the agents your team already runs. Turn on sharing in the Mac app (or `/lightbulb:share`) and a Claude Code or Codex session gets a link. Anyone with the link can watch the terminal live, no account. Your teammates can message it, ask to join, or pick it up when you stop. A stranger can ask to join too, and I decide. The link above is my session right now.
>
> Mac app plus web. 30 days free; the card goes on file at your setup session.

**First comment** (within a minute; a founder vets every number first):

> Some context and numbers, since this is early.
>
> Where we are: 57 accounts, 46 workspaces, about five outside teams active in a given week, and no one paying list price yet. Most workspaces have one person, which taught us the product only works when a second person shows up. So the session link is now also the way into the workspace.
>
> One real handoff: Anna — [one sentence: the agent did X, a person approved Y]. Second Catch — [same, if its card is answered].
>
> How it works: each workspace gets its own isolated cloud runtime, so its agents keep working when nobody's laptop is open. Agents use Lightbulb-managed models, your ChatGPT account or your own API key. The plugin is open at github.com/TheLightbulbCompany/lightbulb-plugin.
>
> What doesn't work yet: [the two honest gaps on launch day].
>
> Pricing: 30 days free, then $50 a month for up to five people and $20 a person from the sixth. Price locked for two years. Happy to answer anything, including "why not just Slack".

### Product Hunt

- **Name:** Lightbulb
- **Tagline:** Your team and your AI employees, in one place
- **Description:** A team workspace where AI employees work in your channels. Mention one, it does the work, and the result comes back where everyone can approve it or pick it up. Share a Claude Code or Codex session by link. Any model. $50 a month for up to five people.
- **Gallery captions:** "Mention an agent in a channel. The work comes back where the whole team can see it." / "Share a Claude Code session by link. Anyone can watch; your team can pick it up." / "Spend, sends and deploys wait for a person. Approve in one tap." / "Six AI employees from day one, each with its own memory, email and tools." / "Any model: Lightbulb-managed, your ChatGPT account or your own key. $50 for up to five people."

**Maker comment:**

> Hi Product Hunt, I'm Prasiddha, one of Lightbulb's three founders.
>
> We built this because we were the problem. Our agents did real work, but each piece of it sat in one person's chat. I was the one who relayed it all: what the agent did, what my cofounder's session found, what was waiting on a yes.
>
> In Lightbulb, AI employees (a CEO, Engineering, Finance, Growth and others) are members of your workspace, with their own memory, email and tools. You mention one in a channel. The work comes back where the team can see it, and anything that spends, sends or deploys waits for a person to approve. You can share a Claude Code or Codex session by link, and a teammate can pick it up.
>
> Anna used it to [handoff, one line]. We're early: a handful of teams, set up by hand. If you run a team of 3 to 15 and already use agents, request a guided setup and one of us will wire your first workflow with you. Questions and hard objections welcome.

### Replying to comments

Reply within 15 minutes for the first eight hours. One to four sentences. Concede what is true. No links except to docs or pricing. Never ask anyone to upvote or comment.

| They say | We say |
|---|---|
| "Coding is single-player." | Agreed for the typing part. The trouble starts after: the cofounder who inherits the PR has no idea how the agent got there. The link exists for that moment, not for two cursors in one editor. |
| "Multiplayer is the VC buzzword of the year." | Fair, and we don't use it. The concrete version: I stop, a teammate opens the same session, and the history is there. |
| "Basic Claude does all that." | For one person, yes. Lightbulb earns its place when a second person needs to see, continue or approve the work. If that never happens on your team, it isn't for you yet. |
| "So like Grok Bot / Slack agents?" | Grok Bot gives each person their own computer that nobody else sees. Slack adds an assistant to people-chat. Here the agents are members of the team: one shared record, any model, and $50 a month covers the first five people. |
| "I'd never give agents all my accounts." | You connect only the tools you choose. Spend, sends, deploys and hires wait for a person, and every action is on the shared record. Each workspace runs in its own isolated runtime. |
| "We already have Slack." | Keep it. Lightbulb is where the agents work. Your team joins the rooms where something is happening. |
| "I don't need six agents." | You talk to one, the CEO. The others are its team, and you can switch them off. |
| "Too complicated." | One early user said exactly that, and we agree it can get messy. That's why we set up the first ten teams by hand: one real workflow, one finished result, not a tour. |
| "Will it do things without asking?" | Not the things that matter: spend, sends, deploys and hires stop for an approval card. |
| "Lock-in? Where's my data?" | Bring your own model or key. Export answer per the pricing FAQ; do not improvise. |
| "Your numbers are tiny." | They are. That is why we're here and why we set teams up by hand. |
| Praise or obvious bot replies | Do not engage. |

## Appendix C. Tools

| Tool | Used for | Who |
|---|---|---|
| PostHog | Every product event, traffic by source, the Monday numbers. Production filter on every query | Prasiddha |
| Clerk | Accounts, workspaces, members. The hand count for teams formed until T02 ships | Prasiddha |
| Stripe | Cards on file, trial end dates, invoices paid at list | Prasiddha |
| The readout script | The Monday page, posted to the founders' channel | Prasiddha |
| The outreach sheet | Every touch: company, source, ask variant, stage. Filled the same day by the founder who sent it | All three |
| Google Search Console and Bing Webmaster Tools | Indexing and the search baseline. Access still to be set up | Prasiddha |
| SES | The welcome, away and trial-end emails from the CEO agent's address | Prasiddha |
| The founders' drafts channel | Where agent drafts land. A founder edits, posts by hand, and pastes the live URL back | All three |

Agents research, draft and build. They never log in to a founder's account, never schedule and never publish.
