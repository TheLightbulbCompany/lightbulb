# Memory layer: design (27 October 2026)

Status: draft for the founder. Builds on `docs/research/2026-10-27-memory-problem.md` (why systems lose points, our UX today), `2026-10-27-oss-beam-failures.md`, `2026-10-27-past-dev.md`, `2026-10-27-memory-building-blocks.md` and `2026-10-27-network-memory-inventory.md`. Supersedes the build-or-buy question in `2026-10-27-memory-decision.md` §6.0: we build.

## 1. What we are building and why

One memory that every agent a team runs can read: the Claude Code and Codex sessions on each person's laptop, the agents in the workspace's rooms, and the Claude app and ChatGPT through the hosted tools. It remembers what was decided, what changed, and who said it, and it says so when two people disagree instead of picking one.

Why us: memory products today are one person's memory (Beacon, past.dev's plugin in practice) or a shared project with no review (past.dev's default). We already have the people, the rooms and the approval step. A fact a teammate's agent found yesterday reaching your agent today, with a name and a date on it, is the thing nobody ships.

**Goals**
1. A solo developer on the free network gets useful memory with zero setup and nothing leaving the computer.
2. A team gets shared memory where every fact has a source, a history and an owner, and disagreements surface in the room.
3. Beat the best published BEAM numbers under the same harness, answerer and judge they used, and publish the run so anyone can repeat it.
4. Cost per workspace stays under a fifth of the $50 plan price.

**Non-goals**: an API product for other companies (later, maybe); capturing tool calls, diffs or command output; memory in the hosted apps beyond the existing tools; LoCoMo numbers (its keys are broken).

**Anti-goals** (failure even if it works): memory that silently decides who is right between two people; a context block every prompt that the developer pays for and never reads; a benchmark score we cannot reproduce or that comes from a harness different from the one we compare against; any of a person's laptop memory reaching our servers without them choosing it.

## 2. What the research says we must fix

Every system loses points for the same reasons (memory-problem §2, §6, §7):

| Failure | Evidence | Where it costs on BEAM |
|---|---|---|
| One fact stored many times crowds out the one update | 12 copies of Milvus 2.2.0 vs one 2.3.1; five copies of 8,000 TRY vs one 7,500 (Mem0) | Knowledge update, information extraction |
| An update phrased differently is not linked to the old value | "p95 latency" vs "95th-percentile response time"; "evaluating" vs "setup"; both stay current | Knowledge update, information extraction |
| Ranking ignores whether a fact is current | Superseded Postgres 14 ranked first, current 16 third | Knowledge update |
| Disagreement is either silently resolved by recency or not noticed | Ana/Ben dates closed by recency; Docker self-contradiction not linked | Contradiction resolution (past.dev 73–78: always says "contradictory", loses on naming both sides) |
| Totals across sessions miss facts; one query, one ranking, budget spent on copies | Multi-session at 10M: 12 of 20 scored 0; in 5 none of the gold numbers on the page | Multi-session reasoning (28% at 10M) |
| Before/after inside one session both stay open | p95 820 ms and 140 ms both current | Knowledge update |
| Dates lost or wrong in the harness | Hindsight stamped ingest date; Honcho none; Mem0 per batch | Temporal, ordering, update (for them, not past.dev) |

past.dev already scores 93–100 on the other six abilities, so the whole contest is in four rows: information extraction, knowledge update, contradiction and multi-session.

## 3. The design

### 3.1 The unit: a fact with a history

A record is one claim about one thing, not one sentence from one message:

```
subject        "production database"            (an entity; resolved to one id)
attribute      "version"                          (normalised: one name per meaning)
value          "Postgres 16"
status         current | replaced | contested
valid_from     when it became true (event time if stated, else message time)
replaced_by    id, when status = replaced
contested_with ids, when status = contested
evidence       [{who, where (room, repo, session), when, quote}]   repeats append here
scope          private(person) | team(workspace)
kind           fact | decision | lesson | preference
```

Repeating a fact adds evidence to the record; it never adds a record. That removes the copies problem at the source rather than at ranking.

### 3.2 Writing: link before you store

For each new claim:
1. Find neighbours: the existing records on the same subject (entity match) plus the nearest by meaning (top 20).
2. One model call decides, for each neighbour: **same** (add evidence), **update** (old becomes `replaced`, new is `current`), **conflict** (both become `contested`), or **unrelated** (new record). The call sees attribute names but decides on meaning, so "latency" and "response time" land on one slot.
3. Rules the model does not get to break: an update from the same person, or a later statement that says it is a change ("upgraded", "now", "switched", "dropped to") supersedes. Two different people stating different values with no change language is `contested`, never superseded. A negation by the same person of their own earlier statement ("I've never used Docker") is `contested`.
4. Inside one session, turns are processed in order, so a before/after in one sitting supersedes.

Where the extraction runs:
- **Laptop, free:** the agent itself. At the end of a session (the Stop/SessionEnd hooks we already install for five agents) the agent is asked what the next session should know, and calls `remember`. `remember` returns the neighbours ("existing: production database version = Postgres 14, current; is this an update?"), and the agent answers with `replaces` or `conflicts`. The developer's own agent does the extraction and the linking, so it costs us nothing and nothing leaves the computer. The agent also calls `remember` whenever the person says "remember that".
- **Cloud, rooms and DMs:** AgentCore's self-managed strategy delivers batches of conversation to our pipeline (SNS → our worker), which runs steps 1–4 with a small model and writes records back with `BatchCreateMemoryRecords` / `BatchUpdateMemoryRecords`, our fields in record metadata.
- **Benchmarks:** the cloud pipeline, nothing special.

### 3.3 Reading: a page of state, not a pile of passages

Recall returns a compact page:

```
Current
- production database version: Postgres 16 (since 15 Sep, Ben in #infra; was Postgres 14)
- API p95 latency: 300 ms (since 2 Oct, Ana; was 420 ms)
Contested
- launch date: 3 March (Ana, #launch, 1 Oct) vs 10 March (Ben, #marketing, 1 Oct). Not resolved.
Evidence
- "Finished the database upgrade last night, production is on 16 now" (Ben, 15 Sep)
```

Steps:
1. **Plan the query.** One cheap model call splits a question into the facts it needs ("total agents across timeouts, format mismatches and state overload" → three lookups). Single-fact questions skip this.
2. **Fetch** each lookup: semantic search in AgentCore (current records first via metadata filter, then replaced ones for history), plus entity match.
3. **Build the page** under the budget: current state first, contested pairs with both sides named, history as "was X", then evidence quotes only if budget remains. Because there is one record per fact, an 8,000-token budget holds dozens of distinct facts instead of one fact six times.

This page format is also what we want inside a developer's context in the product: a few hundred tokens of state, not ten restated sentences.

### 3.4 Where memory reaches people

| Who | When memory appears | What they see and control |
|---|---|---|
| Developer, free network | Session start: the current state for this repo, at most ~400 tokens. On demand: the agent calls `recall`. After compaction: the block again. Never every prompt. | `lightbulb memory` (list, show with history, forget, edit, export); `lightbulb memory last` shows exactly what was injected last time |
| Same developer, signed in | Same, on all their computers | Personal records sync between their own computers over the network, end-to-end encrypted, never stored by us |
| Team member's agent on a laptop | Session start includes team facts for this repo; `recall` covers team scope | Team facts carry who and where; contested facts are marked |
| Workspace agents in rooms | Before each turn, the page for the message (fixing today's bug where the query is routing JSON); `recall` tool | — |
| The room | A contested team fact posts a card: "Ana said 3 March, Ben said 10 March. Which is right?" One tap resolves it, and the answer becomes the current value with both histories kept | |
| Anyone, Memory tab | — | Each fact with source, history, status; edit, replace, delete, confirm; a filter for contested |

**Making something team memory.** `share` (or "share this with the team" to an agent) posts an approval card in the room, like plans and spend today. Approved, it becomes a team record. Facts extracted from rooms are team facts already, since the room is shared. Nothing from a laptop becomes team memory without a person approving it.

Compared with what people use today: past.dev decides everything and shows ten memories every prompt; Beacon makes the person review every lesson; we let the agent write, let the room decide what is shared, and show state only at the start and when asked.

### 3.5 AgentCore: what we use and what we own

AgentCore Runtime is where our agents run; it does not touch memory design. AgentCore Memory stays as the cloud store:
- **We use:** short-term events (already written every turn), the self-managed strategy (delivers conversation batches to us), record storage, semantic search with namespace and metadata filters (up to 100 results), record TTL, IAM.
- **We own:** extraction, linking, the status and history fields, query planning, page building, the room cards.
- **We retire:** the built-in `semanticMemoryStrategy` once ours beats it in the harness. Self-managed storage is also a third of the price ($0.25 vs $0.75 per 1,000 records a month).
- **Not verified yet:** which metadata filter operators work in practice. Day 1 of the build checks it; if filtering by status does not work, we fetch more and filter in our code.

The laptop store is SQLite in `state.db` (already 0700, already there), with FTS5 for keyword search. The engine already runs inside the workspace runtime through the Hermes bridge, so the `remember`/`recall` tools are one implementation in the engine, with a local store on a laptop and AgentCore behind the bridge in a workspace. Exact split between the Rust engine and the Python bridge is the builder's call; the rule is one implementation of linking and page building, not two.

## 4. How we beat the benchmarks

### 4.1 Same harness, or it does not count

past.dev's BEAM harness is MIT-licensed and public. We fork it and replace only the memory calls (`past_api.py`) with ours. Everything else stays identical: ExaBase's prompts (checked by hash with their `verify.py --prompts`), the answer and judge model (`openai/gpt-5.6-luna`, reasoning off), 100 results within 8,000 tokens, the same timestamps (noon UTC on the session day), each question answered once. That is the only way a "we beat 90.02" claim is true.

### 4.2 Measure the noise first

Before tuning anything, on the 100K split (20 conversations, 400 questions, ~2.6M tokens):
- Rerun past.dev ourselves. About 30,000 credits; the account has ~149,900 of 150,000 left, so it is free. This tells us how far our run of their system lands from their published 92.08.
- Run today's AgentCore built-in strategy through the same harness. That is our starting line.
- Judge the same answers three times to see how much the judge alone moves the score.

A difference smaller than the judge's spread is not a win.

### 4.3 Where the points are

Equal weight per ability (70 questions each at 1M). past.dev today and what each mechanism should move, as targets to test, not promises:

| Ability | past.dev 1M | 10M | Mechanism | Target 1M | Target 10M |
|---|---|---|---|---|---|
| Contradiction | 75.4 | 73.1 | `contested` status; page names both sides with who and when (the rubric's lost items) | 90 | 85 |
| Knowledge update | 85.7 | 92.5 | One record per slot; linking by meaning; current ranked first; within-session supersede | 95 | 95 |
| Information extraction | 84.0 | 68.8 | No copies on the page; history shown as "was X" | 92 | 85 |
| Multi-session | 72.8 | 28.2 | Query planning into sub-lookups; one record per fact frees the budget | 80 | 55 |
| Other six | 93–100 | 94–100 | Keep: event times, abstention, preferences | hold | hold |
| **Overall** | **90.65** | **85.03** | | **≈94.6** | **≈90.8** |

If the four targets land and the other six hold, the 1M overall goes from 90.65 to about 94.6, and 10M from 85.0 to about 90.8. Hitting half the targets still clears past.dev at every split.

### 4.4 Not overfitting

- Tune on the 100K split and our own probe cases only (`docs/research/2026-10-27-past-dev-probe2.py`: clean contradictions, rephrased updates, the coding sittings).
- 500K, 1M and 10M are run once at the end, untouched during tuning.
- LongMemEval_S as a second, independent check (500 questions; the field's top is 94–95).
- Known bad keys (Honcho's three, the 400/380 ms physics question) are reported, not removed: we score on the dataset as published and note the disputed ones.
- Every run publishes `summary.json`, `results.jsonl`, the harness commit and the cost, like past.dev does.

### 4.5 The number nobody publishes

A team memory eval from our own rooms (with the team's consent, anonymised): questions one person's agent should answer from what another person said. Measures: right answer, right source named, disagreements surfaced rather than resolved. This is the one we lead with on /network, since it is our product and no one else can run it.

## 5. Cost

Prices: AgentCore list price (read 27 Oct); Claude on the Anthropic API, Haiku 5.5 $0.10/$0.50 per million tokens in/out (prompts under 100K), Sonnet 5.5 $2/$10, batch 50% off. **Bedrock prices for these models may differ; we run inference through Bedrock, so the build confirms them on day 1.**

### 5.1 Running it, per workspace per month

Assumptions (the same sketch as `2026-10-27-past-dev.md` §5, to be measured): 5 people, 3,000 room and DM messages, 3,000 agent turns, 200 shared lessons from laptops, ~2,000 active records.

| Item | How | Haiku 5.5 | Sonnet 5.5 |
|---|---|---|---|
| Extraction and linking | 3,000 turns × ~1,500 tokens, ~3 passes | ~$1.50 | ~$30 |
| Query planning | only multi-fact questions, ~1 in 5 turns | ~$0.10 | ~$2 |
| Record storage | 2,000 × $0.25/1,000 (self-managed) | $0.50 | $0.50 |
| Retrieval | 3,000 turns × 2 scopes × ~1.3 lookups + 400 laptop session starts = ~8,200 × $0.50/1,000 | ~$4 | ~$4 |
| Short-term events | a few MB at $1/GB | <$0.10 | <$0.10 |
| **Total** | | **~$6** | **~$37** |

Haiku keeps us at about 12% of the $50 plan; Sonnet does not fit the goal. So the extraction model is decided by the harness: if Haiku is within the judge's noise of Sonnet on the 100K split, Haiku ships.

Retrieval is the line that grows with use. A past.dev-style recall before every laptop prompt (5 people × 100 prompts a day) would add ~22,000 retrievals, about $11 a month. Another reason recall happens at session start and on demand.

The free network costs us nothing: the person's agent does the extraction, SQLite does the storage, and sync is peer to peer.

### 5.2 Running the benchmarks

| Run | History tokens | Extraction (Haiku, batch) | Answer + judge | Total |
|---|---|---|---|---|
| BEAM 100K (each tuning iteration) | 2.6M | ~$1 | 400 q × 2 calls ≈ 8M tokens: ~$15 | **~$15–20** |
| BEAM full (100K + 500K + 1M + 10M) | 155M | ~$40–70 | 2,000 q: ~$80 | **~$150** |
| Same with Sonnet extraction | 155M | ~$800–1,400 | ~$80 | ~$1,000–1,500 |
| past.dev rerun, 100K | | free credits | ~$15 | ~$15 |
| LongMemEval_S | ~58M (dedupe histories: far less) | ~$10 | ~$20 | ~$30 |

Answer and judge costs assume `gpt-5.6-luna` near Sonnet 5.5 prices through OpenRouter; check the real price before the first run. AgentCore storage for benchmark records is deleted after each run (TTL on write). The whole programme, with 20 tuning iterations, is roughly $600–800 on Haiku extraction.

## 6. What people save

- **Context tokens.** past.dev's plugin injects 850–1,200 tokens before every prompt (measured, probe 2); 100 prompts a day is ~100,000 tokens of mostly repeated facts in the context. Ours: ~400 at session start plus what the agent asks for. That is about 95% fewer injected tokens for the same developer, and less noise in what the agent reads.
- **Re-explaining and re-discovering.** Not measurable from outside; we measure it inside: how often a session asks something already in memory (repeated-question rate); time from session start to first useful action (T7 in the decision doc); how often a recalled fact is used in the reply.
- **Mistakes from stale facts.** Count of recalls where a replaced value would have been served first without status ranking (the harness gives us this for free).
- **Cost to us,** stated above, measured per workspace in Usage once it ships.

## 7. Privacy and security

1. **Free network:** memory is on the computer only. Nothing about it leaves. `lightbulb memory export` and `forget` are the person's.
2. **Signed in:** personal memory syncs between the person's own computers over the existing end-to-end encrypted transport. We never store it. A cloud backup can come later as an opt-in.
3. **Team memory:** only facts from rooms (already shared) and facts a person approved. Stored in AgentCore under the workspace's namespace.
4. **The hard boundary ships with team memory:** laptop sessions and the hosted tools reach team memory only through our backend, which checks the member against the workspace. Today's IAM policy is a product boundary (every agent of a workspace runs under one role); that stays for the runtime, and nothing outside the runtime gets AgentCore access.
5. **Deletion removes derived facts.** Deleting a message or a person's data removes the evidence it contributed; a record with no evidence left is deleted. Owner and member wipes already exist (#4030) and extend to the new records.
6. **Redaction:** `redact_for_egress` already runs before turns are written; it runs on `remember` too.
7. **The privacy policy rewrite (Termly, open) must describe this before team memory or sync ships.**

## 8. Plan and time

One engineer working with agents; weeks are elapsed time with founder review between phases.

| Phase | Weeks | What | Done when |
|---|---|---|---|
| 0. Harness and baseline | 1 | Fork past.dev's harness; adapters for AgentCore built-in, past.dev, ours; 100K split for past.dev and AgentCore; judge run 3×; Bedrock prices and metadata filters checked | We know our starting line and the judge's noise |
| 1. Engine in the cloud | 2–4 | Self-managed strategy; record schema; link-before-store; contested; query planning; page builder; probe cases as tests; tune on 100K; Haiku vs Sonnet | 100K beats our past.dev rerun by more than the judge's spread |
| 2. Rooms switch over | 4–5 | Hermes plugin reads the new page (fixing the routing-JSON query); Memory tab shows source, history, contested; contested → room card; built-in strategy retired | Our own workspace runs on it for a week |
| 3. Free network | 5–7 | `remember` and `recall` in the shared tool spec; SQLite store; session-start block; Stop-hook prompt; `lightbulb memory` commands; sync between own computers | A fresh install remembers across sessions with nothing configured |
| 4. Team over the network | 7–9 | `share` → approval card; laptop and hosted recall of team scope through the backend boundary | A fact one person's agent learned reaches a teammate's agent with a name on it |
| 5. Publish | 9–10 | One untouched run of 500K, 1M, 10M; LongMemEval_S; team eval; results repo with `verify.py`; /network page | Numbers anyone can rerun |

Phases 0–1 change nothing in production. Each later phase ships on its own.

## 9. Decisions for the founder

1. **Personal memory free on the network, team memory paid.** My call in this design; the alternative puts all memory behind sign-in.
2. **The agent writes laptop memory at session end, no review for personal records.** A wrong personal fact costs only that person; a review step on every lesson is what Beacon asks and what people skip.
3. **Contested facts post a card in the room.** This is new noise in rooms; the alternative is a filter in the Memory tab only.
4. **Retire AgentCore's built-in facts when ours beats them in the harness**, migrating confirmed facts as records.
5. **Publish the harness and results** even if a split comes out below past.dev.

## 10. Calls I made that you should check

- Recall at session start and on demand, not before every prompt (cost and context, §5.1, §6).
- One record per fact with evidence attached, instead of storing each statement.
- The developer's own agent does extraction on the free tier, so we pay nothing and nothing leaves.
- Haiku 5.5 as the cloud extraction model if the harness says it holds.
- Same harness and judge as past.dev's published run, so the comparison is direct.

## Sources

AgentCore: [self-managed strategy](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-self-managed-strategies.html), [strategies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-strategies.html), [RetrieveMemoryRecords](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_RetrieveMemoryRecords.html), [pricing](https://aws.amazon.com/bedrock/agentcore/pricing/) (read 27 Oct). past.dev BEAM harness and results: `pastdotdev/benchmarks/beam` (README, `results/*/summary.json`). Code: `apps/infra/hermes/plugins/memory/agentcore/__init__.py`, `apps/infra/hermes/bridge/network.py`, `apps/backend/core/services/network_tools.json`, CLI `src/store.rs`, `src/embed.rs` (v0.6.5).
