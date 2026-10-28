# BEAM Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One BEAM runner, forked from past.dev's MIT harness, with two memory backends — the free CLI engine and the workspace engine — producing results in past.dev's exact format and scoring so the numbers compare directly.

**Architecture:** `benchmarks/beam/` in the monorepo keeps the fork's dataset, prompts, answerer, judge, scoring and verify code unchanged; one adapter interface replaces `past_api.py`; the workspace adapter calls two dev-only backend routes; the free adapter drives a CLI daemon (v2 build) over its socket with a Haiku note-taker playing the harness.

**Tech Stack:** Python 3.11+, httpx, datasets, huggingface_hub, scipy (the fork's); OpenRouter for `openai/gpt-5.6-luna` (answer + judge, as past.dev) and `anthropic/claude-haiku-4.5` (note-taker, loops); the CLI daemon in an OrbStack Linux container.

**Spec:** `docs/superpowers/specs/2026-10-28-memory-layer-v6-amendment.md` §4 over v5 §8. Facts: `docs/research/2026-10-28-beam-runner-facts.md` (BR), `2026-10-27-past-dev-how-it-works.md` (PH).

## Global Constraints

- Licence: the harness is MIT (Past Corp) and is copied with its notice; `exabase_prompts.py` is NOT MIT and is not committed — the runner fetches it from upstream at first use and checks the SHA-256 `verify.py --prompts` expects (BR §1, §4). The dataset and published per-question results are CC BY-SA 4.0: anything we publish carries that licence.
- Same conditions as past.dev for published numbers: answer and judge `openai/gpt-5.6-luna` (reasoning none), ExaBase prompts, 100 results / 8,000 tokens per recall, questions asked at the last message (knowledge_update at the latest cited update turn), timestamps at noon UTC of the session day plus the turn ordinal in microseconds, both roles ingested, past.dev's scoring (`(tau_b+1)/2`, rubric rounded to 0/0.5/1) (BR §1–3).
- Spend guard: every run takes `--budget-usd`; the runner counts tokens per call at the prices in BR §5 and stops at the cap. Keys only from env files under the session scratchpad (`openrouter.env`), never printed, never committed.
- Nothing leaves the user's machine except the model calls above and the dev backend; the free backend's daemon runs in a container with no network except the OpenRouter calls made by the runner process itself.
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. No competitor named in anything published except in the results table where their published numbers are cited with a source.

## Review Focus

1. A recall that returns fewer than 100 results or over 8,000 tokens must be trimmed exactly as past.dev's: test in Task 2.
2. `as_of` must reach both backends and be echoed as `asOf` in the recall JSON, else `run.py`'s moment check fails: tests in Tasks 3 and 4.
3. The note-taker must never see the questions or the gold answers: the free adapter ingests sessions only; test in Task 4.
4. A run killed midway must resume from its results file without re-paying: test in Task 5.
5. The budget cap must stop before the next paid call, not after: test in Task 5.

---

### Task 1: Fork the harness

**Files:**
- Create: `benchmarks/beam/` (the fork's `run.py`, `dataset.py`, `llm.py`, `scoring.py`, `verify.py`, `prompts/` loader, `README.md`, `LICENSE-past` with the MIT notice), `benchmarks/beam/pyproject.toml`
- Test: `benchmarks/beam/tests/test_verify.py`

- [ ] **Step 1:** Copy the MIT files from `github.com/pastdotdev/benchmarks` `beam/` at a pinned commit (record it in README). Do not copy `exabase_prompts.py` or `results/`.
- [ ] **Step 2:** `prompts.py`: `ensure_exabase_prompts(cache_dir)` downloads ExaBase's upstream file to the cache and verifies the SHA-256 that `verify.py` expects; fails loudly on mismatch.
- [ ] **Step 3:** Test: `test_verify_prompts_rejects_a_changed_file` (tamper one byte → error) and `test_fork_pins_a_commit` (README names the commit).
- [ ] **Step 4:** Commit `beam: fork past.dev's MIT runner; ExaBase prompts fetched and verified, never committed`.

### Task 2: The adapter interface

**Files:**
- Create: `benchmarks/beam/adapters/base.py`, `benchmarks/beam/adapters/__init__.py`
- Modify: `run.py` (import the adapter by `--backend`)
- Test: `benchmarks/beam/tests/test_adapter_contract.py`

**Interfaces:**
- Produces: `class MemoryBackend(Protocol)`: `create_project(name) -> str`; `register_identities(project, speakers) -> None`; `ingest_batch(project, turns: list[Turn]) -> None` where `Turn(session_id, ordinal, speaker, role, text, at: datetime)`; `ingestion_status(project) -> "done" | "pending"`; `recall(project, query, as_of: datetime | None, identity) -> RecallPage` where `RecallPage(results: list[dict], as_of: datetime | None, tokens: int)`; the runner trims to 100 results and 8,000 tokens with the fork's tokenizer and serialises exactly as `past_api.py` did, including `asOf`.
- A `FakeBackend` for tests that stores turns and answers recall with the raw turns.

- [ ] **Step 1:** Tests: `test_recall_page_is_trimmed_to_100_results_and_8000_tokens`, `test_as_of_is_echoed_as_asOf`, `test_run_py_uses_the_named_backend`.
- [ ] **Step 2–4:** Implement, pass, commit `beam: one adapter interface in place of past_api`.

### Task 3: The workspace backend (dev-only routes + adapter)

**Files:**
- Create: `apps/backend/routers/internal_bench.py`, `benchmarks/beam/adapters/workspace.py`
- Modify: `apps/backend/main.py` (mount under `/api/v1/internal/bench`, only when `settings.ENVIRONMENT != "prod"`), `apps/backend/core/services/memory_sync.py` or `workspace_memory.py` only if a timestamped turn write needs a parameter
- Test: `apps/backend/tests/unit/routers/test_internal_bench.py`, `benchmarks/beam/tests/test_workspace_adapter.py`

**Interfaces:**
- `POST /internal/bench/projects {name}` → `{owner_id}` (a synthetic owner namespace `bench-<ulid>`); `POST /internal/bench/ingest {owner_id, turns[{session_id, ordinal, speaker, role, text, at}]}` → writes each turn as a record into `/turns/<o>/` with `x-amz-agentcore-memory-createdAt` unavailable to set, so `at` goes into a NUMBER metadata key `at` (epoch seconds) that recall's as-of arm filters on for bench owners, AND as an event (`CreateEvent` with `eventTimestamp = at`) so the semantic strategy extracts facts with `validFrom` = at; `GET /internal/bench/status/{owner_id}` → `pending` while extraction jobs are younger than 60 s; `POST /internal/bench/recall {owner_id, question, as_of, budget_tokens: 8000, limit: 100}` → the five-stage recall page. All behind `_require_hook_token`. `DELETE /internal/bench/projects/{owner_id}` deletes the namespaces (records deleted the same day, per the spec).
- The adapter maps the interface to these routes.

- [ ] **Step 1:** Tests (backend): routes 404 in prod; ingest writes turns with `at` and events with `eventTimestamp`; recall passes `as_of`; delete wipes the namespaces. Adapter tests against a fake HTTP server.
- [ ] **Step 2–4:** Implement, pass, commit `bench: dev-only ingest and recall routes; the workspace adapter`.

### Task 4: The free CLI backend (adapter + daemon ops)

**Files:**
- Create: `benchmarks/beam/adapters/free_cli.py`, `benchmarks/beam/notetaker.py`
- Modify (CLI repo, branch feat/memory-v2, after its Task 5): `src/daemon/ipc.rs` — debug-build-only ops `bench.ingest_turns {session, turns[{seq, role, text, at}]}` (records turns with the given `at`, embeds them) and `memory.remember` accepting `when` from a bench caller; `memory.recall {question, as_of?, limit?, budget_chars?}` (as_of filters entries by `when` and turns by `at`; the page JSON echoes `as_of`).
- Test: `benchmarks/beam/tests/test_free_adapter.py`; CLI tests in `tests/memory_tools.rs` for as_of and the bench op.

**Interfaces:**
- `notetaker.py`: for each session, one Haiku call per 20 turns (prompt in the file, ours, not ExaBase's) that returns `remember` ops `{subject, fact, kind, replaces?}` grounded on the turns it just read, with `when` = the last turn's `at`; the adapter sends them to the daemon; it never receives questions or answers.
- `free_cli.py`: talks to the daemon's Unix socket (the IPC JSON lines the CLI uses; read `src/daemon/ipc.rs` for the framing) inside the container via `docker exec`, or over a forwarded socket; `recall` → `memory.recall` with `as_of`; results = the page's facts plus turn excerpts in the fork's result shape (`kind`, `text`, `validFrom`, `supersededAt` when replaced).

- [ ] **Step 1:** Tests: the note-taker never sees a question (assert on the prompt), `remember` ops carry `when`; as_of recall excludes later entries; a 100-turn session ingests in order.
- [ ] **Step 2–4:** Implement, pass, commit `bench: the free CLI backend; a Haiku note-taker plays the harness`.

### Task 5: Resume, budget, cheap loops, gold-on-page

**Files:**
- Modify: `run.py` (resume from `results.jsonl`; `--budget-usd`; `--answer-model` and `--judge-model` with the published defaults; `--conversations N`), create `benchmarks/beam/goldcheck.py` (is the gold answer's number or phrase on the page: memory / raw turn / nowhere, per PH §2)
- Test: `benchmarks/beam/tests/test_resume_and_budget.py`, `test_goldcheck.py`

- [ ] **Step 1:** Tests: a killed run resumes without repeating paid calls; the cap stops before the next paid call; goldcheck splits a page three ways.
- [ ] **Step 2–4:** Implement, pass, commit.

### Task 6: First runs and the results note

- [ ] **Step 1:** 100K on `workspace` (dev) with Haiku answerer (`--conversations 3` first, then all 20) and on `free` (container with the v2 build); record cost and wall time.
- [ ] **Step 2:** 100K final on both with the published models; judge three times; report mean and spread.
- [ ] **Step 3:** `docs/research/2026-10-28-beam-results.md`: table per ability per backend beside past.dev's published 100K row (with source), conditions, cost, runner commit, what was not comparable. Publish nothing public yet (founder).
