# BEAM runner facts: past.dev's harness, the dataset, the official scorer, prices (28 October 2026)

Scope: what the Lightbulb BEAM runner must copy or replace. Sources read 28 Oct 2026 with WebFetch/WebSearch only; nothing downloaded or run. Anything not from the repos, the paper or HuggingFace is marked [unverified]. Builds on `docs/research/2026-10-27-oss-beam-failures.md` and `2026-10-27-past-dev-how-it-works.md` and the spec section 8.

## 1. past.dev's BEAM runner

Repo `github.com/pastdotdev/benchmarks`, path `beam/` (https://github.com/pastdotdev/benchmarks/tree/main/beam).

**License.** Harness code is MIT, copyright 2026 Past Corp (https://raw.githubusercontent.com/pastdotdev/benchmarks/main/LICENSE). `beam/NOTICE.md` (https://raw.githubusercontent.com/pastdotdev/benchmarks/main/beam/NOTICE.md) carves out three things from the MIT grant: the ExaBase-derived parts of `exabase_prompts.py` (ExaBase's work, not relicensed), the published `results/*/results.jsonl` files (CC BY-SA 4.0 because they contain dataset content), and the BEAM dataset itself (CC BY-SA 4.0). So "fork the MIT runner" is true for everything except `exabase_prompts.py`; we may use the prompts but should not claim them as MIT. Ask ExaBase or reference their file rather than redistribute (open question for the founder).

**Files and what each does** (README https://raw.githubusercontent.com/pastdotdev/benchmarks/main/beam/README.md):

| File | Job | URL stem |
|---|---|---|
| `run.py` | CLI and orchestration in three phases: send (create a project per conversation, register identities, ingest), wait (poll until pushes `completed` and project `settled`), score (recall, answer, judge). Writes `results.jsonl` and `summary.json`. | .../beam/run.py |
| `dataset.py` | Loads HF rows, builds `Conversation`, `Message`, `Question` dataclasses, session dates and per-turn timestamps, question `as_of` times. | .../beam/dataset.py |
| `past_api.py` | The only file that talks to past.dev: `create_project`, `register_identities`, `ingest_batch`, `ingestion_status`, `recall(query, as_of, identity)`. Constants `RECALL_LIMIT = 100`, `RECALL_MAX_TOKENS = 8_000`. | .../beam/past_api.py |
| `exabase_prompts.py` | ExaBase's answer prompt (per-category guidance), rubric-item judge prompt, equivalence prompt, their JSON schemas, `SOURCE_URL`, `SOURCE_SHA256`. | .../beam/exabase_prompts.py |
| `llm.py` | OpenRouter chat client: `complete_text`, `complete_json`; 90 s timeout, 4 attempts (1, 2, 4 s backoff); 401/402/403 stop the run. | .../beam/llm.py |
| `scoring.py` | `score_question`, `judge_rubric_item`, `event_ordering_score`, `ordering_score`, `summarize`. | .../beam/scoring.py |
| `verify.py` | `--prompts` hash check; `verify.py results/<split>` recomputes scores from saved verdicts with no API calls. | .../beam/verify.py |
| `log.py`, `requirements.txt`, `.gitignore`, `NOTICE.md` | support | |

**Models and provider.** Answer and judge are the same model: `openai/gpt-5.6-luna` through OpenRouter (`https://openrouter.ai/api/v1/chat/completions`), `reasoning: {"effort": "none"}`, `usage: {"include": true}`, no temperature or max_tokens sent; the judge calls add a strict `json_schema` response format and `provider.require_parameters: true` (https://raw.githubusercontent.com/pastdotdev/benchmarks/main/beam/llm.py). Env vars: `PAST_API_URL`, `PAST_MANAGEMENT_KEY`, `OPENROUTER_API_KEY`, optional `BEAM_OPEN_CACHE` (README).

**Judge prompts' origin and hash check.** The answer and judge prompts are ExaBase's published BEAM adapter, "unchanged", from https://fabric.so/p/beam-3VjBcqEVRofZyA5CeazeX with SHA-256 `584b672d957f3e6f75e96f881ec31da22e1039da1df5fbedb53dc6a9145cfc43` (NOTICE.md, exabase_prompts.py). `python verify.py --prompts` downloads ExaBase's `.py` (finds a signed `cdn.fabric.so` link on that page), hashes it, then checks that `answer_prompt`, `rubric_item_prompt`, `equivalence_prompt` bodies and the `RUBRIC_SCHEMA` / `EQUIVALENCE_SCHEMA` match after normalising indentation (verify.py). The check is of the upstream file and of the bodies, not of a runtime hash of what is sent.

Prompt shape (exabase_prompts.py): the answer prompt says to answer only from the retrieved context, adds per-category guidance, then question, context, "Answer:". Guidance: abstention uses a fixed refusal sentence ("Based on the provided chat, there is no information related to ..."); event ordering lists only the `ordering_tested` topics as a numbered chronological list; knowledge update gives the latest value; information extraction gives the originally stated value; multi-session reasoning lists items with turn references, dedups, then computes; temporal reasoning shows arithmetic given `time_points` and `calculation_required`; summarization uses the rubric strings (minus "LLM response should contain:") as hints. The judge scores one rubric item per call as 0, 0.5 or 1 with a reason; the equivalence prompt answers YES/NO.

**Timestamps at ingest.** Each session gets a date from the first `time_anchor` in the session; each turn is placed at noon UTC of that day plus its ordinal in microseconds (dataset.py, README). Missing session dates are filled from neighbours (previous plus one hour, or next minus one day). Turn metadata is only role, author, speaker. Messages sent in pushes of up to 100 items or 8 MiB, one push at a time, with a SHA-256 idempotency key (run.py). Recall time (`queryTimestamp`/`as_of`) is the latest message timestamp, except knowledge_update questions, which are asked at the latest timestamp of the turns in `source_chat_ids.updated_info` (dataset.py). Annotations (rubric etc.) never go to past.dev.

**Evidence budget.** Each recall asks for up to 100 results within 8,000 tokens (`RECALL_LIMIT`, `RECALL_MAX_TOKENS`); the whole recall JSON is serialised compact and given to the answer model as `context` (run.py `answer_question`). The tokenizer used for the budget is past.dev's server side [unverified]; our adapter must pick its own counter and state it.

**CLI** (README, run.py): `--split {100k,500k,1m,10m}` (default 100k), `--conversations N` (first N only), `--output DIR` (default `results/beam-<split>`), `--reuse TAG` (skip ingest, re-recall and rescore existing projects). Rerunning skips conversations already in `results.jsonl`. Concurrency: 4 conversations sent at once, 4 scored at once, 1 recall at a time.

**Results format.** `results.jsonl`, one line per question: `questionId, conversationId, category, question, asOf, goldAnswers, recall, answer, judge, score`; failed questions get `error`, `judge: {"method":"failed"}`, score 0. `summary.json`: scores, per-category means, dataset revision, models, failed-call count (failed calls score 0). Summary has `microAverage` (headline) and `macroAverage` (scoring.py).

**Cost and time as stated.** No dollar figure is stated. Time: 10M "takes hours" because of the wait for processing; 10M dataset about 1 GiB downloaded once; one past.dev project per conversation (20, 35, 35, 10), never deleted (README).

**Smallest set to replace to plug in another backend.** Keep `dataset.py`, `exabase_prompts.py`, `llm.py`, `scoring.py`, `verify.py`, and most of `run.py`. Replace `past_api.py` with an adapter exposing: (1) `create_project(name) -> slug` (a namespace per conversation); (2) `register_identities`; (3) `ingest_batch(items, idempotency_key) -> receipt` where items come from `dataset.to_ingest_item` (content, role, author, identity, ISO timestamp); (4) `ingestion_status(id) -> {status, settled}` (our adapter can return completed/settled once the note-taker pass finishes); (5) `recall(query, as_of, identity) -> dict` returning at most 100 results within 8,000 tokens. In `run.py` the touch points are `open_project`, `ingest`, `wait_until_processed`, `answer_question` (calls `project.recall`, then `check_recalled_moment`, which requires the response to echo `asOf`; our recall must echo it or this check be adapted) and `reopen_projects` (only for `--reuse`).

## 2. The BEAM dataset

Hosted on HuggingFace: `Mohammadta/BEAM` (https://huggingface.co/datasets/Mohammadta/BEAM) holds splits `100K` (20 rows), `500K` (35), `1M` (35), total 90 rows, 106 MB of files, parquet. `Mohammadta/BEAM-10M` (https://huggingface.co/datasets/Mohammadta/BEAM-10M) holds `10M` (10 rows), 344 MB; the viewer fails (row group 916 MiB). Per-split size on disk is not listed by the cards; past.dev says the 10M split is about 1 GiB when downloaded and cached (README), which differs from the 344 MB on the hub (probably parquet compression; not explained). Total 100 conversations, 2,000 validated questions (paper abstract https://arxiv.org/abs/2510.27246).

Questions per split from past.dev's results: 100K 400, 500K 700, 1M 700, 10M 200 (README), i.e. 20 per conversation at every split; per-ability counts per split are not stated in the sources read [unverified].

Fields per row: `conversation_id`, `conversation_seed` (dict, title/theme/subtopics), `narratives`, `user_profile` (dict with `user_info`, `user_relationships`), `conversation_plan`, `user_questions`, `chat` (list of sessions; turn fields `role`, `content`, `id`, `time_anchor`), `probing_questions` (a stringified dict keyed by ability; parse with `ast.literal_eval`). For 10M the row also has a `plans` array of ten plans (plan-0..plan-9), each with its own seed, profile, plan, chat; past.dev's loader handles "plans of batches of turn groups" (dataset.py). Question fields used: `question`, a reference answer (first present of `ideal_response`, `answer`, `expected_answer`, `expected`, `gold_answer`, `reference`, with fallbacks), `rubric`, plus `why_unanswerable`, `instruction_being_tested`, `preference_being_tested`, `compliance_indicators`, `time_points`, `calculation_required`, `ordering_tested`, `source_chat_ids.updated_info`.

Ten abilities: abstention, contradiction_resolution, event_ordering, information_extraction, instruction_following, knowledge_update, multi_session_reasoning, preference_following, summarization, temporal_reasoning (dataset card). Rubric format: a list of strings per question (loaded as `data[key][index]['rubric']` in the official runner; the `annotations()` field in past.dev's loader); no rubric means a single item built from the gold answer (scoring.py).

License: CC BY-SA 4.0 for the data; MIT for the BEAM repo code (https://github.com/mohammadtavakoli78/BEAM). ShareAlike matters if we publish results containing dataset text: publish per-question results with the same licence, as past.dev does. The cards state no rate limits or other ToS [unverified beyond the cards].

## 3. The BEAM paper and the official repo

Paper: Tavakoli et al., "Beyond a Million Tokens: Benchmarking and Enhancing Long-Term Memory in LLMs", arXiv 2510.27246 (31 Oct 2025). The arXiv abstract page does not describe the scoring; I did not read the PDF [unverified: paper text].

Repo https://github.com/mohammadtavakoli78/BEAM: `src/answer_probing_questions/` (`answer_generation.py`, `.sh`, `light.py`, `long_term_memory_methods.py`), `src/evaluation/` (`run_evaluation.py`, `compute_metrics.py`, `report_results.py`). Official answerer config: `gpt-4.1-mini` at temperature 0 is the `gpt_llm` (answer_generation.py); the judge is the same `gpt_llm` passed into `run_evaluation.py` (raw files: .../main/src/answer_probing_questions/answer_generation.py, .../main/src/evaluation/run_evaluation.py). Retrieval default `k=5`. The answer prompt text and how turn ids/time anchors are shown live in `long_term_memory_methods.py`, which I did not read [unverified].

Official scoring (https://raw.githubusercontent.com/mohammadtavakoli78/BEAM/main/src/evaluation/compute_metrics.py): per ability, for each rubric item fill `unified_llm_judge_base_prompt`, parse the JSON `score`, average over the rubric. Event ordering: align the system's list to the reference with an LLM YES/NO (or MiniLM embeddings with threshold 0.65), compute set precision/recall/F1, then Kendall tau-b over ranks with missing items at rank `len(union)+1`; `tau_norm = (tau_b + 1)/2`; **`final = tau_norm x F1`**. Nugget scoring (fact extraction, precision/recall/F1, `bge-large-en-v1.5` at 0.7) exists in the file as `semantic_fact_precision_recall_f1`.

**Does the official scorer differ from past.dev's?** Yes, in at least three ways (read from the two files):
1. Event ordering: past.dev scores `(tau_b + 1)/2` only (scoring.py), the official code multiplies by F1. So past.dev's 99.8 to 100 on event ordering is not comparable to official-style numbers; unmatched or extra items are not penalised separately except through the rank rule.
2. Rubric rounding: past.dev rounds the judge's raw score to 0 / 0.5 / 1 (>=0.75 -> 1, >=0.25 -> 0.5) (scoring.py); the official code sums the judge's `score` values directly; whether the official judge emits only 0/0.5/1 depends on the prompt I did not read.
3. Judge and answer model: official is gpt-4.1-mini; past.dev uses ExaBase prompts with gpt-5.6-luna.
The headline in past.dev is a micro average over questions; the paper's report aggregates per ability via `report_results.py` [unverified].

## 4. Numbers to beat

past.dev, answer and judge `openai/gpt-5.6-luna` (reasoning none), ExaBase prompts, 100 results within 8,000 tokens, each question once, query at end of conversation (README https://raw.githubusercontent.com/pastdotdev/benchmarks/main/beam/README.md):

| Category | 100K | 500K | 1M | 10M |
|---|---:|---:|---:|---:|
| Abstention | 98.75 | 92.14 | 97.14 | 100.00 |
| Contradiction resolution | 78.13 | 78.57 | 75.36 | 73.13 |
| Event ordering | 100.00 | 100.00 | 99.81 | 100.00 |
| Information extraction | 95.00 | 86.32 | 83.98 | 68.75 |
| Instruction following | 96.25 | 92.02 | 96.43 | 95.00 |
| Knowledge update | 89.38 | 86.07 | 85.71 | 92.50 |
| Multi-session reasoning | 68.95 | 71.39 | 72.82 | 28.17 |
| Preference following | 100.00 | 99.05 | 98.87 | 100.00 |
| Summarization | 99.38 | 97.25 | 99.60 | 99.00 |
| Temporal reasoning | 95.00 | 93.45 | 96.79 | 93.75 |
| **Overall (micro)** | **92.08** | **89.63** | **90.65** | **85.03** |

Combined over 2,000 questions: 90.02. Conversations/questions: 20/400, 35/700, 35/700, 10/200. Other systems' conditions are in `2026-10-27-oss-beam-failures.md` §1 (not repeated).

## 5. OpenRouter

- `openai/gpt-5.6-luna`: exists; OpenRouter lists it at $0.20 per million input, $1.20 per million output, cache read $0.02, cache write $0.25, about 1.05M context (search summary of https://openrouter.ai/openai/gpt-5.6-luna; also https://openrouter.ai/models/openai/gpt-5.6-luna-20260709). The model page I fetched showed only the EU in-region price, $0.22 / $1.32, which is exactly 10% above $0.20 / $1.20, consistent with the standard price. An article dated 1 Aug 2026 says OpenAI cut it from $1 / $6 (https://propakistani.pk/2026/08/01/openai-cuts-gpt-5-6-luna-price-by-80-soon-after-launch/) [unverified, secondary]. Price cut after past.dev's run: their run cost more than a rerun would. The bare id may now alias a dated snapshot (`...-20260709`); the model behind the id can change, so record the resolved model from the `usage`/response in each run [unverified whether the alias moved]. I could not load the live listing (openrouter.ai/models) through the fetch tool; verify before the final run.
- `anthropic/claude-haiku-4.5`: exists (https://openrouter.ai/anthropic/claude-haiku-4.5). Standard price $1.00 input, $5.00 output per million, cache read $0.10 (aggregator pages via search, e.g. https://openrouter.ai/compare/anthropic/claude-haiku-4.5) [unverified: not read on the official page; the official page I fetched showed only EU $1.10 / $5.50, again 10% above $1.00 / $5.00].

## 6. Practicalities

- Python 3.11 or newer; packages `httpx>=0.27`, `datasets>=3.0`, `huggingface_hub>=0.25`, `scipy>=1.13` (requirements.txt).
- Dataset downloads once from HF and is cached at `~/.cache/beam-open` (rows as `{split}.json` plus a `.revision` file pinned to the dataset SHA); the recorded revision lands in `summary.json`.
- OpenRouter: 401/402/403 abort the run; 429/5xx retried 3 times (1, 2, 4 s). past.dev's own client uses a 300 s timeout, 4 attempts.
- Rate limits or ToS for the dataset: none stated on either card besides CC BY-SA 4.0.
- Resuming: rerun skips finished conversations; interrupted conversation restarts in a new project; `--reuse TAG` rescoring only works if our adapter keeps the conversation's project state.
- For our adapter, ingest idempotency keys hash the batch JSON, so any change to ingest payload changes keys (README).

## What our runner must keep identical

1. Prompts: `exabase_prompts.py` byte for byte, `verify.py --prompts` passing against SHA-256 `584b672d...fc43`; record the hash in each result.
2. Models: `openai/gpt-5.6-luna` via OpenRouter for answer and judge, `reasoning.effort = none`, no temperature or max tokens, json_schema for the judge; record the resolved model per run.
3. Budget: up to 100 results and 8,000 tokens of evidence per question, the whole recall JSON passed as `context`; each question answered once.
4. Ingest: turns in order, noon UTC of the session day plus ordinal microseconds, both roles stored, user and `assistant` identities, user traits from the profile; knowledge_update questions asked at the cited update turn, others at the last message.
5. Scoring: `scoring.py` unchanged (0/0.5/1 rounding at 0.75 and 0.25, event ordering `(tau_b+1)/2`, failed calls score 0, micro average headline); `verify.py` on our results. If we also report official-style event ordering (x F1), label it separately.
6. Dataset: same HF revisions, same splits and `--conversations` semantics (first N).

## Cost estimate

Assumptions (mine, not from sources): split size = conversations x nominal tokens; answer prompt = 8,000 evidence + ~1,000 prompt and question = 9,000 in, ~300 out per question; judge = 3 rubric items per question at ~700 in and ~60 out each (event ordering adds more equivalence calls, ignored); prices gpt-5.6-luna $0.20 / $1.20 and Haiku 4.5 $1.00 / $5.00 per million (no caching). Note-taker pass: Haiku reads ~1.2x the split size in input; output assumed 15% of input (a guess; the real ratio comes from the extraction prompt).

| Split | Conv / Q | Answer in / out | Judge in / out | Answer + judge cost | Haiku note-taker in / out | Haiku cost | Total |
|---|---|---|---|---|---|---|---|
| 100K | 20 / 400 | 3.6M / 0.12M | 0.84M / 0.07M | $1.1 | 2.4M / 0.36M | $4.2 | $5.3 |
| 500K | 35 / 700 | 6.3M / 0.21M | 1.47M / 0.13M | $2.0 | 21M / 3.2M | $37 | $39 |
| 1M | 35 / 700 | 6.3M / 0.21M | 1.47M / 0.13M | $2.0 | 42M / 6.3M | $74 | $76 |
| 10M | 10 / 200 | 1.8M / 0.06M | 0.42M / 0.04M | $0.6 | 120M / 18M | $210 | $211 |

Reading: with the cut luna price, answer and judge are a few dollars per split; the note-taker pass dominates, so the spec's ~$60 for 1M extraction and ~$50-80 for 10M looks low by about 1x and 3x under these assumptions unless batching or caching (Haiku cache read $0.10) or a smaller extraction output cuts it. Excludes the backend's own embedding or storage costs and past.dev's own baseline rerun.
