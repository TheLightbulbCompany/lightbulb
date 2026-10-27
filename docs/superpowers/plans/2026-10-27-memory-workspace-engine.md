# Memory Workspace Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Workspace memory that extracts on AgentCore, indexes in Postgres on the existing Aurora cluster, and recalls through five stages (expand, four-arm search, fuse, rerank, compose) from one backend function used by room agents, hosted tools, the Memory tab and the BEAM harness.

**Architecture:** AgentCore's semantic strategy, with override instructions and a metadata schema, turns room turns into typed, dated records. A backend sync pulls those records and the raw turns into a `memory` database (pgvector + tsvector). `memory_recall.recall()` runs the five stages in SQL plus two Bedrock calls (embeddings, rerank) and returns a page in past.dev's shape. The Hermes plugin and the hosted MCP tools call the backend; nothing outside the backend and the runtime holds credentials.

**Tech Stack:** Python 3.12 backend (FastAPI, pytest asyncio_mode=auto, uv), asyncpg + pgvector, boto3 (`bedrock-agentcore`, `bedrock-runtime` for embeddings, `bedrock-agent-runtime` for `Rerank`), CDK TypeScript (`database-stack.ts`, `service-stack.ts`, `container-stack.ts`), Hermes plugin in `apps/infra/hermes/plugins/memory/agentcore/`.

**Spec:** `docs/superpowers/specs/2026-10-27-memory-layer-design.md` §5–§8.

## Global Constraints

- Branch from clean `origin/main` in a fresh worktree (CLAUDE.md).
- Database: a new `memory` database on `paperclipDbCluster` (Postgres 16, pgvector). Backend ingress to 5432 from the backend security group only. Credentials from the cluster secret, read the way Paperclip reads it (`paperclip-stack.ts:332`).
- Embeddings: `amazon.titan-embed-text-v2:0` (1024 dims) by default; `cohere.embed-english-v3` behind `MEMORY_EMBED_MODEL`. Rerank: `cohere.rerank-v3-5:0` in `us-east-1` via `bedrock-agent-runtime` `Rerank` (Amazon Rerank 1.0 is not offered in us-east-1). ≤100 documents per rerank call.
- Recall defaults: `budget_tokens=8000`, `limit=100`, RRF `k=60`, expansion only when the question has more than one part (the expansion model returns 1 sub-question → skip).
- Tokens counted with `tiktoken` `o200k_base` (same as the harness).
- Page shape (what every consumer gets): `{"asOf": iso, "usedEvidenceTokens": int, "results": [{"rank": int, "kind": "claim|observation|rule|mention|anchor|turn", "content": str, "occurredAt": iso, "status": "current|contested|replaced", "history": [{"value": str, "speaker": str, "occurredAt": iso}], "sources": [{"sourceId": str, "occurredAt": iso, "speaker": str, "excerpt": str}]}]}`.
- Tests needing Postgres use `MEMORY_TEST_DATABASE_URL` (local: `docker run -d -p 5433:5432 -e POSTGRES_PASSWORD=test pgvector/pgvector:pg16`, URL `postgresql://postgres:test@localhost:5433/postgres`) and are marked `@pytest.mark.db`; they skip when the variable is unset. Bedrock and AgentCore are stubbed with `botocore.stub.Stubber` in unit tests.
- Secrets: scrub before storing turns with the existing `redact_for_egress` in the plugin (already applied to events); the backend stores what the plugin sent.
- No new public API without auth: workspace endpoints use the existing member auth dependency in `routers/memory.py`; benchmark endpoints require the admin token and `MEMORY_BENCHMARK_ENABLED=true`, which is never set in prod.

## Review Focus

1. A question with no matching memory must return an empty page quickly, not an exception or a page of unrelated turns above a floor (set the floor: RRF score of the 100th candidate when fewer than 5 candidates exceed 0.0 after rerank → return the top 5 only). (Task 6: `test_recall_empty_workspace_returns_empty_page`, `test_recall_unrelated_question_returns_few_results`.)
2. Two workspaces must never see each other's rows: every query is scoped by `workspace_id` and the tests assert cross-workspace isolation. (Task 4: `test_workspace_isolation`; Task 6: `test_recall_never_crosses_workspace`.)
3. A replaced value must never outrank the current one for the same entity+attribute; contested records must come first. (Task 6: `test_compose_orders_contested_then_current`.)
4. Rerank failure (throttle, region) must degrade to RRF order, logged, not fail the recall. (Task 3: `test_rerank_failure_returns_none`; Task 6: `test_recall_without_rerank_uses_rrf_order`.)
5. A wipe must remove every row of the workspace or member across all four tables and the AgentCore namespaces, and a later recall must return nothing. (Task 4: `test_wipe_workspace_removes_all_tables`; Task 10.)

---

### Task 1: Infrastructure: database, ingress, model permissions

**Files:**
- Modify: `apps/infra/lib/stacks/database-stack.ts` (export the cluster's security group and secret for the backend)
- Modify: `apps/infra/lib/stacks/service-stack.ts` (ingress 5432 from the backend SG; env `MEMORY_DATABASE_SECRET_ARN`, `MEMORY_DATABASE_HOST`, `MEMORY_EMBED_MODEL`; task role: `bedrock:InvokeModel` on the two embedding model ARNs and `bedrock:Rerank` on `cohere.rerank-v3-5:0`; secret read)
- Test: `apps/infra/test/service-stack.test.ts` (existing CDK assertion tests pattern)

**Interfaces:**
- Produces: backend env vars named above; `MEMORY_DATABASE_NAME=memory`.

- [ ] **Step 1: Write the failing CDK assertions**

```ts
test("backend may reach the Paperclip cluster on 5432", () => { template.hasResourceProperties("AWS::EC2::SecurityGroupIngress", { FromPort: 5432, ToPort: 5432 }); });
test("backend task role can embed and rerank", () => { template.hasResourceProperties("AWS::IAM::Policy", Match.objectLike({ PolicyDocument: Match.objectLike({ Statement: Match.arrayWith([Match.objectLike({ Action: Match.arrayWith(["bedrock:Rerank"]) })]) }) })); });
```

- [ ] **Step 2: Run → FAIL**

Run: `cd apps/infra && npx jest test/service-stack.test.ts -t "5432|rerank"`

- [ ] **Step 3: Implement the stack changes**
- [ ] **Step 4: Run → PASS; `npx cdk synth` clean**
- [ ] **Step 5: Commit**

```bash
git add apps/infra && git commit -m "infra(memory): backend reaches the Aurora cluster; embed and rerank permissions"
```

### Task 2: Schema and migration

**Files:**
- Create: `apps/backend/core/services/memory_schema.sql`
- Create: `apps/backend/scripts/memory_migrate.py`
- Create: `apps/backend/core/services/memory_db.py`
- Test: `apps/backend/tests/unit/services/test_memory_schema.py`

**Interfaces:**
- Produces: `memory_db.pool() -> asyncpg.Pool` (lazy, from `MEMORY_DATABASE_*` or `MEMORY_TEST_DATABASE_URL`); `memory_migrate.migrate(dsn: str) -> None` (idempotent: `CREATE EXTENSION IF NOT EXISTS vector`, `CREATE TABLE IF NOT EXISTS …`).
- Tables (all with `workspace_id text not null` first and an index on it):
  - `records(id text pk, workspace_id, scope text check (scope in ('team','private')), member_id text, kind text, entity text, attribute text, value text, content text not null, speaker text, status text check (status in ('current','contested','replaced')), valid_from timestamptz, valid_to timestamptz, replaces text[], source_ids text[], agentcore_record_id text, embedding vector(1024), tsv tsvector generated always as (to_tsvector('english', content)) stored, updated_at timestamptz default now())`
  - `turns(id text pk, workspace_id, room_id text, speaker text, role text, content text, occurred_at timestamptz, embedding vector(1024), tsv tsvector generated …)`
  - `links(workspace_id, entity text, record_id text references records(id) on delete cascade, primary key (workspace_id, entity, record_id))`
  - `entries(id text pk, workspace_id, author text, seq bigint, scope text, kind text, subject text, fact text, replaces text[], occurred_at timestamptz, source jsonb, tombstone boolean default false)`
  - `sync_state(workspace_id text pk, records_after timestamptz, events_after timestamptz)`
  - Indexes: HNSW on both `embedding` columns (`vector_cosine_ops`), GIN on both `tsv`, btree `(workspace_id, entity, attribute)` on records, `(workspace_id, occurred_at)` on turns.

- [ ] **Step 1: Write the failing test (`@pytest.mark.db`)**

```python
async def test_migrate_creates_tables_and_is_idempotent(dsn):
    await migrate(dsn); await migrate(dsn)
    names = {r["tablename"] for r in await conn.fetch("select tablename from pg_tables where schemaname='public'")}
    assert {"records","turns","links","entries","sync_state"} <= names
```

- [ ] **Step 2: Run → FAIL**

Run: `cd apps/backend && MEMORY_TEST_DATABASE_URL=… uv run pytest tests/unit/services/test_memory_schema.py -v`

- [ ] **Step 3: Write the SQL, `migrate`, and `pool`**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/memory_schema.sql apps/backend/scripts/memory_migrate.py apps/backend/core/services/memory_db.py apps/backend/tests/unit/services/test_memory_schema.py && git commit -m "feat(memory): Postgres schema and migration"
```

### Task 3: Embeddings and rerank

**Files:**
- Create: `apps/backend/core/services/memory_embed.py`
- Test: `apps/backend/tests/unit/services/test_memory_embed.py`

**Interfaces:**
- Produces: `async def embed(texts: list[str]) -> list[list[float]]` (batches of 32; Titan or Cohere by `MEMORY_EMBED_MODEL`; truncates each text to 8,000 characters); `async def rerank(query: str, documents: list[str], top_n: int) -> list[tuple[int, float]] | None` (index into `documents`, score; `None` on any `ClientError`, logged at warning; ≤100 documents, extra are dropped from the tail before the call).

- [ ] **Step 1: Write the failing tests (Stubber)**

```python
async def test_embed_batches_of_32(stub): ...   # 70 texts -> 3 InvokeModel calls
async def test_rerank_returns_indices_sorted_by_score(stub): ...
async def test_rerank_failure_returns_none(stub): ...   # ThrottlingException -> None
async def test_rerank_caps_documents_at_100(stub): ...
```

- [ ] **Step 2: Run → FAIL** (`uv run pytest tests/unit/services/test_memory_embed.py -v`)
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "feat(memory): Bedrock embeddings and rerank"`

### Task 4: Index writes

**Files:**
- Create: `apps/backend/core/services/memory_index.py`
- Test: `apps/backend/tests/unit/services/test_memory_index.py` (`@pytest.mark.db`, embeddings patched to a deterministic hash vector)

**Interfaces:**
- Produces, all taking `workspace_id: str` first:
  - `async def upsert_records(workspace_id, records: list[Record]) -> int` where `Record` is a dataclass with the `records` columns minus embedding/tsv; embeds `content`; refreshes `links` from `entity`.
  - `async def upsert_turns(workspace_id, turns: list[Turn]) -> int`
  - `async def delete_record(workspace_id, record_id: str) -> bool`
  - `async def wipe_workspace(workspace_id) -> dict[str, int]` (rows deleted per table)
  - `async def wipe_member(workspace_id, member_id: str) -> dict[str, int]` (records with `scope='private' and member_id`, entries with `author=member_id` tombstoned)

- [ ] **Step 1: Write the failing tests**

```python
async def test_upsert_records_is_idempotent(db): ...          # same id twice -> one row, updated content
async def test_links_follow_entity(db): ...                   # record with entity "shop-api" -> one links row
async def test_workspace_isolation(db): ...                   # rows for ws A invisible when querying ws B
async def test_wipe_workspace_removes_all_tables(db): ...     # counts >0 before, all 0 after
```

- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "feat(memory): index writes, links, wipes"`

### Task 5: Pure stages: expansion parsing, RRF, composition

**Files:**
- Create: `apps/backend/core/services/memory_stages.py`
- Test: `apps/backend/tests/unit/services/test_memory_stages.py` (no DB, no AWS)

**Interfaces:**
- Produces:
  - `def rrf(rankings: list[list[str]], k: int = 60) -> list[tuple[str, float]]` (ids sorted by fused score desc)
  - `def compose(candidates: list[Candidate], budget_tokens: int, as_of: datetime) -> Page` where `Candidate` carries the record/turn fields plus `fused: float`; order: `status == "contested"` first, then `current` claims/observations/rules/anchors by `fused`, then turns by `fused`, then `replaced` as `history` attached to their current record (not as separate results); stops at the budget; `usedEvidenceTokens` counted on the serialised page.
  - `def parse_subquestions(model_text: str, original: str) -> list[str]` (one per line; if ≤1 non-empty line → `[original]`)
  - `def time_window(question: str, as_of: datetime) -> tuple[datetime | None, datetime | None]` (month names, "last week", "in 2024", ISO dates; `dateparser` is already a transitive dependency? if not, `python-dateutil` only; unknown → `(None, None)`)
  - `EXPANSION_PROMPT: str` (asks for one sub-question per line, at most 5, or the question itself if it has one part)

- [ ] **Step 1: Write the failing tests**

```python
def test_rrf_k60_matches_hand_computation(): ...       # two rankings of 3 ids -> scores 1/61+1/62 etc.
def test_compose_orders_contested_then_current(): ...   # contested record first; replaced value appears only in history
def test_compose_stops_at_budget(): ...                 # 50 candidates × 300 tokens, budget 8000 -> usedEvidenceTokens <= 8000
def test_parse_subquestions_single_line_returns_original(): ...
def test_time_window_month_and_year(): ...              # "in March 2024" -> (2024-03-01, 2024-03-31 23:59:59)
```

- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "feat(memory): RRF, composition, expansion parsing, time windows"`

### Task 6: Recall

**Files:**
- Create: `apps/backend/core/services/memory_recall.py`
- Test: `apps/backend/tests/unit/services/test_memory_recall.py` (`@pytest.mark.db`; Bedrock stubbed; expansion model stubbed)

**Interfaces:**
- Consumes: Tasks 3, 4, 5.
- Produces: `async def recall(workspace_id: str, question: str, *, as_of: datetime | None = None, budget_tokens: int = 8000, limit: int = 100, scopes: tuple[str, ...] = ("team",), member_id: str | None = None, rerank: bool = True, expand: bool = True) -> Page`.
  Stages: (1) `expand`: one Haiku call with `EXPANSION_PROMPT` (through the existing Bedrock inference path the backend uses) → `parse_subquestions`; (2) per sub-question, four arms, each returning up to `limit` ids: cosine over `records` and `turns` (one query each, `<=>`), `ts_rank_cd` over both, `links` lookup by entities named in the sub-question (entities = distinct `entity` values whose name appears in the text, case-insensitive), time window filter applied as a fifth ranking (records/turns inside the window ordered by closeness to `as_of`); all arms scoped by `workspace_id`, `scope in scopes` (private rows only when `member_id` matches), and `valid_from <= as_of and (valid_to is null or valid_to > as_of)` when `as_of` is given; (3) `rrf` over all arms of all sub-questions; (4) `rerank` on the top 100 by `content` (fallback: RRF order when `None`); (5) `compose`. Also records a `recall_log` row? No: YAGNI; log at info with stage timings (expansionMs, searchMs, fuseMs, rerankMs, composeMs).

- [ ] **Step 1: Write the failing tests**

```python
async def test_recall_empty_workspace_returns_empty_page(db): ...
async def test_recall_never_crosses_workspace(db): ...
async def test_recall_as_of_excludes_later_facts(db): ...            # record valid_from after as_of is absent
async def test_recall_prefers_current_over_replaced(db): ...         # Postgres 14 (replaced) only in history of the 16 record
async def test_recall_without_rerank_uses_rrf_order(db, stub_rerank_none): ...
async def test_recall_multi_part_question_expands(db, stub_expansion): ...   # expansion returns 2 lines -> both entities' records on the page
async def test_recall_unrelated_question_returns_few_results(db): ...
```

- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement `recall`** (SQL in the module; one `asyncpg` connection per call; arms run with `asyncio.gather`)
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "feat(memory): five-stage recall"`

### Task 7: Sync from AgentCore into the index

**Files:**
- Create: `apps/backend/core/services/memory_sync.py`
- Modify: `apps/backend/core/services/workspace_memory.py` (export `_client()` and namespace helpers; no behaviour change)
- Test: `apps/backend/tests/unit/services/test_memory_sync.py` (Stubber for `bedrock-agentcore`; `@pytest.mark.db`)

**Interfaces:**
- Consumes: Task 4.
- Produces: `async def sync_workspace(workspace_id: str, *, owner_id: str, member_ids: list[str]) -> dict` → `{"records": n, "turns": n}`. Reads `sync_state`; `ListMemoryRecords` on the team namespace (`/owners/<owner>/`) with filter `x-amz-agentcore-memory-updatedAt AFTER records_after` (paged), maps each record to a `Record` (`kind`, `entity`, `attribute`, `status`, `valid_from`, `valid_to` from metadata; `content` from text; `speaker` parsed from the leading `Name:` prefix; `source_ids` from metadata `sources` if present); `ListEvents` for the owner actor after `events_after` → `Turn`s (`redact` already applied upstream); writes both; advances `sync_state`. Private namespaces are not synced (DMs stay in AgentCore, spec §5). Also `async def sync_if_stale(workspace_id, …, max_age_s: int = 60) -> None` used by recall.

- [ ] **Step 1: Write the failing tests**

```python
async def test_sync_maps_metadata_to_record_columns(db, stub): ...   # one record with kind/entity/attribute/status/validFrom -> row columns set
async def test_sync_advances_watermarks(db, stub): ...
async def test_sync_skips_private_namespaces(db, stub): ...
async def test_sync_if_stale_noop_within_max_age(db, stub): ...
```

- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "feat(memory): sync AgentCore records and events into Postgres"`

### Task 8: AgentCore strategy overrides and metadata schema

**Files:**
- Modify: `apps/infra/lib/stacks/container-stack.ts:236-249` (the `CfnMemory`): add `indexedKeys` (`kind`, `entity`, `attribute`, `status` STRING; `validFrom`, `validTo` NUMBER) and, on `workspace_facts`, `memoryRecordSchema.metadataSchema` with definitions and `allowedValues` (`kind`: claim, observation, rule, mention, anchor; `status`: current, contested) and the override configuration (`extraction` and `consolidation` instructions from the two prompt files, model `MEMORY_EXTRACT_MODEL` default a Haiku-class Bedrock id), plus a second `CfnMemory` `BenchmarkMemory` with the same strategy in dev only
- Create: `apps/infra/prompts/memory-extraction.md`, `apps/infra/prompts/memory-consolidation.md` (ours; they append to AWS's defaults per the override docs; the consolidation text carries the one-record-per-slot, current-then-history and contested rules from spec §5)
- Create: `apps/infra/scripts/memory-strategy-apply.ts` (fallback: applies the same via `UpdateMemory` through the SDK if `CfnMemory` rejects any of the fields; the test below decides which path)
- Test: `apps/infra/test/container-stack.test.ts`

**Interfaces:**
- Produces: metadata keys exactly as Task 7 reads them.

- [ ] **Step 1: Write the failing CDK assertion**

```ts
test("workspace memory declares the six indexed keys", () => { template.hasResourceProperties("AWS::BedrockAgentCore::Memory", Match.objectLike({ IndexedKeys: Match.arrayWith([Match.objectLike({ Key: "validFrom", Type: "NUMBER" })]) })); });
```

- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement; if `cdk synth` rejects override or schema properties, keep `indexedKeys` in CDK and put the rest in `memory-strategy-apply.ts`, run from the deploy workflow after the stack**
- [ ] **Step 4: Run tests; `cdk synth`; deploy to dev; `aws bedrock-agentcore-control get-memory` shows the strategy with overrides**
- [ ] **Step 5: Probe the open question** — send the two probe-2 contradiction cases and the three rephrased updates as events to the dev memory; after extraction, `list-memory-records` shows: one record per slot with history in the text, `status=contested` on the Ana/Ben case; record the answer in `docs/research/2026-10-28-beam-baselines.md` ("consolidation can/cannot set metadata on the rewritten record"). If it cannot, open the fallback (self-managed strategy) as a follow-up plan; this plan continues either way.
- [ ] **Step 6: Commit** — `git commit -m "infra(memory): extraction and consolidation overrides, metadata schema, benchmark memory"`

### Task 9: Router: recall, remember, benchmark endpoints

**Files:**
- Modify: `apps/backend/routers/memory.py`
- Modify: `apps/backend/core/services/network_tools.json` (add `recall` and `remember` tool specs; the same file is mirrored into the CLI by the log plan)
- Modify: `apps/backend/core/services/hosted_mcp.py` (map the two tools to the endpoints)
- Test: `apps/backend/tests/unit/routers/test_memory_recall_routes.py`

**Interfaces:**
- Consumes: Task 6 `recall`, Task 7 `sync_if_stale`, Task 4 writes.
- Produces:
  - `POST /api/v1/memory/recall {question, as_of?, budget_tokens?=8000, limit?=100}` → page; member auth; `scopes=("team","private")` with `member_id` = caller; calls `sync_if_stale` first.
  - `POST /api/v1/memory/remember {subject, fact, kind, scope="me"|"team", replaces?: [id]}` → `{"queued": bool, "neighbours": [...]}`: returns the current records on the subject (from `records` + `entries`) and forwards the write to the cloud member's `memory.remember` op through `network_member` (queued when parked; `queued=true`).
  - Benchmark, gated by admin token and `MEMORY_BENCHMARK_ENABLED`: `POST /benchmark/projects {name}` → `{workspace_id}` (`beam-<name>`); `POST /benchmark/ingest {workspace_id, idempotency_key, items}` → writes `CreateEvent` per item into `BenchmarkMemory` under actor `workspace_id`, upserts `turns`, returns `{ingestion_id}` (= idempotency key; a repeated key returns the same id without re-ingesting, tracked in `sync_state`); `GET /benchmark/ingest/{id}` → polls record count stable for 300 s, then runs `sync_workspace`, returns `{status:"completed", settled:true}`; `DELETE /benchmark/projects/{workspace_id}` → `wipe_workspace` + AgentCore namespace delete.
  - Tool specs: `recall(question: str) -> page` ("Call when a task starts or you need something a teammate may know"); `remember(subject, fact, kind, scope?, replaces?)` ("Returns what is already known on the subject; call again with `replaces` if yours is a change").

- [ ] **Step 1: Write the failing route tests (TestClient; services patched)**

```python
def test_recall_requires_member(client): ...                 # 401 without auth
def test_recall_calls_sync_then_recall(client, patched): ...
def test_remember_returns_neighbours_and_queues(client, patched): ...
def test_benchmark_routes_404_when_disabled(client, monkeypatch): ...
def test_benchmark_ingest_idempotent(client, patched): ...
```

- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "feat(memory): recall and remember endpoints, benchmark endpoints, tool specs"`

### Task 10: Hermes plugin and wipes

**Files:**
- Modify: `apps/infra/hermes/plugins/memory/agentcore/__init__.py` (`prefetch`, `_search`, `handle_tool_call`: call the backend's `/api/v1/memory/recall` with the runtime's existing backend credentials instead of `RetrieveMemoryRecords`; the query is the user's text only, never the turn-context JSON; `sync_turn` unchanged)
- Modify: `apps/backend/core/services/workspace_memory.py` (`delete_owner_memory`, `delete_member_memory` also call `memory_index.wipe_workspace` / `wipe_member`)
- Test: `apps/infra/hermes/tests/test_memory_plugin_recall.py`, `apps/backend/tests/unit/services/test_workspace_memory.py` (extend)

- [ ] **Step 1: Write the failing tests**

```python
def test_prefetch_query_is_user_text_only(monkeypatch): ...     # turn-context JSON prefix stripped before the request
def test_prefetch_injects_page_lines_under_budget(monkeypatch): ...
async def test_owner_wipe_calls_index_wipe(patched): ...
```

- [ ] **Step 2: Run → FAIL** (`cd apps/infra/hermes && pytest tests/test_memory_plugin_recall.py -v`; backend as before)
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "feat(memory): room agents recall through the backend; wipes cover the index"`

### Task 11: Memory tab and one-time import

**Files:**
- Modify: `apps/backend/routers/memory.py` (`GET /api/v1/memory` lists team records from `records` and `entries` with `status`, `history`, `speaker`, plus the private AgentCore scope as today; `POST /{id}/share`, `/confirm`, `DELETE /{id}` route to the right store by id prefix)
- Create: `apps/backend/scripts/memory_import.py` (`import_workspace(owner_id) -> int`: AgentCore team records → `records` + a `memory.import` op to the cloud member with author "workspace (imported)")
- Modify: `apps/frontend/src/components/control/panels/AgentMemoryTab.tsx` (status badge "contested", a history line "was … (who, when)", a contested filter; nothing else)
- Test: `apps/backend/tests/unit/routers/test_memory_list.py`, `apps/frontend/src/__tests__/components/control/AgentMemoryTab.test.tsx`

- [ ] **Step 1: Write the failing tests** (`test_list_merges_records_and_entries_with_status`, `renders contested badge and history line`)
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "feat(memory): Memory tab shows status and history; one-time import"`

### Task 12: Deploy to dev and run our own workspace on it

- [ ] **Step 1: Deploy** (`gh workflow run deploy.yml` for dev; the founder runs prod deploys)
- [ ] **Step 2: Migrate** — `uv run scripts/memory_migrate.py` against dev
- [ ] **Step 3: Import our workspace** — `uv run scripts/memory_import.py <owner>`
- [ ] **Step 4: Smoke** — a room agent answers a question from a fact a laptop `remember`ed; the Memory tab shows it with the sharer's name; the harness's Lightbulb arm runs on 100K (harness plan Task 5)
- [ ] **Step 5: Record stage timings and costs for a day in `docs/research/2026-10-28-beam-baselines.md`**
