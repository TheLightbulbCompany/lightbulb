# 17. Ideas: the "what don't we know" sweep

## Purpose

Moves the plan's stage-1 goal (ten founding teams, a second person within 48 hours, a handoff in week one) and the inbound launch. Serves §8 Go-to-market and the "Always on, in parallel" line: the plays the plan does not name.

## What we know

- **The plan's channels are named and owned.** Leo: X and Reddit. Aden: LinkedIn and setup sessions. Prasiddha: Hacker News, Product Hunt, plugin listings. Agents research and draft; a founder signs every public post (plan §8). High.
- **The product only earns its place with a second person.** 33 of 38 outside workspaces have one person; Second Catch's first break was adding the cofounder (plan §5, §9). High.
- **We have a public plugin repo nobody can find.** `TheLightbulbCompany/lightbulb-plugin` is public, created 7 Oct, 0 stars, no homepage URL (GitHub API, 13 Oct). Its README installs into Claude Code and Codex. High.
- **Anthropic runs an official directory.** The Claude Code docs say: to offer a plugin "to everyone: submit it to Anthropic's directory" ([Claude Code docs](https://code.claude.com/docs/en/plugin-marketplaces)). OpenAI said self-serve publishing to the Codex directory was "coming soon" at launch ([Unite.AI](https://www.unite.ai/openai-adds-plugin-marketplace-to-codex/)); current state unverified. Medium.
- **The brand collides twice, not once.** Workstream 05 found TheLightbulb.ai, an Indian emotion-AI startup ([Inc42](https://inc42.com/company/thelightbulb-ai/)), and The Light Bulb Company, a UK lighting retailer ([Companies House](https://find-and-update.company-information.service.gov.uk/company/02855611)). The brief merged the two; they are different companies. High.
- **Every customer agent emails from our domain.** Agent addresses are `{agent}@{slug}.thelightbulb.company` (CLAUDE.md, `AGENT_EMAIL_DOMAIN`). High.
- **The live homepage** still leads "Your team and your agents, in one place" with "Get started free" as the only button (fetched 13 Oct). High.

### How comparable teams got their first hundred

| Company | What they did | Cheap and repeatable for three founders? | Source, confidence |
|---|---|---|---|
| Slack | "begged and cajoled our friends at other companies"; "maybe six to ten companies"; shared with "progressively larger groups" | Yes: it is our founding-teams stage, and it worked for a team product | [First Round](https://review.firstround.com/from-0-to-1B-slacks-founder-shares-their-epic-launch-strategy), high (founder interview) |
| Linear | Waitlist plus a survey ("what are your current problems?"); hand-picked about 10 users a week; one founder's 10-person team switched first; opened only once retention held | Yes: the survey is our `/request-setup` form; the weekly cohort is our quota | [First Round](https://review.firstround.com/linears-path-to-product-market-fit/), high |
| Superhuman | Manually onboarded every user on a 30-60 minute call; at peak "only 20 people" did it | Yes at ten teams: it is our setup session | [Lenny's Newsletter](https://www.lennysnewsletter.com/p/superhumans-secret-to-success-rahul-vohra), medium (call length varies by source) |
| Loom / Figma | The shared artifact is the door; Figma's viewer clicks "Ask to edit" and the owner approves | Covered by workstream 06 | 06, high |
| Notion | Templates as the answer to the empty workspace: "51 million template duplications" in a year; creators submit to a gallery | Partly: needs a copyable workflow in the product | [Notion blog](https://www.notion.com/blog/new-template-gallery), high (first-party) |
| Raycast | Developers built "more than 100 extensions" in a month on a beta API; a store inside the product | Partly: our analogue is the plugin and the marketplace, not a new API | [Raycast](https://raycast.com/blog/series-a), [TechCrunch](https://techcrunch.com/?p=2240096), medium |
| Replit | Shared editing on by default; teachers drop in on student work; education was the wedge | The lesson is the cohort shape: groups that already work together for a fixed period | [Replit blog](https://blog.repl.it/teams-for-education), medium |
| PostHog | Wrote the handbook in public before the HN launch to look credible; "300 deployments in a couple of days" | Yes: costs writing time only | [PostHog handbook](https://posthog.com/handbook/story), high (first-party) |
| Cursor | A short social push, then "monk mode"; word of mouth | Not a play: a product outcome | [eWeek](https://www.eweek.com/news/anysphere-cursor-ai-startup/), low (secondary) |
| Warp, Zed | Waitlist-gated beta; no first-hundred tactic found | Nothing new to copy | [Warp](https://www.warp.dev/blog/introducing-warp), low for tactics |

## Findings (ranked by impact)

1. **Every team product in the set got its first teams by hand, from people the founders could reach.** Slack's six to ten, Linear's ten a week, Superhuman's calls. Nothing in the set beat founder-led setup for the first hundred. The plan already bets on this; the gap is how many doors lead to a founder.
2. **The second person enters faster when they are needed, not invited.** Figma's "Ask to edit" (06) and Slack's spread inside Rdio both moved because a person had a reason. Our strongest reason is an approval waiting on them. The plan's item 1 asks "Who should see this with you?"; a sharper version routes the first approval to the person who should decide.
3. **We own unexploited distribution surfaces.** The public plugin repo, the official Claude Code directory, and every agent email sent from `thelightbulb.company`. None appear in the plan's channel list except as "plugin listings".
4. **The wedge gathers in fixed-term groups.** Accelerator batches, hackathon teams and AI Tinkerers chapters ("screened for hands-on AI work", live demos, not decks; [AI Tinkerers](https://nyc.aitinkerers.org/all_cities?m=r&page=4)) are teams of 3 to 15 who already share agent work. One organiser reaches many teams.
5. **Proof is the scarce asset, and we generate it daily.** Three founders run the company through Lightbulb (plan §10). That workspace is a demo, a changelog and a weekly post, if a founder signs it off.
6. **The collision is mostly a search problem.** Workstream 05 owns the fix on the site. The remaining plays are where the name is free (plugin ids, GitHub, the agent email domain) and one physical gift that uses the word.

## Recommendation

Run the top ten below inside the existing stage-1 rhythm; most are under two founder-hours a week. Start with the three that cost nothing and use what we already own: list the plugin properly, set up thread alerts, and make the founders' own workspace the weekly proof. Push the one product change that matters most, the first approval routed to a second person, into plan item 1. Reject paid, discounted and automated-posting plays (section 4).

## Deliverable

### 1. The ideas (32), by group

Effort: S = under 2 founder-hours, M = a day or a small build, L = a multi-day build. Owner by the plan's channel split.

**A. Get the second person in (network effect plays)**

| # | The play | Why it fits Lightbulb | Effort, owner |
|---|---|---|---|
| A1 | **Two on the call.** Ask for the second person at booking; book anyway if the founder insists, and make the first approval card the invite (A2). The run sheet starts with both in the room when they come. | Activation is the second person (§8); Superhuman-style calls work, but ours must form a team, not one fan. | S, Aden |
| A2 | **The first approval goes to someone else.** When the CEO files the first approval card, it asks "Who should decide this?" and invites that person by email to answer it. | A reason to join beats a request to look (finding 2); Second Catch's $5k plan is exactly this shape. | M, Forge (extends plan item 1) |
| A3 | **An invite that says what is waiting.** The invite text names the task: "Sam, the seller plan is waiting on your OK," drafted by the CEO, sent by the person. | Plan item 6: the second person is invited by text; a generic invite is ignorable. | M, Forge |
| A4 | **Invite the contractor.** A one-click room with a team's freelancer, bookkeeper or agency, the way our own vendor room works with each founding team. | 3-15 teams lean on outside help; that person is often the second human. Guest access is unbuilt; scope unverified. | L, Forge |
| A5 | **The founder's "who else" question at week four.** Next to the very-disappointed question, ask "Who else do you know who carries context by hand?" | Superhuman's growth came from users telling others ([Lenny's](https://www.lennysnewsletter.com/p/superhumans-secret-to-success-rahul-vohra)); stage 1 has no referral ask before 06's stage-2 programme. | S, Aden |

**B. Reach the wedge where it already is**

| # | The play | Why it fits Lightbulb | Effort, owner |
|---|---|---|---|
| B1 | **Keyword alerts on the pain.** F5Bot alerts for "handoff", "shared memory", "claude code team", "cofounder" + "claude" on Reddit, HN and Lobsters; replies within a day. | Plan §8 names these threads as the lead source but no way to see them as they happen; F5Bot is "Free to use" ([F5Bot](https://f5bot.com/)). | S, Leo |
| B2 | **Demo at AI Tinkerers.** Apply to give a live five-minute demo of a real handoff at a local chapter; collect names for setups. | Screened builders, live demos only (finding 4); our demo is real work, not slides. | M, Prasiddha |
| B3 | **One programme manager, one batch.** Each founder lists his real accelerator contacts in the outreach log this week. Offer a listed accelerator's current batch a free 45-minute setup through the programme's perks list or office hours. | Plan names accelerator bios as a source; one intro reaches a cohort of the exact shape. Which programmes accept vendors: unverified. | S per programme, Aden |
| B4 | **A hackathon prize for the best team handoff.** Sponsor a prize (the founding offer, not cash) at an agents hackathon; every team gets a workspace for the weekend. | A hackathon team is 3-5 people sharing agent work for 48 hours: the wedge in miniature, with a deadline. | M, Leo |
| B5 | **Go where the DIY glue lives.** Answer issues and discussions on public "shared memory for Claude Code teams" repos; contact maintainers only via their public site. | Their alternative is DIY glue (plan §5); the people building it have the problem and the skill. | S, Prasiddha |
| B6 | **List the plugin in Anthropic's official directory**, and in community lists, with a README that says what Lightbulb is and links to `/request-setup` with a UTM. | The repo is public with 0 stars and no homepage (What we know); the directory is the official path ([docs](https://code.claude.com/docs/en/plugin-marketplaces)). | S, Prasiddha (workstream 14 owns listings) |

**C. Make the founding teams the marketing**

| # | The play | Why it fits Lightbulb | Effort, owner |
|---|---|---|---|
| C1 | **One handoff, told in full.** With consent, a one-page teardown per founding team: the ask, the agent's work, the approval card, who picked it up. | Plan §8 wants handoffs as Show HN material; this makes each one an asset as it happens. | S each, Aden |
| C2 | **A real spots counter.** "Founding teams: 10 spots, 3 set up" on the homepage, updated by hand. | Ten is the founders' true capacity (§7); honest scarcity, no discount. | S, Prasiddha |
| C3 | **A founders' room inside Lightbulb.** One shared channel where founding teams talk to each other and to us; the community lives in the product. | Notion's ambassadors had a private Slack group ([nogood](https://nogood.io/2022/01/21/notion-growth-strategy)); ours shows the product working across companies. Needs a cross-workspace room: confirm the vendor-room mechanism extends. | M, Aden + Forge |
| C4 | **Workflows from real teams, copyable.** Each team's first wired workflow (routine, agents, approval rule) becomes a starter a new workspace can copy. | Notion's templates solved the empty workspace ([Notion](https://www.notion.com/blog/new-template-gallery)); plan item 2 needs "one finished thing" in the first ten minutes. | L, Forge |
| C5 | **Guest posts by founding teams** on their own blogs or X about what they handed off. | Third-party pages are what AI engines cite (05, Part C); also disambiguates the name. | S each, the team; Aden asks |

**D. Agents do marketing work, under founder sign-off**

| # | The play | Why it fits Lightbulb | Effort, owner |
|---|---|---|---|
| D1 | **Every post through an approval card.** The Growth agent drafts each reply from B1 alerts into an approval card; the founder edits, approves, and posts by hand. | The plan's rule made visible; the cards themselves become content. | S, Leo |
| D3 | **A setup dossier before every call.** The Analytics agent reads the prospect's public posts and drafts the first workflow to wire. | Workstream 04's 45 minutes go further when the workflow is chosen before the call. | S, Aden |
| D4 | **A monthly "what our agents did" report** from our own workspace: runs, approvals, handoffs; aggregates only. | Proprietary data content (skill idea 6); nobody else in the set publishes this; plan §10 says we are our own first customer. | S, Prasiddha |
| D5 | **Competitor and launch watch.** An agent tracks Delta, Lody, Egregore, Grok Bot and AMP changelogs weekly for the founders. | Founder ruling: a crowded space is to be learned from (context doc). Internal only. | S, Leo |

**E. The brand collision: route around it or use it**

| # | The play | Why it fits Lightbulb | Effort, owner |
|---|---|---|---|
| E1 | **Own the free namespaces.** `lightbulb` in the plugin directory, the GitHub org, the X and LinkedIn handles, all with the 05 definition line. | Where developers search, the collision does not exist. | S, Prasiddha |
| E2 | **The agent email as a brand carrier.** Customers' agents already send from `…thelightbulb.company`; add an opt-out footer line, on by default (check first whether agent mail already carries one): "Sent by Finance, an AI employee at {Company}, on Lightbulb." | Every send is an impression on the right domain; unlike a badge on `/s` (rejected in 06), the recipient does not know us yet. | M, Forge |
| E3 | **The first-handoff bulb (adopted).** After a team's first handoff, mail the team a real light bulb with a card naming what the team handed off, from the founder who set them up, under his own name. | Passes the skill's three-rule test: relevant, a gift not a pitch, a hand-qualified list of ten. Cost per unit: unverified. | S, Aden |
| E4 | **Say the full name in every first mention.** "Lightbulb, the team workspace for AI employees" in bios, listings, podcasts and HN titles. | Workstream 05's fix, applied by the founders in what they post. | S, all |

**F. Cheap proof: build in the open**

| # | The play | Why it fits Lightbulb | Effort, owner |
|---|---|---|---|
| F1 | **"Three people, six agents": a weekly build log** from the founders' workspace, one real handoff a week. | PostHog wrote in public to look credible to a sceptical market ([handbook](https://posthog.com/handbook/story)); our product builds our product. | S, rotating |
| F2 | **A public changelog that says who shipped it**, agent or person, with the approval. | Skill idea 108; it proves the record claim with our own record. | S, Prasiddha |
| F3 | **A watch-us-work link.** An "Anyone with the link" shared session of an agent working on a public Lightbulb issue, posted with each launch. | Session links are our one outside door (plan item 4); an outsider who opens one gets the same "Ask to join" teammates have (on by default per session; the owner's Allow also makes them a workspace member). This gives a stranger a reason to open one. Needs a review for leaks. | M, Prasiddha |
| F4 | **Office hours on a shared session.** Thirty minutes a fortnight; founders hand a real task to an agent live and take questions. | Live public demos (skill idea 109) cost nothing and show handoff, not features. | S, Leo |

**G. Partnerships**

| # | The play | Why it fits Lightbulb | Effort, owner |
|---|---|---|---|
| G1 | **Vendor customer stories.** Offer Clerk, Stripe (we ship a Stripe App) and Vercel a short "built on" story. | Their pages are read by founders building products; free third-party mentions. Whether each takes small customers: unverified. | M, Prasiddha |
| G2 | **The open-source projects we run on.** A write-up of running an open-source agent framework for teams, offered to that project's community. | Its users are agent builders. Hermes and Paperclip may be named as "built on", never as our product's name (B10). | M, Prasiddha |
| G3 | **Accelerator perks lists.** Be on one or two programmes' standing perks pages with the founding offer. | Turns B3 from a one-off into a standing door. | S, Aden |

### 2. Impact × effort

| | **Low effort** | **High effort** |
|---|---|---|
| **High impact** | A1, A5, B1, B3, B6, C1, D1, D3, F1 | A2, A3, B2, B4, C4, E2, F3 |
| **Low impact (now)** | C2, C5, D4, D5, E1, E3, E4, F2, F4, G3 | A4, C3, G1, G2 |

Impact is judged against the five Monday numbers; every placement is our judgement, not measured.

### 3. The top ten

| Rank | Idea | First step | Add to |
|---|---|---|---|
| 1 | A1 Two on the call | Add "bring the person who should approve the first result" to the booking form, the confirmation and the run sheet; book anyway if they insist | 04-sales-enablement |
| 2 | A2 First approval goes to someone else | Write the Forge task (Build tasks, T1) and attach it to plan item 1 | 08-onboarding-and-signup, 06 |
| 3 | B6 Plugin in the official directory | Set the repo's homepage and README intro, then submit to Anthropic's directory | 14-directories |
| 4 | B1 Keyword alerts | Create eight F5Bot alerts routed to the Growth agent's inbox | 02-prospecting, 13-content-and-social |
| 5 | D1 Every post through an approval card | Route Leo's next five drafts through cards in the founders' workspace | 13-content-and-social |
| 6 | F1 Weekly build log | Pick this week's handoff in the founders' workspace and post it Friday | 13-content-and-social |
| 7 | B3 One programme manager, one batch | Each founder lists his real accelerator contacts in the outreach log this week; send one email per listed programme | 02-prospecting, 03-cold-email |
| 8 | C1 One handoff, told in full | Second Catch may be named (B1); a founder checks the wording with them at their next Thursday check-in | 07-launch, 16-customer-research |
| 9 | D3 Setup dossier | Add "dossier ready" as a step before each booked call | 04-sales-enablement |
| 10 | A5 "Who else" at week four | Add the question to the week-4 script | 16-customer-research, 06 |

### 4. Considered and rejected

| Idea | Why not |
|---|---|
| Lifetime deal or AppSumo-style launch | The plan does not discount to acquire (§7); it attracts the wrong buyer and no team. |
| Agents that reply in Reddit or HN threads on their own | Breaks the plan's rule that agents never publish; communities ban it; one bad reply costs the channel. |
| Our own Discord or Slack community | A second place to talk, outside the product we sell; C3 puts the community inside Lightbulb instead. |
| Buying a colliding domain or renaming | Costs money and time for a search problem 05 can fix with copy and structured data. |
| A public address that reaches our CEO agent (was D2) | Not yet; the shared channel per team is the line (C15). |
| Paid newsletter or podcast sponsorship now | Plan §8 gates spend behind a channel that already works for free; we have none yet. |

## Build tasks

**T1. The first approval asks who should decide.** Today: approval cards go to workspace members only; nothing asks who else should decide. Change: on the first approval card in a one-person workspace, add "Ask someone else to decide" with an email field; the invite email names the decision and opens the card after sign-up. Why: a reason to join (finding 2). Measure: team formed within 48 hours, split by invites that carry a card.

**T2. Agent email footer.** Today: agent mail carries no line about Lightbulb (unverified; check the outbound template). Change: one plain footer line naming the agent, the company and Lightbulb, with a UTM link; on by default, owner can switch it off. Why: E2. Measure: sign-ups with `utm_source=agent_email`.

**T3. Plugin repo as a door.** Today: `lightbulb-plugin` has no homepage URL and a technical README. Change: homepage `https://thelightbulb.company/?utm_source=github&utm_medium=plugin`, a three-line "What is Lightbulb" intro, a link to `/request-setup`. Why: B6. Measure: setup requests with that source.

**T4. Copyable workflow (later).** Today: no way to copy another team's routine and approval rule. Change: a starter that creates the routine, the agents' room membership and the approval rule in one step. Why: C4. Measure: first artifact in the first session, for workspaces that used a starter.

## Measures

- Setup requests by source, with new sources: `plugin`, `agent_email`, `f5bot`, `aitinkerers`, `accelerator:<name>`, `hackathon:<name>`.
- Teams formed within 48 hours, split by setup calls with two people versus one (A1) and invites that carried a card (A2).
- First handoff in week one for teams from each new source.
- Week-4 answers to "who else" (A5): names given, and setups booked from them.
- B1: alerts per week, replies posted, conversations, setups.

## Founder decisions (13 Oct 2026)

- C4: Ask for the second person at booking; book anyway if they insist; the first approval card is the invite.
- C15: No public address to our CEO agent yet; the shared channel per team is the line (D2 idea dropped).
- D14: Opt-out footer on customers' agent mail, on by default; check whether it already exists first.
- B10: Hermes and Paperclip may be named as "built on", never as the product's name.
- C13: Each founder lists real accelerator contacts in the outreach log this week.
- C14: Mail a light bulb after a team's first handoff, from the founder who set them up (E3 adopted).
- D1, D2 (product doors): An outsider on an Anyone-with-the-link session gets "Ask to join"; the owner's Allow also makes them a workspace member; on by default per session (F3).
- D4 (product doors): The public session page is the only no-account look.

## Sources

- Plan: `docs/business-plan.md` §5, §7, §8, §9, §10, Engineering/Product items 1, 2, 4, 6.
- Context: `.agents/product-marketing.md` (v7). Workstreams 01, 03, 04, 05, 06, 10, 12 in this folder.
- Code on `origin/main`: `packages/claude-plugin/README.md`, `.claude-plugin/marketplace.json`; CLAUDE.md `AGENT_EMAIL_DOMAIN`. GitHub API for `TheLightbulbCompany/lightbulb-plugin` (13 Oct).
- Live site: https://thelightbulb.company (13 Oct).
- [First Round: Slack's launch strategy](https://review.firstround.com/from-0-to-1B-slacks-founder-shares-their-epic-launch-strategy)
- [First Round: Linear's path to product-market fit](https://review.firstround.com/linears-path-to-product-market-fit/)
- [Lenny's Newsletter: Superhuman](https://www.lennysnewsletter.com/p/superhumans-secret-to-success-rahul-vohra)
- [Notion: new template gallery](https://www.notion.com/blog/new-template-gallery); [nogood: Notion growth strategy](https://nogood.io/2022/01/21/notion-growth-strategy)
- [Raycast Series A](https://raycast.com/blog/series-a); [TechCrunch on Raycast](https://techcrunch.com/?p=2240096)
- [Replit Teams for Education](https://blog.repl.it/teams-for-education)
- [PostHog: how we got here](https://posthog.com/handbook/story)
- [eWeek on Cursor](https://www.eweek.com/news/anysphere-cursor-ai-startup/); [Warp launch post](https://www.warp.dev/blog/introducing-warp)
- [Claude Code docs: create a marketplace](https://code.claude.com/docs/en/plugin-marketplaces); [Unite.AI on Codex plugins](https://www.unite.ai/openai-adds-plugin-marketplace-to-codex/)
- [AI Tinkerers](https://nyc.aitinkerers.org/all_cities?m=r&page=4); [F5Bot](https://f5bot.com/)
- [Inc42: TheLightbulb.ai](https://inc42.com/company/thelightbulb-ai/); [Companies House 02855611](https://find-and-update.company-information.service.gov.uk/company/02855611)
- Skill reference: marketing-ideas (139 ideas; guerrilla-marketing three-rule direct-mail test).
