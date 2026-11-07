# Marketing execution: the brief every workstream follows

This folder holds the execution layer under `docs/business-plan.md`. One file per workstream, written by a research agent and read by the founders. The plan decides *what*; these files decide *how*, with evidence.

## The goal

Ten founder-led teams of 3–15 people, each running two or more AI agents, set up by a founder and paying $50 a month at list by May 2027, with a second person in each workspace within 48 hours and a completed handoff in week one; inbound launched; the five Monday numbers reported from real events. Every workstream exists to move one of those.

## The brief (read by every agent before starting)

1. **Sources.** The plan (`docs/business-plan.md`), the shared context (`.agents/product-marketing.md`), the research under `docs/research/`, the code on `origin/main` (never the stale local checkout; use `git show origin/main:<path>` and `git grep ... origin/main --`), and the live site at thelightbulb.company. Memory notes, older `.md` drafts and hearsay are not sources. If a fact is not in one of these or on a page you opened, say "unverified".
2. **Invoke the skill.** Call the named marketing skill(s) with the Skill tool first and follow their method; do not improvise a method.
3. **Due diligence.** Open the pages you cite; quote under 15 words; link every claim; label confidence (high: 3+ independent sources or first-party data; medium: 2; low: 1). Check competitor and vendor claims against their own pages. No fabricated numbers, names or quotes. No scraping behind logins, no CAPTCHA bypass, public business contact routes only.
4. **Customer data.** Never read or store customer messages, transcripts or personal data. Aggregates and code are fine. Nothing from production lands on this Mac beyond what is already in the plan.
5. **Voice.** Plain, specific, calm; short sentences; customer language; the plan's words (team, AI employees, channel, mention, approve, pick up); never "multiplayer", "AI-native", "orchestration", "bot", "prompt" in public copy. No exclamation marks, no hype adjectives.
6. **Output: one file**, `docs/marketing/<nn>-<slug>.md`, under 2,500 words plus tables, in this structure:
   - **Purpose** (two lines: which plan goal this moves, which section it serves)
   - **What we know** (facts from the sources, with links and confidence)
   - **Findings** (what the research says, ranked by impact)
   - **Recommendation** (the decision, in plain words; alternatives rejected and why)
   - **Deliverable** (the actual asset: the list, the emails, the page copy, the tracking plan, the spec; ready to use)
   - **Build tasks** (anything the product or site must change, written as Forge-ready tasks: title, what today, what changes, why, measure)
   - **Measures** (how we'll know it worked, tied to the Monday numbers)
   - **Open questions for the founders** (decisions only they can make)
   - **Sources**
7. **Hand back** a 10-line summary: the recommendation, the deliverable's location, the build tasks, the open questions. Write nothing else in the repo; commit nothing.

## Workstreams (all delivered 13 Oct 2026)

| # | File | Skills | Serves | What it holds |
|---|---|---|---|---|
| 00 | [00-decisions.md](00-decisions.md) | | The founders | 72 decisions with a recommendation each, answered by the founder on 13 Oct (table at the end); the answers are applied in every file below |
| 01 | [01-marketing-plan.md](01-marketing-plan.md) | marketing-plan | The 90-day calendar | Week-by-week calendar with owners, pipeline model (30 touches/week → ~10 set up, ~3 paying by 12 Jan), dependency table, Monday readout template, cut order |
| 02 | [02-prospecting.md](02-prospecting.md) | prospecting | The lead list | 26 named teams (9 Warm), ten pain signals, three qualification questions, search log, stop rule |
| 03 | [03-cold-email.md](03-cold-email.md) | cold-email | The asks | Two first touches (A/B on the ask), day 3/8/16 sequence, per-source observation lines, X/Reddit reply, LinkedIn DM |
| 04 | [04-sales-enablement.md](04-sales-enablement.md) | sales-enablement | The calls | Battle card, nine objections, the 45-minute run sheet with "if it fails", never-promise list |
| 05 | [05-seo.md](05-seo.md) | seo-audit, schema, ai-seo | The site | Audit (stale story, wrong facts, ten thin pages, brand collision), JSON-LD for home and pricing, AI-search baseline, tasks B1–B11 |
| 06 | [06-referrals-and-loops.md](06-referrals-and-loops.md) | referrals, marketing-loops | Network effect | Loop map, the existing "Ask to join" opened to outsiders on the session page, first-session invite, referral credit deferred to stage 2, tasks T1–T8 |
| 07 | [07-launch.md](07-launch.md) | launch | Inbound | Five-item readiness gate, Show HN and Product Hunt copy, reply playbook, D−14 to D+7 calendar, HN timing data |
| 08 | [08-onboarding-and-signup.md](08-onboarding-and-signup.md) | onboarding, signup | Items 1–3, 9 | Activation definition, sign-up audit, first-session script, away-email rules, two welcome emails, 14 tasks in order |
| 09 | [09-analytics.md](09-analytics.md) | analytics, attribution | Item 7 | Tracking plan, identity rule, UTM convention, source question, tested HogQL for the Monday readout, data guards, 11 tasks |
| 10 | [10-website-copy.md](10-website-copy.md) | copywriting, cro | The pages | Homepage copy in the plan's order, /pricing, sign-up two doors, /request-setup form, meta, 11 tasks by component |
| 11 | [11-competitors.md](11-competitors.md) | competitors | Comparison pages | Six `/compare/*` pages with verified facts (13 Oct), sourcing table, update rule |
| 12 | [12-lifecycle-emails.md](12-lifecycle-emails.md) | emails | Owned channel | Nine emails from the CEO agent's address with triggers and suppression; sweeper design |
| 13 | [13-content-and-social.md](13-content-and-social.md) | content-strategy, social | Always-on reach | Editorial line, 36-row calendar, per-platform rules, three blog outlines, drafting routine |
| 14 | [14-directories.md](14-directories.md) | directory-submissions | Listings | Ranked surfaces (plugin directories first), the one description, four-week calendar, plugin-repo fixes |
| 15 | [15-trial-to-paid.md](15-trial-to-paid.md) | paywalls, churn-prevention | Money | No guarantee; day-28 recap card, reading-only day 31, one pause per company, 90-day keep then deletion notice, founder save script, 11 tasks |
| 16 | [16-customer-research.md](16-customer-research.md) | customer-research | Learning | Setup discovery guide, churn-call script, week-4 question, notes schema, cadence |
| 17 | [17-ideas.md](17-ideas.md) | marketing-ideas | What we don't know | 32 ideas, impact × effort, top ten with first steps, five rejections |
| 18 | [18-build-order.md](18-build-order.md) | | The board | Every product and site task from 01–17, duplicates merged, in dependency order (35 tasks, T01–T35), each with today / change / why / measure / size; the source for the Forge board |
| 19 | [19-plan-vetting.md](19-plan-vetting.md) | | The plan | Claim-by-claim check of the business plan (443 claims: 28 wrong, 9 stale, 67 unverified), contradictions, cuts and keeps per section; the wrong and stale items were corrected in the plan on 13 Oct |
| 20 | [20-marketing-plan-long-2026-10-13.md](20-marketing-plan-long-2026-10-13.md) | marketing-plan | Reference | The 13-section AARRR version of the plan (24k words), kept for its idea bank and ops stack. Superseded on 14 Oct by `docs/marketing-plan.md`, the regimented version (live Google Doc `107VLG5j9JK6bQnecjJgClCV0wnJdNibJF-kKDfkkVTM`, Part 2; Aden's 13 Oct additions are Part 3 there and in `21-aden-additions-2026-10-13.md`): product track (35 tasks due Thu 15 Oct), measurement track, outreach track with named leads per founder, calendar, launch Thu 15 Oct |
| 26 | [26-messaging-repositioning.md](26-messaging-repositioning.md) | product-marketing, competitors, cro, copywriting, seo-audit, ai-seo, launch, analytics | §5–§6, §8, Part 2 §2/§5/§7 | The 26–27 Oct repositioning: messaging for people and their AI agents, the free network as the front door, no competitor named on the site; what shipped (PR #4430), the launch shape, the Termly rewrite, the measures |
| 27 | [27-speedrun-neighbours-2026-10-27.md](27-speedrun-neighbours-2026-10-27.md) | competitor-profiling, competitors, product-marketing, copywriting | §6, §7, §8, 11-competitors | The a16z speedrun SR007 neighbours (Infragrid, Contxt, Alia, ChatOverflow, Sonder, Proper Motion, Space, AgentCat, Codos, Bounty and others): investor sentence vs public page, and the plan redlines for shared memory (the record is the memory), recommended automations (Q1, from repeated handoffs) and saving money (seat-free, approvals) |

Research behind the plan: `docs/research/2026-10-13-employee-voice-and-consumer-agents.md`, `2026-10-13-socially-aware-agents.md`, `2026-10-13-messaging-datasets.md`, `2026-10-13-muse-walkthrough.md`, `2026-10-09-grok-bot-teardown.md`.

**Build tasks** live in each file's "Build tasks" section, written for Forge. Standing rule from the founder (13 Oct): build the real thing before the site depends on it; seat billing and the trial checkout come first, then `/pricing`, the comparison pages and any copy that quotes a price. Overlaps to build once: the activation events (08, 09, 06, 12, 15 all need `team_formed` and `handoff_completed`), `/request-setup` with the source question (01, 02, 09, 10, 14), `/pricing` (05, 10, 11, 15), the session-page doors (06 supersedes 07's join-link task).
