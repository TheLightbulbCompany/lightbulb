# Memory BEAM Runner Implementation Plan

> **Status (27 Oct 2026): deferred.** The founder's call: build from the engineering, run no benchmarks for now. Nothing in this plan is built until we decide to run. The engine plan's Task 1 Step 5 (the probe cases against the dev memory, cents) is the only measurement kept.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run BEAM (100K through 10M) on past.dev's own benchmark runner with three memory backends behind one adapter interface (past.dev, today's AgentCore built-in, the Lightbulb engine), a free evidence-on-page signal for tuning, a cheap answerer for loops, and the fixed model once per split for the published numbers.

**Architecture:** past.dev's MIT runner is vendored unchanged except `past_api.py`, which becomes one of three adapters chosen by `--memory`. The Lightbulb adapter talks HTTP to the backend's dev-only benchmark endpoints (engine plan Task 6), so the runner works from a laptop. Results keep past.dev's `results.jsonl` and `summary.json` shapes so `verify.py` works on ours.

**Tech Stack:** Python 3.11, httpx, the vendored runner (`run.py`, `dataset.py`, `scoring.py`, `llm.py`, `exabase_prompts.py`, `verify.py`), OpenRouter for the answer and judge model, boto3 for the AgentCore baseline.

**Spec:** `docs/superpowers/specs/2026-10-27-memory-layer-design.md` §8; `docs/research/2026-10-27-past-dev-how-it-works.md` §1–§2.

## Global Constraints

- Branch from clean `origin/main` in a fresh worktree (CLAUDE.md).
- The vendored runner keeps its MIT `LICENSE` and `NOTICE.md`; ExaBase's prompts unchanged; `verify.py --prompts` passes (SHA-256 `584b672d…fc43`).
- Published numbers: answer and judge `openai/gpt-5.6-luna` via OpenRouter, reasoning off; recall `limit=100`, `maxTokens=8000`; each question answered once. Tuning loops may use `--answer-model anthropic/claude-haiku-5-5` and are never published as comparable.
- Dates: each turn at noon UTC on its BEAM day plus its position in microseconds (`dataset.py`, unchanged); adapters pass timestamps through.
- Secrets only from environment variables (`PAST_MANAGEMENT_KEY`, `OPENROUTER_API_KEY`, `LIGHTBULB_ADMIN_TOKEN`, AWS credentials).
- Results directories: `results/<memory>-<split>[-<tag>]/`; past.dev's published results stay untouched under `results/past-published/`.
- Benchmark records are deleted the same day a run finishes (`DELETE /benchmark/projects/{id}` per conversation at the end of `run.py`, unless `--keep`).

## Review Focus

1. A recall page over 8,000 tokens from any adapter must be refused, not passed through. (Tasks 2, 3: `test_recall_page_respects_budget`.)
2. Questions asked before ingest has settled look like memory failures; adapters block until settled and the summary records settle time. (Task 2: `test_ingestion_status_waits_for_stable_count`.)
3. An interrupted run resumes without re-ingesting or double-counting. (Task 3: `test_ingest_batch_is_idempotent`.)
4. Repeated judge passes are averaged and their spread reported, never overwritten. (Task 4: `test_judge_runs_average_and_spread`.)
5. The evidence signal must not be fooled by a gold number that appears in an unrelated turn; it reports memory / turn-only / nowhere separately and the category breakdown, and is labelled a proxy in every output. (Task 4: `test_evidence_signal_splits_memory_turn_nowhere`.)

---

### Task 1: Vendor the runner

**Files:**
- Create: `benchmarks/beam/` (copy of `pastdotdev/benchmarks/beam` at the commit in `docs/research/2026-10-27-past-dev.md`; its `results/*` moved to `results/past-published/`)
- Create: `benchmarks/beam/README.md` (what changed from upstream)
- Create: `benchmarks/beam/memory/__init__.py`, `benchmarks/beam/memory/base.py`

**Interfaces:**
- Produces: the adapter protocol every backend implements:
  ```python
  class MemoryApi(Protocol):
      async def create_project(self, name: str) -> str: ...
      async def register_identities(self, identities: list[dict]) -> None: ...
      async def ingest_batch(self, items: list[dict], idempotency_key: str) -> dict: ...   # {"ingestionId": str}
      async def ingestion_status(self, ingestion_id: str) -> dict: ...                       # {"status", "settled": bool}
      async def recall(self, query: str, as_of: str, identity: str) -> dict: ...             # past.dev page shape
      async def delete_project(self, project: str) -> None: ...
      async def close(self) -> None: ...
  ```

- [ ] **Step 1: Copy the upstream directory; move results; keep `LICENSE` and `NOTICE.md`**
- [ ] **Step 2: Verify** — `cd benchmarks/beam && python -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python verify.py results/past-published/100k && .venv/bin/python verify.py --prompts` → 92.08% and "prompts match"
- [ ] **Step 3: Write `memory/base.py`; move `past_api.py` to `memory/past.py` (add a no-op `delete_project`; the harness never deletes past.dev projects)**
- [ ] **Step 4: Run `verify.py` again**
- [ ] **Step 5: Commit** — `git add benchmarks/beam && git commit -m "bench(beam): vendor past.dev's BEAM runner with the adapter protocol"`

### Task 2: AgentCore built-in adapter (today's memory as a baseline)

**Files:**
- Create: `benchmarks/beam/memory/agentcore.py`
- Test: `benchmarks/beam/tests/test_agentcore_adapter.py` (Stubber)

**Interfaces:**
- Produces: `class AgentCoreApi(MemoryApi)` constructed with `(memory_id: str, region: str, run_tag: str)`: `create_project(name)` → namespace `/beam/<run_tag>/<name>/`; `ingest_batch` → `CreateEvent` per item (`actorId=f"beam-{run_tag}-{name}"`, `sessionId` = BEAM session, `eventTimestamp` = item timestamp, text `f"{author}: {content}"`); `ingestion_status` → `ListMemoryRecords` count unchanged for 300 s → settled; `recall` → `RetrieveMemoryRecords(topK=100)` packed newest-first into 8,000 tokens (`tiktoken` `o200k_base`) in the page shape with `kind="claim"`; `delete_project` → `BatchDeleteMemoryRecords` of the namespace.

- [ ] **Step 1: Write the failing tests** — `test_namespace_per_conversation`, `test_ingest_batch_creates_one_event_per_item_with_timestamp`, `test_ingestion_status_waits_for_stable_count`, `test_recall_page_respects_budget`, `test_delete_project_removes_namespace`
- [ ] **Step 2: Run → FAIL** — `cd benchmarks/beam && .venv/bin/pytest tests/test_agentcore_adapter.py -v`
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "bench(beam): AgentCore built-in adapter"`

### Task 3: Lightbulb adapter

**Files:**
- Create: `benchmarks/beam/memory/lightbulb.py`
- Test: `benchmarks/beam/tests/test_lightbulb_adapter.py` (`respx`)

**Interfaces:**
- Consumes: engine plan Task 6 endpoints: `POST /api/v1/memory/benchmark/projects`, `/benchmark/ingest`, `GET /benchmark/ingest/{id}`, `POST /benchmark/recall {workspace_id, question, as_of, budget_tokens, limit, extraction}`, `DELETE /benchmark/projects/{id}`; header `Authorization: Bearer $LIGHTBULB_ADMIN_TOKEN`.
- Produces: `class LightbulbApi(MemoryApi)` constructed with `(base_url: str, token: str, extraction: str = "full")`.

- [ ] **Step 1: Write the failing tests** — `test_ingest_batch_is_idempotent`, `test_recall_page_respects_budget` (page with `usedEvidenceTokens` 9000 → `BenchmarkError`), `test_recall_passes_as_of_limit_and_extraction`
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement** (retries 1, 2, 4 s on 429/5xx as `PastApi`)
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "bench(beam): Lightbulb adapter over the backend's benchmark endpoints"`

### Task 4: Runner flags, repeated judging, the evidence signal, cost columns

**Files:**
- Modify: `benchmarks/beam/run.py`, `benchmarks/beam/scoring.py`
- Create: `benchmarks/beam/evidence.py`
- Test: `benchmarks/beam/tests/test_runner.py`, `benchmarks/beam/tests/test_evidence.py`

**Interfaces:**
- Produces: flags `--memory {past,agentcore,lightbulb}`, `--extraction {full,none}` (Lightbulb only), `--judge-runs N`, `--answer-model <id>`, `--score {full,evidence}`, `--tag`, `--keep`. With `--score evidence`, no answer or judge call is made; per question `results.jsonl` holds `evidence: {"where": "memory"|"turn"|"nowhere", "goldNumbers": [...]}` and `summary.json` holds the three shares overall and per category, labelled `"proxy": true`. With `--judge-runs N`: `judgeScores` per question, `score` their mean, `judgeSpread` in the summary. `summary.json` also gains `memory`, `answerModel`, `settleSeconds`, `costUsd` (answer+judge tokens × `OPENROUTER_PRICE_IN`/`_OUT` per million, `null` if unset).
  - `evidence.where(page: dict, gold_answers: list[str]) -> tuple[str, list[str]]`: numbers of ≥2 digits from the gold answers; "memory" if any appears in a result whose `kind` is not `turn`/`entry`/`source`, else "turn" if in any content or excerpt, else "nowhere" (the method from `past-dev-how-it-works.md` §2).

- [ ] **Step 1: Write the failing tests** — `test_judge_runs_average_and_spread`, `test_memory_flag_selects_adapter`, `test_evidence_signal_splits_memory_turn_nowhere`, `test_score_evidence_makes_no_llm_calls`
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement** (`build_memory(name, env) -> MemoryApi` in `run.py`)
- [ ] **Step 4: Run tests; `verify.py results/past-published/100k` unchanged**
- [ ] **Step 5: Re-score past.dev's published pages with the evidence signal** — `.venv/bin/python run.py --split 1m --memory past --score evidence --reuse-results results/past-published/1m` → must reproduce the §2 table of `past-dev-how-it-works.md` (knowledge update: 46 memory, 7 turn, 8 nowhere)
- [ ] **Step 6: Commit** — `git commit -m "bench(beam): flags, judge spread, evidence signal, cost"`

### Task 5: Baselines on 100K and the 10M floor

**Files:**
- Create: `benchmarks/beam/results/past-rerun-100k/`, `results/agentcore-100k/`, `results/lightbulb-100k/`, `results/lightbulb-none-10m-c3/`
- Create: `docs/research/2026-10-28-beam-baselines.md`

- [ ] **Step 1: past.dev rerun** — `--split 100k --memory past --judge-runs 3 --tag rerun` (≈30,000 free credits; the founder confirms first)
- [ ] **Step 2: AgentCore built-in** — `--split 100k --memory agentcore --judge-runs 3` against `BenchmarkMemory` in dev
- [ ] **Step 3: Lightbulb, evidence only, then full** — `--split 100k --memory lightbulb --score evidence`, then `--judge-runs 3`
- [ ] **Step 4: The 10M retrieval floor** — `--split 10m --conversations 3 --memory lightbulb --extraction none --answer-model anthropic/claude-haiku-5-5` (~$10)
- [ ] **Step 5: Verify each** — `verify.py results/<dir>`
- [ ] **Step 6: Write the baselines note**: overall and per-ability scores, `judgeSpread`, `settleSeconds`, `costUsd`, the evidence shares, AgentCore extraction throughput, the Task 1 (engine plan) merge-metadata answer; the gap to 92.08 and whether it exceeds the spread
- [ ] **Step 7: Commit** — `git add benchmarks/beam/results docs/research/2026-10-28-beam-baselines.md && git commit -m "bench(beam): 100K baselines and the 10M retrieval floor"`
