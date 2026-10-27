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

**Leave**

1. **Thirty-nine harness integrations.** Five agents cover our buyer; Beacon spent a day on sixty rule fixes for breadth.
2. **Full-trace capture.** Prompts, tool calls and edits recorded to disk are the privacy objection we win on today ("never files or the agent's conversation"). Lessons cross; traces do not. If lessons prove too thin, local-only capture is a later, opt-in step.
3. **A third-party evaluator.** Two constants with one anecdote, and every evaluated trace leaves the machine. The agent writes the lesson; the person reviews.
4. **Cloud by default.** Beacon preselects Cloud, backfills 30 days and trains on data unless opted out. Ours stays on the computer until a person shares; what crosses the network is ciphertext.
5. **Security and SIEM positioning, blocking, the token leaderboard.** Not our buyer, not our page.

## 4. The product line this draws

The founder's framing on 27 Oct: the network is messaging with nothing to open; the workspace is the head (a room people read, memory, agents that stay on, a lead agent). Memory splits along the same line:

- **On the network, free: memory that follows the person.** A lesson written by Claude Code on one laptop is there for Codex on the other. Stored on the person's computers, served by the engine, shared with nobody unless the person shares it. This is exactly Beacon's free offer, without the account, the cloud default or the scorer, and it makes the network something you keep installed after the first message. It costs nothing to serve.
- **In the workspace, paid: memory the team shares, with the head.** A lesson shared to the team goes through the room (an approval card, the same as spend), appears in the Memory tab with its provenance, is recalled by every agent the team admits (Hermes agents in rooms, laptop sessions on the network, the hosted apps), and can be confirmed, corrected or deleted by any member. The cloud member is one more peer on the network and the always-on holder of the team's memory, so a lesson shared at 23:00 is there at 09:00 whether or not the sharer's laptop is open.

Two-person networks without a workspace: they can message each other's agents and each keeps their own memory. Sharing a lesson across people is the workspace. This is the free/paid line the plan already draws (a single person has something to do on day one; the second person is the activation event) and it keeps the network out of the race to zero (docs/marketing/26: don't sell the network).

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

1. **Is memory on the free network the right line?** The alternative is memory only in the workspace (the 27 Oct framing read literally). My recommendation is personal memory free, team memory paid, for the reasons in §4; if the founder wants memory wholly in the paid head, phase 0 still ships but only for signed-in computers.
2. **AgentCore's future.** Keep it as the fact store for rooms and add lessons beside it, or retire it once team lessons exist. Cost today is unmeasured (AWS list: $0.75 per 1,000 records a month plus $0.50 per 1,000 retrievals); the recall query is known to be polluted by routing JSON. I lean towards retiring it in phase 2 and would want a cost read first.
3. **Capture.** Confirm "lessons cross, traces do not" as a rule we publish, or leave the door open to opt-in local capture.
4. **The numbers.** Which of T1–T9 go on the /network page, and whether we publish the retrieval set.

## 7. Sources

`docs/research/2026-10-27-agent-beacon-pass.md`; `docs/research/2026-10-27-network-memory-inventory.md`; `docs/research/2026-10-21-agent-beacon-reference.md`; `docs/research/2026-10-21-enterprise-agent-beacon-ramp-glass.md`; `docs/marketing/26-messaging-repositioning.md`; `docs/marketing/27-speedrun-neighbours-2026-10-27.md`; the CLI repo at v0.6.5; the monorepo at `5cb2c07f9` (27 Oct).
