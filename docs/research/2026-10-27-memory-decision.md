# Memory: what Lightbulb takes from Agent Beacon, what it leaves, and what the network should become (27 October 2026)

Written after two passes the same day: `2026-10-27-agent-beacon-pass.md` (Beacon as built, with every number sourced) and `2026-10-27-network-memory-inventory.md` (everything Lightbulb has built, designed or researched for the network and memory, with file paths). This file is the decision layer: use cases first, then what fits, then the sequence and the numbers we would publish. It supersedes the 21 Oct plan of record ("integrate Beacon's telemetry, don't build it; defer session-to-skill"), which predates both the messaging repositioning and Beacon's pivot to memory.

## 1. The people and the moments

| Who | Moment | What they have from Lightbulb today | What Beacon gives them today |
|---|---|---|---|
| **A. One developer, one or two computers, Claude Code and Codex** (the network's free user) | A new session starts from zero; a correction made in Claude Code is explained again in Codex; the fix found yesterday is found again today | Nothing. The engine carries messages only and deletes text once read (inventory §2) | Per-project lessons, reviewed by hand, served over MCP; cloud optional but preselected |
| **B. Two people on one network** (the activation event the plan counts) | "Your agent remembers what you did. It has no idea what your cofounder spent four hours figuring out" (the homepage) | The agents can ask each other, if both are online inside 24 h. Nothing persists | Nothing. Memory is one project, one account; team memory is an enterprise promise |
| **C. A workspace team** (3–15 people, paying) | The room's agents know the team's facts; the laptop sessions do not; what a coding session learned never reaches the room | Workspace memory for Hermes agents only (AgentCore: team and private scopes, Memory tab, confirm/share/delete); no path from a laptop session in or out (inventory §3) | Nothing for the room; per-person lessons on the laptop |
| **D. The founders' roadmap** | A shared memory layer, cost savings, recommended automations (speedrun study, docs/marketing/27) | The record in rooms; spend only for inference through the gate; no recommendation | Capture of every session; a token leaderboard; no recommendation |

Beacon serves A and nobody else. Lightbulb serves C partly and A and B not at all. The gap that matters is A and B, and both sit on the network.

## 2. What Beacon actually is, in one paragraph

Capture everything an agent does into one local log (39 harnesses). Optionally send a redacted slice to a third-party scorer that returns three probabilities and no text. A trace clears two constants and becomes a candidate; a person or the agent writes the lesson; a person approves it. Approved lessons are served by substring matching over SQLite, capped at five, or written into a SKILL.md. Scope: one project locally, one account in the cloud. No benchmarks exist; the only published instrument measures capture fidelity. 2,872 stars, $5.5M seed, nine public profiles, three public traces (all by the CEO). Full detail and sources in the Beacon pass.

The part that produces value is small: an agent drafts a lesson at the end of a session and a person approves it. The capture pipeline and the scorer are what make the demo and the funding story; the lesson text is written by the agent either way.

## 3. What fits Lightbulb and what does not

**Take**

1. **Memory as reviewed lessons with a kind and provenance** (workflow, correction, gotcha, convention; which agent, computer, repo, branch, when). Our AgentCore facts have none of that; the Memory tab can only confirm, share or delete a sentence with no source. The lesson model is the right unit for coding work.
2. **The agent distills at the end of a session.** Our hooks already fire on Stop and SessionEnd for Claude Code, Codex, Cursor, Grok CLI and OpenCode. Asking the agent "what did you learn that the next session should know" at that moment costs nothing and needs no scorer and no trace egress.
3. **Serve over the tools the agent already has.** One tool spec feeds the engine's MCP and the hosted MCP today (`network_tools.json`). `recall` and `remember` belong in it, so Claude Code, Codex, Cursor, OpenCode, Grok CLI, the Claude app, ChatGPT and the workspace agents all get the same memory.
4. **Local-first storage.** `state.db` is already SQLite on the person's computer with 0700 permissions. Memory lives there too. SQLite FTS5 (BM25) is a strictly better matcher than Beacon's substring-AND and is one table away.
5. **A review gate, but ours is the room.** Beacon's gate is a CLI command plus a scoring API. Ours is a person saying yes in a room, which the workspace already does for plans and spend ("Needs you").
6. **Publish numbers.** Beacon publishes none. The nine targets in the Beacon pass §5 are measurable from events we already emit or can add in a week.

**Take, but in our form, not theirs** (revised the same day after the founder's read: capture, an evaluator, a cloud default and the security facts are all worth having)

1. **Session capture.** Yes, and locally first. Claude Code's hooks already hand us the transcript path and every tool call; Codex, Cursor and Grok CLI write session stores to disk; OpenCode has a plugin seam. Recording what a session did is the raw material for lessons, for "where did the tokens go", and later for "this handoff repeated four times" (the recommendation feature). The rule to publish: the record is written on the person's computer; it leaves only when the person signs in and chooses what to send (full, or metadata-only), and it goes to Lightbulb, never to a third party. Beacon's form (cloud preselected, 30-day backfill, research use unless opted out) is the part to leave.
2. **An evaluator.** Yes, and ours. Beacon sends a redacted 80-event slice to TypeSafe or SubQ for three probabilities. We run managed inference behind our own gate; a cheap model reads an uploaded session and drafts the lesson, the skill or the finding, and a person approves it in the room. First-party, metered, and the draft carries the evidence (which events) instead of a score.
3. **Cloud by default.** Yes, as a suggestion at install, not as a silent default. `lightbulb login` already exists and is free. The wizard offers sign-in first ("your sessions and memory follow you across computers; the Claude app and ChatGPT can reach your agents") with local-only one keypress away, exactly as Beacon's wizard does, minus the backfill and the training clause. The network keeps working without an account, which is what the homepage promises.
4. **Security facts, not security positioning.** Publish everything (`/security` already does; the four-step "what leaves your machine" band is open): keys never leave, relays see ciphertext, nothing is captured off the machine without sign-in, approvals before spend. Do not sell to the CISO; Beacon's press calls it runtime security while its launch calls it memory, and the two audiences do not meet. Our buyer is the founder of a 3–15 person team; the security page is what lets that founder say yes.

**Leave**

1. Thirty-nine harness integrations. Five agents cover our buyer; Beacon spent a day on sixty rule fixes for breadth.
2. The third-party scorer and the trace egress that comes with it.
3. Research and training use of customer sessions.
4. SIEM packs, detection rules, blocking as a product. If an enterprise asks, the audit export is a query over rows we already keep.
5. The public token leaderboard (below).

**Why Beacon has a token leaderboard if it is for solo developers.** It is a launch-growth mechanic, not a product feature: public profiles ranked by lifetime tokens ("#1: 385.6B"), with a share button, meant to make Cloud sign-ups visible on X and to seed the "Discover" marketplace of public traces. It ties to the free Cloud line "See where your token spend goes." Six days after launch it has nine profiles, three of them staff, and three public traces, all by the CEO. The lesson for us is the opposite feature: a private spend view per agent, person and room inside the workspace (the finance layer, PRs #4432/#4433), not a public ranking.

## 4. Free network versus cloud: the honest ladder

Beacon's own ladder, from its pricing page (27 Oct): Open Source $0 (local, "nothing leaves your machine") → Beacon Cloud $0 (every agent and machine in one timeline, token spend, 50 findings, session search over MCP, share a session by link) → Pro $20/mo (unlimited findings, **memories over MCP**, skills from sessions, evidence trail, alerts) → Enterprise (block or approve, org-wide findings, fleet rollout, SSO, own bucket). Note where memory sits: local memory is free in the CLI; cloud memory is paid.

Lightbulb's ladder, proposed:

| Tier | Price | What it holds | Why it sits there |
|---|---|---|---|
| **Network, no account** | $0 | Messaging between your agents and your teammates' agents; local capture of your own sessions (`lightbulb sessions` to browse them on your disk); personal lessons and recall on this computer; the hooks for five agents | This is the install that spreads. It must work with nothing leaving the machine, or the trust story is gone. Costs us nothing to serve |
| **Cloud, free account** (`lightbulb login`) | $0 | Your sessions and lessons follow you across your computers; one timeline of every agent and machine; tokens per agent and computer; the Claude app, ChatGPT, Muse and Grok Bot reach your agents (already shipped); share a session by link; a capped number of drafted lessons per month from your uploaded sessions (the evaluator) | The on-ramp to a team: an account is what an invite attaches to. Storage and a capped evaluator are cheap; uncapped drafting is not, so the cap is the paid line, as it is for Beacon's 50 findings |
| **Workspace** | $50/mo up to 5 people, $20 a member after (from `getPricing()`) | The head: rooms people read; agents that stay on; team memory with approval cards and provenance in the Memory tab, recalled by every agent the team admits (rooms, laptops, hosted apps); unlimited drafted lessons and skills shared across the team; spend budgets and meters per room and agent; later, recommended automations from the record | Everything that needs a second person or a server that never sleeps. This is where the plan puts revenue (ten teams with a card by 4 January) |
| **Enterprise** (later) | custom | SSO, own bucket and region, org-wide policy and audit export, procurement | The 21 Oct note's warning stands: the trust stack (SOC 2 Type II, SAML, SCIM) costs months, not features. Not before the ten teams |

Two honest tensions the founder should see:

- **Solo developers get a lot for free** (capture, personal memory, sync across computers, hosted-app reach). Beacon charges $20 for cloud memory; we would not. The reason is the plan: the ICP is the team, and a solo developer with everything working is the person who invites a cofounder. If solo cloud costs more than we expect (storage, evaluator), the lever is the drafting cap, not a solo paywall.
- **Capture changes the homepage promise.** Today the network page can say "never files or the agent's conversation". With local capture that stays true for teammates (they still see presence, not transcripts), but the privacy page must say plainly that sessions are recorded on your own disk and what sign-in sends. The Termly rewrite (open, founder signs) has to carry this before capture ships.

**On reading Beacon's cloud app with REA.** Beacon Cloud has a free tier. I cannot create the account (that is a line I do not cross), but if the founder signs up, an hour with the bundle analyser on the web app would map its client routes and feature flags, which is how we confirmed Ramp Glass's Router, limits and automations. The two things worth learning: whether any team or org memory exists in the client (the founder's HN claim), and what the "dreaming" findings flow actually shows. It is worth that hour only if the answer would change the ladder above; today it would not, so it is optional.

## 5. What to build, in order

Each phase ships on its own and is measurable. Nothing here is specced yet; the spec comes after the founder rules on §6.

| Phase | What | Where it lands | Measure |
|---|---|---|---|
| 0. Personal memory on the network | A `memory` table in `state.db` (FTS5); records with kind, title, body, tags, repo, branch, agent, computer, created, confirmed; `remember` and `recall` in the shared tool spec; a Stop/SessionEnd hook that asks the agent for lessons; a SessionStart recall that injects the top five for this repo; `lightbulb memory list|show|forget|export`; counts only in telemetry | cli repo; `network_tools.json` | T7 time to first useful context; recall events with hit/miss; retention of installs after week two (today: 3 daily computers) |
| 1. Memory follows the person across computers | Records marked "mine" replicate to the person's other computers over the network (members with the same `lightbulb login` subject, or an explicit pairing for people who never sign in) | cli repo | share of records recalled on a computer other than the one that wrote them |
| 2. Team memory through the head | `share` sends a candidate to the workspace room as an approval card; approved records are held by the cloud member, shown in the Memory tab with provenance, recalled by Hermes agents and by every signed-in laptop session; confirm/correct/delete by any member; decide whether AgentCore facts stay beside them or are retired | monorepo (`network_member.py`, `routers/memory.py`, Memory tab, Hermes plugin) | T1 cross-person recall share; T5 hygiene; T8 review without a third party |
| 3. Numbers in public | Pre-registered metrics in the repo; a labeled retrieval set built from real rooms; Beacon's matcher run as the baseline; a page under /network | docs, `/network` | T2, T3, T4, T6, T9 |
| Later, if lessons are too thin | Opt-in local capture of a session's commands and edits, never leaving the computer, feeding the distill step | cli repo | — |
| Later, from the record | "This handoff repeated four times; make it a standing task" (the speedrun study's recommendation feature); token reporting from Claude Code and Codex into Usage (the finance layer, PRs #4432/#4433) | monorepo | — |

## 6. Decisions for the founder

0. **Build or buy the recall.** `2026-10-27-past-dev.md` (same day) makes the case for past.dev behind our existing memory-provider seam for the cloud tiers, with a two-week pilot on our own workspace first, and for keeping the no-account tier to messaging and local capture. If agreed, phases 0 and 1 below collapse into "hooks ingest under the person's identity" and phase 2 starts now.

1. **Is memory on the free network the right line?** The alternative is memory only in the workspace (the 27 Oct framing read literally). My recommendation is personal memory free, team memory paid, for the reasons in §4; if the founder wants memory wholly in the paid head, phase 0 still ships but only for signed-in computers.
2. **AgentCore's future.** Keep it as the fact store for rooms and add lessons beside it, or retire it once team lessons exist. Cost today is unmeasured (AWS list: $0.75 per 1,000 records a month plus $0.50 per 1,000 retrievals); the recall query is known to be polluted by routing JSON. I lean towards retiring it in phase 2 and would want a cost read first.
3. **Capture.** Confirm "lessons cross, traces do not" as a rule we publish, or leave the door open to opt-in local capture.
4. **The numbers.** Which of T1–T9 go on the /network page, and whether we publish the retrieval set.

## 7. Sources

`docs/research/2026-10-27-agent-beacon-pass.md`; `docs/research/2026-10-27-network-memory-inventory.md`; `docs/research/2026-10-21-agent-beacon-reference.md`; `docs/research/2026-10-21-enterprise-agent-beacon-ramp-glass.md`; `docs/marketing/26-messaging-repositioning.md`; `docs/marketing/27-speedrun-neighbours-2026-10-27.md`; the CLI repo at v0.6.5; the monorepo at `5cb2c07f9` (27 Oct).
