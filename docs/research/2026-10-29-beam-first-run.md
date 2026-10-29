# BEAM 100K, first run: the free engine through the Codex path (29 October 2026)

Internal. Not for publication (founder). The per-question results contain BEAM dataset text (CC BY-SA 4.0), so they are not committed here. They are in the session scratchpad under `beam/runs/` (`cal-a-rejudge`, `cal-b-reanswer`, `free-100k`). Every number below comes from a `results.jsonl` that `verify.py` recomputes ("everything checks out" on all four, past.dev's published file included). The table comes from `benchmarks/beam/table.py`.

## Results (BEAM 100K: 20 conversations, 400 questions; mean x 100, bootstrap 95% interval over questions)

| Category | Published 100K (a) | Our judge on their answers | Our answerer + judge on their pages | Free engine v0.7.0 |
|---|---:|---:|---:|---:|
| abstention | 98.75 [96.2, 100.0] | 95.00 [87.5, 100.0] | 97.50 [92.5, 100.0] | 97.50 [92.5, 100.0] |
| contradiction_resolution | 78.12 [74.4, 81.9] | 71.25 [68.1, 74.4] | 69.38 [65.9, 72.5] | 69.06 [65.0, 72.8] |
| event_ordering | 100.00 [100.0, 100.0] | 100.00 [100.0, 100.0] | 98.78 [96.9, 100.0] | 99.78 [99.4, 100.0] |
| information_extraction | 95.00 [91.7, 97.9] | 93.75 [89.6, 97.3] | 89.69 [83.4, 94.9] | 81.82 [73.7, 89.3] |
| instruction_following | 96.25 [90.0, 100.0] | 96.88 [93.1, 100.0] | 91.88 [83.8, 98.1] | 93.12 [86.2, 98.8] |
| knowledge_update | 89.38 [79.4, 97.5] | 89.38 [79.4, 97.5] | 85.62 [75.0, 95.0] | 80.62 [68.1, 91.9] |
| multi_session_reasoning | 68.95 [57.6, 79.7] | 67.76 [56.5, 78.1] | 64.63 [52.8, 75.9] | 64.86 [53.2, 76.0] |
| preference_following | 100.00 [100.0, 100.0] | 100.00 [100.0, 100.0] | 98.12 [95.0, 100.0] | 100.00 [100.0, 100.0] |
| summarization | 99.38 [98.1, 100.0] | 99.17 [97.7, 100.0] | 98.25 [96.2, 99.8] | 98.06 [96.6, 99.3] |
| temporal_reasoning | 95.00 [88.8, 99.4] | 93.75 [87.5, 98.8] | 92.50 [86.2, 97.5] | 93.75 [88.1, 98.1] |
| **overall (micro)** | **92.08 [90.0, 94.0]** | **90.69 [88.5, 92.7]** | **88.63 [86.2, 90.9]** | **87.86 [85.4, 90.2]** |

**Free engine caveat: its recall was more generous than the shipped page.** The free-engine column gave the answerer 50 facts and 50 turn excerpts per question (as structured results, trimmed to 100 / 8,000 tokens). The shipped `recall` hands an agent a page of at most 1,600 characters with 5 turns. At the shipped page size (`BEAM_FREE_SHIPPED=1`: the release's page text, 5 turns, at most 1,600 characters, as the agent gets it; same ingest), the free engine scores **73.82 [70.1, 77.5]**. By category: abstention 100.00, contradiction 50.94, event ordering 99.94, information extraction 56.35, instruction following 75.00, knowledge update 48.75, multi-session 20.57, preference 97.50, summarization 91.67, temporal 97.50. That run is verified, with 0 failed calls; it paid 1,205 calls, 9,382,644 input tokens (8,481,664 cached) and 74,992 output tokens, in 24.4 minutes.

(a) past.dev, `github.com/pastdotdev/benchmarks` `beam/results/100k/results.jsonl` at a712cd993e249cadd214168c3c3ee674f89bd5e4 (CC BY-SA 4.0). Its README table matches it.

## The Codex path offset (to go next to every number)

- **Judge offset (calibration a).** We re-judged past.dev's saved answers. Our verdict matches theirs exactly on 95.8% of 878 verdicts (rubric items, plus event-ordering questions). The mean score is 1.39 points lower (90.69 vs 92.08). The largest per-category gap is contradiction_resolution: 86.9% agreement, -6.9 points. Every other category is at or above 94.9% agreement and within 3.8 points.
- **Full model-path offset (calibration b).** We re-answered from past.dev's own saved recall pages, then judged with our judge. The result is 3.45 points below their published number (88.63 vs 92.08). Per category: abstention -1.25, contradiction -8.75, event ordering -1.22, information extraction -5.31, instruction following -4.38, knowledge update -3.75, multi-session -4.32, preference -1.88, summarization -1.13, temporal -2.50.
- **Reading.** Column (b) is the like-for-like comparison for the free engine: same answerer, same judge, same prompts, same trim. Only the memory differs.
  - Overall, the free engine is 0.77 points below past.dev's memory, and the intervals overlap almost entirely.
  - The free engine is clearly behind on information_extraction (-7.9) and knowledge_update (-5.0).
  - It is level or slightly ahead on event ordering, instruction following, preference following and temporal reasoning (all inside the noise).

## Conditions

- **Dataset.** `Mohammadta/BEAM` 100K at revision 3205395e897e7318c7b094ef4e6047b9b82dbb03, the same revision as past.dev's run. The question ids, question texts and as-of times match theirs 400/400.
- **Prompts.** ExaBase's prompts, SHA-256 584b672d…fc43, derived at run time from the hash-checked file. They are checked equal to past.dev's copy for all categories.
- **Scoring.** past.dev's `scoring.py`, unchanged.
- **Models.** Answer and judge: `gpt-5.6-luna`, reasoning effort none, through `codex exec` 0.163.0 under the founder's ChatGPT subscription (not OpenRouter). Codex's instructions were replaced by one line ("Follow the user's instructions."). Its optional tools, context and memories were off, which leaves about 7.4k tokens of Codex wrapper per call. That wrapper is the measured deviation above. Codex's memory store in the isolated home stayed empty (0 rows).
- **Recall budget.** Up to 100 results within 8,000 tokens. Tokens are counted as past.dev's `usedEvidenceTokens` counts them (ceil of characters / 4 per result; reproduces 400/400 of their rows).
- **Free engine.** The v0.7.0 code (cli main 47d0a11) with a `bench` cargo feature that adds dated turn ingest and recall `as_of` (branch bench/daemon-ops, f1080e7). It ran as a release build in a Linux container with no network: one daemon per conversation, model potion-base-8M.
  - **Ingest.** Turns went in with the dataset's times, both roles. A note-taker (`gpt-5.6-luna` through Codex, our own prompt, one call per 20 turns, never shown questions or answers) wrote 3,164 entries through the real `memory.remember`, 0 refused.
  - **Recall.** The engine's ranked facts (50, as released), then turn excerpts (50; the release shows 5), trimmed as above.
- **Runner.** feat/beam-runner, harness lb-1, each question answered once, judged once.

## Cost and time (subscription; tokens from Codex's own usage events)

| Run | Paid calls | Input tokens (cached) | Output tokens | Wall |
|---|---:|---:|---:|---:|
| Calibration a (re-judge) | 1,050 | 8,266,312 (6,877,184) | 41,303 | 18.5 min |
| Calibration b (re-answer) | 1,454 | 15,549,804 (10,010,112) | 99,140 | 25.4 min |
| Free engine (296 note-taker + 400 answer + judge) | 1,747 | 18,699,390 (11,867,392) | 243,779 | 34.8 min |
| **Total** | **4,251** | **42,515,506 (28,754,688)** | **384,222** | **78.7 min** |


## What was not comparable

1. **The model path.** Codex wrapper instructions and tools versus a bare OpenRouter call. Measured above. It costs 1.4 points through the judge alone and 3.5 points across answer and judge.
2. **The free engine's writer.** In the product an agent decides what to `remember`. Here a scripted note-taker does it, with our prompt, 20 turns at a time. A different note-taker would give different numbers.
3. **Turn excerpts.** 240 characters each (the product's), against past.dev's longer evidence. The turn limit was raised to 50 for this run (the release recalls 5).
4. **Times.** The daemon keeps seconds, so turns were placed one second apart instead of one microsecond. Order and day are kept; displayed times map back exactly.
5. **One run, one judgement each.** The plan's "judge three times" was not done. The intervals above are over questions only, not over judge noise.
6. **Event ordering.** Scored as past.dev scores it (`(tau_b+1)/2`), not the official BEAM scorer's x F1.

## Gold on page (goldcheck, lexical; free engine)

The gold answer was in memory / only in a turn / nowhere on the page:
- knowledge_update: 37 / 1 / 2
- information_extraction: 24 / 2 / 14
- multi_session: 12 / 2 / 26
- contradiction: 5 / 0 / 35

The IE and KU gaps against column (b) are mostly misses on the page, not misreadings.

## The two engines on BEAM 100K, lined up

All columns are verified by verify.py; each cell is the mean with a bootstrap 95% interval over questions. The answerer and judge are gpt-5.6-luna (reasoning effort none) through the Codex CLI for every column except past.dev's published one.

| Category | past.dev published (a) | Our judge on their answers | Our answer + judge on their pages (b) | **Free engine** (CLI, shipped page v0.7.1 + contested-line fix) | Workspace engine + validTo-0 fix | **Workspace engine + validTo-0 fix + page fix** (text page) |
|---|---:|---:|---:|---:|---:|---:|
| abstention | 98.75 | 95.00 | 97.50 | 98.75 [96.2, 100.0] | 96.25 [90.0, 100.0] | 100.00 [100.0, 100.0] |
| contradiction_resolution | 78.12 | 71.25 | 69.38 | 67.81 [64.1, 71.2] | 58.13 [52.5, 63.4] | 65.94 [60.9, 70.6] |
| event_ordering | 100.00 | 100.00 | 98.78 | 99.94 [99.8, 100.0] | 99.52 [98.6, 100.0] | 100.00 [100.0, 100.0] |
| information_extraction | 95.00 | 93.75 | 89.69 | 84.69 [76.8, 91.6] | 65.42 [53.6, 76.8] | 71.93 [61.4, 81.9] |
| instruction_following | 96.25 | 96.88 | 91.88 | 92.50 [85.6, 98.1] | 88.12 [78.8, 96.2] | 86.88 [77.5, 95.0] |
| knowledge_update | 89.38 | 89.38 | 85.62 | 78.12 [65.6, 89.4] | 60.62 [45.6, 75.0] | 68.12 [53.8, 81.9] |
| multi_session_reasoning | 68.95 | 67.76 | 64.63 | 65.59 [53.8, 76.5] | 56.15 [44.1, 68.0] | 62.15 [50.6, 73.2] |
| preference_following | 100.00 | 100.00 | 98.12 | 100.00 [100.0, 100.0] | 99.38 [98.1, 100.0] | 99.38 [98.1, 100.0] |
| summarization | 99.38 | 99.17 | 98.25 | 97.69 [95.4, 99.4] | 97.29 [95.1, 99.1] | 98.85 [97.6, 99.8] |
| temporal_reasoning | 95.00 | 93.75 | 92.50 | 95.62 [92.5, 98.1] | 95.00 [88.8, 99.4] | 95.00 [88.8, 99.4] |
| **overall (micro)** | **92.08** | **90.69** | **88.63** | **88.07 [85.6, 90.4]** | **81.59 [78.5, 84.6]** | **84.82 [82.0, 87.5]** |

**Like-for-like reading.** Column (b) holds the answerer, judge, prompts and budget fixed and changes only the memory, so it is the comparison.
- **Free engine:** 88.07 against (b)'s 88.63, inside the noise. Without the contested-line fix (the shipped v0.7.1) it is 87.43.
- **Workspace engine:** 81.59, a paired -7.05 [-9.70, -4.54] against (b) and -6.48 [-8.95, -4.20] against the free engine. Both gaps are outside the noise.
  - Its losses are in information extraction (65.42), knowledge update (60.62), contradiction (58.13) and multi-session (56.15).
  - Its recall pages are small: 1,870 of 8,000 evidence tokens on average (max 5,653), mostly extracted claims with almost no turns. It runs expansion and rerank as the Memory tab does.

**The workspace page fix.** Measured commit: feat/workspace-recall-page eae5d4b6a. 46e52d0a9 changes only edge cases in `page_text`, none of which occur on this dataset's pages. The bench routes are on top: bench/workspace-page 323e26ef9, local.
- **What the fix does:** compose charges the content, not the JSON; a page holds up to 50 turns plus 50 other results; each search arm keeps its top 50 before rerank, with 800-character matched windows; and the answerer reads `memory_stages.page_text`, the text page an agent gets.
- **Result:** **84.82 [82.0, 87.5]**. Paired, that is +3.24 [+0.76, +5.84] against 81.59, -3.25 [-5.42, -1.18] against the free engine's 88.07, and -3.81 [-5.79, -1.92] against (b). All three are outside the noise.
- **Against 81.59, by category:**
  - contradiction +7.81 [+2.81, +13.12] (outside the noise);
  - information extraction +6.51, knowledge update +7.50, multi-session +6.01 (all inside the noise);
  - instruction following -1.25.
- **Pages:** now 7,188 evidence tokens on average, against 1,870 before.
- **Gap that remains:** `page_text` dates a claim by its record's creation time (the ingest day, 2026-10-29), not its validFrom. So every claim line on these pages carries the wrong day; the claim text's own "(since …)" form is the only date it shows.
- **AWS side:** ingest 963 s of request time; extraction settled 185–362 s after the last ingest (median 268 s), producing 4,488 claims; 400 recalls took 1,674 s in all (4.2 s each). 0 throttles, 0 retries. The run took 43.1 min wall (06:47:18–07:30:24 UTC).
- **Codex:** 1,450 paid calls, 14,003,440 input tokens (10,219,264 cached), 105,302 output.
- **Cleanup:** the bench owners were deleted twice and confirmed empty.


**Workspace engine conditions.** Run locally on this Mac (`apps/backend/scripts/bench_local.py` from bench/workspace-routes 1f8906af2, routes only, none of main.py's sweepers), against dev's AgentCore workspace memory (`lightbulb_dev_workspace_memory-uNdawU9Abr`; prod ids refused in code), AWS profile isol8-admin. Each conversation is a synthetic `bench-<ulid>` owner.
- **Ingest.** Each exchange went in as one CreateEvent at the dataset's time; AgentCore's `workspace_claims` strategy extracted 4,512 claims. Each turn was also written as a `/turns/` record.
- **The two deviations above:** the dated event prefix, and turns dated by validFrom.
- **Plus the validTo-0 fix**, without which every extracted claim is invisible to recall: the as-is engine recalls turns only.
- **AWS side:**
  - Ingest: 5,732 turns, 2,866 events, 949 s of request time across 4 parallel conversations (wall 4.3 min).
  - Extraction settled 210–356 s after each conversation's last ingest (median 254 s).
  - 400 recalls took 1,826 s in all, 4.6 s each on average.
  - 0 throttles, 0 retries.
- **Whole run:** 45.8 min wall (05:39:59–06:25:47 UTC).
- **Codex:** 1,453 paid calls, 14,766,022 input tokens (10,050,944 cached), 93,900 output.
- **Cleanup:** every bench owner was deleted, then deleted again at least 3 minutes later, and confirmed at 0 facts / 0 turns. That includes the 20 owners from the paused attempt.

## CLI loop (free engine; same ingest, harness and calibration columns)

Misses are classified in the session ledger (beam-cli-loop.md). The top cause in information extraction and multi-session reasoning: the answer sits in a retrieved turn, but past the 240-character opening the excerpt shows. Pages used 4.6k of 8k tokens.

| Category | Re-answer (b) | Free engine | + matched excerpt window | Delta vs free | Delta vs (b) |
|---|---:|---:|---:|---:|---:|
| information_extraction | 89.69 | 81.82 | 80.83 [72.3, 88.6] | -0.99 | -8.86 |
| knowledge_update | 85.62 | 80.62 | 81.88 [69.4, 92.5] | +1.26 | -3.74 |
| contradiction_resolution | 69.38 | 69.06 | 67.19 | -1.87 | -2.19 |
| multi_session_reasoning | 64.63 | 64.86 | 63.21 | -1.65 | -1.42 |
| temporal_reasoning | 92.50 | 93.75 | 95.62 | +1.87 | +3.12 |
| **overall (micro)** | **88.63** | **87.86** | **87.65 [85.1, 90.1]** | **-0.21** | **-0.98** |

- **Change 1 (cli loop/turn-excerpt-window 3e291ac, on main 10eb2ac).** A turn's excerpt is the 240-character window with the most question words.
  - **It did not move the two weak categories.** Both stay inside the noise. In none of the 20 targeted misses did the gold reach the page: 240 characters is too short for these multi-part answers, wherever the window sits.
  - **Run:** verified, 0 failed; 1,174 paid calls, 11,735,379 input (8,312,832 cached), 96,849 output; 25.5 min.
- **Change 2 (cli loop/turn-excerpt-length 0209a77, on top of change 1).** `memory.recall` takes `excerpt_chars` (default 240, so the shipped page is unchanged). Asked at 800, pages now fill the budget (7,851 of 8,000 tokens on average).

| Category | Re-answer (b) | Free engine | + matched 800-char excerpts | Paired delta vs free [95%] | Delta vs (b) |
|---|---:|---:|---:|---:|---:|
| information_extraction | 89.69 | 81.82 | 85.62 [78.4, 92.0] | +3.80 [+0.73, +7.03] | -4.07 |
| knowledge_update | 85.62 | 80.62 | 85.62 [74.4, 95.0] | +5.00 [0.00, +12.50] | 0.00 |
| multi_session_reasoning | 64.63 | 64.86 | 68.80 [57.6, 79.4] | +3.94 [-2.25, +11.62] | +4.17 |
| contradiction_resolution | 69.38 | 69.06 | 67.81 | -1.25 [-4.38, +1.88] | -1.57 |
| instruction_following | 91.88 | 93.12 | 96.25 | | +4.37 |
| **overall (micro)** | **88.63** | **87.86** | **89.62 [87.3, 91.8]** | **+1.76 [+0.44, +3.20]** | **+0.99** |

  - **Reading.** Paired over the same 400 questions, the change moves information extraction (+3.8) and the overall (+1.8) outside the noise. Knowledge update reaches the edge of it (+5.0, lower bound 0). Multi-session is up but inside the noise.
  - **Against the like-for-like column (b).** The free engine with this change is now level with past.dev's memory overall: 89.62 vs 88.63, a difference inside the noise.
  - **Confounds.**
    - The answers are fresh samples on changed pages, so some of the delta is answer and judge noise.
    - The bench binary also carries main's #90 (the embedding model looked up per store).
  - **Lexical check.** The 20 targeted misses still show the gold as "nowhere". These golds are long prose, so the lexical check under-counts them.
  - **Caveat, unchanged.** This is the generous recall (50 facts + 50 excerpts, now 800 characters each). The shipped page row (73.82) is the product as an agent gets it today.
  - **Run:** verified, 0 failed; 1,189 paid calls, 12,885,631 input (8,410,368 cached), 100,517 output; 23.9 min.

- **Change 3 (cli loop/fact-dates 8b8f27d, on top of change 2).** Each recalled fact carries a dated line, "subject: value (YYYY-MM-DD)", and among facts sharing a subject the newest comes first.
  - **Result: no gain; do not fold.** Overall 88.61, paired vs 89.62: -1.01 [-2.66, +0.59]. Knowledge update fell to 76.88, -8.75 [-20.00, +1.25]: worse, though inside the noise. Information extraction -1.93 [-5.89, +1.46]; multi-session +3.23 [-2.71, +10.38]; the rest within about 1.3 points.
  - **Run:** verified, 0 failed; 1,173 paid calls, 12,869,611 input (8,320,512 cached), 100,282 output; 24.9 min.

## Free engine, shipped page (v0.7.1)

This run uses cli feat/recall-page 35c2fa5, with the bench ops (dated ingest, as_of) on top. The answerer reads exactly what an agent gets: the daemon's `page` text for a question. That page holds up to 50 facts and 50 turn excerpts (800-character matched windows), cut by the daemon's own 100 results / 8,000 tokens trim, with each line's author and date. There is no bench-side excerpt option. Pages averaged 7,980 tokens (max 8,197); the runner passes the page through as it came.

| Category | Re-answer (b) | Shipped 0.7.0 page | Generous 800 (arrays) | **Shipped page v0.7.1** | Paired vs 89.62 [95%] |
|---|---:|---:|---:|---:|---:|
| abstention | 97.50 | 100.00 | 98.75 | 98.75 [96.2, 100.0] | 0.00 |
| contradiction_resolution | 69.38 | 50.94 | 67.81 | 66.88 [62.5, 70.9] | -0.94 [-3.12, +1.25] |
| event_ordering | 98.78 | 99.94 | 99.94 | 99.94 [99.8, 100.0] | 0.00 |
| information_extraction | 89.69 | 56.35 | 85.62 | 81.77 [72.9, 89.5] | -3.85 [-9.48, +0.52] |
| instruction_following | 91.88 | 75.00 | 96.25 | 95.62 [90.6, 99.4] | -0.62 [-6.25, +6.25] |
| knowledge_update | 85.62 | 48.75 | 85.62 | **68.12 [53.8, 81.9]** | **-17.50 [-30.00, -7.50]** |
| multi_session_reasoning | 64.63 | 20.57 | 68.80 | 68.30 [57.0, 79.0] | -0.50 [-9.14, +8.00] |
| preference_following | 98.12 | 97.50 | 100.00 | 100.00 | 0.00 |
| summarization | 98.25 | 91.67 | 98.42 | 98.04 | -0.38 [-2.15, +1.48] |
| temporal_reasoning | 92.50 | 97.50 | 95.00 | 96.88 | +1.88 [-3.12, +7.50] |
| **overall (micro)** | **88.63** | **73.82** | **89.62** | **87.43 [84.8, 89.8]** | **-2.19 [-4.14, -0.46]** |

- **Reading.** The shipped page goes from 73.82 to 87.43 (+13.6). That is 1.2 points below like-for-like (b), inside the noise.
- **Against the arrays.** It is 2.19 points below the same evidence given as arrays (89.62), and that gap is outside the noise. All of it is knowledge update (-17.5, outside the noise); every other category is within the noise.
- **Why knowledge update drops.** In 6 of the 7 knowledge-update questions that lost half a point or more, the gold value sits inside one long "Contested:" line. That line joins every fact sharing a subject with " vs ", stale value first, dated "15 Mar" without a year.
  - Example (2_knowledge_update_0): "weather app: … 1,000 daily call limit (bench, 15 Mar) vs …", and the answer gives 1,000 against the gold 1,200.
  - The note-taker reuses broad subjects ("weather app"), so unrelated facts get grouped as contested.
  - The arrays gave the answerer separate results, with ISO dates and the turn excerpts.
- **Run:** verified, 0 failed; 1,168 paid calls, 11,958,257 input (8,344,704 cached), 99,270 output; 22.9 min.

### Loop 3: the page says Contested only when two people disagree

Branch: cli loop/contested-line 94d73ff, on v0.7.1 main 53d48f3. One author's values for a subject now print as separate lines, in rank order, each dated with the full YYYY-MM-DD, instead of one "Contested: a vs b vs …" line. The answerer still reads the daemon's page text.

| Category | Shipped page v0.7.1 | **+ contested line fix** | Paired vs 87.43 [95%] | Paired vs 89.62 (lists) [95%] |
|---|---:|---:|---:|---:|
| information_extraction | 81.77 | 84.69 | +2.92 [-0.52, +8.02] | -0.94 [-3.85, +1.67] |
| knowledge_update | 68.12 | **78.12** | **+10.00 [+2.50, +20.00]** | -7.50 [-17.50, 0.00] |
| multi_session_reasoning | 68.30 | 65.59 | -2.71 [-10.86, +5.33] | -3.21 [-8.00, +0.17] |
| contradiction_resolution | 66.88 | 67.81 | +0.94 [-1.56, +3.44] | 0.00 |
| instruction_following | 95.62 | 92.50 | -3.12 [-8.75, 0.00] | -3.75 [-11.88, +3.75] |
| temporal_reasoning | 96.88 | 95.62 | -1.25 [-3.75, +1.25] | +0.62 |
| **overall (micro)** | **87.43** | **88.07 [85.6, 90.4]** | **+0.64 [-0.79, +2.22]** | **-1.55 [-3.00, -0.25]** |

- **Knowledge update moves outside the noise:** +10.0 over v0.7.1. It does not reach the lists: 78.12 vs 85.62.
- **The overall does not move outside the noise:** 88.07, +0.64. Against like-for-like (b) at 88.63 it is -0.56, inside the noise.
- **Run:** verified, 0 failed; 1,138 paid calls, 11,877,757 input (8,143,104 cached), 99,528 output; 22.2 min.

Results files: `benchmarks/beam/results/100k/`, stripped to ids, answers, verdicts and scores (no recall pages, questions or gold text). `verify.py` recomputes all six.
