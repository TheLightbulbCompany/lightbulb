# Memory layer: design (27 October 2026, v2)

Status: draft for the founder. v2 replaces v1's custom pipeline (S3, SNS, Lambda, our own linking engine, a local SQLite engine) after reading the AgentCore Memory docs properly: almost everything v1 built by hand is a configuration of the memory we already run. Research behind it: `docs/research/2026-10-27-memory-problem.md`, `2026-10-27-oss-beam-failures.md`, `2026-10-27-past-dev.md`, `2026-10-27-network-memory-inventory.md`.

## 1. What and why

One memory every agent a team runs can read: the Claude Code and Codex sessions on each person's laptop, the workspace agents in rooms, and the hosted apps. It remembers what was decided and what changed, with who said it and when, and it says so when two people disagree.

**Goals:** shared memory with a source on every fact; disagreements surfaced, not silently resolved; beat the best published BEAM numbers under the same harness and judge; under a fifth of the $50 plan per workspace.

**Non-goals:** a memory API for other companies; capturing tool calls, diffs or command output from laptops; LoCoMo.

**Anti-goals:** memory that picks a winner between two people; a block injected before every prompt; a benchmark number from a different harness than the one we compare against; laptop content reaching our servers without the person choosing it.

## 2. What we must fix (from the research)

past.dev scores 93–100 on six of BEAM's ten abilities. The contest is in four, and every system fails them the same way:

| Failure | Ability it costs |
|---|---|
| The same fact stored many times crowds out the one update (12 copies of Milvus 2.2.0 vs one 2.3.1) | Knowledge update, information extraction |
| An update in different words is not linked to the old value ("p95 latency" vs "95th-percentile response time") | Knowledge update, information extraction |
| The replaced value ranks above the current one | Knowledge update |
| Two people disagreeing is settled by recency, or not noticed | Contradiction (past.dev 73–78: names the conflict, loses on naming both sides) |
| Totals across sessions miss facts | Multi-session (28% at 10M) |

## 3. What AgentCore Memory already does

From the AgentCore developer guide (sources at the end):

| Need | AgentCore feature | Status for us |
|---|---|---|
| Turn conversation into facts | Semantic strategy extraction. Its default prompt already resolves relative dates against message timestamps ("last Saturday" → a date) and keeps speakers' names when several people talk | In use (built-in) |
| One record per fact, updates merged into it | Consolidation: for each new fact it retrieves similar existing records and chooses `AddMemory`, `UpdateMemory` (rewrite the existing record) or `SkipMemory`. Similar by meaning, so "latency" and "response time" meet here | In use, with the default instructions, which resolve contradictions by "confidence" and keep one side |
| Our rules for updates and disagreements | **Built-in with overrides**: we replace the extraction and consolidation instructions and pick the Bedrock model; the output format stays theirs | Not used. One `UpdateMemory` call on the existing resource |
| Status, kind, source on each record | **Metadata schema** on the strategy: keys the model fills on every record, constrained to allowed values (`status`: current / contested; `kind`: fact / decision / lesson / preference), plus keys copied exactly from the event (`STRICTLY_CONSISTENT`, up to 3) | Not used |
| Filter recall by status, kind, time | **Metadata filters** on `RetrieveMemoryRecords` (up to 5, AND; `EQUALS_TO`, `CONTAINS`, `BEFORE`/`AFTER` on created/updated time); `ListMemoryRecords` filters without search | Not used |
| Correct dates for history and benchmarks | `CreateEvent.eventTimestamp` is ours to set | In use (now) |
| Lessons from agent work | Episodic strategy: detects a finished episode, records situation, intent, outcome, and reflects across episodes into lessons ("avoid repeating failed approaches"); AWS names code assistants as a use case | Not used |
| Add a fact without a conversation | `IngestData`: same extraction and consolidation, no short-term event | Not used |
| Write exactly, no model | `BatchCreateMemoryRecords` / `BatchUpdateMemoryRecords` with our metadata | In use (share, confirm) |
| Isolation | Namespaces per owner and member, IAM per owner | In use |

Overrides also cut storage from $0.75 to $0.25 per 1,000 records a month; the model calls are billed to our AWS account instead.

**Not in the docs, so phase 0 measures them:** how many similar records consolidation looks at; whether CloudFormation's `CfnMemory` accepts overrides, indexed keys and the metadata schema (else one SDK call at deploy); which models overrides can use (Haiku 5.5 on Bedrock is the target); extraction speed at benchmark volume.

## 4. The design

### 4.1 Writing: config, not code

Change the existing `workspace_facts` strategy from built-in to built-in with overrides, on a new memory resource first (benchmarks, our own workspace), then production.

1. **Consolidation instructions**, added to AWS's defaults:
   - Same thing said again: `SkipMemory`, or `UpdateMemory` to add the new source. Never a second record.
   - A change ("upgraded", "now", "switched", "dropped to"), or a later value from the same person: `UpdateMemory` so the record reads current value first, then history: "Production database: Postgres 16 (Ben, 15 Sep; was Postgres 14, Ana, 2 Aug)". The old value stays inside the one record as history, so it cannot outrank the current one.
   - Two different people, different values, no change language: `UpdateMemory` to "Contested: launch date 3 March (Ana, #launch, 1 Oct) vs 10 March (Ben, #marketing, 1 Oct)", with `status = contested`. Never pick one.
   - A person contradicting their own earlier statement without saying it changed: contested.
2. **Extraction instructions**, added: one fact per attribute, with the speaker's name and the date; decisions and lessons as their own facts; requests such as "help me with the Milvus 2.3.1 setup" also state the fact they imply.
3. **Metadata schema:** `status` (current / contested), `kind` (fact / decision / lesson / preference), both model-filled with allowed values; `source` (room, DM or laptop) copied from the event.
4. **Events:** the Hermes plugin already writes every turn. It adds the speaker's name to the text and `source` to the event metadata.

The episodic strategy is a later experiment for lessons from long agent tasks, measured against what the semantic strategy already extracts. Not in v1.

### 4.2 Reading: one function

One function, used by the Hermes plugin, the backend and the benchmark adapter:

1. `RetrieveMemoryRecords` in the caller's namespaces, `topK` up to 100.
2. Contested records first, then the rest by score. Pack them into the token budget, nothing else on the page.
3. For a question that needs several facts ("how many in total across..."), the workspace agents and laptop agents call `recall` again with narrower questions. That is how agents already work, so there is no planner. The benchmark adapter does the same split with one small model call, since its answerer gets a single page. Adapter code only, not product.

Fixes the known defect while there: the plugin's recall query starts with routing JSON, so the search text is wrong today.

### 4.3 Where memory reaches people

| Who | What changes |
|---|---|
| Workspace agents in rooms | Same prefetch and `recall` tool, with the new page |
| A signed-in person's laptop agents | Two new tools in the shared spec (`network_tools.json`, so Claude Code, Codex, Cursor, OpenCode, Grok CLI and the hosted apps all get them): `recall` and `remember`. The backend runs them against the person's private namespace and their workspace's team namespace, checked against the signed-in member. `remember` uses `IngestData`, so a laptop fact is consolidated with everything else. The tool description tells agents when to call: at the start of a task, and when the person says "remember". Nothing is sent unless an agent calls `remember` |
| Sharing with the team | `remember` with `scope: team`, or the existing share endpoint (it already moves a private fact into the team namespace). Immediate, named, editable by any member. No approval |
| Contested facts | The page marks them, so any agent that recalls one says it is contested. The Memory tab gets a contested filter and a "pick the right one" action. Pushing them to the "needs you" list is a later step if people miss them |
| Memory tab | Shows the history and source that now live in the record text; edit, delete, share and confirm as today |
| No account, free network | No change in v1. Each agent's own local memory (Claude Code's memory files, `AGENTS.md`) already covers one person on one computer. What the network adds is memory across people and agents, and that needs an account |

## 5. How we beat the benchmarks

1. **Same harness.** Fork past.dev's MIT-licensed BEAM harness and replace only `past_api.py`: `CreateEvent` per turn with the turn's date, wait until records stop changing, recall through §4.2. Same ExaBase prompts (hash-checked), same answer and judge model (`openai/gpt-5.6-luna`, reasoning off), 100 results within 8,000 tokens, each question once. A separate memory resource per run, deleted afterwards.
2. **Measure the noise.** On the 100K split (20 conversations, 400 questions): rerun past.dev ourselves (~30,000 of our ~149,900 free credits); run today's built-in strategy as our starting line; judge the same answers three times. A gap smaller than the judge's spread is not a win.
3. **Tune by editing prompts.** Each iteration is one `UpdateMemory` with new instructions and a fresh 100K ingest. Probe cases from `docs/research/2026-10-27-past-dev-probe2.py` run every time (clean contradictions, rephrased updates).
4. **Targets** (to test, not promises; each ability weighs a tenth):

| Ability | past.dev 1M | 10M | What moves it | Target 1M / 10M |
|---|---|---|---|---|
| Contradiction | 75.4 | 73.1 | Contested records naming both sides with who and when | 90 / 85 |
| Knowledge update | 85.7 | 92.5 | One record with current value first and history after | 95 / 95 |
| Information extraction | 84.0 | 68.8 | No copies on the page | 92 / 85 |
| Multi-session | 72.8 | 28.2 | Split lookups; budget freed by no copies | 80 / 55 |
| Overall | 90.65 | 85.03 | the other six held | ≈94.6 / ≈90.8 |

5. **No overfitting.** Tune on 100K and the probes only; run 500K, 1M and 10M once at the end; LongMemEval_S as an independent check; publish `summary.json`, `results.jsonl`, the harness commit, the prompts and the cost.
6. **The limit of this route.** We do not control how many neighbours consolidation sees, or its output format. If the 100K score stalls below past.dev's for that reason, the next step is AgentCore's self-managed strategy (our own extraction and consolidation, AgentCore still stores and searches). Only then, and only for the part that stalls.

## 6. Cost

All pay per use; an idle workspace pays only for stored records. Prices are list prices read 27 Oct: AgentCore (storage $0.25 per 1,000 records a month with overrides, $0.50 per 1,000 retrievals, short-term $1 per GB); Claude Haiku 5.5 $0.10/$0.50 per million tokens on the Anthropic API. **Bedrock's price for the override model is confirmed in phase 0.**

Per workspace per month (5 people, 3,000 messages, 3,000 agent turns, ~2,000 records; assumptions to measure):

| Item | Haiku 5.5 |
|---|---|
| Extraction and consolidation (AgentCore calls the model in our account), ~13M tokens in | ~$1.50 |
| Records, 2,000 × $0.25/1,000 | $0.50 |
| Retrievals: 3,000 turns × 2 namespaces + laptop `recall` calls ≈ 8,000 | ~$4 |
| Short-term events, a few MB | <$0.10 |
| **Total** | **~$6, about 12% of the plan** |

No Lambda, queue, bucket or new service: writing is the `CreateEvent` call the plugin already makes; reading runs in the plugin and the backend, which already run.

Benchmarks: ~$15–20 per 100K iteration (answer and judge dominate); ~$150 for one full run of all four splits; ~$500 with 20 tuning iterations. The answer model's OpenRouter price is checked before the first run.

People save context tokens: past.dev's plugin puts 850–1,200 tokens before every prompt (measured); our agents pull a page when they need one.

## 7. Privacy

- Nothing leaves a laptop unless an agent calls `remember`, and the person can see and delete every record in the Memory tab.
- Laptop tools reach memory only through the backend, which checks the signed-in member against the namespace. That is the hard boundary the inventory found missing; nothing outside the runtime and the backend gets AgentCore access.
- `redact_for_egress` runs on `remember` as it does on turns.
- Deleting a person or workspace already wipes their records and events (#4030).
- The privacy policy rewrite (Termly, open) describes laptop `remember` before it ships.

## 8. Plan

Working days with agents. The only open-ended part is tuning, so it has a timebox.

| Step | Days | What |
|---|---|---|
| 0. Harness and checks | 2 | Fork the harness, AgentCore adapter; 100K baselines (past.dev rerun, built-in strategy, judge ×3); check Bedrock model and price, consolidation behaviour, CloudFormation support |
| 1. Overrides and tuning | timebox 10 | Instructions and metadata schema on a test memory resource; tune on 100K and the probes. Done when we beat our past.dev rerun by more than the judge's spread, or we stop and report |
| 2. Rooms | 2 | Plugin: speaker and source on events, new page, routing-JSON query fixed; Memory tab contested filter; switch production's strategy to the tuned overrides |
| 3. Laptops | 3 | `recall` and `remember` in the tool spec; backend endpoints with the member check; docs |
| 4. Publish | 2 | One run of 500K, 1M, 10M and LongMemEval_S; results repo; /network page |

About three to four weeks, most of it tuning runs. Steps 2 and 3 do not depend on the benchmark result and can ship while tuning runs.

## 9. Decisions for the founder

1. Memory needs an account (signed-in personal plus workspace team); the no-account network stays messaging only.
2. Sharing to the team is immediate and named, with no approval.
3. Contested facts are shown, not routed to people, in v1.
4. Publish the harness and results even if a split comes out below past.dev.

## Sources

AgentCore developer guide: [built-in strategies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/built-in-strategies.html), [overrides](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-custom-strategy.html), [semantic prompts](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-system-prompt.html), [metadata](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/long-term-memory-metadata.html), [episodic](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/episodic-memory-strategy.html), [self-managed](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-self-managed-strategies.html); API: [CreateEvent](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_CreateEvent.html), [RetrieveMemoryRecords](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_RetrieveMemoryRecords.html); [pricing](https://aws.amazon.com/bedrock/agentcore/pricing/). Code: `apps/infra/lib/stacks/container-stack.ts` (the memory resource), `apps/infra/hermes/plugins/memory/agentcore/__init__.py`, `apps/backend/core/services/workspace_memory.py`, `apps/backend/core/services/network_tools.json`. past.dev harness: `pastdotdev/benchmarks/beam`.
