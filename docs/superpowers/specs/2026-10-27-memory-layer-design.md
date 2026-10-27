# Memory on the network: design (27 October 2026, v4)

Status: draft for the founder; a builder check is run on it before hand-over. v4 settles three things earlier drafts left loose: the two networks and what the business is, how the free log and AgentCore work together without a loop, and what we claim about benchmarks (not a win). Research: `docs/research/2026-10-27-memory-problem.md`, `2026-10-27-oss-beam-failures.md`, `2026-10-27-past-dev.md`, `2026-10-27-network-memory-inventory.md`, `2026-10-27-memory-decision.md`.

## 1. What this is

Memory for people and their agents on the Lightbulb network: any agent on the team remembers what any other agent on the team learned, across people, harnesses (Claude Code, Codex, Cursor, OpenCode, Grok CLI, the hosted apps) and sessions. On the free network it is peer to peer and nothing touches our servers. In a workspace it gains an always-on copy, room agents that read and write it, search by meaning, automatic extraction from room conversation, and a Memory tab.

## 2. The two networks and the business

The business is messaging for people and their AI agents. The free network is the distribution: it must spread on its own, cost us nothing per install, and be trusted with nothing leaving the machine. The workspace is the revenue: $50 a month for up to five people, $20 a member after (the plan's number on the wall: ten founder-led teams with a card on file by 4 January 2027). Memory follows that split exactly.

| | Free network (no account) | Signed in (free) | Workspace (paid) |
|---|---|---|---|
| What exists today | Messaging between your agents and your teammates' agents; presence; approvals | Hosted apps (Claude app, ChatGPT) reach your agents | Rooms, always-on agents, tasks, spend gates, Memory tab on AgentCore (room agents only) |
| Memory added | Personal memory across your harnesses and computers; team memory with the people on your network; keyword search; `lightbulb memory` | Same, plus your personal memory follows you to new computers via your identity (already how teammates' computers are linked) | Everything on the left, plus: a copy that is up while the workspace runs, so facts cross between teammates who are never online together; room agents and hosted apps read and write it; search by meaning; facts extracted from room conversation without anyone saving them; Memory tab with source, history and contested; wipe |
| Why the line is here | Cross-person memory with no server is the thing nobody ships and the reason to install. It costs us $0 | An account is what an invite attaches to; nothing to charge for yet | Everything that needs a server that is on when you are not, or a model we pay for |
| Cost to us | $0 | $0 | ~$6 per workspace a month (§7), inside the plan |

Two things this does not do: charge solo developers for memory (the person with everything working is the one who invites a cofounder), and put any memory on our servers for someone who never signed up.

## 3. What the research says we must get right

Every memory system loses the same way (memory-problem §2, §6, §7): the same fact stored many times crowds out the one update; an update in other words is not linked to the old value; the replaced value ranks above the current one; two people disagreeing is settled by recency or not noticed; totals across sessions miss facts. The product rules in §4 exist to avoid these, whatever the benchmark says.

## 4. The log: how memory moves

### 4.1 What is stored

A second signed, replicated log beside the roster, synced the same way (`src/roster.rs`, `Frame::Roster` paging in `src/net/mod.rs`): every member computes the same memory from the same entries whatever order they arrive. Entries are small (the roster's 16 KB cap holds).

```
remember  {id, subject, fact, kind: fact|decision|lesson|preference, when, source: {harness, repo, session}, scope: me|team}
replace   {id, replaces: [ids], fact, when, source}     a change; the old entries become history
forget    {id: target}                                   the content is dropped on every copy
```

- **Scope.** `team` entries sync to every member computer on the network. `me` entries sync only to the writer's own computers. A member who leaves stops receiving entries; what they already hold they keep, as with messages they already read. The privacy page says so.
- **One line per subject at read time.** Recall collapses the replace chain: the current fact first, then "was X (who, when)". Copies cannot pile up on the page.
- **Contested.** Two `replace` entries on the same fact, or a `remember` whose subject matches a current fact with a different value and no `replaces`, means two people hold different current values. Recall shows both with names and dates and the word "contested". Nobody's entry overwrites anybody's. Whoever settles it writes a `replace` of both.
- **Search.** SQLite FTS5 (BM25) over subject and fact. Keyword only; §6 measures what that costs and §5 says what a workspace adds.
- **Storage.** `state.db`, already 0700 on laptops, already snapshotted to S3 per workspace for the cloud member.

### 4.2 Writing

No model runs in the engine. The agents already on the network are the models.

- **`remember(subject, fact, kind, scope?)`**: before storing, the engine returns the current facts on that subject ("Existing: production database = Postgres 14 (Ben, 2 Aug). A change? Call again with `replaces`."). The agent, which has just read the whole conversation, decides new, change or disagreement. That is the linking step in §3, done where the context is.
- **When:** the person says "remember"; the agent judges something worth keeping; and at session end the Stop/SessionEnd hook (installed for all five harnesses, `src/hook.rs`) asks once for anything the next session or a teammate should know.
- **Scope default is `me`.** The agent may say "this would help the team, share it?" and writes `team` only on a yes, or when the person says so. A laptop session holds secrets and half-formed thoughts; nothing from it reaches a teammate without the person choosing. `lightbulb memory share <id>` does the same by hand. Room agents (§5) write `team`, since a room is shared already.
- **Secrets.** The same scrubbing that runs on egress today runs on `remember` before anything is stored.

### 4.3 Reading

- **`recall(question, budget?)`**: searches the local copy and returns a page: contested facts first, then current facts with history, newest first, packed into the budget. Local, so it works offline and costs nothing.
- **Session start:** the hook injects the current team and personal facts for this repo, at most about 400 tokens. Never before every prompt (past.dev's plugin injects 850–1,200 tokens every prompt; measured).
- **Several facts:** the agent calls `recall` again with narrower questions, as agents already do.
- **`lightbulb memory`**: list, show (history and who), forget, export, share, `last` (what was injected last time).

## 5. The workspace: AgentCore alongside the log

AgentCore Runtime hosts the workspace agents and the cloud member (`apps/infra/hermes/bridge/network.py`), so it hosts the workspace's copy of the log with no new work. AgentCore Memory does two jobs the log cannot do for free, and nothing else:

| Source | Direction | Mechanism |
|---|---|---|
| Room and DM conversation | AgentCore → log | The semantic strategy keeps extracting from the events the Hermes plugin already writes, with override instructions (one fact per attribute; speaker and date in the text; a change rewrites current-then-history; two people's different values become one record marked contested, never a pick). The cloud member polls new and updated records (`ListMemoryRecords`, `updatedAt AFTER` last sync) and writes them as `team` entries signed "workspace" |
| Every log entry | log → AgentCore | The cloud member mirrors each entry as a record in its own namespace with `BatchCreateMemoryRecords`, no strategy on that namespace, so nothing merges it. Workspace recall (room agents, hosted apps, Memory tab) runs `RetrieveMemoryRecords` over the mirror for search by meaning, then builds the same page as §4.3 |

One source, two indexes, one direction per source: no loop. The log is what people see and correct; AgentCore is an extractor and a search index over it.

- **`forget`** deletes the log entry everywhere and the mirror record by the id we created. An extracted record's log entry can be forgotten the same way; the room message it came from stays until the person deletes it, as today.
- **Cutover:** import today's AgentCore records once as `team` entries signed "workspace (imported)"; the Hermes plugin's prefetch and search tool read the mirror (fixing today's defect where the search text is routing JSON); Memory tab reads the cloud member's copy.
- **Hard boundary:** laptop and hosted tools never call AgentCore. They read the log. Only the cloud member and the runtime touch AgentCore, under the workspace's role, as today.

## 6. Measurement: what we will and will not claim

We do not expect to beat past.dev's 90 on BEAM; their score took a team a year and their losses are in the four hardest abilities. We measure anyway, honestly, because a number anyone can rerun is worth more than a claim.

1. **Same harness.** Fork past.dev's MIT-licensed BEAM harness and replace only the memory calls. ExaBase's prompts hash-checked, same answer and judge model, 8,000-token budget, each question once.
2. **Four arms on the 100K split** (~$80 total): past.dev rerun (our ~149,900 free credits cover it), AgentCore built-in as today, AgentCore with the §5 overrides, and the log with model-driven extraction standing in for the agent. Judge each three times to see the noise.
3. **What the arms decide:** which extraction the workspace uses (built-in or overrides), and how much keyword search costs against search by meaning. Both have a price per workspace beside the score.
4. **Tuning** is a two-week timebox on the leading arm. One untouched run of 500K, 1M, 10M and LongMemEval_S at the end. Publish results, prompts, harness commit, cost and variance, whatever they show.
5. **The number we lead with** is the one nobody else runs: a team-memory set from our own rooms (with consent, anonymised), scored on whether one person's agent answers from what another person said, names the source, and surfaces disagreements instead of resolving them. The public line is about that and about where the data lives, never a competitor's name.

## 7. Cost

- **Free network: $0 to us.** The person's agent writes, their computer stores and searches, the network carries it.
- **Workspace, per month, 5 people, 3,000 room messages, ~2,000 records (assumptions to measure):** extraction with overrides on a Haiku-class Bedrock model ~$1.50; records $0.25 per 1,000 with overrides ($0.50); mirror records the same (~$0.50); retrievals $0.50 per 1,000 × ~3,000 room turns × 2 namespaces (~$3); short-term events <$0.10. **About $6, 12% of the plan.** Laptop recall is local and free. Bedrock's model price and retrievals per turn are measured in week 1.
- **Benchmarks:** ~$80 for the four baselines, ~$15–20 per tuning run, ~$150 for the final full run; ~$500 all in.
- **People's tokens:** a few hundred at session end, ~400 at session start.

## 8. Privacy

Same promise as messages, same transport. Free memory lives on members' computers and moves only between them, encrypted end to end; we never see it. Nothing leaves a laptop session for a teammate unless the person says yes. In a workspace the cloud member holds a copy, as it already holds the workspace's messages, and only it touches AgentCore. `forget` removes content from every copy that comes online. Owner and member wipes extend to the mirror and extraction namespaces. The privacy policy rewrite and `docs/security.md` describe this before release.

## 9. Plan

Working days with agents. Memory ships to users before anything is benchmarked.

| Step | Days | What | Done when |
|---|---|---|---|
| 1. The log | 5 | Entries, signing, replay and sync on the roster's pattern; FTS5; `remember`/`recall` in the shared tool spec; hooks; `lightbulb memory`; two-computer tests; CLI release; privacy and security docs | A fresh install on two computers shares a fact with a name on it |
| 2. Harness and baselines (parallel) | 3 | Fork; four adapters; 100K ×4, judge ×3; Bedrock price check | We know where each arm stands and what the judge's noise is |
| 3. Workspace | 5 | Cloud member imports and mirrors; override instructions; plugin reads the mirror; Memory tab reads the copy; one-time import; wipes | Our own workspace runs on it for a week |
| 4. Tuning | timebox 10 | Leading arm only; probe cases every run | Improvement beyond the judge's spread, or stop and report |
| 5. Publish | 3 | Final runs; team-memory set; results repo; /network page | Numbers anyone can rerun |

About four to five weeks; user-visible memory at the end of week one.

## 10. Decisions for the founder

1. Memory is part of the free network, peer to peer, no account, including team memory between members.
2. Personal by default; sharing to the team needs the person's yes.
3. AgentCore Memory stays in workspaces as extractor and search index over the log, never as a second store people edit.
4. We publish benchmark numbers with cost and variance even when they are below the leader, and the public line is about team memory and where data lives.

## 11. Calls I made (check these while reading)

- Keyword search on the free tier, search by meaning only in workspaces, until the harness says keyword is not enough.
- The agent decides new/change/disagreement at write time, using the neighbours `remember` returns.
- The cloud member polls AgentCore for extracted records instead of a push; it is up only while the workspace runtime is, so facts cross between offline teammates on the workspace's schedule, not instantly.
- Session-end hook asks once; no capture of tool calls or diffs.

## Sources

CLI v0.6.5 (`f9ea8d5`): `src/roster.rs`, `src/net/mod.rs`, `src/net/frames.rs`, `src/store.rs`, `src/hook.rs`, `src/embed.rs`, `docs/security.md`. Monorepo: `apps/backend/core/services/network_tools.json`, `apps/infra/hermes/bridge/network.py`, `apps/infra/hermes/plugins/memory/agentcore/__init__.py`, `apps/backend/core/services/workspace_memory.py`, `apps/infra/lib/stacks/container-stack.ts`. AgentCore: [overrides](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-custom-strategy.html), [semantic prompts](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-system-prompt.html), [metadata and filters](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/long-term-memory-metadata.html), [RetrieveMemoryRecords](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_RetrieveMemoryRecords.html), [pricing](https://aws.amazon.com/bedrock/agentcore/pricing/). past.dev harness: `pastdotdev/benchmarks/beam`.
