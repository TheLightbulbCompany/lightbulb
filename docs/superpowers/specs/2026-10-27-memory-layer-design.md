# Memory layer: design (27 October 2026, v3)

Status: draft for the founder. v3 puts memory in the network itself, because the network's job is cross-person, cross-harness, cross-session work and memory is part of that job. v2's AgentCore configuration survives only as the interim store for room facts. v1's custom cloud pipeline is gone. Research: `docs/research/2026-10-27-memory-problem.md`, `2026-10-27-oss-beam-failures.md`, `2026-10-27-past-dev.md`, `2026-10-27-network-memory-inventory.md`.

## 1. What and why

Any agent on the network remembers what any agent on the team learned: across people (Ana's Claude Code knows what Ben's Codex found), across harnesses (Claude Code, Codex, Cursor, OpenCode, Grok CLI, the hosted apps) and across sessions. Free, no account, nothing on our servers. A workspace adds an always-on copy, the room agents, and the Memory tab.

**Goals:** memory on the free network with the same trust story as messages; a source and a date on every fact; disagreements shown, never silently resolved; beat the best published BEAM numbers under the same harness and judge.

**Non-goals:** capturing tool calls, diffs or command output; a memory API for other companies; LoCoMo.

**Anti-goals:** memory that picks a winner between two people; a block before every prompt; a benchmark number from a different harness than the one we compare against; memory on our servers for someone who never signed up.

## 2. What we must fix

From the research, every system loses BEAM points the same way: the same fact stored many times crowds out the one update; an update in other words is not linked to the old value; the replaced value ranks first; two people disagreeing is settled by recency; totals across sessions miss facts. past.dev already scores 93–100 on six of ten abilities; the contest is knowledge update, information extraction, contradiction and multi-session.

## 3. What the network already gives us

From the CLI source (v0.6.5):

- **A replicated, signed log.** The roster (`src/roster.rs`) is a hash-linked graph of signed entries with one root, "replayed deterministically, so every member computes the same team from the same set of entries whatever order they arrived in". Entries are at most 16 KB.
- **Sync between members.** Peers exchange their heads in `hello` and page missing entries with `Frame::Roster { entries, more }` (`src/net/mod.rs`, `src/net/frames.rs`), over QUIC, end-to-end encrypted between member keys; relays see ciphertext.
- **One tool spec for every harness.** `network_tools.json` feeds the engine's MCP server (wired by `setup` into Claude Code, Codex, Cursor, Grok CLI and OpenCode) and the hosted MCP.
- **Hooks at session start and end** for all five harnesses (`src/hook.rs`).
- **Local SQLite** (`state.db`, 0700), already holding the roster.
- **The workspace is a member.** The cloud member embeds the same engine (`src/embed.rs`) inside the workspace runtime through the Hermes bridge, so it receives what members receive and never sleeps.

So team memory is a second log beside the roster, synced the same way, read and written through two new tools. No new service, no server holding free users' data.

## 4. The design

### 4.1 The memory log

Each entry is signed by the member who wrote it:

```
remember  {id, subject, fact, kind (fact|decision|lesson|preference), when (event time), source (harness, repo, session), scope (team|me)}
replace   {id, replaces: <id>, fact, when, ...}        a change: the old entry becomes history
forget    {id: <target>}                              the content is dropped on every copy
```

- **Duplicates cannot pile up.** The writer is shown the existing facts on the subject before writing (4.2), so the same thing again is not written. A repeat that slips through still collapses at read, because the page shows one line per subject.
- **Contested falls out of the graph.** Two `replace` entries pointing at the same fact, or a `remember` that contradicts a current fact without replacing it, mean two people hold different current values. Recall shows both with names and dates. Nobody's entry overwrites anybody's. Whoever settles it writes a `replace` of both.
- **Scope.** `team` entries sync to every member computer. `me` entries sync only to the writer's own computers (the network already knows which computers are the same person's). A member who leaves stops receiving entries; what they already had they keep, as with messages they already read. The privacy page says so.
- **Search** is SQLite FTS5 (BM25) over subject and fact. Measured on the probe set and BEAM 100K. If rephrased updates are missed ("latency" vs "response time"), add small local embeddings, and only then.

### 4.2 Writing: the agent does it

No model runs in the engine. The agents already on the network are the models.

- **`remember`** takes a fact and a subject. Before storing, it returns the current facts on that subject: "Existing: production database = Postgres 14 (Ana, 2 Aug). Is yours a change? Call again with `replaces`." The agent decides update, contest or new, and that is the linking step every system in §2 gets wrong, done by the model that just read the whole conversation.
- **When:** the person says "remember"; and at session end, the Stop/SessionEnd hook asks the agent once for anything the next session or a teammate should know, in a few facts. That is a few hundred tokens of the developer's own agent, nothing billed to us.
- **Which scope:** default `me`. The agent writes `team` when the fact is about shared work (the repo, a decision, a bug) and the person has not turned it off (`lightbulb memory team off`), and says so in its reply ("shared with the team: …"). A wrong team fact is fixed by any teammate's `replace` or `forget`.

### 4.3 Reading

- **`recall(question)`** searches the local copy and returns a page: contested facts first with both sides, then current facts with "was X" history, newest first, packed into a token budget. Local, so it works offline and costs nothing.
- **Session start:** the hook injects the current team and personal facts for this repo, at most ~400 tokens. Then the agent calls `recall` when it needs more. Nothing before every prompt.
- **Questions needing several facts:** the agent calls `recall` again with narrower questions. Agents already do this.

### 4.4 Who sees what

| Who | Memory |
|---|---|
| One developer, no account | Personal memory across Claude Code, Codex and every session on their computers |
| Two people on one free network | Team memory: one person's agent learns it, the other's knows it, with a name and date on it |
| `lightbulb memory` | list, show (history and who), forget, export, `team off` |
| A workspace | The cloud member holds a copy and is always online, so a fact reaches a teammate whose computer was asleep when it was written. Room agents use the same tools through the bridge. The Memory tab reads the cloud member's copy: source, history, contested, edit and forget |
| Room conversations | Interim: AgentCore Memory with the v2 overrides (contested and history rules in the consolidation instructions) keeps extracting room facts as today. End state: the room agents write to the log like any other agent, and AgentCore's long-term memory is retired once the harness shows the log at least as good |

### 4.5 Privacy

The same promise as messages, because it is the same transport. Free memory lives on members' computers and moves only between them, encrypted end to end. We never see it. In a workspace the cloud member holds a copy, as it already holds the workspace's messages. `redact_for_egress`-style secret scrubbing runs before `remember` stores anything. `forget` removes the content from every copy that comes online. The privacy page and the network security doc say this before it ships.

## 5. How we beat the benchmarks

1. **Same harness.** Fork past.dev's MIT-licensed BEAM harness; replace only `past_api.py`. Same ExaBase prompts (hash-checked), same answer and judge model (`openai/gpt-5.6-luna`, reasoning off), 100 results within 8,000 tokens, each question once.
2. **The product path, not a benchmark path.** BEAM has no agent in the loop, so the adapter plays the agent: after each session it gives a model the session and the `remember` tool, exactly as the Stop hook does, with the neighbours `remember` returns. Recall is the real `recall`. What we tune is what ships: the hook's instruction, the tool descriptions, the page format, and whether FTS5 is enough.
3. **Measure the noise first** on the 100K split: rerun past.dev ourselves (~30,000 of our ~149,900 free credits), run today's AgentCore built-in as a second baseline, judge the same answers three times.
4. **Targets** (to test; each ability weighs a tenth):

| Ability | past.dev 1M / 10M | What moves it | Target 1M / 10M |
|---|---|---|---|
| Contradiction | 75.4 / 73.1 | Contested shown with both sides, who and when | 90 / 85 |
| Knowledge update | 85.7 / 92.5 | `replace` linked by the writer; current first, history after | 95 / 95 |
| Information extraction | 84.0 / 68.8 | One line per subject on the page | 92 / 85 |
| Multi-session | 72.8 / 28.2 | Several recalls; budget not spent on copies | 80 / 55 |
| Overall | 90.65 / 85.03 | other six held | ≈94.6 / ≈90.8 |

5. **No overfitting.** Tune on 100K and the probe cases (`docs/research/2026-10-27-past-dev-probe2.py`); run 500K, 1M, 10M and LongMemEval_S once at the end; publish results, prompts, harness commit and cost.
6. **The risk.** BM25 on 10M-token histories may miss facts a semantic index finds. The harness tells us; the fix is local embeddings, not a server.

## 6. Cost

- **Free network: $0 to us.** The person's own agent writes, their computer stores and searches, the network carries it.
- **Workspace:** the cloud member's copy is a few MB in SQLite. Room facts through AgentCore in the interim: ~$6 a month per workspace (v2's numbers: overrides storage $0.25 per 1,000 records, retrieval $0.50 per 1,000, Haiku-class extraction ~$1.50), going to roughly zero when AgentCore long-term memory is retired.
- **Benchmarks:** the agent stand-in runs a small model over each session: 155M tokens for the full suite, ~$50 at Haiku 5.5 list price, plus answer and judge (~$80 for 2,000 questions). ~$15–20 per 100K tuning iteration; ~$500 with tuning. Bedrock and OpenRouter prices are checked before the first run.
- **People's tokens:** a few hundred at session end and ~400 at session start, against 850–1,200 before every prompt with past.dev's plugin (measured).

## 7. Plan

Working days with agents; tuning is the only open-ended part, so it has a timebox.

| Step | Days | What |
|---|---|---|
| 0. Harness | 2 | Fork; adapter with the agent stand-in; baselines on 100K (past.dev rerun, AgentCore built-in, judge ×3) |
| 1. Memory log in the engine | 5 | Entries, signing and replay on the roster's pattern; `memory` heads in `hello` and a paged `Memory` frame; FTS5; `remember` and `recall` in `network_tools.json`; session-start and Stop hook text; `lightbulb memory` commands; tests including two-computer sync |
| 2. Tuning | timebox 10 | Hook instruction, tool text, page format, FTS5 vs embeddings, on 100K and the probes; done when we beat our past.dev rerun by more than the judge's spread, or we stop and report |
| 3. Workspace | 3 | Cloud member holds the copy; room agents get the tools through the bridge; Memory tab reads it; AgentCore overrides for room facts in the interim |
| 4. Release and publish | 3 | CLI release; privacy and security docs; one run of 500K, 1M, 10M and LongMemEval_S; results repo |

About four weeks. Step 1 ships to users before tuning finishes: memory that works is useful before it is benchmarked.

## 8. Decisions for the founder

1. Memory is part of the free network, peer to peer, no account.
2. Agents share to the team by default when the fact is about shared work, say so when they do, and the person can turn it off. The alternative is personal by default with an explicit "share".
3. AgentCore long-term memory is interim for room facts and retired when the log matches it.
4. Publish the harness and results even if a split comes out below past.dev.

## Sources

CLI v0.6.5 (`f9ea8d5`): `src/roster.rs`, `src/net/mod.rs`, `src/net/frames.rs`, `src/store.rs`, `src/hook.rs`, `src/embed.rs`, `docs/security.md`. Monorepo: `apps/backend/core/services/network_tools.json`, `apps/infra/hermes/bridge/network.py`, `apps/infra/hermes/plugins/memory/agentcore/__init__.py`, `apps/infra/lib/stacks/container-stack.ts`. AgentCore (interim room facts): [overrides](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-custom-strategy.html), [semantic prompts](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-system-prompt.html), [metadata](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/long-term-memory-metadata.html), [pricing](https://aws.amazon.com/bedrock/agentcore/pricing/). past.dev harness: `pastdotdev/benchmarks/beam`.
