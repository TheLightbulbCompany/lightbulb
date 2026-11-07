# 07 · Launch: Show HN, Product Hunt and the plugin listings, for inbound in stage 1

## Purpose

Moves the stage-1 goal "inbound launched" and feeds the ten founding teams with setup requests that cost founder time, not money. Serves Go To Market, "Launch for inbound, in this stage", and Engineering/Product item 4 (a shared session is a door into the workspace).

## What we know

| Fact | Source | Confidence |
|---|---|---|
| The plan launches Show HN and Product Hunt "as soon as the website brief has shipped and a session link brings a viewer into the workspace", with the founding teams' handoffs as the story. Prasiddha owns HN, Product Hunt and the plugins; Leo X and Reddit; Aden LinkedIn. | `docs/business-plan.md`, Go To Market | high (first-party) |
| Today's site: H1 "Your team and your agents, in one place."; no "Request a guided setup" button; `/pricing` and `/request-setup` return 404. | curl of thelightbulb.company, 13 Oct | high (first-party) |
| The public session page (`/s`) shows an "Anyone with the link" session as a live terminal, view only, with no sign-in, and carries no ask for an outsider. Item 4 (the same "Ask to join" teammates have, opened to outsiders) is not built. | `apps/frontend/src/app/s/page.tsx` on origin/main | high (first-party) |
| The `lightbulb` plugin is public at `TheLightbulbCompany/lightbulb-plugin` (0 stars), installs in Claude Code and Codex from that repo, and is in neither official directory. | origin/main `packages/claude-plugin/README.md`; `gh repo view` | high (first-party) |
| Funnel, 30 days to 12 Oct: 227 product-page visitors → 18 sign-up clicks → 20 trials → 10 first agent replies → about 5 active outside workspaces a week. No one pays list. One team is inside the wedge (Second Catch). | plan §9 | high (first-party) |
| Show HN rules: the thing must be something people can run or try. Sign-up pages and reading material are off topic. "Make it easy for users to try your thing out, ideally without barriers such as signups". "Please don't ask friends to upvote or comment." | [HN Show HN guidelines](https://news.ycombinator.com/showhn.html) | high |
| Product Hunt's own guide: "12:01 am Pacific Time is the best time to launch"; a third-party hunter has "no discernible advantage"; you "cannot ask people directly to upvote". | [PH launch guide](https://www.producthunt.com/launch/guide) | high (vendor's own page) |
| Product Hunt featured share: 4% of launches in 2026 (6% in 2025); 885 of 14,605 launches featured in the last 30 days. | [hunted.space Pulse](https://hunted.space/product-hunt-pulse) | low (one tracker; a second tracker shows similar numbers) |
| Anthropic's directory takes plugins from a GitHub repo via the developer portal at claude.ai/directory/manage, runs validation plus a security scan, and a reviewer publishes the first version. The repo must be public to go live. | [Submit your plugin](https://claude.com/docs/plugins/submit) | high |
| ChatGPT and Codex share one public plugin catalog. OpenAI documents a review-based "Submit plugins" flow; we did not confirm whether it is open to us yet. | [ChatGPT plugins docs](https://learn.chatgpt.com/docs/plugins) | medium; submission status unverified |

## Findings (ranked by impact)

**1. Do not launch yet. Three of the five gate items fail today, and the one that matters most to HN is item 4.** HN wants something a reader can try without signing up. Our only no-sign-up surface is the public session page. Today that page is a dead end: a terminal with no way into the workspace. Until item 4 ships, an HN reader who likes the session has nowhere to go but the homepage, which still has no setup door. Launching now spends our one Show HN on a funnel that converts 6% of visitors to a sign-up click (plan §9).

**2. "Multiplayer" and "people plus agents" lose on HN. A concrete handoff wins.** On the Zed Delta thread ([HN 49276574](https://news.ycombinator.com/item?id=49276574), 679 points, 259 comments) the top skeptic wrote "Coding is a single-player game" ([SwellJoe](https://news.ycombinator.com/item?id=49279669)). Another wrote "Multiplayer agent thingy is a VC keyword these days" ([bitlad](https://news.ycombinator.com/item?id=49279872)). A third said "multiplayer AI" is "the buzz word for next year" ([bearjaws](https://news.ycombinator.com/item?id=49277435)). Even the positive comment there valued one concrete case: open the thread behind a sloppy PR and "see how the results came about" ([vipshek](https://news.ycombinator.com/item?id=49277029)). Egregore's Show HN, titled "Shared memory and coordination for multiplayer Claude Code", got 5 points ([HN 47806427](https://news.ycombinator.com/item?id=47806427)). Lody's "Local-first workspace for teams and coding agents" got 2 ([HN 49450167](https://news.ycombinator.com/item?id=49450167)). The best-scoring 2026 entry in this space we found shows a thing you can see working: "Real-time dashboard for Claude Code agent teams", 77 points, with a first post full of build notes ([HN 47602986](https://news.ycombinator.com/item?id=47602986)). That is our shape: lead with the session link and one handoff, not the category. Confidence: high (first-party HN data, three threads).

**3. HN's objections to "agents with your accounts" are trust objections, not feature objections.** In the Grok Bot thread ([HN 49261514](https://news.ycombinator.com/item?id=49261514), 351 points), the top replies were "so like OpenClaw ???", "they all seem...kinda the same", and anxiety about agents "running non stop with access to all your accounts". Our answers already exist: approval cards, one shared record, any model, an isolated runtime per workspace. The reply playbook below leads with them. Confidence: medium (one thread, consistent with the r/startups quote in `.agents/product-marketing.md`).

**4. Timing on HN matters less than the title. On Product Hunt, being featured matters more than timing.** Our pull of all 37,623 Show HN posts from January to September 2026 (Algolia HN API) found that 2.9% reached 50 points. Weekday made little difference (2.6% to 3.5%; weekends slightly better, with less competition). Posts submitted 15:00–18:00 UTC (08:00–11:00 PT) did best at 3.7%, and 06:00–09:00 UTC did worst at 1.8%. On Product Hunt, about 4% of launches are featured, and unfeatured launches are not ranked. Grok Bot's listing, hunted by Chris Messina, shows 0 upvotes and "not featured" ([hunted.space](https://hunted.space/product/grok-bot)). A small Claude Code session tool, ccshare, was featured and placed #17 with 122 upvotes ([hunted.space](https://www.hunted.space/product/ccshare)). We found no Product Hunt launch for Egregore or Lody (unverified that none exists). Treat PH as a credibility and backlink event, not the source of teams. Confidence: HN timing medium (first-party, one method); PH low.

**5. The plugin directories are the one launch surface that keeps working after launch day.** A listing in Anthropic's directory puts the plugin in `/plugin` Discover for people who already run Claude Code, which is exactly the wedge (plan §5). The Codex catalog is shared with ChatGPT. Submissions need review, so they must start two weeks before launch day. The plugin's README today talks to installers, not to someone who has never heard of Lightbulb. Confidence: high for Anthropic's process; Codex submission status unverified.

## Recommendation

Launch for inbound when the five-item gate below is all green, not on a date. The order is: plugin listings submitted on D−14, then Show HN on a Tuesday at 08:00 PT (15:00 UTC), then Product Hunt self-hunted on the Thursday of the same week at 00:01 PT. Each surface sends people to the same two doors: request a guided setup (primary) and start free (secondary). Every link carries a UTM. The HN post leads with a live, view-only session link a reader can open without signing up, plus one real handoff from a founding team. It does not use the word multiplayer, and the first comment gives our honest numbers.

**Rejected:**
- *Launch now, fix as we go.* We get one Show HN for this product, and HN shows what it thinks of a sign-up wall. The session page would be a dead end.
- *Product Hunt first.* Only 4% of launches are featured. PH's audience is broad, and our buyer is a founder with Claude Code open, who reads HN and `/plugin`.
- *A hunter.* PH says there is no advantage, and the Grok Bot listing with a well-known hunter was not featured.
- *Lead with "six AI employees".* On HN that reads like the Grok Bot thread ("they all seem the same"). The session link and the handoff are the parts nobody else in the thread can show.

## Deliverable

### A. The readiness gate (all five must be green; Prasiddha signs off)

| # | Must be true | Today (13 Oct) | Owner | Check |
|---|---|---|---|---|
| 1 | Website brief shipped: headline, "Request a guided setup" as the primary button, "Start free" as the second, team-shaped steps, FAQ | Not shipped (H1 differs; no setup button) | Prasiddha (workstream 10) | Open the homepage in a private window |
| 2 | Seat billing and the trial checkout are built, then `/pricing` exists: $50 for up to 5, $20 a member from the 6th, 30 days free with no card (founding teams put a card on file at setup, first charge on day 31, price locked for two years), credits or bring your own | 404; billing not built | Prasiddha | `curl -I /pricing` returns 200 after the checkout works end to end |
| 3 | A session link joins the workspace: the `/s` page offers "Ask to join" (the owner taps Allow, which also makes the outsider a member) | View-only terminal, no ask for outsiders | Prasiddha (item 4) | A signed-out stranger goes from a session link to a seat in the workspace in under two minutes |
| 4 | Two founding teams with a real handoff we may show (a person approved or continued an agent's work): Anna and Second Catch, named. Nobody else until three teams pay. | One wedge team (Second Catch); Anna cleared | Aden; founders check wording with each team | A screenshot each, vetted by a founder |
| 5 | Guided-setup form live with "How did you first hear about us?", and UTMs captured at sign-up | `/request-setup` 404 | Prasiddha (workstreams 08/09) | A test request with `utm_source=hn` shows in the Monday readout |

**SLC check (from the launch skill).** *Simple:* one job, which is to share an agent's work with your team and let someone pick it up. *Lovable:* "lovable" means two teams will say so on the record (gate 4). *Complete:* the session link → workspace → handoff path works end to end (gate 3). Anything not on this list, such as seat billing, the App Store app or agent voice, is "one more feature" and does not hold the launch.

### B. ORB channel map

| Type | Channel | Role at launch | Leads to |
|---|---|---|---|
| Owned | thelightbulb.company (home, /pricing, /request-setup) | Every link lands here | Setup request / trial |
| Owned | Founding teams' shared channels, the founders' email | Thank-you and follow-up | Teams formed |
| Rented | Hacker News (Show HN) | The main event | Session link → workspace |
| Rented | Product Hunt | Credibility, backlink, PH newsletter if featured | Home |
| Rented | Claude Code directory, Codex catalog | Always-on discovery for the wedge | Plugin → workspace sign-in |
| Rented | X, r/ClaudeAI, r/ClaudeCode (Leo); LinkedIn (Aden) | Each founder's own post on their own network, posted by them. No vote asks, no direct HN or PH links. | Home with UTM |
| Borrowed | Founding teams' own posts about their handoff | Proof in someone else's voice | Home with UTM |

### C. Show HN

**Title** (73 characters): `Show HN: Lightbulb – Share a Claude Code session by link, then hand it off`

**URL:** a live "Anyone with the link" session page that Prasiddha runs on a clean public repo (the plugin repo) all day. It opens with no sign-in, which meets the Show HN rule. The viewer cap is raised after a load test, or a fallback page is ready, and the page shows "full / N people watching" regardless. Before posting, check that it has no secrets, no `.env` and no customer data.

**Body (150 words; put it in the submission's text field if the form accepts text with a URL, otherwise make it the first paragraph of the first comment):**

> Hi HN, I'm Prasiddha. My cofounders and I run most of our engineering through Claude Code and Codex. The work kept getting stuck in one person's session: I'd spend an hour with an agent, then re-explain all of it to a cofounder so they could carry on.
>
> Lightbulb is our fix. Turn on sharing in the Mac app (or `/lightbulb:share`) and the session gets a link. Anyone with the link can watch the terminal live, no account. People in your workspace can send it a message, ask to join, or pick it up when you stop. A stranger can ask to join too, and I decide. The link above is my session right now.
>
> The session sits inside a team workspace: channels where your people and AI agents work together. An agent can draft the PR, a teammate approves it, and the record of who did what stays in one place. Spend, sends and deploys wait for a person.
>
> Mac app plus web. 30 days free; the card goes on file at your setup session.

**First comment (the honest numbers; posted within a minute):**

> Some context and numbers, since this is early.
>
> Where we are (a founder vets every number before this is posted): 57 accounts, 46 workspaces, about five outside teams active in a given week, and no one paying list price yet. Most workspaces have one person, which taught us the product only works when a second person shows up. So the session link is now also the way into the workspace.
>
> One real handoff: Anna — [one sentence: the agent did X, the cofounder approved/continued Y]. Second Catch — [same].
>
> How it works: each workspace gets its own isolated cloud runtime, so its agents keep working when nobody's laptop is open. Agents use Lightbulb-managed models, your ChatGPT account or your own API key. The plugin is open at github.com/TheLightbulbCompany/lightbulb-plugin.
>
> What doesn't work yet: [the two honest gaps on launch day].
>
> Pricing: 30 days free, then $50 a month for up to five people and $20 a person from the sixth. Price locked for two years. Happy to answer anything, including "why not just Slack".

### D. Product Hunt

- **Name:** Lightbulb
- **Tagline (45/60):** Your team and your AI employees, in one place
- **Description (≤260):** A team workspace where AI employees work in your channels. Mention one, it does the work, and the result comes back where everyone can approve it or pick it up. Share a Claude Code or Codex session by link. Any model. $50 a month for up to five people.
- **Maker comment:**

> Hi Product Hunt, I'm Prasiddha, one of Lightbulb's three founders.
>
> We built this because we were the problem. Our agents did real work, but each piece of it sat in one person's chat. I was the one who relayed it all: what the agent did, what my cofounder's session found, what was waiting on a yes.
>
> In Lightbulb, AI employees (a CEO, Engineering, Finance, Growth and others) are members of your workspace, with their own memory, email and tools. You mention one in a channel. The work comes back where the team can see it, and anything that spends, sends or deploys waits for a person to approve. You can share a Claude Code or Codex session by link, and a teammate can pick it up.
>
> Anna used it to [handoff, one line]. We're early: a handful of teams, set up by hand. If you run a team of 3 to 15 and already use agents, request a guided setup and one of us will wire your first workflow with you. Questions and hard objections welcome.

- **Gallery captions (five):**
  1. "Mention an agent in a channel. The work comes back where the whole team can see it."
  2. "Share a Claude Code session by link. Anyone can watch; your team can pick it up."
  3. "Spend, sends and deploys wait for a person. Approve in one tap."
  4. "Six AI employees from day one, each with its own memory, email and tools."
  5. "Any model: Lightbulb-managed, your ChatGPT account or your own key. $50 for up to five people."

### E. Founder reply playbook (HN and PH comments)

Rules: reply within 15 minutes for the first 8 hours. Each reply is one to four sentences. Concede what is true. No links in replies except to docs or pricing. Never ask anyone to upvote or comment, on any channel (both platforms forbid it, and HN detects it).

| Objection | Reply |
|---|---|
| "Coding is single-player." | Agreed for the typing part. The trouble starts after: the cofounder who inherits the PR has no idea how the agent got there. The link exists for that moment, not for two cursors in one editor. |
| "Multiplayer is the VC buzzword of the year." | Fair, and we don't use it. The concrete version: I stop, a teammate opens the same session, and the history is there. |
| "Basic Claude does all that." | For one person, yes. Lightbulb earns its place when a second person needs to see, continue or approve the work. If that never happens on your team, it isn't for you yet. |
| "So like OpenClaw / Grok Bot / Slack agents?" | Grok Bot gives each person their own computer that nobody else sees. Slack adds an assistant to people-chat. Here the agents are members of the team: one shared record, any model, and $50 a month covers the first five people. |
| "I'd never give agents all my accounts." | You connect only the tools you choose. Spend, sends, deploys and hires wait for a person, and every action is on the shared record. Each workspace runs in its own isolated runtime. |
| "We already have Slack / iMessage." | Keep them. Lightbulb is where the agents work. Your team joins the rooms where something is happening. |
| "I don't need six agents." | You talk to one, the CEO. The others are its team, and you can switch them off. |
| "Too complicated / slowed us down." | One early user said exactly that, and we agree it can get messy. That's why we set up the first ten teams by hand: one real workflow, one finished result, not a tour. |
| "Will it do things without asking?" | Not the things that matter: spend, sends, deploys and hires stop for an approval card. |
| "Lock-in? Where's my data?" | Bring your own model or key. [Export answer per workstream 10's FAQ; do not improvise.] |
| "Your numbers are tiny." | They are. That is why we're here and why we set teams up by hand. |
| Generic praise or obvious bot replies | Do not engage. Flag spam to the mods; never seed comments. |

### F. Two-week calendar (D = Show HN Tuesday; starts only when the gate is green)

| Day | Prasiddha (HN, PH, plugins) | Leo (X, Reddit) | Aden (LinkedIn) |
|---|---|---|---|
| D−14 (Tue) | Submit plugin to Anthropic directory; submit to Codex catalog if open (launch goes ahead without it if it is closed or its hooks rule disqualifies the plugin); rewrite plugin README opening for strangers | Draft X thread + r/ClaudeCode post (value-first: "how we hand off Claude Code sessions") | Handoff screenshots from Anna and Second Catch, vetted by a founder |
| D−10 | Record 60 s demo (session link → join → pick up → approval); PH gallery | Post the Reddit how-to (no launch link), reply to "handoff" threads per workstream 02 | Draft founder post |
| D−7 (Tue) | Gate review with all three; go/no-go | Warm list: DM people who asked about handoffs, sharing the problem, not a vote | Same on LinkedIn |
| D−3 | Pick the clean repo; dry-run the public session for 8 hours; test UTMs end to end | Schedule nothing; posts are manual | Prepare the week-of setup calendar (slots for setup calls) |
| D−1 | Prepare the PH draft (self-hunt, scheduled 00:01 PT Thu); preload replies | — | Block D and D+2 for setup calls |
| **D (Tue)** | 08:00 PT: Show HN; first comment within 1 minute; in the thread until 18:00 PT | 10:00 PT: X thread about the handoff with a homepage link (`utm_source=x`), no HN link | 10:00 PT: LinkedIn post (`utm_source=linkedin`) |
| D+1 | Reply to late comments; email every person who requested setup | Reply to X replies | Run setup calls |
| **D+2 (Thu)** | 00:01 PT: PH live, maker comment posted; replies all day | Share PH page with the line "we launched; feedback welcome" (no vote ask) | Same on LinkedIn |
| D+3 | Short post-mortem in the GTM channel: sources, setup requests, objections heard | r/ClaudeAI follow-up only if the D−10 post earned it | Setup calls |
| D+6 (Mon) | Monday readout: launch cohort by source | — | — |
| D+7 | Thank-you email to everyone who engaged; plugin listing status chased | Thread: "what we learned from launch week" | Founder post: one handoff story |

## Build tasks (Forge-ready)

1. **Seat billing and the trial checkout** — Built first; everything below that quotes the price waits for it. Change: $50 for up to 5, $20 a member from the 6th, a Stripe checkout with a 30-day trial and a card at setup for founding teams, first charge on day 31; self-serve keeps the no-card month and adds a card when it ends. Why: gate 2; no copy may rely on billing that does not exist. Measure: a test setup ends with a card on file and a trial end date.
2. **`/pricing` page** — Today 404. Change (after task 1): one plan, $50 for up to 5, $20 a member from the 6th, 30 days free with no card (founding teams: card on file at setup, price locked for two years), credits at provider price ×1.2 or bring your own, FAQ. Why: gate 2; every launch reply about price points here. Measure: pricing → setup/trial clicks.
3. **`/request-setup` form with source capture** — Today 404. Change: five fields plus "How did you first hear about us?", required on this form (Hacker News, Product Hunt, Claude Code/Codex plugin, X, Reddit, LinkedIn, a friend, other + text); store the UTM; emit `setup_requested` with source. Why: gate 5; launch measures. Measure: setup requests by source.
4. **UTM and referrer persistence through sign-up** — Today three quarters of traffic is unattributed (plan §9). Change: keep first-touch UTM and referrer from landing (including `/s`) through Clerk sign-up onto the workspace; the same source question at sign-up is optional. Measure: share of trials with a known source over 80%.
5. **Plugin README written for a stranger** — Today it opens with what the plugin contains. Change: the first three lines say what Lightbulb is, link to the homepage with `utm_source=plugin`, and give a 30-second install path; keep the desktop-app warning. Why: it is the directory listing's landing text. Measure: plugin-sourced workspaces.
6. **Plugin directory submissions** — Today the plugin is in neither directory. Change: run `claude plugin validate`, then submit via claude.ai/directory/manage; check whether OpenAI's "Submit plugins" flow is open and submit if so. Measure: listing live; installs on the directory's Usage tab.
7. **Session page: Ask to join** — Not built here. The join-link task is dead. Workstream 06 (T3 for the outsider's "Ask to join", T6 for the "full / N people watching" state and the cap raise after a load test) builds it; gate 3 waits on those.

## Measures

Tied to the Monday numbers (setup requests by source, teams formed, first handoff in week one):

| Measure | Window | Read from | Bar for "worth it" (our estimate, not a benchmark) |
|---|---|---|---|
| Setup requests by source (hn, producthunt, plugin, x, reddit, linkedin) | D, D+7 | `setup_requested` events + form answer | 5 or more from launch sources in the week |
| Trial starts by source | D, D+7 | Sign-up source + UTM | Exceeds the previous 30 days' 20 trials |
| Teams formed (2+ people within 48 h) from the launch cohort | D+7, D+14 | team-formed event | 3 or more |
| First handoff in week one, launch cohort | D+7 to D+14 | handoff event | 1 or more |
| Joins started from a session link | D to D+7 | workstream 06 T3 event | Any above zero proves the loop |
| Reach (context only) | D | HN points, PH rank, Vercel Analytics visitors | HN ≥ 50 points (2.9% of Show HNs reach it) |

Founding-team slots filled from launch traffic is the number we report beside these, because the stage is judged on teams, not upvotes.

## Founder decisions (13 Oct 2026)

- B2: Anna and Second Catch are the named teams; nobody else until three teams pay.
- B3: the Show HN first comment publishes the real numbers.
- B4: our own numbers and founder-workspace screenshots may be used; a founder vets every number and screenshot before it is posted.
- B11: the Show HN link is Prasiddha's live terminal on a public demo repo, cap raised after a load test or a fallback page ready.
- E13: launch without the Codex directory listing if it is closed or its hooks rule disqualifies the plugin.
- D1 / D3 / D4: the session page uses "Ask to join", not a join link; the public session page is the only no-account look.
- A8 / A4: seat billing and the trial checkout are built before `/pricing` and any launch copy that quotes the price.
- F4: each founder writes a 60-word bio this week.

## Sources

- `docs/business-plan.md` (§5, §6, §7, §9, Engineering/Product item 4, Go To Market); `.agents/product-marketing.md` v7
- origin/main: `apps/frontend/src/app/s/page.tsx`, `packages/claude-plugin/README.md`, `.github/workflows/sync-claude-plugin.yml`
- Live site checks, 13 Oct 2026: thelightbulb.company `/`, `/pricing`, `/request-setup`
- Hacker News: [Show HN guidelines](https://news.ycombinator.com/showhn.html); [Delta](https://news.ycombinator.com/item?id=49276574); [SwellJoe](https://news.ycombinator.com/item?id=49279669); [bitlad](https://news.ycombinator.com/item?id=49279872); [bearjaws](https://news.ycombinator.com/item?id=49277435); [vipshek](https://news.ycombinator.com/item?id=49277029); [Egregore Show HN](https://news.ycombinator.com/item?id=47806427); [Lody](https://news.ycombinator.com/item?id=49450167); [Agents Observe Show HN](https://news.ycombinator.com/item?id=47602986); [Grok Bot](https://news.ycombinator.com/item?id=49261514)
- Show HN timing: our analysis of 37,623 Show HN posts, Jan–Sep 2026, via the [HN Algolia API](https://hn.algolia.com/api) (3-day windows capped at 1,000 hits each, so a few busy windows may be undercounted)
- [Lucas F. Costa, How to do a successful HN launch](https://www.lucasfcosta.com/blog/hn-launch) (title and repo-link advice; secondary)
- Product Hunt: [launch guide](https://www.producthunt.com/launch/guide); [hunted.space Pulse](https://hunted.space/product-hunt-pulse); [Grok Bot listing](https://hunted.space/product/grok-bot); [ccshare listing](https://www.hunted.space/product/ccshare)
- Plugins: [Anthropic, Submit your plugin](https://claude.com/docs/plugins/submit); [Claude Code, Install and manage plugins](https://code.claude.com/docs/en/discover-plugins); [ChatGPT/Codex plugins](https://learn.chatgpt.com/docs/plugins); [Unite.AI on Codex plugins](https://www.unite.ai/openai-adds-plugin-marketplace-to-codex/) (secondary)
- Grok Bot launch facts: [x.ai](https://x.ai/news/grok-bot-more-plans)
