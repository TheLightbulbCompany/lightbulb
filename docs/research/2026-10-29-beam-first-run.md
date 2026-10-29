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
