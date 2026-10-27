# Memory BEAM Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run BEAM (100K through 10M) on past.dev's own harness with three memory backends behind one adapter interface: past.dev, today's AgentCore built-in, and the Lightbulb engine, with the judge run several times so we know the noise.

**Architecture:** past.dev's MIT harness is vendored unchanged except `past_api.py`, which becomes one of three adapters chosen by `--memory`. The Lightbulb adapter talks HTTP to the backend's benchmark endpoints (plan `2026-10-27-memory-workspace-engine.md`, Task 9), so the harness runs from a laptop against the dev environment. Results keep past.dev's `results.jsonl` and `summary.json` shapes so `verify.py` works on ours.

**Tech Stack:** Python 3.11, httpx, the vendored harness (`run.py`, `dataset.py`, `scoring.py`, `llm.py`, `exabase_prompts.py`, `verify.py`), OpenRouter for the answer and judge model, boto3 for the AgentCore baseline.

**Spec:** `docs/superpowers/specs/2026-10-27-memory-layer-design.md` §6; `docs/research/2026-10-27-past-dev-how-it-works.md` §1 for the page shape.

## Global Constraints

- Branch from clean `origin/main` in a fresh worktree; never stack on another branch (CLAUDE.md).
- The vendored harness keeps its MIT `LICENSE` and `NOTICE.md`; ExaBase's prompts are reproduced unchanged and `verify.py --prompts` must pass (SHA-256 `584b672d…fc43`).
- Answer and judge model: `openai/gpt-5.6-luna` via OpenRouter, reasoning off. Recall: `limit=100`, `maxTokens=8000`. Each question answered once per run.
- Dates: each turn at noon UTC on its BEAM day, one microsecond after the previous turn (the harness's `dataset.py` already does this; adapters must pass `occurredAt`/`eventTimestamp` through unchanged).
- Secrets only from environment variables (`PAST_MANAGEMENT_KEY`, `OPENROUTER_API_KEY`, `LIGHTBULB_ADMIN_TOKEN`, AWS credentials); never in files or logs.
- Results directories: `results/<memory>-<split>[-<tag>]/`; the published baselines from past.dev stay untouched under `results/past-published/`.

## Review Focus

1. A recall page over 8,000 tokens from any adapter must be truncated to the budget before answering, not passed through; otherwise the comparison is unfair. (Task 2, Task 3: `test_recall_page_respects_budget`.)
2. A conversation whose ingest is still being extracted when questions are asked produces a low score that looks like a memory failure. Adapters must block until settled, and the runner must record the settle time. (Task 2: `test_ingestion_status_waits_for_stable_count`.)
3. A run interrupted mid-conversation must resume without re-ingesting or double-counting (past.dev's harness skips scored conversations; adapters must make ingest idempotent). (Task 3: `test_ingest_batch_is_idempotent`.)
4. Three judge passes must be averaged per question and the spread reported, not silently overwritten. (Task 4: `test_judge_runs_average_and_spread`.)
5. The AgentCore baseline must use a fresh namespace per conversation so one conversation's records cannot leak into another's recall. (Task 2: `test_namespace_per_conversation`.)

---

### Task 1: Vendor the harness

**Files:**
- Create: `benchmarks/beam/` (copy of `pastdotdev/benchmarks/beam` at the commit in `docs/research/2026-10-27-past-dev.md`: `run.py`, `dataset.py`, `scoring.py`, `llm.py`, `log.py`, `exabase_prompts.py`, `verify.py`, `requirements.txt`, `NOTICE.md`, `LICENSE`, `results/past-published/{100k,500k,1m,10m}/`)
- Create: `benchmarks/beam/README.md` (what changed from upstream: adapters, `--memory`, `--judge-runs`)
- Create: `benchmarks/beam/memory/__init__.py`

**Interfaces:**
- Produces: the adapter protocol every backend implements, in `benchmarks/beam/memory/base.py`:
  ```python
  class MemoryApi(Protocol):
      async def create_project(self, name: str) -> str: ...            # returns a project id/slug
      async def register_identities(self, identities: list[dict]) -> None: ...
      async def ingest_batch(self, items: list[dict], idempotency_key: str) -> dict: ...  # returns {"ingestionId": str}
      async def ingestion_status(self, ingestion_id: str) -> dict: ...  # {"status": "completed"|"processing", "settled": bool}
      async def recall(self, query: str, as_of: str, identity: str) -> dict: ...  # past.dev page shape: {"asOf","usedEvidenceTokens","results":[...]}
      async def close(self) -> None: ...
  ```
  Items are the harness's existing shape: `{"id","content","label","timestamp","identity","metadata":{"role","author","speaker"}}`.

- [ ] **Step 1: Copy the upstream directory, move its `results/*` to `results/past-published/`, keep `LICENSE` and `NOTICE.md`**
- [ ] **Step 2: Verify the published numbers and prompts still check**

Run: `cd benchmarks/beam && python -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python verify.py results/past-published/100k && .venv/bin/python verify.py --prompts`
Expected: both print the published score (92.08%) and "prompts match".

- [ ] **Step 3: Write `memory/base.py` with the `MemoryApi` protocol above and move `past_api.py` to `memory/past.py` (class `PastApi` unchanged)**
- [ ] **Step 4: Run `verify.py` again to be sure nothing in scoring moved**
- [ ] **Step 5: Commit**

```bash
git add benchmarks/beam && git commit -m "bench(beam): vendor past.dev's BEAM harness with the adapter protocol"
```

### Task 2: AgentCore built-in adapter (today's memory as a baseline)

**Files:**
- Create: `benchmarks/beam/memory/agentcore.py`
- Test: `benchmarks/beam/tests/test_agentcore_adapter.py`

**Interfaces:**
- Consumes: `MemoryApi` (Task 1).
- Produces: `class AgentCoreApi(MemoryApi)` constructed with `(memory_id: str, region: str, run_tag: str)`. `create_project(name)` returns the namespace `/beam/<run_tag>/<name>/`; `ingest_batch` calls `CreateEvent` once per item with `actorId=f"beam-{run_tag}-{conversation}"`, `sessionId=<BEAM session id>`, `eventTimestamp=item["timestamp"]`, text `f"{author}: {content}"`; `ingestion_status` polls `ListMemoryRecords` for the namespace until the count is unchanged for 300 s, then returns `settled=True`; `recall` calls `RetrieveMemoryRecords(topK=100)` and packs results newest-first into 8,000 tokens (token count via `tiktoken` `o200k_base`) in past.dev's page shape with `kind="claim"`.

- [ ] **Step 1: Write the failing tests (boto3 client mocked with `botocore.stub.Stubber`)**

```python
def test_namespace_per_conversation():
    api = AgentCoreApi("mem-x", "us-east-1", "t1")
    assert asyncio.run(api.create_project("conv-3")) == "/beam/t1/conv-3/"

def test_ingest_batch_creates_one_event_per_item_with_timestamp(stubbed_client): ...
    # asserts CreateEvent called len(items) times, eventTimestamp == item timestamp, text startswith f"{author}: "

def test_ingestion_status_waits_for_stable_count(stubbed_client, monkeypatch): ...
    # counts 3, 5, 5, 5 across polls with a patched clock -> settled only after 300 s of 5

def test_recall_page_respects_budget(stubbed_client): ...
    # 100 records of 200 tokens each -> usedEvidenceTokens <= 8000 and len(results) < 100
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd benchmarks/beam && .venv/bin/pytest tests/test_agentcore_adapter.py -v`
Expected: FAIL with `ImportError: AgentCoreApi`

- [ ] **Step 3: Implement `AgentCoreApi` in `memory/agentcore.py`**

Pack order: newest `occurredAt` first; stop when the next record would exceed the budget.

- [ ] **Step 4: Run the tests**

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add benchmarks/beam/memory/agentcore.py benchmarks/beam/tests/test_agentcore_adapter.py && git commit -m "bench(beam): AgentCore built-in adapter"
```

### Task 3: Lightbulb adapter

**Files:**
- Create: `benchmarks/beam/memory/lightbulb.py`
- Test: `benchmarks/beam/tests/test_lightbulb_adapter.py`

**Interfaces:**
- Consumes: `MemoryApi`; the backend's benchmark endpoints from the engine plan Task 9: `POST /api/v1/memory/benchmark/projects {name}` → `{workspace_id}`; `POST /api/v1/memory/benchmark/ingest {workspace_id, idempotency_key, items}` → `{ingestion_id}`; `GET /api/v1/memory/benchmark/ingest/{ingestion_id}` → `{status, settled}`; `POST /api/v1/memory/benchmark/recall {workspace_id, question, as_of, budget_tokens, limit}` → page. Header `Authorization: Bearer $LIGHTBULB_ADMIN_TOKEN`.
- Produces: `class LightbulbApi(MemoryApi)` constructed with `(base_url: str, token: str)`.

- [ ] **Step 1: Write the failing tests (`respx` to mock httpx)**

```python
def test_ingest_batch_is_idempotent(respx_mock):
    # two calls with the same idempotency_key -> the mock sees identical bodies and the adapter returns the same ingestion_id
def test_recall_page_respects_budget(respx_mock):
    # response with usedEvidenceTokens 9000 -> adapter raises BenchmarkError("page over budget")
def test_recall_passes_as_of_and_limit(respx_mock):
    # body has as_of == given, limit == 100, budget_tokens == 8000
```

- [ ] **Step 2: Run to see them fail**

Run: `cd benchmarks/beam && .venv/bin/pytest tests/test_lightbulb_adapter.py -v`
Expected: FAIL with `ImportError: LightbulbApi`

- [ ] **Step 3: Implement `LightbulbApi` in `memory/lightbulb.py`** (retries as `PastApi`: 1, 2, 4 s on 429/5xx)
- [ ] **Step 4: Run the tests → PASS**
- [ ] **Step 5: Commit**

```bash
git add benchmarks/beam/memory/lightbulb.py benchmarks/beam/tests/test_lightbulb_adapter.py && git commit -m "bench(beam): Lightbulb adapter over the backend's benchmark endpoints"
```

### Task 4: Runner flags, repeated judging, cost columns

**Files:**
- Modify: `benchmarks/beam/run.py` (argument parsing; the per-question answer/judge loop; `summary.json`)
- Modify: `benchmarks/beam/scoring.py` (spread)
- Test: `benchmarks/beam/tests/test_runner.py`

**Interfaces:**
- Produces: `--memory {past,agentcore,lightbulb}` (default `past`), `--judge-runs N` (default 1), `--tag <str>`. Per question, `results.jsonl` gains `judgeScores: [float]` (one per judge run) and `score` becomes their mean; `summary.json` gains `judgeRuns`, `judgeSpread` (mean per-question max−min), `memory`, `settleSeconds` (sum over conversations), `costUsd` (answer+judge tokens × OpenRouter price from env `OPENROUTER_PRICE_IN`/`_OUT` per million; `null` if unset).

- [ ] **Step 1: Write the failing test**

```python
def test_judge_runs_average_and_spread(tmp_path, fake_llm):
    # fake judge returns 1.0, 0.5, 1.0 for one question -> score 0.8333, judgeScores len 3, summary judgeSpread 0.5
def test_memory_flag_selects_adapter():
    assert build_memory("agentcore", env).__class__.__name__ == "AgentCoreApi"
```

- [ ] **Step 2: Run → FAIL (unknown argument `--judge-runs`)**
- [ ] **Step 3: Implement: `build_memory(name: str, env: Mapping[str, str]) -> MemoryApi` in `run.py`; the judge loop; the summary fields**
- [ ] **Step 4: Run tests and `verify.py results/past-published/100k` → PASS, score unchanged**
- [ ] **Step 5: Commit**

```bash
git add benchmarks/beam/run.py benchmarks/beam/scoring.py benchmarks/beam/tests/test_runner.py && git commit -m "bench(beam): --memory, --judge-runs, spread and cost in the summary"
```

### Task 5: Baselines on 100K

**Files:**
- Create: `benchmarks/beam/results/past-rerun-100k/`, `results/agentcore-100k/`, `results/lightbulb-100k/` (each with `summary.json`, `results.jsonl`)
- Create: `docs/research/2026-10-28-beam-baselines.md`

- [ ] **Step 1: past.dev rerun** — `PAST_MANAGEMENT_KEY=… .venv/bin/python run.py --split 100k --memory past --judge-runs 3 --tag rerun` (≈30,000 credits of the ~149,900 free; the user confirms before the run)
- [ ] **Step 2: AgentCore built-in** — `.venv/bin/python run.py --split 100k --memory agentcore --judge-runs 3` against a benchmark memory resource (engine plan Task 8) in dev
- [ ] **Step 3: Lightbulb** — `.venv/bin/python run.py --split 100k --memory lightbulb --judge-runs 3` once engine plan Tasks 1–9 are deployed to dev
- [ ] **Step 4: Verify each** — `verify.py results/<dir>` prints the score
- [ ] **Step 5: Write the baselines note**: table of overall and per-ability scores, `judgeSpread`, `settleSeconds`, `costUsd` for each arm beside past.dev's published 92.08; the gap and whether it exceeds the spread
- [ ] **Step 6: Commit results and note**

```bash
git add benchmarks/beam/results docs/research/2026-10-28-beam-baselines.md && git commit -m "bench(beam): 100K baselines for past.dev rerun, AgentCore built-in and Lightbulb"
```
