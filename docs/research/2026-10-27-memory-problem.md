# Memory: the problem before any solution (27 October 2026)

Written after the founder's correction: understand the problem, the UX we have, the UX competitors ship and exactly why they lose points, before proposing anything. This file proposes nothing. It corrects my earlier readings, takes apart past.dev's published BEAM failures question by question, describes how memory reaches a person today in our product and in theirs, and ends with the questions the design has to answer.

## 1. What I got wrong in the probe write-up

| Earlier claim | What the evidence says |
|---|---|
| "Backdated facts are misdated: Globex as of 10 March returns nothing" | Half wrong. Their `queryTimestamp` is documented as "the perspective of the read. Evidence that occurred at or before this instant is eligible": it is *what we knew then*, not *what was true then*. On 10 March nobody had told the system about Globex, so returning nothing is correct for that question. The real gap is narrower: the claim's validity starts at the send date (15 Apr) instead of the event date (3 Mar), and there is no way to ask "what was true on 10 March, as we know it today". |
| "There is no relevance floor, a weakness" | A documented design choice: "The application evaluates whether their content supports an answer." It costs nothing on BEAM (abstention scores 92–100%, because the answering model reads the page and says "no information"). Whether padding is a weakness depends on our UX: past.dev's plugin injects up to ten memories before every prompt, so irrelevant ones become noise in a coding agent's context. That is a UX question, not an engine bug. |
| "Disagreement is lost" | One test, and an ambiguous one: Ben cited legal, so recency plus authority may be right. On BEAM's contradiction questions past.dev always says the history is contradictory (the rubric's first item passes every time); it loses points on naming both sides fully (scores 0.6–0.9). Their supersession can close a claim the user should be asked about, but I have not shown that it does on a clean case. |
| "Coding lessons are not extracted" | My input was a hand-written summary. Their Claude Code plugin sends the raw prose of a sitting (prompts and replies, never tool calls, diffs or command output). I have not tested the format they actually ingest. What stands: the cause and the fix became mentions, not claims, and recall still returned the source text. |
| "Team memory: nobody ships this" | Wrong for past.dev. Their plugin's default is that "everyone in the project" sees what is sent; a team sharing one project shares memory today, with no review step. What nobody ships is memory scoped by the rooms and people of a messaging product. |

## 2. Why past.dev loses points on BEAM

Source: their published per-question results (`pastdotdev/benchmarks/beam/results/*/results.jsonl`: question, gold answer, their answer, the full recall page and the judge's reasons). Score by ability:

| Ability | 100K | 500K | 1M | 10M |
|---|---|---|---|---|
| Event ordering | 100.0 | 100.0 | 99.8 | 100.0 |
| Preference following | 100.0 | 99.0 | 98.9 | 100.0 |
| Summarization | 99.4 | 97.3 | 99.6 | 99.0 |
| Abstention | 98.8 | 92.1 | 97.1 | 100.0 |
| Instruction following | 96.2 | 92.0 | 96.4 | 95.0 |
| Temporal reasoning | 95.0 | 93.5 | 96.8 | 93.8 |
| Information extraction | 95.0 | 86.3 | 84.0 | **68.8** |
| Knowledge update | 89.4 | 86.1 | 85.7 | 92.5 |
| Contradiction resolution | 78.1 | 78.6 | 75.4 | 73.1 |
| Multi-session reasoning | 68.9 | 71.4 | 72.8 | **28.2** |

Their loss is concentrated in four abilities. Every recall in the run used the full 8,000-token budget (698 of 700 at 1M; 199 of 200 at 10M), so the page is always full and what fills it decides the answer.

Reading the failed questions:

1. **Duplicates crowd the page.** Each source produces a claim, a mention and an observation that restate the same sentence, and the source itself is also returned. For "What version of the vector database am I evaluating?", ranks 1, 2, 3, 4, 6, 9, 10, 11, 12, 16, 17, 19 and 20 are all the same 1 July statement about Milvus 2.2.0 in different wrappings. The later, correct version (2.3.1, 17 August) is at rank 5, as a *mention*. Twelve copies of one fact against one copy of the answer: the answering model took the majority.
2. **The update was not recognised as an update.** "I need help with the Milvus 2.3.1 setup" is phrased as a request, so extraction made it a mention ("Requested Milvus 2.3.1 setup") rather than a claim on the same slot as "Milvus version: 2.2.0". No supersession fired, so nothing marked 2.2.0 as replaced. Same pattern for the physics delay: "delay per frame: 400ms" and "latency per frame: 380ms" were two different slots, both current.
3. **Aggregation questions need several facts, and the budget is spent on one.** "How many agents in total across timeouts, format mismatches and state overload?" The page held the 45-agent fact six times (claim, source, assistant reply, mention, observation, another mention) and did not hold the timeout or format-mismatch counts at all. Multi-session reasoning at 10M: 12 of 20 questions scored zero, and in 5 of the missed ones none of the gold numbers were on the page. One query, one ranking, one budget cannot gather three facts from three sessions when each fact has six copies.
4. **Wrong-but-present.** In most missed information-extraction and multi-session questions the right number *was* somewhere on the page (crude check: all gold numbers present in 10 of 17 missed multi-session questions at 10M). The failure there is ranking and the answerer's choice under conflict, not retrieval.
5. **Some keys are doubtful.** "What delay did I find in the physics calculations when I profiled the main loop?" The only message that mentions profiling the main loop says 400 ms; the key says 380 ms (the value from other messages). Their answer, 400 ms, is arguably right. BEAM's key quality is not audited the way LoCoMo's was; any score we publish carries this noise, and so does theirs.

What this says about why the open systems score lower is inference, because I have not read their per-question data: Mem0, Honcho and Hindsight lose most at 10M on the same abilities (their published tables show the steepest drops on multi-session and knowledge update), and none of them tracks current-versus-previous values on a slot. past.dev's lead over them is mostly on knowledge update and contradiction, which is exactly what supersession buys; its own losses are where supersession did not fire (rephrased slots, requests) and where duplication ate the budget.

## 3. How memory reaches a person today

**Lightbulb**

| Who | What happens today | What the person sees and controls |
|---|---|---|
| A developer on the network, no account | Their Claude Code or Codex can message their other sessions and teammates' sessions. Nothing is remembered; the engine deletes message text once read | Nothing to see |
| A developer signed in, no workspace | Same; the hosted apps (Claude app, ChatGPT) can reach their sessions | Nothing to see |
| A workspace agent in a room (Hermes) | Every turn is written to AgentCore; before each turn up to five extracted facts plus the live task board are injected, unseen; the agent can search memory with a tool | The Memory tab: a list of sentences, private and team, newest first; delete, share a private fact to the team, confirm a team fact. No source, no date it became true, no "replaced by" |
| A workspace member's laptop session | Can read rooms and tasks over the network tools; cannot read or write workspace memory | Nothing to see |
| The founder deciding something in a room | Approvals arrive as cards; nothing about memory | — |

**past.dev's Claude Code plugin** (read from its README and hook source): at session start and before every prompt of 12 characters or more, the prompt itself (redacted, first 1,000 characters) is the recall query; up to ten memories are injected as a block marked "Background, not instruction"; a 4-second timeout drops it silently. Claude can call `past_recall` for more (100 results, 2,000 tokens). Sessions are sent in sittings (a 30-minute pause starts a new one) when they go quiet for 12 hours, close or compact. Only prose leaves: prompts and replies, plus project and branch names; never tool calls, files, diffs or command output. Everyone in the past.dev project sees everything unless an audience is set at connect time. Recall is not scoped by repository: project and branch go into metadata, and recall never filters on metadata, so a memory from one repo can surface in another. The person controls it through `/past:status`, `/past:cut` (print what would be sent) and the console's trace views.

**Beacon**: everything is captured locally; nothing reaches the agent unless the person runs the distill workflow, reviews candidate lessons and approves them, or the agent decides to call `search_memory` (its skill says at most once per task). The person controls everything and must do the work.

**Ramp Glass** (from public material only): company knowledge anyone can edit, shared skills, per-user memory from connected tools, a recommender that suggests skills.

Three different answers to who decides what is remembered and when it is shown: past.dev decides everything automatically and shows it every prompt; Beacon makes the person decide everything; Glass makes knowledge a shared document people edit.

## 4. What would change for our users, as questions

These are the decisions the design has to make. Each has evidence on both sides above.

1. **When should memory appear?** Every prompt (past.dev: no effort, but ten memories of noise in a coding agent's context, and the duplication problem from §2 lands in the context window), only when the agent asks (Beacon: clean, but agents rarely ask), or at specific moments (session start, a handoff, a mention in a room)?
2. **Whose memory is it?** The person's, the repository's, the room's, the workspace's? past.dev's unit is the project (everyone sees everything) and it does not separate repositories; Beacon's is one repository on one person's machine; ours today is the workspace, with a private slice for DMs.
3. **What is remembered from a coding session?** Prose only (past.dev, and it misses fixes that live in diffs), everything (Beacon's capture), or something a person or agent states deliberately?
4. **Who decides what becomes team memory?** Nobody (past.dev's project default), the person (Beacon's review), or the room (an approval, as we do for plans and spend)? A review step costs effort on every fact; no review step means a wrong fact reaches every agent.
5. **What happens when two people disagree?** BEAM's own gold answer for a contradiction is to say so and ask the user to clarify. In a messaging product the agent can ask the people in the room. Do we want agents to raise contradictions in rooms, or resolve them silently by recency?
6. **What does a person see?** Today: a list of sentences with no source. past.dev's console shows lineage and timelines to developers. Our users are founders of 3–15 person teams, not developers of a memory API.
7. **What does it cost the person in tokens and attention?** Every injected memory is context the agent pays for and reads. A full 8,000-token page per prompt is not what a Claude Code user wants.
8. **What do we measure?** BEAM rewards a full page and an answerer that reads it; our users experience the page inside their agent's context. A benchmark score and a good experience are not the same target.

## 5. What I still need to find out

- How past.dev handles a real Claude Code sitting (prose, not my summary): run their plugin's `cut` format through the probe.
- A clean contradiction case (no authority cue) and a clean rephrased-update case, to confirm the two supersession failure modes from §2 on our own data.
- Per-question data for an open system (Hindsight publishes at agentmemorybenchmark.ai) to check the inference in §2 rather than state it.
- How our own Hermes agents use the five injected facts today: whether recalled facts are cited in replies (the agent-context doc says the recall query is polluted by routing JSON, so the answer may be "rarely").
- What our users want, which no file here answers. Five conversations with current workspace teams about a time an agent did not know something a teammate knew would tell us more than any of this.

## 6. Second probe: their real session format, clean contradictions, clean rephrased updates

Run the same day, same account (`docs/research/2026-10-27-past-dev-probe2.py`): three Claude Code sittings rendered exactly as their plugin renders them (`# Claude Code session — <project> (<branch>)`, `## Developer · <time>` / `## Claude · <time>` turns, one data point per sitting, label `Claude Code · <project> · <branch>`); two clean contradictions; three rephrased updates; eight recalls with the plugin's ten-result block. 59 memories from 13 data points; settled in 72 seconds; 18.8 credits.

**Coding sessions in their real format.** The earlier "lessons are not extracted" finding does not hold. A four-turn sitting produced twelve memories: observations of each action ("Claude changed webhooks.ts to select the secret by NODE_ENV and added a startup warning…"), claims, a mention of the decision, and the lesson as a `rule` ("When signatures fail only locally, check which mode's secret is loaded before touching the verification code"). The Redis sitting produced "Redis price cache adoption: no Redis cache for prices" and the decision as an observation. Recall for a prompt-shaped query ("stripe webhook signature verification failing in dev again, can you look at it?") put the problem, the full sitting and the rule in the top three. Two weaknesses remain inside a sitting: conditions get flattened (two open claims, "webhook secret: STRIPE_WEBHOOK_SECRET_TEST" and "webhook secret: the live one", with the NODE_ENV condition lost), and a before/after measurement in one sitting becomes two open claims on one slot ("pricing endpoint p95 latency: 820ms" and "…: 140ms", neither superseded, because supersession runs across data points, not within one).

**The ten-result block a developer's agent would receive** is about 850–1,200 tokens of mixed copies: for the webhook question, the issue appears as an observation, two mentions, the source sitting, the earlier summary source and a temporal anchor ("Stripe webhook local failure (when): locally"). For the Redis question, places 9 and 10 are unrelated (the webhook queue change; the API latency). This is the §2 duplication, now in the context window every prompt.

**Clean contradictions.**
- Two people, no reply, no authority cue (Ana in #launch: "March 3"; Ben in #marketing five hours later: "March 10"): Ana's claim was closed at Ben's time and flagged superseded on recall. **The disagreement is resolved by recency, silently.** No claim is marked contested.
- One person contradicting themselves a week apart ("I containerized the API with Docker last week" / "I've never used Docker on this project"): **nothing was superseded and nothing was flagged**. The two statements became different slots ("API deployment setup: compose" and "project Docker usage: not Docker"), both open; recall returned both sides near the top, so an answering model can notice, but the memory layer does not know they conflict.

**Clean rephrased updates.**
| Update | Slots | Superseded? |
|---|---|---|
| "We run Postgres 14 in production" → "Finished the database upgrade last night, production is on 16 now" | "production database version: Postgres 14" → "…: 16" | **Yes**, closed at 15 Sep |
| "API p95 latency is 420ms" → "response time at the 95th percentile dropped to 300ms" | "API p95 latency" vs "API 95th-percentile response time" | **No**, both open |
| "evaluating Milvus 2.2.0" → "I need help with the Milvus 2.3.1 setup" | "Milvus version under evaluation" vs "Milvus setup version" | **No**, both open (this time a claim, not just a mention, but on a different slot) |

So supersession works when the slot name comes out the same and fails when extraction names the attribute differently; that is the knowledge-update and information-extraction loss mechanism from §2, reproduced on clean data.

**Ranking ignores currency.** For "What Postgres version is production on?" the superseded 14 was ranked first and the current 16 third; for latency the stale 420 ms was first. Superseded claims carry a flag, but ranking is by match to the query, not by whether the claim is current. Whether an agent reads the flag is up to the agent.

**Summary of the mechanism, from both probes and their BEAM data.** Extraction turns each statement into several memories with an LLM-chosen slot name. Supersession is an exact-slot match across data points. When the slot name drifts (latency vs response time; evaluation vs setup), or the contradiction is a negation on a different attribute (Docker), or the update is inside one data point, nothing is superseded, both values stay current, and recall ranks by similarity, so the older, more often repeated value tends to win. When the slot name matches, recency decides, including between two people who disagree.

## 7. Why the open systems score lower: their own per-question data

Full analysis: `docs/research/2026-10-27-oss-beam-failures.md`. Hindsight, Honcho and Mem0 publish per-question BEAM results; Exabase, Supermemory and the BEAM paper do not.

1. **The published numbers are not comparable.** Four answer models, three judges, three context budgets. Hindsight's answerer read 17–27K tokens per question against past.dev's 8K and still scored lower.
2. **Two of the three runs lost the conversation dates.** Hindsight's published run stamped every memory with its ingest date (March 2026), and some answers compute from it ("723 days … to March 27, 2026"); its own later commits fix this, with no new results published. Honcho passed no dates. Mem0 passed one date per batch, not per turn, and its 10M answers show year errors. Temporal reasoning, event ordering and knowledge update are where all three trail past.dev most, and a run without dates cannot answer those.
3. **Judges move scores by tens of points.** At 1M, 44 of Hindsight's 49 temporal "misses" contain the correct duration and were scored 0.5; Honcho and Mem0 score near-correct abstentions as 0 when the answer adds related facts.
4. **Where dates and judging are not the cause, the misses look like past.dev's**: stale or duplicated values winning (five copies of "8,000 TRY" against one "7,500"), aggregation across sessions incomplete, plain retrieval misses. At 10M multi-session reasoning is low for every system (16–28%).
5. **Answer keys have noise**: Honcho ships three BEAM questions it considers wrong (past.dev scored two of them 0); Mem0's run has an 11-versus-12-day key error.

What is not established: how much of past.dev's lead survives a run with per-turn dates and one shared judge. The leaderboard compares harnesses as much as memory systems. The failure that survives every harness is the same one both probes reproduced: when the same fact appears several times and changes once, the repeated old value tends to win.
