# Why the open memory systems score lower than past.dev on BEAM: per-question evidence (27 October 2026)

Scope: per-question BEAM results published by the systems themselves, read file by file. No code from any cloned repository was run; every number below comes from Python scripts written for this pass, reading the published JSON. past.dev's per-ability numbers are from `pastdotdev/benchmarks/beam/results/*/results.jsonl`, as in `docs/research/2026-10-27-memory-problem.md` §2.

## 1. What exists

| System | Per-question data? | Where | Answer model | Judge | What the answerer reads | Conversation dates passed at ingest? |
|---|---|---|---|---|---|---|
| past.dev | Yes, all four splits, with the full recall page | github.com/pastdotdev/benchmarks `beam/results/{100k,500k,1m,10m}/results.jsonl` | openai/gpt-5.6-luna, reasoning off | the same model, Exabase's BEAM rubric prompts (SHA-256 checked); Kendall tau-b for event ordering | one recall, 8,000-token evidence page (698/700 pages full at 1M) | Yes: each turn at noon UTC on its session day |
| Hindsight (Vectorize) | Yes, all four splits, with the retrieved context, the answer and the answerer's reasoning | github.com/vectorize-io/agent-memory-benchmark `outputs/beam/hindsight/single-query/*.json.gz` (committed 1 Apr 2026, commit 5d5e8db) | gemini-3.1-pro-preview | gemini-2.5-flash-lite, AMB's own per-category judge prompts (not Exabase's) | 17,655 (100K) to 27,342 (10M) context tokens on average | **No.** The loader at 5d5e8db sets no document timestamp; every retrieved memory carries `_mentioned: 2026-03-…` (the ingest date). AMB's own later commits fix this: aa9273a (Jun 2026) "fixes year corruption 2024->2026"; d97d496 (9 Oct 2026) also stores the assistant's side (before, "what the assistant proposed or built in reply was never stored") and dates 10M sessions. The published numbers predate both fixes |
| Honcho (Plastic Labs) | Yes, all four splits, answer and judge reasoning per question; no retrieved context | github.com/plastic-labs/honcho-benchmarks `a1d689b/beam_*_2025121*.json` (15–16 Dec 2025) | claude-haiku-4.5 "dialectic" agent with up to 20 tool calls; deriver gemini-2.5-flash-lite; dreams off | not named in the file; rubric "nugget" judge in `tests/bench/beam_common.py`, which treats equivalent number forms as equal | agentic search, up to 100,000 input tokens | **No.** `tests/bench/beam.py` reads no `time_anchor` and sets no message time |
| Mem0 (platform) | Yes, 1M and 10M only; answer and per-nugget judge reasons; no retrieved text (only the count) | github.com/mem0ai/memory-benchmarks `results/platform/beam_{1m,10m}_results.json` (26 Mar 2026) | gpt-5 (Azure) | gpt-5, nugget rubric | top 200 memories | Yes: earliest `time_anchor` of each batch as the add timestamp (`benchmarks/beam/run.py` l.275–476) |
| Exabase M-1 | No per-question data found | checked GitHub (futurebrowser/exabase-*), exabase.io blog | — | — | — | — |
| Supermemory | No BEAM results found | supermemoryai repositories (supermemory, memorybench not present) | — | — | — | — |
| BEAM paper (LIGHT and baselines) | Code only; no released predictions | github.com/mohammadtavakoli78/BEAM `src/answer_probing_questions/`, `src/evaluation/` | — | — | — | — |

The four runs that publish data used four different answer models, three different judges (or judge prompts) and three different context budgets. No two scores in the side-by-side below were produced under the same conditions.

## 2. Scores by ability, side by side (mean score ×100)

**1M** (all four systems have data):

| Ability | past.dev | Hindsight | Honcho | Mem0 |
|---|---|---|---|---|
| Abstention | 97.1 | 90.0 | 47.9 | 52.5 |
| Contradiction resolution | 75.4 | 59.5 | 67.1 | 35.7 |
| Event ordering | 99.8 | 80.9 | 50.0 | 53.6 |
| Information extraction | 84.0 | 61.3 | 75.5 | 70.0 |
| Instruction following | 96.4 | 93.3 | 69.3 | 85.2 |
| Knowledge update | 85.7 | 66.4 | 57.9 | 65.0 |
| Multi-session reasoning | 72.8 | 46.2 | 63.5 | 65.2 |
| Preference following | 98.9 | 97.0 | 79.8 | 88.3 |
| Summarization | 99.6 | 84.5 | 57.6 | 63.5 |
| Temporal reasoning | 96.8 | 59.6 | 62.1 | 61.8 |
| **Overall** | 90.65 | 73.9 | 63.1 | 64.1 |

**10M**:

| Ability | past.dev | Hindsight | Honcho | Mem0 |
|---|---|---|---|---|
| Abstention | 100.0 | 80.0 | 30.0 | 40.0 |
| Contradiction resolution | 73.1 | 56.9 | 45.0 | 32.5 |
| Event ordering | 100.0 | 61.6 | 50.0 | 20.2 |
| Information extraction | 68.8 | 51.2 | 46.2 | 56.2 |
| Instruction following | 95.0 | 92.5 | 53.8 | 82.5 |
| Knowledge update | 92.5 | 65.0 | 40.0 | 75.0 |
| Multi-session reasoning | 28.2 | 16.6 | 25.1 | 26.1 |
| Preference following | 100.0 | 97.5 | 65.0 | 90.4 |
| Summarization | 99.0 | 78.2 | 28.0 | 46.9 |
| Temporal reasoning | 93.8 | 41.2 | 22.5 | 16.2 |
| **Overall** | 85.03 | 64.1 | 40.6 | 48.6 |

(Hindsight and Honcho at 100K and 500K show the same shape; full tables in the scripts' output. Hindsight 100K/500K/1M/10M overall 73.4/71.1/73.9/64.1; Honcho 63.0/64.9/63.1/40.6, matching their published headline numbers.)

Pattern: the largest gaps to past.dev are the same across all three systems: **temporal reasoning, event ordering, knowledge update, summarization**, and for Honcho and Mem0 **abstention**. Multi-session reasoning at 10M is low for everyone (16–28): past.dev's lead there is small.

## 3. Failure classes, with quoted examples

Classes: (a) gold evidence not retrieved; (b) retrieved but outranked or duplicated; (c) stale value chosen over the updated one, or no way to tell which is newer; (d) aggregation incomplete; (e) answerer reasoning error with evidence present; (f) doubtful gold key; plus two that the data forced: (g) **judge under-scores a correct answer**, (h) **ingest-date defect** (conversation dates lost or wrong).

### Hindsight (1M unless stated)

**Temporal reasoning: mostly (g).** Of the 49 missed temporal questions at 1M, **44 answers contain the gold duration**; 34 of those scored exactly 0.5. At 100K 19 of 28 misses contain the gold duration, at 10M 18 of 20. Examples:
- Q "How many days are there between the start of my first sprint and the beginning of my second sprint?" Gold "16 days … March 4 … March 20." Answer "There are 16 days between the start of your first sprint (March 4, 2024) and the beginning of your second sprint (March 20, 2024)." Score 0.5. (g)
- Q "How many days are there between the start of Sprint 9 and my project's scheduled completion date?" Gold "26 days … July 1 … July 27." Answer "There are 26 days between the start of Sprint 9 (July 1, 2024) and the project's scheduled completion date (July 27, 2024)." Score 0.5. (g)
- Q "How many days do I have between submitting my modules for peer review and the deadline to provide feedback?" Gold "2 days … March 16 … March 18." Answer "exactly 2 days" (dates in its reasoning, not its answer). Score 0.5. (g, partly a format miss: the rubric's second item wants the date range stated)
- Q "How many days after I first asked for help with the function subject to the constraint did I confirm…?" Gold "8 days … January 10 … January 18, 2024." Answer "either 723 days (from … April 3, 2024 to March 27, 2026) or 14 days". Score 0. **(h)**: March 27, 2026 is an ingest timestamp leaking into the answer.
- Q "How many days do I have between finishing the initial feature extraction and completing the transformer training?" Gold "45 days … April 15 … May 30." Answer gives 45 days for the original timeline and 51 for an adjusted one. Score 0. (c/f: the conversation contains a later adjusted timeline; the key takes the original)

**Knowledge update: (c), with (h) as the likely cause.** The gold value is in the retrieved context in 9 of the 10 misses read; the answer picks an older or a different value, and with every memory stamped with the same 2026 ingest date there is no reliable "newer":
- Q "What is my average time for solving Jacobian and change of variables problems…?" Gold "17 minutes". Answer "22 minutes per problem". "17" present in context. (c)
- Q "How many new LinkedIn connections have you logged so far?" Gold "30". Answer "25 … since March 15". "30" present. (c)
- Q "What is the average amount for my monthly household expenses?" Gold "$2,800". Answer "$2,500 … most recently updated", listing $2,800, $2,860, $2,960, $4,050 as older. (c, possibly f: the answerer had to guess recency without dates)
- Q "What step size do I use for Euler's method…?" Gold "h = 0.25". Answer "h = 0.1", citing two memories. "0.25" present. (c/b)
- Q "What is the estimated cost for repairing the minor plumbing leaks?" Gold "7,500 TRY". Answer "8,000 TRY" citing five memories that say 8,000. "7500" present. (b: five copies of the old value against one of the new)
- Q "How many variation of parameters problems have I completed…?" Gold "6 … 91%". Answer "0 … you mentioned you have 'never actually solved any'". The answerer's own reasoning lists 4 → 6 as an update, then takes a later BEAM-injected contradiction. (e/f: contradiction and update questions share conversations)
- Q "How many epochs does my LSTM model training pipeline complete…?" Gold "60 epochs … 0.028". Answer "50 epochs … 0.032 … later reduced to 30". (c)
- Q "What is my daily water intake goal…?" Gold "3 liters". Answer "2.5 to 3 liters" (0.5). (c)

**Multi-session reasoning: (a) and (d).** Zero-score misses at 1M:
- Q "How many total weekly workout sessions … strength training, HIIT, and cycling?" Gold "9: 4 strength, 2 HIIT, 3 cycling". Answer "4 … 2 strength, 1 HIIT, 1 cycling". (d)
- Q "How many different study tools or decorations have I added…?" Gold lists eight. Answer "2 … Turkish posters and flashcards". (a/d)
- Q "How much total did I pay in transaction fees…?" Gold "$17.50". Answer "$15". "17.50" not in context. (a)
- Q "How many total exercises have I completed from both … books?" Gold "40". Answer "The provided context lacks the information". (a)
- Q "How much more does the 3-bedroom apartment cost…?" Gold "40,000 TRY". Answer "40,000 TRY more … (620,000 vs 580,000) … 41,200 TRY with closing costs". Score 0. (g)

### Honcho (1M)

**Knowledge update: (c) surfaced as contradiction, with (h) as the cause.** Honcho's runner passes no conversation dates, so successive values look simultaneous and the agent reports a contradiction instead of the latest value:
- Q "What is the TTL value set for caching recent translations…?" Gold "20 minutes". Answer "I found contradictory TTL values … 15-minute … 10-minute…". Score 0. (c/h)
- Q "How many positive online reviews have I received … in June?" Gold "7". Answer "there's a contradiction … 7 client reviews … never received…". Score 0. (c, with a BEAM-injected contradiction in the same conversation)
- Q "What blood pressure goal am I aiming to reach within three months?" Gold "Below 125/75". Answer "Two different targets appear: Below 130/80 …". Score 0. (c/h)
- Q "How much initial capital have I allocated for real estate investing?" Gold "$60,000". Answer "contradictory observations … Most Recent Update: $60,000". Score 0.5. (right value, hedged)
- Q "How many installments … hospital payment plan?" Gold "Four … $150". Answer "conflicting observations … Version 1: 4 monthly installments of $150…". Score 0.5. (right value, hedged)
- Q "What is my average time to solve a spherical geodesic problem?" Gold "12 minutes". Answer "15 minutes … reduced from an initial 22". (c)
- Q "How many new LinkedIn connections have you logged so far?" Gold "30". Answer "25 … since March 15". (c; the same miss Hindsight makes)

**Abstention: (g) and genuine hallucination.** Gold answers say "there is no information"; the judge treats any related fact as a violation:
- Q "Can you tell me more about my background in psychology…?" Answer "I don't have specific information about where you studied … What I do know: extensive experience…". Score 0. (g: correct abstention plus context)
- Q "Can you tell me more about Omar's professional background…?" Answer "I don't have specific information … What I do know about Omar: he is 67…". Score 0. (g)
- Q "What was the feedback … to the dynamic language switching feature?" Answer "Overall Satisfaction Rate: 90%…". Score 0. (genuine: an answer built from unrelated facts)
- Q "Could you provide the detailed breakdown of the mortgage options…?" Answer "Let me compile the detailed breakdown…". Score 0. (genuine)

**Temporal reasoning: (c)/(h), (e), (f):**
- Q "How many days passed between the argument … and the communication workshop…?" Gold "46 days … February 28 … April 15". Answer "48 days" with both dates right. (e: arithmetic)
- Q "How many days after I started the 24-hour Holter monitor test did my follow-up … take place?" Gold "8 days". Answer "6 days". (e or c)
- Q "How many days do I have between aligning my sleep schedule … and reaching my sleep efficiency goal?" Answer "unable to answer … contradictory information … multiple different target dates". (c/h)
- Q "How many days had I been studying spherical geometry by the time my exam…?" Gold: the exam (January 10) came before the study start (March 20). Answer reports the contradiction. Score 0. (f: a trick key)

### Mem0 (10M)

**Temporal reasoning: (a) and (h).**
- Q "…between when I started working on the context window management module and … query rewriting pipelines?" Gold "45 days, November 1 – December 16, 2024". Answer "I don't have enough information". (a; 200 memories retrieved)
- Q "…began the architecture design phase for the self-driving car simulation project?" Gold "15 days, July 1 – July 16, **2025**". Answer "8 days … July 1, **2024** … July 9, 2024". (h: year off by one)
- Q "…after I finished finalizing stakeholder interviews … development environment?" Gold "10 days, 2024-07-09 – 2024-07-19". Answer "105 days … 2024-07-17 … 2024-10-30". (h/c)
- Q "…after I started the comprehensive testing suite phase … deployment pipeline?" Gold "12 days, January 21 – February 1, 2025". Answer "11 days … January 21 … February 1". Score 0.5. (f: 21 Jan to 1 Feb is 11 days; the key says 12)
- Q "…after I started learning the fundamental concepts of PDEs … separation of variables?" Gold "46 days, August 1 – September 16". Answer "51 days … 2024-07-27 … 2024-09-16". (h: a batch dated by its earliest anchor)
- Three further misses answer "I don't have enough information". (a)

**Event ordering: (a).** Four of eight read answer "I don't have enough information" for questions that name the date window ("from 2024-07-01 to 2024-07-29"); the others list the right period's topics partially (scores 0.15–0.86).

### Doubtful keys, as flagged by the vendors themselves

Honcho ships `tests/bench/incorrect_beam_qs.txt`: three BEAM questions it considers wrong. past.dev scored two of them 0 (100K: "By what date am I aiming to complete all my onboarding modules?", knowledge update; "How many different shoe sizes have I mentioned across my messages?", multi-session) and one 1.0. Together with the physics-delay key in the past.dev analysis and the 11-vs-12-day key above, every system's score carries some key noise, unmeasured.

## 4. What this says about why they score lower, as far as the evidence goes

1. **The published runs are not comparable, and the harness explains a large part of the gap before any memory design does.** Hindsight's and Honcho's published runs ingested the conversations without their dates. That breaks exactly the abilities where they trail most: knowledge update (no way to know which value is newer), temporal reasoning and event ordering (ingest timestamps leak into answers, e.g. "March 27, 2026"). AMB's own commit history records the defect and fixes it after the published run. Mem0 passed dates but at batch granularity, and its 10M answers show year and anchor errors. past.dev placed every turn on its session day.
2. **Judges differ enough to move abilities by tens of points.** In Hindsight's run, 44 of 49 temporal "misses" at 1M contain the correct duration; most score 0.5 under AMB's gemini-2.5-flash-lite judge. Honcho's and Mem0's nugget judges score a correct abstention plus extra context as a failure (Honcho abstention 30–48, Mem0 40–55, against past.dev's 97–100 under Exabase's rubric). These are judge and answer-style effects, not memory.
3. **Where dates were present and the judge is not the issue, the residual losses match past.dev's own:** stale or duplicated values winning (Hindsight: five copies of "8,000 TRY" against one "7,500"), aggregation across sessions incomplete (all systems; 10M multi-session is 16–28 for everyone, past.dev included), and outright retrieval misses ("I don't have enough information" with 200 memories retrieved, Mem0 10M).
4. **Summarization is the one large gap with no harness explanation found.** past.dev returns source text alongside memories and scores 97–100; the others score 28–87. Hindsight's run did not store the assistant's side of the conversation (AMB commit d97d496), which is consistent with weaker project summaries; for Honcho and Mem0 no cause was established from the data.
5. **What cannot be said from this data:** how much of past.dev's lead would survive a run under the same answer model, judge and dated ingestion. AMB's post-fix loader exists but no post-fix Hindsight BEAM outputs are published; none of the four runs used the same judge.

## 5. Sources (read 27 Oct 2026)

- github.com/vectorize-io/agent-memory-benchmark: README; `outputs/beam/hindsight/single-query/{100k,500k,1m,10m}.json.gz`; `data/beam/100k/documents.json.gz`; `src/memory_bench/dataset/beam.py` at 5d5e8db, aa9273a, 5ad91b0 and main; commits 5d5e8db (1 Apr 2026), aa9273a (23 Jun 2026), 5ad91b0 (6 Oct 2026), d97d496 (9 Oct 2026). Also vectorize-io/hindsight-benchmarks (`benchmark-runner/datasets/beam_128k_subset.json`, not used).
- github.com/plastic-labs/honcho-benchmarks `a1d689b/beam_{100K,500K,1M,10M}_2025121*.json`; github.com/plastic-labs/honcho `tests/bench/beam.py`, `beam_common.py`, `incorrect_beam_qs.txt`.
- github.com/mem0ai/memory-benchmarks `results/platform/beam_{1m,10m}_results.json`, `benchmarks/beam/run.py`, `prompts.py`.
- github.com/pastdotdev/benchmarks `beam/results/*/results.jsonl`.
- Checked, no per-question BEAM data: github.com/mohammadtavakoli78/BEAM (code only); supermemoryai repositories; futurebrowser/exabase-* repositories; agentmemorybenchmark.ai/dataset/beam (page served no content to the fetcher).
