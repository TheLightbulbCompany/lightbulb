# Memory on the network: design (27 October 2026, v4)

Status: draft for the founder; a builder check is run on it before hand-over. v4 settles three things earlier drafts left loose: the two networks and what the business is, how the free log and AgentCore work together without a loop, and what we claim about benchmarks (not a win). Research: `docs/research/2026-10-27-memory-problem.md`, `2026-10-27-oss-beam-failures.md`, `2026-10-27-past-dev.md`, `2026-10-27-network-memory-inventory.md`, `2026-10-27-memory-decision.md`.

## 1. What this is

Memory for people and their agents on the Lightbulb network: any agent on the team remembers what any other agent on the team learned, across people, harnesses (Claude Code, Codex, Cursor, OpenCode, Grok CLI, the hosted apps) and sessions. On the free network it is peer to peer and nothing touches our servers. In a workspace it gains an always-on copy, room agents that read and write it, search by meaning, automatic extraction from room conversation, and a Memory tab.

## 2. The two networks and the business

The business is messaging for people and their AI agents. The free network is the distribution: it must spread on its own, cost us nothing per install, and be trusted with nothing leaving the machine. The workspace is the revenue: $50 a month for up to five people, $20 a member after (the plan's number on the wall: ten founder-led teams with a card on file by 4 January 2027). Memory follows that split exactly.

| | Free network (no account) | Signed in (free) | Workspace (paid) |
|---|---|---|---|
| What exists today | Messaging between your agents and your teammates' agents; presence; approvals | Hosted apps (Claude app, ChatGPT) reach your agents | Rooms, always-on agents, tasks, spend gates, Memory tab on AgentCore (room agents only) |
| Memory added | Personal memory across every harness on this computer; team memory with the people on your network; keyword search; `lightbulb memory` | Same, plus your personal memory syncs between your own computers (sign-in is how the engine knows two computers are one person; without it there is no pairing) | Everything on the left, plus: a copy that is up while the workspace runs, so facts cross between teammates who are never online together; room agents and hosted apps read and write it; search by meaning; facts extracted from room conversation without anyone saving them; Memory tab with source, history and contested; wipe |
| Why the line is here | Cross-person memory with no server is the thing nobody ships and the reason to install. It costs us $0 | An account is what an invite attaches to; nothing to charge for yet | Everything that needs a server that is on when you are not, or a model we pay for |
| Cost to us | $0 | $0 | ~$6 per workspace a month (§7), inside the plan |

Two things this does not do: charge solo developers for memory (the person with everything working is the one who invites a cofounder), and put any memory on our servers for someone who never signed up.

## 3. What the research says we must get right

Every memory system loses the same way (memory-problem §2, §6, §7): the same fact stored many times crowds out the one update; an update in other words is not linked to the old value; the replaced value ranks above the current one; two people disagreeing is settled by recency or not noticed; totals across sessions miss facts. The product rules in §4 exist to avoid these, whatever the benchmark says.

## 4. The log: how memory moves

### 4.1 What is stored

A replicated set of signed entries beside the roster. Not the roster's hash-linked DAG: `me` entries are withheld from teammates and `forget` strips content, and both break parent links and content hashes. Instead each entry is signed by its author and numbered per author; peers exchange "highest number I hold per author" and page the gap, the way the roster pages entries today. Order comes from `replaces` and `when`, so every member computes the same memory from the same set whatever order it arrives. A `forget` is a signed tombstone that keeps the id and drops the body. Memory frames are gated on a `memory` capability in `hello`, so an older build ignores them and keeps messaging; no protocol bump. Entries are small (the roster's 16 KB cap holds).

```
remember  {id, subject, fact, kind: fact|decision|lesson|preference, when, source: {harness, repo, session}, scope: me|team}
replace   {id, replaces: [ids], fact, when, source}     a change; the old entries become history
forget    {id: target}                                   the content is dropped on every copy
```

- **Scope.** `team` entries sync to every member computer on the network. `me` entries sync only to the writer's own computers (signed in), or stay on this computer (not signed in). A member who leaves stops receiving entries; what they already hold they keep, as with messages they already read. The privacy page says so.
- **One line per subject at read time.** Recall collapses the replace chain: the current fact first, then "was X (who, when)". Copies cannot pile up on the page.
- **Contested.** Two `replace` entries on the same fact, or a `remember` whose subject matches a current fact with a different value and no `replaces`, means two people hold different current values. Recall shows both with names and dates and the word "contested". Nobody's entry overwrites anybody's. Whoever settles it writes a `replace` of both.
- **Search.** SQLite FTS5 (BM25) over subject and fact. Keyword only; §6 measures what that costs and §5 says what a workspace adds.
- **Storage.** `state.db`, already 0700 on laptops, already snapshotted to S3 per workspace for the cloud member.

### 4.2 Writing

No model runs in the engine. The agents already on the network are the models.

- **`remember(subject, fact, kind, scope?)`**: before storing, the engine returns the current facts on that subject ("Existing: production database = Postgres 14 (Ben, 2 Aug). A change? Call again with `replaces`."). The agent, which has just read the whole conversation, decides new, change or disagreement. That is the linking step in §3, done where the context is.
- **When:** the person says "remember", or the agent judges something worth keeping. The tool description and the session-start block tell it to call `remember` when it finishes a task. No hook asks: `Stop` fires after every turn, not at session end, and `SessionEnd` cannot reach the model (`src/hook.rs`), so a forced "ask at the end" is either every turn or never. We measure the share of sessions that write anything; if it is low, the follow-up is a once-per-session `Stop` nudge using the same block mechanism mail delivery uses today.
- **Scope default is `me`.** The agent may say "this would help the team, share it?" and writes `team` only on a yes, or when the person says so. A laptop session holds secrets and half-formed thoughts; nothing from it reaches a teammate without the person choosing. `lightbulb memory share <id>` does the same by hand. Room agents (§5) write `team`, since a room is shared already.
- **Secrets.** The engine has no scrubber today (Hermes's `redact_for_egress` lives in the runtime image). `remember` checks the text against a small pattern list in the engine (API keys, tokens, private-key blocks, credentials in URLs, ported from the backend's `session_redact.py` where they apply) and refuses the entry with a reason rather than storing a scrubbed version: a secret in a replicated log cannot be un-read.

### 4.3 Reading

- **`recall(question, budget?)`**: searches the local copy and returns a page: contested facts first, then current facts with history, newest first, packed into the budget. Local, so it works offline and costs nothing.
- **Session start:** the hook injects the current team and personal facts for this repo, at most about 400 tokens. Never before every prompt (past.dev's plugin injects 850–1,200 tokens every prompt; measured).
- **Several facts:** the agent calls `recall` again with narrower questions, as agents already do.
- **`lightbulb memory`**: list, show (history and who), forget, export, share, `last` (what was injected last time).

## 5. The workspace engine: extraction on AgentCore, retrieval on Postgres, five stages in our code

The target is past.dev's 10M split (85.03), not just the easy splits. `2026-10-27-past-dev-how-it-works.md` shows what that takes: typed, dated memories plus searchable raw turns, retrieved by several strategies, fused, reranked and composed to a budget. AgentCore Memory's search is one strategy (semantic over records, top 100, metadata filters) with no keyword arm, no reranker and no control over fusion, so it cannot be the whole pipeline. Hindsight (MIT) implements exactly this pipeline, but as a 180,000-line service with 94 dependencies; its `engine/search/` (3,600 lines: fusion, reranking, temporal extraction, keyword term selection, fact budget) is the reference we lift algorithms from, not a service we run.

| Part | What | Where |
|---|---|---|
| Extraction and consolidation | AgentCore semantic strategy with override instructions and a metadata schema: `kind` (claim/observation/rule/mention/anchor), `entity`, `attribute`, `status` (current/contested) model-filled with allowed values; `validFrom`/`validTo` as NUMBER (epoch seconds). Consolidation rule: one record per entity+attribute, current value first, history after; two people's different values become one `contested` record naming both; never a pick | AgentCore Memory (existing resource, `UpdateMemory`), managed and billed per record |
| Fallback extraction | If consolidation cannot set `validTo`/`status` on the record it rewrites, or cannot ingest 100M tokens in a day, the same prompts run in AgentCore's self-managed strategy (its batches → our worker → `BatchCreate/UpdateMemoryRecords`) | Lambda, CDK pattern from `ses-inbound-stack.ts` |
| Index | A `memory` database on the existing Aurora Serverless v2 cluster (`database-stack.ts`: Postgres 16, pgvector, scale-to-zero, 0–4 ACU). Tables: `records` (text, kind, entity, attribute, value, speaker, valid_from, valid_to, status, replaces, source ids, embedding, tsvector), `turns` (raw room turns, embedding, tsvector), `links` (entity ↔ record), `entries` (the log, mirrored). Embeddings from Bedrock (Titan Text Embeddings V2 or Cohere Embed; chosen in week 1 on price and the 100K score) | Aurora, new database; backend gets security-group ingress like Paperclip has |
| Recall (five stages) | 1 expand: one Haiku call splits a multi-part question into sub-questions (skipped for single-part); 2 search, per sub-question, four arms in SQL: cosine over `records` and `turns`, `ts_rank` keyword, entity links, time window from the question; 3 fuse: reciprocal rank fusion, k=60; 4 rerank: Bedrock `Rerank` with `cohere.rerank-v3-5:0` over ≤100 candidates ($2 per 1,000 queries); 5 compose to the token budget: contested first, current claims, raw turns with excerpts, history | `apps/backend/core/services/memory_recall.py`, one function, called by the Hermes plugin, the hosted tools and the harness |
| Writers into the index | The cloud member: polls new and updated AgentCore records (`ListMemoryRecords`, `updatedAt AFTER` last sync) into `records` and, for room facts, into the log as `team` entries signed "workspace"; mirrors every log entry into `entries`; mirrors room turns into `turns` as the plugin writes them | `apps/infra/hermes/bridge/network.py` (poll), plugin `sync_turn` (turns) |

One source, two indexes, one direction per source: no loop. The log is what people see and correct; AgentCore extracts; Postgres is searched. AgentCore's own search is not used by the product once Postgres is live (it stays for the Memory tab's private DM scope, below).

- **DMs stay private, as today.** A 1:1 with a workspace agent keeps extracting into that person's private AgentCore scope (`/owners/<owner>:<user>/`), which never enters the log. The Memory tab shows that private list beside the team list from the log, and `share` writes a team entry signed by the workspace carrying the person's name. Anything else would make a DM a team fact, which is a privacy regression.
- **`forget`** deletes the log entry everywhere and its row in `entries`; a record's row in `records` and its AgentCore record go with it. The room message it came from stays until the person deletes it, as today. Any workspace member can forget a team entry, as any member can delete a team fact today.
- **Wipe:** a member wipe deletes their private scope and tombstones every entry they signed; an owner wipe does that for everyone plus the workspace's rows in every table and its AgentCore namespaces.
- **When the runtime is parked:** the Memory tab reads Postgres and the cloud member's `state.db` snapshot in S3 (the backend already reads from S3 rather than cold-start a parked runtime, ~55 s). Writes from the tab (forget, share) queue and apply when the cloud member next runs, shown as pending until then.
- **Cutover:** import today's team records once as `team` entries signed "workspace (imported)" and into `records`; private scopes are untouched; the Hermes plugin's prefetch and search tool call the backend's recall (fixing today's defect where the search text is routing JSON).
- **Hard boundary:** laptop and hosted tools never touch AgentCore or Postgres directly. Laptops read the log; hosted tools and the plugin call the backend's recall, which checks the member against the workspace. Only the runtime and the backend hold AgentCore and database credentials.

## 6. Measurement: the 10M target

The target is past.dev's published 10M result (85.03 overall; multi-session 28.2, information extraction 68.8, contradiction 73.1, knowledge update 92.5, the other six 93–100), on their harness, so the comparison is direct. We do not promise it; we build toward it and publish what we get.

1. **Same harness.** Fork past.dev's MIT-licensed BEAM harness and replace only the memory calls with an adapter that ingests through AgentCore events (per-turn `eventTimestamp`) and recalls through §5's five stages. ExaBase's prompts hash-checked, same answer and judge model, 100 results within 8,000 tokens, each question once, `queryTimestamp` honoured through `valid_from`/`valid_to`.
2. **Baselines on 100K** (~$100): past.dev rerun (our ~149,900 free credits), AgentCore built-in as today, ours. Judge each three times to see the noise. Optional fourth arm: Hindsight's Docker image behind the same adapter, to see what a Hindsight-class pipeline scores with correct dates.
3. **Where the points are**, from `2026-10-27-past-dev-how-it-works.md`: their page is 80% raw turns plus 20% typed memories; the hard abilities are carried by the memory layer (knowledge update 0.93 with the current value as a claim on the page, 0.71 in a raw turn only, 0.38 missing). Their leaks: duplicate current claims on 62 of 700 pages; the replacement absent for 133 of 170 superseded claims; aggregation across sessions. Ours fixes the first two by construction and attacks the third with expansion plus exhaustive listing by entity (`links`), which a top-k search cannot do.
4. **Tuning** on 100K and 1M, two-week timebox, each stage measured against the judge's spread: no expansion vs expansion; RRF vs RRF+rerank; memories-first vs mixed composition; Titan vs Cohere embeddings; built-in vs override extraction. 10M is run only on the tuned configuration (hours per run, ~$150), untouched during tuning.
5. **Publish** results, prompts, harness commit, cost and variance, whatever they show. The number we lead with publicly is still the team-memory set from our own rooms (with consent, anonymised): one person's agent answering from what another said, naming the source, surfacing disagreements. The public line is about that and where the data lives, never a competitor's name.

## 7. Cost

- **Free network: $0 to us.** The person's agent writes, their computer stores and searches, the network carries it.
- **Workspace, per month, 5 people, 3,000 room messages, ~2,000 records (assumptions to measure):** extraction with overrides on a Haiku-class Bedrock model ~$1.50; AgentCore records $0.25 per 1,000 (~$0.50); embeddings for 3,000 turns and 2,000 records (<$0.10 at Cohere's $0.10 per million tokens; Titan is cheaper); Aurora: the cluster already exists and scales to zero, a workspace's queries add well under 1 ACU-hour a day (~$4 at $0.12 per ACU-hour, shared across all workspaces); rerank $2 per 1,000 recalls × ~3,000 room turns (~$6); short-term events <$0.10. **About $8–12 per workspace, under a quarter of the plan**, and the rerank line is the lever if it must come down (rerank only multi-part questions). Laptop recall is local and free. Bedrock prices are confirmed in week 1.
- **Benchmarks:** ~$100 for the baselines; ~$15–20 per 100K tuning run, ~$60 per 1M run; a 10M run ≈ 100M tokens: extraction ~$50 on Haiku, embeddings ~$10, AgentCore records (~400K, deleted after) ~$100 for the month, Aurora at 4 ACU for a day ~$12, answer and judge ~$20: **~$200 per 10M run**; ~$800 all in.
- **People's tokens:** ~400 at session start, plus whatever `remember` and `recall` calls the agent chooses to make.

## 8. Privacy

Same promise as messages, same transport. Free memory lives on members' computers and moves only between them, encrypted end to end; we never see it. Nothing leaves a laptop session for a teammate unless the person says yes. In a workspace the cloud member holds a copy, as it already holds the workspace's messages, and only it touches AgentCore. `forget` removes content from every copy that comes online. Owner and member wipes extend to the mirror and extraction namespaces. The privacy policy rewrite and `docs/security.md` describe this before release.

## 9. Plan

Working days with agents. Memory ships to users before anything is benchmarked.

| Step | Days | What | Done when |
|---|---|---|---|
| 1. The log | 5–6 | Signed per-author entries and tombstones; `memory` capability in `hello` and paged sync; FTS5; `remember`/`recall` in the shared tool spec with the secret check; session-start block; `lightbulb memory`; two-computer and mixed-version tests; CLI release; privacy and security docs | A fresh install on two computers shares a fact with a name on it; an old build keeps messaging |
| 2. Harness and baselines (parallel) | 3 | Fork; adapters for past.dev, AgentCore built-in and ours; 100K ×3, judge ×3; Bedrock prices, consolidation metadata behaviour and AgentCore throughput checked | We know where each arm stands, the judge's noise, and whether the fallback extractor is needed |
| 3. Engine | 8 | `memory` database and migrations on the Aurora cluster; backend ingress; embeddings and rerank permissions; override instructions and metadata schema on a test memory resource; cloud member polling into `records` and the log; turns mirror; the five-stage recall; plugin and hosted tools call it; Memory tab; one-time import; wipes | Our own workspace runs on it for a week; the harness runs end to end on 100K |
| 4. Tuning | timebox 10 | Stage-by-stage on 100K and 1M; probe cases every run; then one 10M run | 10M result in hand, whatever it is |
| 5. Publish | 3 | Final runs; team-memory set; results repo; /network page | Numbers anyone can rerun |

About five to six weeks; user-visible memory at the end of week one.

## 10. Decisions for the founder

1. Memory is part of the free network, peer to peer, no account, including team memory between members. Without an account, personal memory is per computer; the free account syncs your own computers. (The alternative is a pairing command for people who never sign in: a new roster entry type and CLI command in step 1.)
2. Personal by default; sharing to the team needs the person's yes.
3. AgentCore Memory stays as the workspace's extractor; retrieval moves to Postgres on the cluster we already run, with the five stages in our code. AgentCore's own search is not used by the product. (The alternative, AgentCore search alone, cannot reach the 10M target: one strategy, no keyword arm, no reranker.)
5. The 10M split is the target and we spend ~$200 a run on it, after tuning on the small splits only.
4. We publish benchmark numbers with cost and variance even when they are below the leader, and the public line is about team memory and where data lives.

## 11. Calls I made (check these while reading)

- Keyword search on the free tier, search by meaning only in workspaces, until the harness says keyword is not enough.
- The agent decides new/change/disagreement at write time, using the neighbours `remember` returns.
- The cloud member polls AgentCore for extracted records instead of a push; it is up only while the workspace runtime is, so facts cross between offline teammates on the workspace's schedule, not instantly.
- No hook forces a write; the agent calls `remember` when it finishes a task, and we measure whether that is enough before adding a once-per-session nudge. No capture of tool calls or diffs.
- Without sign-in, personal memory is per computer; syncing your own computers is what the free account adds.
- A flat signed set with tombstones, not the roster's DAG; memory gated on a `hello` capability so old builds are not cut off.
- `remember` refuses entries that match a secret pattern instead of scrubbing them.
- DMs keep today's private AgentCore scope and never enter the log.
- The Memory tab reads the S3 snapshot when the runtime is parked and queues its writes.
- Hindsight's retrieval algorithms are lifted (MIT), not its service: 180,000 lines and 94 dependencies for 3,600 lines of search code we need.
- Rerank through Bedrock's managed Cohere model rather than a local cross-encoder, so no torch in the backend image.

Settled with a builder check (seven questions from a reviewer who could not ask the author); the one question that was the founder's is in §10.

## Sources

CLI v0.6.5 (`f9ea8d5`): `src/roster.rs`, `src/net/mod.rs`, `src/net/frames.rs`, `src/store.rs`, `src/hook.rs`, `src/embed.rs`, `docs/security.md`. Monorepo: `apps/backend/core/services/network_tools.json`, `apps/infra/hermes/bridge/network.py`, `apps/infra/hermes/plugins/memory/agentcore/__init__.py`, `apps/backend/core/services/workspace_memory.py`, `apps/infra/lib/stacks/container-stack.ts`. AgentCore: [overrides](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-custom-strategy.html), [semantic prompts](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-system-prompt.html), [metadata and filters](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/long-term-memory-metadata.html), [RetrieveMemoryRecords](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_RetrieveMemoryRecords.html), [pricing](https://aws.amazon.com/bedrock/agentcore/pricing/). past.dev harness: `pastdotdev/benchmarks/beam`.
