# 02 · Prospecting: the founding-team lead sheet

*Workstream 02. Method: `marketing-skills:prospecting`, Demand-signal branch. Research date: 2026-10-13. Every row is a **potential customer based on public signals**. None of them has said they are interested, and none has consented to contact.*

## Purpose

This workstream serves the plan's goal of ten founding teams (3–15 people, 2+ agents, $50 a month by May 2027). It serves §5 (the wedge and the qualification list) and §8 stage 1 (founder-led outreach).

## What we know

- **The wedge** (business plan §5; `.agents/product-marketing.md`): founder-led teams of 3–15 people. They run two or more agents across two functions, and one person routes context between people and agents by hand. Their DIY glue is a shared markdown repo, Linear or Confluence as the bus, n8n, and copy-paste. Confidence: high (first-party).
- **The plan's lead HN quote is competitor copy.** "Your agent remembers what you did… no idea what your co-founder just spent four hours figuring out" is Egregore's founders pitching Egregore ([HN 47027197](https://news.ycombinator.com/item?id=47027197), 2026-02-15). It shows the pain is real, but it is not a buyer speaking. Confidence: high (opened the item).
- **All four Reddit quotes in the plan are verified, and two of them come from tool builders.**
  - "The hard part so far isn't 'can the agent write code?'" is the original post by Pairon's developer, a competitor ([r/ClaudeAI](https://www.reddit.com/r/ClaudeAI/comments/1w7bx1r/), 2026-09-04).
  - "Every new Claude Code session starts from zero" opens the launch post for samemind, a solo-built memory tool ([r/ClaudeCode](https://www.reddit.com/r/ClaudeCode/comments/1vr4zg0/), 2026-08-17).
  - "Handoffs are where this breaks" is a commenter running an n8n pipeline ([r/smallbusiness](https://www.reddit.com/r/smallbusiness/comments/1w2eq75/has_anyone_here_successfully_set_up_ai_agents/p6t23zh/), 2026-08-30). The thread's original poster is a one-founder agency.
  - "Require a named owner to approve anything hard to unwind" is a commenter ([r/startups](https://www.reddit.com/r/startups/comments/1vf8m2e/worst_ai_replacement_stories_i_will_not_promote/p1qg5i6/), 2026-08-04).
  - With Egregore, three of the plan's six customer quotes are vendors describing their own market.
  - The §5 description of today's glue also traces to commenters: "a git repo of markdown that every teammate clones" ([fresh_squeezed_code](https://www.reddit.com/r/ClaudeAI/comments/1w7bx1r/), an indie builder), Linear as the bus ([traderjames7](https://www.reddit.com/r/ClaudeAI/comments/1wdf533/)), and a Confluence page (ehosca, same thread).
  - Confidence: high (each thread opened 2026-10-13).
- **The cited research files hold no prospect threads.** `docs/research/2026-10-13-socially-aware-agents.md` and `-messaging-datasets.md` link datasets only, so every lead below comes from new searches.
- **The space is crowded with builders.** The sweep found about 40 products that sell shared context, session handoff or team memory for coding agents (list under Sources). Many of the loudest "pain" posts are launch posts. Confidence: high (40+ items opened).

## Findings (ranked by impact)

1. **Pain is plentiful, but few buyers can be named.** Hacker News and Reddit hold many first-person quotes, for example "My team relies heavily on a homegrown /resume skill" ([HN](https://news.ycombinator.com/item?id=49745346), 2026-09-17) and "none of it syncs" ([r/smallbusiness](https://www.reddit.com/r/smallbusiness/comments/1wzbc1q/small_teams_using_separate_personal_claudechatgpt/), 2026-10-06). Almost none of these authors name their company. Across about 1,700 screened posts, comments, issues and repos, **28 companies** qualify with a public business route. **None scores Hot**, because no single source proves team size, two named agents, a dated pain and a contact route together.
2. **The best named leads show the DIY glue, not a complaint.** Brainwaves built its own Slack bot running Claude Code and Codex, with Linear as "a nice substrate shared across dev machines… and shared chat". Gem Wallet's AGENTS.md defines a handoff routine for Claude Code and Codex. Driver AI ships one plugin for both agents, with a handoff-docs folder. This is §5's "what they do today", visible in public.
3. **Small-company hiring posts are a timing signal.** CABLE ("Team of six… all ship with coding agents"), TrakPro (team of 5, "Daily Claude Code or Codex user"), Mondrio (4 people) and Pagelove (Claude Code and Codex) are hiring now. A new hire is exactly the moment when the context lives in one founder's head.
4. **Non-engineers using agents is the strongest fit signal.** CABLE's "energy specialists and go-to-market colleagues" and CircleIt's "feedback from the rest of the team" both describe agent work crossing roles. That is the Lightbulb case, not the code-only case the session tools cover.
5. **GitHub issues hold first-person, dated, named requests.** [openai/codex#48339](https://github.com/openai/codex/issues/48339) (Airntell, 14-person team, 2026-09-26) asks for "shared agent context and handoffs" across teammates' accounts. Competitors (Joxo, Bellows) already reply in those threads.

## Recommendation

- **Work the six Warm leads by hand this week** (the nine, less Foremerge, Feyn's Critic and Driver AI, which are not approached for now). Cross-role teams like CABLE go first. Each gets one source-based question through its own public route. The cold-email workstream (03) writes the copy.
- **Stop expecting public-signal mining to fill the list of ten.** At about 26 nameable companies from four channels, it gives 2–4 setup calls at best. The rest of the founding teams have to come from the founders' networks and from inbound (workstream 07). Rejected alternative: scaling the same sweep, because the bottleneck is identifying the author, not the volume of posts.
- **Turn unattributed signals into presence, not outreach.** The founder replies in his own name, in threads where a reply is normal, with a useful answer and no pitch. People who reply back identify themselves. A DM follows only after the person asks for help in the thread. Rejected alternative: DMing pseudonymous authors. That breaks the brief's public-route rule and the channel's norms.
- **Fix the plan's evidence.** Replace or relabel the Egregore quote, and re-find the four Reddit quotes or drop them.

## Deliverable

### Scoring used

- **Score** (skill rubric, adapted to demand signals):
  - **Hot**: team 3–15 evidenced, 2+ agents evidenced, a pain or handoff signal dated after 2026-06-15, and a public business route.
  - **Warm**: fit mostly evidenced, but the signal is softer or one fact is inferred.
  - **Cold**: loose fit, no pain signal, or an unverified route.
- **Conf.** (skill definition): high = 2+ independent sources or the official business page; medium = one credible source plus consistent evidence; low = incomplete.
- **Fit**: the skill's 0–100 demand-fit score (pain 25, fit 25, timing 20, reachability 15, evidence 15).
- **Last verified**: 2026-10-13 for every row. HN links are `news.ycombinator.com/item?id=…`.

### Lead sheet: 28 companies (0 Hot · 9 Warm · 19 Cold)

| # | Company | Person (role) | Team size (evidence) | Agents (evidence) | Pain or signal (quote, link, date) | Public contact route | Score | Conf. | Fit | Unverified |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | XYZ by FORMATION GmbH, Berlin | Jilles van Gurp (CTO, per his [HN profile](https://hn.algolia.com/api/v1/users/jillesvangurp)) | Small; [about page](https://formationxyz.com/about) names 3+ people | "Agentic workflows" ([about](https://formationxyz.com/about)) | "invite a colleague to some ongoing coding session" ([49283492](https://news.ycombinator.com/item?id=49283492), 2026-08-13) | Inquiry form / Book a Call on [formationxyz.com](https://formationxyz.com) | Warm | medium | 69 | Headcount; which agents |
| 2 | CABLE (cable.energy), Sydney | Theo (named contact in post) | "Team of six" ([49928430](https://news.ycombinator.com/item?id=49928430), 2026-10-02) | "all ship with coding agents" (same post) | Engineers, energy and GTM staff on one "agent-driven development platform" (same post): timing, hiring now | theo@cable.energy (their domain, in their post); [jobs.cable.energy](https://jobs.cable.energy) | Warm | medium | 68 | Agent names; Theo's role; email not seen on site |
| 3 | Brainwaves (getbrainwaves.com) | grrowl (HN poster; GitHub org [brainwavesio](https://github.com/brainwavesio)) | Unverified | Claude Code + Codex inside their Slack bot ([48943863](https://news.ycombinator.com/item?id=48943863)) | Linear as "a nice substrate shared across dev machines…" (same, 2026-07-17) | hello@brain-waves.io, mailto on [getbrainwaves.com](https://getbrainwaves.com) | Warm | medium | 66 | Team size; person's role |
| 4 | Airntell (drone surveillance) | Inusha De Silva (embedded developer) | 14 on [team page](https://www.airntell.com/team) | Codex ([codex#48339](https://github.com/openai/codex/issues/48339)) | "teammates using their own Codex accounts and local machines" (2026-09-26) | Pilot-request form on [airntell.com](https://www.airntell.com/) | Warm | high | 62 | Second agent; decision-maker |
| 5 | GPTree (Foremerge) | Nick (naw103, building Foremerge) | Unverified | Claude Code, Codex, Cursor ([49789356](https://news.ycombinator.com/item?id=49789356)) | "we run several coding agents across our team on one repo" (2026-09-21) | [github.com/naw103/foremerge](https://github.com/naw103/foremerge) | Warm | low | 52 | Company site; also sells a coordination tool (adjacent). Not approached for now (neither customer nor partner) |
| 6 | Driver AI | Adam Tilton (co-founder) | 15 ([YC page](https://www.ycombinator.com/companies/driver-ai)) | Claude Code + Codex ([driver-sdlc-plugin](https://github.com/driver-ai/driver-sdlc-plugin)) | Plugin runs on Linear plus a handoff-docs folder (undated) | [driver.ai/contact](https://driver.ai/contact) | Warm | medium | 50 | Dated pain; at the size cap; sells context to agents (adjacent). Not approached for now (neither customer nor partner) |
| 7 | Feyn Labs (Critic; chonkie.ai) | Shreyash (snyy) | About 4, inferred from [demo UI](https://www.critic.run) | Codex + Claude ([49834098](https://news.ycombinator.com/item?id=49834098)) | "harder to loop people in on the impact of a PR" (2026-09-24) | support@chonkie.ai on [chonkie.ai](https://www.chonkie.ai) | Warm | low | 50 | Team size; Critic is adjacent. Not approached for now (neither customer nor partner) |
| 8 | Gem Wallet | No named person | 6 human committers since June ([repo](https://github.com/gemwalletcom/wallet)) | Claude Code + Codex ([AGENTS.md](https://github.com/gemwalletcom/wallet/blob/main/AGENTS.md)) | AGENTS.md defines a "handoff" and a sweep "each teammate runs" (undated) | X @GemWallet via [GitHub org](https://github.com/gemwalletcom) | Warm | low | 45 | Headcount; decision-maker; email |
| 9 | Fazeshift | Chris (VP Engineering) | About 11 engineers ([49923384](https://news.ycombinator.com/item?id=49923384), 2026-10-01) | Conductor, Linear, "multiple frontier models" (same) | "We use coding agents heavily ourselves." (same): hiring | [fazeshift.com](https://fazeshift.com) | Warm | low | 44 | Total headcount may exceed 15; no contact page found |
| 10 | Pagelove | samj (HN poster) | Founders only; first non-founder hire ([49529763](https://news.ycombinator.com/item?id=49529763), 2026-09-01) | "Claude Code and Codex do substantial implementation work" (same) | No pain stated; hiring = timing | [careers page](https://ats.pagelove.com/roles/founding-software-engineer/) | Cold | medium | 58 | Headcount; poster's role |
| 11 | Proximal Energy | skrhee (engineer) | Unverified | AGENTS.md + skills; tools not named | "different engineers wanted to have different things in Agents.md" ([48619419](https://news.ycombinator.com/item?id=48619419), 2026-06-21) | [GitHub org](https://github.com/ProximalEnergy) | Cold | low | 55 | Company link inferred from a package name |
| 12 | Sunspell (venture lab, NYC) | dscarp23 | "We are six people" ([49525270](https://news.ycombinator.com/item?id=49525270), 2026-09-01) | "build with Claude Code daily" (same) | No pain stated; hiring | [careers page](https://sunspell.fm/careers/founding-engineer) (did not render) | Cold | low | 50 | Second agent; contact |
| 13 | TrakPro (Ireland) | Not named | "Small team of 5" ([48000122](https://news.ycombinator.com/item?id=48000122), 2026-05-03) | "Daily Claude Code or Codex user" required (same) | No pain stated; was hiring 3rd engineer | HN post only (site did not load) | Cold | low | 42 | Site; contact; signal is old |
| 14 | Autumn (YC S25) | johnyeocx | 5 ([YC page](https://www.ycombinator.com/companies/autumn)) | Claude Code, Codex, OpenCode across the team ([48601140](https://news.ycombinator.com/item?id=48601140)) | "check how much AI spend each of us" (2026-06-19): spend, not handoff | Discord only on [useautumn.com](https://useautumn.com) | Cold | medium | 48 | Business email |
| 15 | Mondrio (Brazil) | Not named | "lean (4 people)" ([46858502](https://news.ycombinator.com/item?id=46858502), 2026-02-02) | "all-in on Claude Code" (same) | No pain; old | [mondrio.com/contact](https://www.mondrio.com/contact) | Cold | low | 35 | Second agent |
| 16 | Merge (mergeoa.com) | harshithl1777 (co-founder) | 5 founders ([49213554](https://news.ycombinator.com/item?id=49213554), 2026-08-07) | Not named | "reviewing code that another engineer's AI has generated" (same) | Contact page on [mergeoa.com](https://mergeoa.com) | Cold | low | 36 | Quote concerns hiring tests, not their own team |
| 17 | Keystroke (YC W24) | dallinbentley (co-founder) | 4 ([YC page](https://www.ycombinator.com/companies/keystroke)) | Cursor + in-house Slack agent ([49160376](https://news.ycombinator.com/item?id=49160376)) | None on handoff (2026-08-03) | Contact link on [keystroke.ai](https://keystroke.ai) | Cold | medium | 33 | Pain |
| 18 | TesterArmy (YC 2026) | okwasniewski (co-founder) | 6 ([YC page](https://www.ycombinator.com/companies/testerarmy)) | Claude Code in demo | None found | [tester.army/contact](https://tester.army/contact) | Cold | medium | 30 | Pain; second agent |
| 19 | HyperProbe (YC 2026) | shailendraht (co-founder) | 8 ([YC page](https://www.ycombinator.com/companies/hyperprobe)) | Product works with Cursor ([49185389](https://news.ycombinator.com/item?id=49185389)) | None found | Book a demo on [hyperprobe.co](https://www.hyperprobe.co) | Cold | low | 26 | Own agent use |
| 20 | CircleIt | mintone | Unverified | Claude Code + Codex plugin ([circleit.ai](https://circleit.ai)) | "the bottleneck has moved to feedback from the rest of the team" ([50020545](https://news.ycombinator.com/item?id=50020545), 2026-10-09) | None readable (address obfuscated) | Cold | low | 30 | Company identity; adjacent |
| 21 | Meridian (NYC) | vipshek | Unverified | Unverified | "most coding agent sessions are user-local" ([49277029](https://news.ycombinator.com/item?id=49277029), 2026-08-12) | None verified | Cold | low | 44 | Company link rests on a 2025 hiring post |
| 22 | Campus Tours Live | Not named | 5 committers since June ([repo](https://github.com/Campus-Tours-Live/backend)) | Claude Code + Codex + Cursor ([AGENTS.md](https://github.com/Campus-Tours-Live/backend/blob/main/AGENTS.md)) | Shared agent setup across 3 tools (undated) | None found | Cold | low | 30 | Site; contact |
| 23 | Modelence (YC S25) | Aram Shatakhtsyan (CEO) | 3 ([YC page](https://www.ycombinator.com/companies/modelence)) | Claude Code plugins ([repo](https://github.com/modelence/modelence)) | None found | support@modelence.com via [GitHub org](https://github.com/modelence) | Cold | low | 33 | Pain; second agent |
| 24 | Dojo Coding Labs | Daniel Bejarano (CEO, per [site](https://www.dojocoding.io)) | Unverified | Own agent; Claude plugin ([repo](https://github.com/DojoCodingLabs/make-no-mistakes-toolkit)) | "Agents write, senior engineers review" (site, undated) | hello@dojocoding.io on own site | Cold | low | 32 | Team size; agency (may resell) |
| 25 | Cauchy | Maria Vechtomova (co-founder) | Unverified | Codex + Claude config ([awow](https://github.com/CauchyIO/awow)) | "Agent code is production code" (site, undated) | [cauchy.io/contact](https://cauchy.io/contact/) | Cold | low | 30 | Team size; consultancy |
| 26 | VOYGR (YC W26) | Yarik (cofounder, CTO) | Unverified | Claude Code + Cursor named in post ([49925202](https://news.ycombinator.com/item?id=49925202), 2026-10-01) | No pain; hiring founding engineer | HN post; YC page | Cold | low | 30 | Team size; route |
| 27 | JCODE Agency (Belgium) | Founder ([u/MyKMDBag34 profile](https://www.reddit.com/user/MyKMDBag34/)) | Unverified; likely founder-only (anti-persona risk) | Four agents passing work "through files" ([post](https://www.reddit.com/r/smallbusiness/comments/1w2eq75/), 2026-08-30) | Agents invented tools and prices; "my manual review caught it" (same) | contact@jcode.store on [jcode.store](https://jcode.store/) | Cold | medium | 40 | Team size; also resells AI agents to small shops |
| 28 | VarynForge | Founder ([u/fresh_squeezed_code profile](https://www.reddit.com/user/fresh_squeezed_code/)) | Unverified; profile says "Indie builder" with a day job | Agent-native MCP tool; agents run from a shared repo ([comment](https://www.reddit.com/r/ClaudeAI/comments/1w7bx1r/), 2026-09-04) | "a git repo of markdown that every teammate clones" (same) | hello@varynforge.com on [varynforge.com](https://varynforge.com/); [GitHub](https://github.com/VarynForge) | Cold | medium | 38 | Whether a team exists; whose "teammates" |

**Below the bar on purpose.** The list is 28 rows against a ceiling of 100. Padding it would break the skill's honesty rule: "no signal, no shortlist".

### Top outreach targets

1. **CABLE.** A six-person team where non-engineers ship with agents, hiring now. This is the cross-role case. Ask: "When an energy specialist's agent session needs an engineer, how does it change hands today?"
2. **XYZ by FORMATION.** The CTO publicly wants to "invite a colleague to some ongoing coding session". Reply through their inquiry form and cite the Delta thread.
3. **Brainwaves.** They built our product's shape themselves (Slack bot, Claude Code, Codex, Linear as the bus). Ask what breaks when a second person continues the bot's work.
4. **Airntell.** A dated, named, first-person request on GitHub from a 14-person team. Reply on [codex#48339](https://github.com/openai/codex/issues/48339) only if the reply is useful on its own terms, or use the pilot form.
5. **Gem Wallet.** Its AGENTS.md describes a hand-built handoff routine. Approach through the public GitHub org.

### Signals with no identifiable company (reply in the thread, as the founder, no pitch)

| Where | Date | Signal (under 15 words) |
|---|---|---|
| [HN 49745346](https://news.ycombinator.com/item?id=49745346) (btown) | 2026-09-17 | "homegrown /resume skill… merge colleagues' sessions" |
| [HN 49368260](https://news.ycombinator.com/item?id=49368260) (superfrank) | 2026-08-19 | "I use Codex, my non-technical co-founder uses Claude" |
| [HN 48848840](https://news.ycombinator.com/item?id=48848840) (snowhy, flagged) | 2026-07-09 | "My cofounder and I each run our own Claude Code agent" |
| [HN 48959477](https://news.ycombinator.com/item?id=48959477) (sreekanth850) | 2026-07-18 | "a team of 3"; Codex seat per member |
| [HN 48997035](https://news.ycombinator.com/item?id=48997035) (teach) | 2026-07-21 | "getting the humans and agents to talk together is really fragmented" |
| [HN 49279530](https://news.ycombinator.com/item?id=49279530) (vadepaysa) | 2026-08-12 | "people on my team tend to work more in isolation with agents" |
| [r/smallbusiness](https://www.reddit.com/r/smallbusiness/comments/1wzbc1q/small_teams_using_separate_personal_claudechatgpt/) | 2026-10-06 | Two cofounders, separate accounts: "none of it syncs" |
| [r/ChatGPTCoding](https://www.reddit.com/r/ChatGPTCoding/comments/1wysqo1/how_is_your_team_standardizing_ai_coding/) | 2026-10-06 | "re-explaining the stack in their prompts every session" |
| [r/ChatGPTCoding](https://www.reddit.com/r/ChatGPTCoding/comments/1x1ks1u/our_coding_agent_got_stuck_in_a_loop_and_burned/) | 2026-10-09 | "small team of 5… coding agent pick up tickets overnight" |
| [r/ChatGPTCoding](https://www.reddit.com/r/ChatGPTCoding/comments/1wz72y2/im_tired_of_reviewing_code_nobody_actually_wrote/) | 2026-10-06 | "20 or so PRs that nobody on my team actually wrote" |
| [r/smallbusiness, TheCryptoBillionaire](https://www.reddit.com/r/smallbusiness/comments/1wzbc1q/) (agency owner per profile) | 2026-10-07 | "6 people all living in their own ChatGPT/Claude bubbles"; Notion as glue |
| [r/ChatGPTCoding, sapplex1](https://www.reddit.com/r/ChatGPTCoding/comments/1wysqo1/) | 2026-10-07 | "a team of four"; AGENTS.md per service (profile says indie iOS dev) |
| [r/ChatGPTCoding, London-Boat](https://www.reddit.com/r/ChatGPTCoding/comments/1wysqo1/) | 2026-10-07 | "what worked for us": AGENTS.md plus a CLAUDE.md pointing at it |
| [r/AI_Agents, QuanTradin](https://www.reddit.com/r/AI_Agents/comments/1wz5soi/) (builds Quantradin) | 2026-10-06 | "six agents, one shared file, one line each per night" |
| [r/ClaudeAI, traderjames7](https://www.reddit.com/r/ClaudeAI/comments/1wdf533/) | 2026-09-11 | Linear issues so Claude and ChatGPT talk "without me having to copy paste" |
| [r/startups, BasicConclusion2093](https://www.reddit.com/r/startups/comments/1vf8m2e/) | 2026-08-04 | "built out a sales team around our Open Claw agent" |

### The three qualification questions (the founder's first reply)

Ask all three in one short message, after mentioning their post:

1. **"Who on your team runs an agent today, and for what?"** This checks for 3–15 people and 2+ agents in recurring use across two functions.
2. **"When one person's agent work has to reach someone else, how does it get there now? Tell me about the last time it went wrong."** This finds the human router and the missed handoff in the last 30 days, in their words.
3. **"If we set it up together, would you bring one teammate and one real piece of work to the first session?"** This checks the second person and the first workflow, and that no procurement gate stands in the way.

Disqualify on: a solo operator, a pair whose work does not overlap, "we want a cheaper Slack", or a buyer of purely deterministic automation (§5).

## Build tasks

1. **Title:** `/request-setup` captures where the lead came from.
   - **Today:** the URL returns 404 on thelightbulb.company (checked 2026-10-13). The plan makes "request a guided setup" the primary button.
   - **Change:** when workstream 10 builds the page, add two optional fields: "Where did you hear about us?" and "Link to a post or thread (optional)". Store both on the request.
   - **Why:** reply-in-thread outreach can only be measured if the lead can name the thread.
   - **Measure:** the share of setup requests that carry a source.
2. **Title:** an outreach log the founders keep (no product change). Track lead, channel, date, reply (y/n), call booked, second person invited. Each founder also lists the accelerators where he has a real contact. Keep it in the founder workspace, not in this repo. **Measure:** reply rate and calls booked per 10 touches.

## Measures

- **Week 1:** 6 Warm leads contacted, 3 replies, 2 setup calls.
- **Weeks 1–4:** 10 useful thread replies from the founder in his own name, and the number of people who answer back with a company name.
- **Monday numbers:** setup calls booked; workspaces with a second person within 48 hours; handoffs completed in week one, attributed by the source field (build task 1).
- **Stop rule:** if fewer than 2 calls come from 6 Warm touches, drop public-signal outreach and put the hours into network intros and the launch.

## Founder decisions (13 Oct 2026)

- C5: Cross-role teams first. CABLE leads the order ahead of pure engineering teams.
- C6: Foremerge, Feyn's Critic and Driver AI are neither customers nor partners for now; revisit at stage 2.
- C13: Each founder lists real accelerator contacts in the outreach log this week.
- Not answered: whether the three vendor quotes in §5 (Egregore, Pairon, samemind) are relabelled or swapped. Still open.
- C2 (public replies in each founder's own name, no pitch; a DM only after a person asks for help in the thread) also applies to the unattributed-signals table.

## Sources

- **First-party:** `docs/business-plan.md` §5; `.agents/product-marketing.md` v7; `docs/marketing/README.md`; thelightbulb.company/request-setup (404, 2026-10-13).
- **Hacker News:** the Algolia API (`hn.algolia.com/api/v1/search`, `/items`, `/users`) with 100+ queries across comments, Ask HN, Show HN and Launch HN after 2025-06. Who is hiring threads June–October 2026 were filtered for 2+ named agents. Threads mined for commenters: Delta 49276574, Skillsync 49743049, Buzz 48995213, Almanac 49511007 and others.
- **Reddit:** search.json through a public page, no login: 113 subreddit-query pairs over 15 subreddits. A follow-up read the comments of 16 threads (the ten signal posts, plus 1w7bx1r, 1wdf533, 1vr4zg0, 1w2eq75, 1r6r9fg and 1vf8m2e) on the public pages, and checked about 20 public profiles. An isolated browser context got a "Prove your humanity" page; it was closed and not bypassed. The page was closed afterwards.
- **GitHub:** code search for `.claude-plugin/marketplace.json` and for CLAUDE.md/AGENTS.md team phrases (about 280 org repos); contributors screened for 3–15 active humans; issue search in anthropics/claude-code and openai/codex. About 450 API calls.
- **Sites and YC pages opened:** formationxyz.com, cable.energy, getbrainwaves.com, airntell.com/team, driver.ai, gemwallet.com, chonkie.ai, mondrio.com, and ycombinator.com/companies (driver-ai, autumn, keystroke, testerarmy, hyperprobe, modelence).
- **Product Hunt and founder blogs:** WebSearch returned nothing usable. Status: unverified, not covered.
- **Competitors seen (not leads):** Egregore, Zed Delta, Lody, Skillsync, Compendium, Capacitor, Ocean, AgentCouch, Parley, Vibivibi, Praxos, Almanac, Forklane, Murmell, Amika, Type.com, Decispher, Rafter, ZeroShot, Backplanes, Superset, Block Buzz, Joxo, Bellows, SageOx, Packmind, sx, Spec Kitty, Traycer, Cyrus, Vilix, DevBoardAI, Dory, LongLeash, Klerm, Hops AI, Hatchdoor, scape.work, Agent Mesh, OpenLore. Joxo and Bellows reply inside openai/codex issues; that is the bar for reply-in-thread presence.
