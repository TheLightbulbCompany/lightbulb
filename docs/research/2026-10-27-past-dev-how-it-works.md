# How past.dev gets 90 on BEAM, and what parity takes (27 October 2026)

Sources, all read today: their published per-question results (`pastdotdev/benchmarks/beam/results/{100k,500k,1m,10m}/results.jsonl`, 2,000 questions with the full recall page each), their harness (`run.py`, `past_api.py`), the console's data model (`2026-10-27-past-dev-console-rea.md`), and our two API probes (`2026-10-27-memory-problem.md` §6, `scratchpad/pastdev-probe/result2.json`: lineage, nodes and recall for real Claude Code sittings). Numbers below are from the 1M split (700 questions) unless marked. Nothing here is their code or prompts; it is what their outputs show.

## 1. The engine, as the outputs show it

**Two node kinds.** Every data point becomes a `source` node (the raw turn, indexed first and searchable within seconds) and, after a queued extraction pass, a set of `memory` nodes derived from it. Our four-turn webhook sitting produced twelve memories: 5 observations, 4 claims, 1 rule, 2 mentions. Across probe 2: 21 claims, 18 observations, 16 mentions, 2 rules, 2 temporal anchors from 13 data points. Every memory derives from exactly one source; no memory on any of the 20,842 page results has more than one source, and the probe lineage has no memory→memory edges. So there is no evidence merging: the same fact said twice is two memories.

**Memory kinds and their text shape** (verbatim examples from the pages):

| Kind | Shape | Example |
|---|---|---|
| claim | `<speaker>: <entity> <attribute>: <value>.` | `Jamie Perez: React Query version: React Query v4.29.` |
| observation | a sentence of what happened, speaker-prefixed | `Claude changed webhooks.ts to select the secret by NODE_ENV and added a startup warning…` |
| mention | a topic index line: Raised an issue / Decided / Set a goal | `Jamie Perez: Set a goal: Clinical psychology chatbot fine-tuning.` |
| temporal-anchor | a dated reference resolved to a date | `…deadline (deadline): by April 20, 2024. Resolved to 2024-04-20.` |
| rule | a generalised lesson with applicability and status | `Assistant (proposed): For clinical psychology data, start with temperature=0.5 … Applies to GPT-4 API response configuration` |
| membership | list membership | `…working branches: feature/lang-detect (member).` |

**Bi-temporal.** Every memory carries `validFrom` (the source's time), `validTo`, `current`, `replacedId`. Supersession closes the old claim when a new claim lands on the same slot text; the page shows the old one with `supersededAt`. In probe 2 this fired for "production database version" and not for "latency" vs "response time" or "evaluating" vs "setup" (different slot text).

**Recall** is five stages (expansion, search, materialisation, rerank, composition; the console reports each stage's milliseconds). The page: median 28 results, always the full 8,000 tokens (698 of 700 pages). Ranking is kind-agnostic: mean rank is 18–22 for every kind. **80% of page characters are raw source turns with excerpts**; claims, observations, mentions, anchors and rules together are 20%. The harness hands this JSON to the answer model verbatim, so the answerer sees kinds, dates, `supersededAt` and the excerpts.

## 2. Where the score comes from

Split each question by where the gold answer's number appears on the page: in the state layer (claims, observations, anchors, rules), only in raw sources, or nowhere.

| Ability (1M) | n with a numeric gold | gold in state | only in sources | nowhere | score: state / sources-only / nowhere |
|---|---|---|---|---|---|
| Knowledge update | 61 | 46 | 7 | 8 | **0.93 / 0.71 / 0.38** |
| Multi-session | 46 | 24 | 14 | 8 | **0.75 / 0.69 / 0.51** |
| Information extraction | 40 | 34 | 6 | 0 | 0.84 / 1.00 / – |
| Temporal reasoning | 64 | 53 | 10 | 1 | 0.98 / 0.93 / 1.00 |
| Event ordering | 30 | 6 | 23 | 1 | 1.00 / 0.99 / 1.00 |
| Summarization | 30 | 10 | 12 | 8 | 1.00 / 1.00 / 1.00 |

Two readings. The six easy abilities are carried by raw-turn retrieval plus a strong answer model; the state layer barely matters there (event ordering: 23 of 30 answered from sources alone at 0.99). The hard abilities are carried by the state layer: a knowledge-update question with the current value present as a claim scores 0.93, with it only in a raw turn 0.71, and with it missing 0.38. Retrieval misses (nowhere) are the largest single loss on both hard abilities.

**Their mechanisms leak points in measurable ways:**
- 62 of 700 pages carry two or more *current* claims on the same slot (106 extra claims): the duplication that outvotes an update.
- Of 170 superseded claims shown on pages, the replacement is on the same page only 37 times. The flag says "replaced" and the page does not say by what.
- 10M multi-session misses are aggregation: the answer found 200K + 800K and reported 1M against a gold of 1.8M; found 3,000 and reported 7,006 against 5,000. The parts are spread over sessions and one ranked page does not gather them all.

## 3. What parity takes, on what we run

AgentCore Memory stores and searches extracted records; it has no search over raw turns and no reranker. The log (the free network's memory) has FTS5 over its entries. Mapping their stages onto that:

| Their stage | Ours |
|---|---|
| Source index (raw turns searchable) | Mirror each room turn as a record in a strategy-less namespace (`BatchCreateMemoryRecords`), so AgentCore's semantic search covers raw turns; the cloud member's FTS5 covers them for keyword. 3,000 turns a month ≈ $0.75 in storage |
| Extraction into typed memories | Override instructions on the semantic strategy: one line per fact in their shapes (speaker prefix; `entity attribute: value` for claims; resolved dates; rules with "applies to"); `kind` as model-filled metadata with allowed values; `validFrom` as numeric metadata from the turn's time |
| Supersession | Consolidation instructed to keep one record per slot: current value first, history after ("was X, who, when"). This is *better* than theirs on the two leaks above (no duplicate current claims; the replacement is always beside the old value). Whether consolidation can also set `validTo`/`status` metadata on the old record is undocumented: week-1 check; if not, the self-managed strategy does it with our own consolidation |
| Expansion | One cheap model call that turns a question into the sub-questions it needs (the 1.8M case needs two lookups); only when the question has several parts |
| Search + materialisation | Per sub-question: semantic over memories (current first via metadata filter), semantic over raw turns, FTS5 over both; union to ~100 candidates |
| Rerank | A model-based listwise rerank of the candidates against the question (or a cross-encoder in the runtime); measured against no rerank |
| Composition | Pack to 8,000 tokens: contested first, then current claims, then raw turns with excerpts, then history; always fill the budget, as they do |
| As-of recall | `validFrom`/`validTo` numeric filters (`LESS_THAN`/`GREATER_THAN` on NUMBER metadata) |

## 4. What we would and would not expect

- **Likely at or above them:** knowledge update and contradiction, because one-record-per-slot with history and contested-both-sides remove the two leaks their own pages show. That is ~14 points of headroom on knowledge update and ~25 on contradiction at 1M.
- **Reachable with work:** the six easy abilities, if raw-turn search plus the reranker is as good as theirs. This is retrieval engineering, not memory design, and it is where their year went.
- **Hard for everyone:** multi-session at 10M (them 28, Exabase 10, Honcho 19). Expansion into sub-questions is the one lever, and it is unproven.
- **The honest sequence:** baseline → their shapes on AgentCore → measure each stage's gain on 100K against the judge's spread. Parity on 100K (their 92) is plausible within a two-week timebox; 1M and 10M are a separate question answered by running them.

## 5. What is still unknown

Their extraction model and prompts; the expansion method; whether search is hybrid; the reranker; how many candidates reach composition. None of it is needed to build to the same shapes; all of it may matter for the last few points.
