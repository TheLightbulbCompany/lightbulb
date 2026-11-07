# Memory, capture and the evaluator on the network: design (27 October 2026, v5)

Status: founder-reviewed draft; builder-checked (v4). v5 restores the four items the founder ruled on in the Beacon pass that v4 had dropped (session capture, a first-party evaluator, sign-in suggested at install, security facts with evidence), stages the workspace engine as AgentCore-only first with Postgres as a measured second stage, and prices the benchmark programme at a fraction of v4. Research: `docs/research/2026-10-27-memory-problem.md`, `2026-10-27-oss-beam-failures.md`, `2026-10-27-past-dev.md`, `2026-10-27-past-dev-how-it-works.md`, `2026-10-27-agent-beacon-pass.md`, `2026-10-27-network-memory-inventory.md`, `2026-10-27-memory-decision.md`.

Vocabulary: a *harness* here is an agent program we hook into (Claude Code, Codex, Cursor, OpenCode, Grok CLI; Hermes in the workspace). The *benchmark runner* is the script that runs BEAM.

## 1. What this is

Memory for people and their agents on the Lightbulb network: any agent on the team remembers what any other agent on the team learned, across people, harnesses and sessions. Beside it: a local record of what each agent did (capture), an evaluator that turns that record into proposed lessons and findings a person approves, and a security page whose every claim carries its evidence. On the free network memory and capture are peer to peer and local; nothing touches our servers. A workspace adds an always-on copy, room agents that read and write memory, search by meaning, facts extracted from room conversation, the evaluator, a timeline of every agent and machine, and the Memory tab.

## 2. The two networks and the business

The business is messaging for people and their AI agents. The free network is the distribution: it must spread on its own, cost us nothing per install, and be trusted with nothing leaving the machine. The workspace is the revenue: $50 a month for up to five people, $20 a member after (the plan's number on the wall: ten founder-led teams with a card on file by 4 January 2027).

| | Free network (no account) | Signed in (free) | Workspace (paid) |
|---|---|---|---|
| What exists today | Messaging between your agents and your teammates' agents; presence; approvals | Hosted apps (Claude app, ChatGPT) reach your agents | Rooms, always-on agents, tasks, spend gates, Memory tab on AgentCore (room agents only) |
| Memory | Personal memory across every harness on this computer; team memory with the people on your network; keyword search; `lightbulb memory` | Personal memory syncs between your own computers (sign-in is how the engine knows two computers are one person) | A copy that is up while the workspace runs; room agents and hosted apps read and write it; search by meaning; facts extracted from room conversation; Memory tab with source, history, contested; wipe |
| Capture | Every session your agents run, recorded on this computer: tool calls, durations, tokens. `lightbulb sessions` to browse. Nothing leaves | Choose at sign-in: keep local, upload metadata only (tokens, durations, tool names), or upload full sessions. One timeline of your agents across computers. Sessions follow you | Timeline of every agent and machine in the workspace; tokens per agent, person and room in Usage; a session shared by link |
| Evaluator | — | Uploaded full sessions are read by the evaluator; a capped number of drafted lessons a month | Unlimited drafts; findings (repeated failures, a secret in a tool call, what worked); approve in the Memory tab or a room and it becomes team memory |
| Why the line is here | Cross-person memory with no server is what nobody ships and the reason to install. $0 to us | An account is what an invite attaches to; the evaluator needs the session on our side | Everything that needs a server that is on when you are not, or a model we pay for |
| Cost to us | $0 | Storage and a capped evaluator, cents per person | ~$6–10 per workspace a month (§9) |

Two things this does not do: charge solo developers for memory, and put anything on our servers for someone who never signed in.

## 3. What the research says we must get right

Every memory system loses the same way (memory-problem §2, §6, §7; past-dev-how-it-works §2): the same fact stored many times crowds out the one update; an update in other words is not linked to the old value; the replaced value ranks above the current one; two people disagreeing is settled by recency or not noticed; totals across sessions miss facts. The product rules in §4 and §5 exist to avoid these, whatever the benchmark says.

## 4. The log: how memory moves

### 4.1 What is stored

A replicated set of signed entries beside the roster. Not the roster's hash-linked DAG: `me` entries are withheld from teammates and `forget` strips content, and both break parent links and content hashes. Each entry is signed by its author and numbered per author; peers exchange "highest number I hold per author" and page the gap, the way the roster pages entries today. Order comes from `replaces` and `when`, so every member computes the same memory from the same set whatever order it arrives. A `forget` is a signed tombstone that keeps the id and drops the body. Memory frames are gated on a `memory` capability in `hello`, so an older build ignores them and keeps messaging; no protocol bump. Entries are small (the roster's 16 KB cap holds).

```
remember  {id, subject, fact, kind: fact|decision|lesson|preference, when, source: {harness, repo, session}, scope: me|team}
replace   {id, replaces: [ids], fact, when, source}     a change; the old entries become history
forget    {id: target}                                   the content is dropped on every copy
```

- **Scope.** `team` entries sync to every member computer on the network. `me` entries sync only to the writer's own computers (signed in), or stay on this computer (not signed in). A member who leaves stops receiving entries; what they already hold they keep, as with messages they already read. The privacy page says so.
- **One line per subject at read time.** Recall collapses the replace chain: the current fact first, then "was X (who, when)". Copies cannot pile up on the page.
- **Contested.** Two `replace` entries on the same fact, or a `remember` whose subject matches a current fact with a different value and no `replaces`, means two people hold different current values. Recall shows both with names and dates and the word "contested". Nobody's entry overwrites anybody's. Whoever settles it writes a `replace` of both.
- **Search.** SQLite FTS5 (BM25) over subject and fact.
- **Storage.** `state.db`, already 0700 on laptops, already snapshotted to S3 per workspace for the cloud member.

### 4.2 Writing

No model runs in the engine. The agents already on the network are the models.

- **`remember(subject, fact, kind, scope?)`**: before storing, the engine returns the current facts on that subject ("Existing: production database = Postgres 14 (Ben, 2 Aug). A change? Call again with `replaces`."). The agent, which has just read the whole conversation, decides new, change or disagreement.
- **When:** the person says "remember", or the agent judges something worth keeping. The tool description and the session-start block tell it to call `remember` when it finishes a task. No hook asks: `Stop` fires after every turn, not at session end, and `SessionEnd` cannot reach the model (`src/hook.rs`). We measure the share of sessions that write anything; if low, a once-per-session `Stop` nudge follows, using the block mechanism mail delivery uses today. The evaluator (§6) is the other path: it proposes what the agent did not write.
- **Scope default is `me`.** The agent may say "this would help the team, share it?" and writes `team` only on a yes. `lightbulb memory share <id>` does the same by hand. Room agents write `team`, since a room is shared already.
- **Secrets.** The engine has no scrubber today (Hermes's `redact_for_egress` lives in the runtime image). `remember` checks the text against a small pattern list (API keys, tokens, private-key blocks, credentials in URLs) and refuses the entry with a reason rather than storing a scrubbed version: a secret in a replicated log cannot be un-read.

### 4.3 Reading

- **`recall(question, budget?)`**: searches the local copy and returns a page: contested facts first, then current facts with history, newest first, packed into the budget. Local, offline, free.
- **Session start:** the hook injects the current team and personal facts for this repo, at most about 400 tokens. Never before every prompt (past.dev's plugin injects 850–1,200 tokens every prompt; measured).
- **`lightbulb memory`**: list, show (history and who), forget, export, share, `last` (what was injected last time).

### 4.4 Install

`lightbulb setup` offers the free sign-in first and says what it adds (memory and sessions follow you across computers; the Claude app reaches your agents; the evaluator reads your sessions if you upload them), with local-only one keypress away. No backfill of old sessions, no training clause. The capture upload choice (off, metadata only, full) is asked once at sign-in and changeable with `lightbulb sessions upload`.

## 5. The workspace engine

**Stage one, AgentCore only.** The target is past.dev's 10M split (85.03). `past-dev-how-it-works.md` shows what that takes: typed, dated memories plus searchable raw turns, retrieved several ways, fused, reranked and composed to a budget. AgentCore Memory gives extraction and merging under our instructions, a metadata schema, semantic search with exact filters and as-of filters; what it lacks is a keyword arm and cheap bulk storage. The rerank and composition stages are our code either way, so stage one needs no new infrastructure.

| Part | What | Where |
|---|---|---|
| Extraction and merging | The existing semantic strategy with override instructions and a metadata schema: `kind` (claim/observation/rule/mention/anchor), `entity`, `attribute`, `status` (current/contested), model-filled with allowed values; `validFrom`/`validTo` as numbers. Merge rule: one record per entity+attribute, current value first, history after; two people's different values become one `contested` record naming both; never a pick. Record text in past.dev's shapes (speaker prefix; `entity attribute: value`; resolved dates; rules with "applies to") | AgentCore Memory, existing resource, one `UpdateMemory` |
| Fallback extraction | If merging cannot set `validTo`/`status` on the record it rewrites, or cannot ingest 100M tokens in a day, the same prompts run in AgentCore's self-managed strategy (its batches → our worker → batch record writes) | Lambda, CDK pattern from `ses-inbound-stack.ts` |
| Three namespaces per workspace | `facts` (extracted, as today); `turns` (raw room turns written as plain records, no strategy, so search by meaning covers them); `entries` (the log mirrored in, no strategy). The private DM scope is unchanged | AgentCore Memory |
| Recall, five stages | 1 expand: one Haiku call splits a multi-part question (skipped for one part); 2 search, per part: semantic over `facts` (current first by `status` filter), `turns` and `entries`, up to 100 each; exact `entity` filter for exhaustive listing (what aggregation questions need and a top-k search cannot give); `validFrom`/`validTo` filters for as-of; 3 fuse: reciprocal rank fusion, k=60; 4 rerank: Bedrock `Rerank`, `cohere.rerank-v3-5:0`, ≤100 candidates; 5 compose: contested first, current claims, raw turns with excerpts, history, to the token budget, always filled | `apps/backend/core/services/memory_recall.py`, one function, called by the Hermes plugin, hosted tools, the Memory tab and the benchmark runner |
| Sync | The backend pulls new and updated `facts` records (`ListMemoryRecords`, `updatedAt AFTER` last sync) and imports them into the log through the cloud member as `team` entries signed "workspace"; mirrors new log entries into `entries`; runs when a recall finds the sync older than 60 s and on a five-minute sweep while the runtime is up | backend `memory_sync.py`, cloud member ops |

One source, one direction per source, no loop. The log is what people see and correct; AgentCore extracts, stores and searches.

**Reconciliation when something changes.** One rule in both places: the same subject with two different current values and no `replaces` is contested; nobody's value overwrites anybody's. Laptop facts never enter AgentCore's merge step (they live in the strategy-less `entries` namespace), so AgentCore cannot pick a winner.
- A laptop `replace` syncs to the cloud member; the backend mirrors it into `entries` and marks the replaced record `status=replaced`. A `forget` deletes the mirrored record.
- A room conversation that changes a fact makes AgentCore rewrite that record; the sync imports the rewrite into the log as a `replace` of the entry it imported last time (a record-to-entry mapping is kept), so laptops see the change with history.
- The same subject changed on a laptop and in a room: the log's replay marks the pair contested on every laptop; the workspace's recall marks them contested on the page, since they carry the same entity and attribute with different values. Whoever settles it writes a `replace` of both, and that flows out through the same two paths.
- Forgetting an imported fact from a laptop also deletes its AgentCore record, so it is not re-imported; the room message stays until someone deletes it.

**Stage two, Postgres, on a measured trigger.** Per-question results from stage one are split the way `past-dev-how-it-works.md` §2 splits past.dev's: gold evidence on the page as a memory, only in a raw turn, or nowhere. If "nowhere" is dominated by exact strings (versions, IDs, numbers) present in turns that semantic search missed, a keyword arm is the fix: a `memory` database on the Aurora cluster we already run (`database-stack.ts`: Postgres 16, pgvector, scale-to-zero), mirroring `facts`, `turns` and `entries` with tsvector and embeddings, Hindsight's four-arm design lifted (MIT; its `engine/search/`, 3,600 lines, not its 180,000-line service). Also the fallback if AgentCore storage at 10M scale costs more than Aurora. Not before the data says so.

- **DMs stay private, as today.** A 1:1 with a workspace agent keeps extracting into that person's private AgentCore scope, which never enters the log. The Memory tab shows that private list beside the team list; `share` writes a team entry signed by the workspace carrying the person's name.
- **`forget`** deletes the log entry everywhere and the mirror record by the id we created; an extracted record's log entry can be forgotten the same way and its AgentCore record is deleted. Any workspace member can forget a team entry, as any member can delete a team fact today.
- **Wipe:** a member wipe deletes their private scope, their uploaded sessions and drafts, and tombstones every entry they signed; an owner wipe does that for everyone plus all three namespaces.
- **When the runtime is parked:** the Memory tab reads AgentCore and the cloud member's `state.db` snapshot in S3; writes from the tab queue and apply when the cloud member next runs, shown as pending.
- **Cutover:** import today's team records once as `team` entries signed "workspace (imported)"; private scopes untouched; the plugin's prefetch and search tool call the backend's recall with the user's text only (fixing today's defect where the search text is routing JSON).
- **Hard boundary:** laptop and hosted tools never touch AgentCore directly. Laptops read the log; hosted tools and the plugin call the backend, which checks the member against the workspace.

## 6. Capture and the evaluator

**Capture, local first.** The hooks `setup` already installs for all five harnesses fire on session start, every tool call, and stop/end. The engine records, in `state.db`: sessions (harness, computer, repo, branch, start, end, prompt and completion tokens) and tool calls (name, a 200-character argument summary with secrets refused, duration, success). Never file contents, diffs or command output. Folders hidden with `lightbulb folder off` are not captured. `lightbulb sessions list|show|forget|export`. This is the audit trail the security page promises, the token source for Usage, and the evaluator's raw material.

**Upload, by choice.** Signed in, the person chooses once: keep local, metadata only, or full. Uploads go to Lightbulb, under the person's identity, never to a third party. Retention: 30 days on a free account, a year in a workspace (my call). Teammates still see presence, not sessions; in a workspace the timeline shows sessions to the workspace's members because the workspace pays for and owns that record, and the privacy page says so.

**The evaluator, first-party, on AgentCore.** Uploaded full sessions are written as events into AgentCore's episodic strategy under the person's namespace. Its reflections across episodes are exactly Beacon's findings: which approaches kept failing and what resolved them, patterns worth a rule, a secret that appeared in a tool call. Each reflection becomes a draft with its evidence (the session ids and turns). Drafts appear in the Memory tab's "Suggested" list and, for a solo signed-in user, in `lightbulb memory drafts`. Approve → a `remember` (team in a workspace, me for a solo user), signed by the approver. Dismiss → gone. Nobody's session is scored by a third party, and no score without text.

**Usage, timeline, share.** Token counts from capture metadata land in the workspace's Usage view per agent, person and room (the finance layer's tables, PRs #4432/#4433). The timeline lists sessions across every agent and machine. A session can be shared by link (signed URL, seven days). The public token leaderboard stays out: a public ranking is a launch mechanic; the private view is the feature.

## 7. Security: the claims and the evidence

A page a founder can read in two minutes, every line verifiable. Not a compliance programme: SOC 2 and SAML are months, and no customer has asked.

| Claim | Evidence |
|---|---|
| Your keys never leave your computer | `identity.key`, mode 0600, never uploaded (`docs/security.md`); the cloud member's key comes from a boot secret and is never written to S3 (`bridge/network.py`) |
| Relays see ciphertext, keys, addresses, timing and sizes, never text | iroh end-to-end encryption between member keys (`docs/security.md`) |
| Memory lives on members' computers and moves only between them | §4.1; sync frames carry entries only to admitted members, `me` entries only to your own computers |
| Nothing from a laptop session leaves without sign-in and a choice | §4.4, §6: capture is local by default; upload is a setting you set |
| Secrets are refused before they are stored | §4.2 pattern list; `remember` returns the reason |
| Every fact carries who, where and when; disagreements are shown, not settled | §4.1 entries and contested status |
| You can see exactly what your agent was told | `lightbulb memory last`; the session-start block is logged locally |
| Forgetting reaches every copy that comes online | §4.1 tombstones; a computer offline forever keeps what it had, as with messages |
| Agents ask before they spend | spend gates and approvals, shipped |
| Teammates see presence, not your sessions or files | `docs/security.md` "What teammates see"; capture stays local unless uploaded |
| In a workspace, a wipe deletes everything, including the extracted facts | #4030, extended to the three namespaces, uploads and drafts (§5) |

The page never names a competitor.

## 8. Measurement: the 10M target, cheaply

The target is past.dev's published 10M result (85.03 overall; multi-session 28.2, information extraction 68.8, contradiction 73.1, knowledge update 92.5, the other six 93–100), on their runner, so the comparison is direct. We build toward it and publish what we get.

1. **Same runner.** Fork past.dev's MIT-licensed BEAM runner and replace only the memory calls with an adapter that ingests through the backend's dev-only benchmark endpoints (per-turn timestamps) and recalls through §5. ExaBase's prompts hash-checked, same answer and judge model for the published numbers, 100 results within 8,000 tokens, each question once.
2. **A free tuning signal.** The dataset's gold answers let us score "is the gold evidence on our page" with no answer model and no judge, the same split used on past.dev's pages (memory / raw turn only / nowhere). Tuning loops cost cents.
3. **Cheap loops, one expensive run.** A Haiku answerer on 100K (~$3) for deltas; three conversations of 10M while developing (`--conversations 3`); the fixed model once per split at the end, judge three times on 100K only. A raw-turns-only 10M run first (~$30) gives the retrieval floor before extraction is spent on. Records deleted the same day.
4. **Where the points are**: their page is 80% raw turns plus 20% typed memories; the hard abilities are carried by the memory layer (knowledge update 0.93 with the current value as a claim on the page, 0.71 in a raw turn only, 0.38 missing). Their leaks: duplicate current claims on 62 of 700 pages; the replacement absent for 133 of 170 superseded claims; aggregation across sessions. Ours fixes the first two by construction and attacks the third with expansion plus exhaustive listing by entity.
5. **Publish** results, prompts, runner commit, cost and variance, whatever they show. The number we lead with publicly is still the team-memory set from our own rooms (with consent, anonymised). The public line is about that and where the data lives, never a competitor's name.

## 9. Cost

- **Free network: $0 to us.** The person's agent writes, their computer stores and searches, the network carries it. Capture is local.
- **Signed in:** uploads to S3 (a session ~50 KB; cents), the evaluator on a capped number of sessions a month (AgentCore episodic extraction on a Haiku-class model: about a cent a session), a few cents per active person.
- **Workspace, per month, 5 people, 3,000 room messages, ~2,000 facts, 200 uploaded sessions (assumptions to measure):** extraction with overrides ~$1.50; AgentCore records across the three namespaces (~5,000 at $0.25 per 1,000) ~$1.25; retrievals ($0.50 per 1,000 × ~3,000 room turns × ~4 calls) ~$6; rerank ($2 per 1,000) ~$6, or ~$1 if only multi-part questions are reranked; evaluator ~$2; short-term events <$0.10. **About $6–10 per workspace**, and rerank-only-when-needed is the lever. Bedrock prices confirmed in week one.
- **Benchmarks:** ~$100 for the baselines (past.dev rerun on free credits; AgentCore built-in; ours); cents per tuning loop on the free signal; ~$3 per Haiku 100K loop; final runs ~$15 (100K), ~$60 (1M), ~$50–80 (10M, records deleted the same day); **~$200–250 all in.**
- **People's tokens:** ~400 at session start, plus the `remember` and `recall` calls the agent chooses to make.

## 10. Privacy

Same promise as messages, same transport. Free memory and capture live on members' computers; memory moves only between them, encrypted end to end; capture moves only when the person uploads it. In a workspace the cloud member holds a copy of memory, as it already holds the workspace's messages, and only the backend and the runtime touch AgentCore. `forget` removes content from every copy that comes online. Wipes extend to the three namespaces, uploads and drafts. The privacy policy rewrite (Termly, open) and `docs/security.md` describe memory, capture, upload and the evaluator before release.

## 11. Plan

Split by who runs the code, in `docs/superpowers/plans/`:

| Plan | Days | What | Done when |
|---|---|---|---|
| Free network, the engine (`2026-10-27-memory-free-network.md`, Rust, the CLI) | 9 | Signed entries and tombstones; capability-gated sync; FTS5; `remember`/`recall`; session-start block; `lightbulb memory`; cloud-member ops; setup suggests sign-in; local capture and `lightbulb sessions`; upload by choice and drafts from the CLI; docs; release v0.7.0 | Two computers share a fact with a name on it; an old build keeps messaging; nothing uploads with mode off |
| Workspace (`2026-10-27-memory-workspace.md`, backend, AgentCore, plugin, frontend) | 12 | Overrides and metadata schema; turns and entries namespaces; five-stage recall; sync; routes and tool specs; plugin; wipes, import, Memory tab; sessions endpoints; the evaluator on the episodic strategy; approve drafts; usage, timeline, share; the security page | Our workspace runs on it a week; a real session's finding is approved into team memory; the security page is live |
| Benchmark runner (`2026-10-27-memory-beam-runner.md`) | deferred | Built only when we decide to run benchmarks | — |

About four to five weeks with agents, native execution; user-visible memory at the end of week one. The AWS docs settle how metadata reaches merged records (re-extraction from the record text on consolidation), so no probe run is needed; the first dev deploy smoke-checks it for cents. Postgres (stage two) only if the per-question data ever asks for it.

## 12. Decisions for the founder

1. Memory is part of the free network, peer to peer, no account, including team memory between members. Without an account, personal memory is per computer; the free account syncs your own computers. (Alternative: a pairing command for people who never sign in.)
2. Personal by default; sharing to the team needs the person's yes.
3. AgentCore Memory is the workspace's extractor, store and search in stage one; Postgres only on the measured trigger in §5.
4. Capture is local by default; the upload choice is the person's; workspace members see the workspace's timeline.
5. The evaluator is first-party on AgentCore's episodic strategy; no third-party scorer.
6. The security page makes the claims in §7 and no compliance claims.
7. We publish benchmark numbers with cost and variance even when below the leader.

## 13. Calls I made (check these while reading)

- Keyword search on the free tier; search by meaning in workspaces; Postgres only when the per-question data shows exact-string misses.
- The agent decides new/change/disagreement at write time, using the neighbours `remember` returns.
- No hook forces a write; the evaluator proposes what the agent did not write; a once-per-session nudge only if sessions write nothing.
- A flat signed set with tombstones, not the roster's DAG; memory gated on a `hello` capability so old builds are not cut off.
- `remember` refuses entries that match a secret pattern instead of scrubbing them.
- DMs keep today's private AgentCore scope and never enter the log.
- The Memory tab reads AgentCore and the S3 snapshot when the runtime is parked and queues its writes.
- Capture records tool names, argument summaries, durations and tokens; never file contents, diffs or output.
- Upload retention 30 days free, a year in a workspace.
- Hindsight's retrieval algorithms are a reference for stage two, not a service we run; rerank through Bedrock's managed Cohere model, no torch in the backend image.

## Sources

CLI v0.6.5 (`f9ea8d5`): `src/roster.rs`, `src/net/mod.rs`, `src/net/frames.rs`, `src/store.rs`, `src/hook.rs`, `src/embed.rs`, `src/agents/*.rs`, `docs/security.md`. Monorepo: `apps/backend/core/services/network_tools.json`, `apps/infra/hermes/bridge/network.py`, `apps/infra/hermes/plugins/memory/agentcore/__init__.py`, `apps/backend/core/services/workspace_memory.py`, `apps/infra/lib/stacks/container-stack.ts`, `database-stack.ts`. AgentCore: [overrides](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-custom-strategy.html), [semantic prompts](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-system-prompt.html), [episodic](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/episodic-memory-strategy.html), [metadata and filters](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/long-term-memory-metadata.html), [RetrieveMemoryRecords](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_RetrieveMemoryRecords.html), [pricing](https://aws.amazon.com/bedrock/agentcore/pricing/); Bedrock [rerank](https://docs.aws.amazon.com/bedrock/latest/userguide/rerank-supported.html). past.dev runner: `pastdotdev/benchmarks/beam`. Hindsight: `vectorize-io/hindsight` (MIT).
