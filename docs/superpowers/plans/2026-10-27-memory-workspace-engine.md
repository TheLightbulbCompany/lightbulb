# Memory Workspace Engine Implementation Plan (stage one, AgentCore only)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Workspace memory that extracts, stores and searches on AgentCore Memory under our instructions and metadata, with a five-stage recall (expand, search, fuse, rerank, compose) in one backend function used by room agents, hosted tools, the Memory tab and the benchmark runner.

**Architecture:** The existing semantic strategy gets override instructions and a metadata schema. Two strategy-less namespaces hold raw room turns and the mirrored log. `memory_recall.recall()` calls AgentCore several ways per sub-question, fuses, reranks through Bedrock and composes a page in past.dev's shape. `memory_sync` moves extracted facts into the log through the cloud member and log entries into AgentCore. No new database or service.

**Tech Stack:** Python 3.12 backend (FastAPI, pytest asyncio_mode=auto, uv), boto3 (`bedrock-agentcore`, `bedrock-agentcore-control`, `bedrock-agent-runtime` for `Rerank`), CDK TypeScript (`container-stack.ts`), Hermes plugin `apps/infra/hermes/plugins/memory/agentcore/`.

**Spec:** `docs/superpowers/specs/2026-10-27-memory-layer-design.md` §5, §8, §10.

## Global Constraints

- Branch from clean `origin/main` in a fresh worktree (CLAUDE.md).
- Namespaces per workspace owner `<o>`: facts `/owners/<o>/` (unchanged), turns `/owners/<o>/turns/`, entries `/owners/<o>/entries/`; private DM scope `/owners/<o>:<user>/` unchanged. Benchmark: `/beam/<workspace_id>/…` on `BenchmarkMemory` (dev only).
- Metadata keys (indexed): `kind` STRING ∈ {claim, observation, rule, mention, anchor}; `entity` STRING; `attribute` STRING; `status` STRING ∈ {current, contested}; `validFrom` NUMBER; `validTo` NUMBER (epoch seconds). Up to 5 filters per query.
- Rerank: `cohere.rerank-v3-5:0`, `us-east-1`, ≤100 documents per call; on any error fall back to RRF order.
- Recall defaults: `budget_tokens=8000`, `limit=100`, RRF `k=60`; expansion only when the model returns more than one line.
- Tokens counted with `tiktoken` `o200k_base`.
- Page shape: `{"asOf": iso, "usedEvidenceTokens": int, "results": [{"rank": int, "kind": "claim|observation|rule|mention|anchor|turn|entry", "content": str, "occurredAt": iso, "status": "current|contested|replaced", "history": [{"value": str, "speaker": str, "occurredAt": iso}], "sources": [{"sourceId": str, "occurredAt": iso, "speaker": str, "excerpt": str}]}]}`.
- AgentCore and Bedrock are stubbed with `botocore.stub.Stubber` in unit tests; no test calls AWS.
- Auth: workspace endpoints use the member auth dependency already in `routers/memory.py`; the plugin uses the runtime's existing backend credentials; benchmark endpoints require the admin token and `MEMORY_BENCHMARK_ENABLED=true`, never set in prod.
- `RetrieveMemoryRecords` counts by `searchCriteria.topK`, not `maxResults` (plugin comment, prod audit 24 Sep).

## Review Focus

1. A question with no matching memory returns an empty page quickly, not an exception or unrelated turns: when fewer than 5 candidates score above 0 after rerank, return at most those. (Task 4: `test_recall_empty_namespace_returns_empty_page`.)
2. Two workspaces never see each other's records: every call carries the workspace's namespaces and the tests assert no cross-namespace call is made. (Task 4: `test_recall_only_queries_own_namespaces`.)
3. A replaced value never outranks the current one for the same entity+attribute; contested records come first. (Task 3: `test_compose_orders_contested_then_current`.)
4. Rerank failure degrades to RRF order, logged. (Task 2: `test_rerank_failure_returns_none`; Task 4: `test_recall_without_rerank_uses_rrf_order`.)
5. A wipe removes every record in the three namespaces and the private scope, and a later recall returns nothing. (Task 8: `test_owner_wipe_covers_three_namespaces`.)

---

### Task 1: AgentCore strategy overrides, metadata schema, namespaces, benchmark memory

**Files:**
- Modify: `apps/infra/lib/stacks/container-stack.ts:236-249` (`CfnMemory`): `indexedKeys` for the six keys; on `workspace_facts`, `memoryRecordSchema.metadataSchema` with definitions, `allowedValues` and `LATEST_VALUE` for `status`; override configuration with extraction and consolidation instructions from the prompt files and model `MEMORY_EXTRACT_MODEL` (default a Haiku-class Bedrock id); a `BenchmarkMemory` with the same strategy, dev only
- Create: `apps/infra/prompts/memory-extraction.md`, `apps/infra/prompts/memory-consolidation.md` (append to AWS's defaults; the consolidation text carries one-record-per-slot, current-then-history and contested from spec §5; the extraction text carries the shapes: speaker prefix, `entity attribute: value`, resolved dates, rules with "applies to")
- Create: `apps/infra/scripts/memory-strategy-apply.ts` (fallback through `UpdateMemory` if `CfnMemory` rejects any field)
- Modify: `apps/infra/lib/stacks/service-stack.ts` (backend task role: `bedrock:Rerank` on `cohere.rerank-v3-5:0`; `bedrock-agentcore:BatchCreateMemoryRecords|BatchDeleteMemoryRecords|ListMemoryRecords|RetrieveMemoryRecords` on both memories; env `MEMORY_BENCHMARK_MEMORY_ID` in dev)
- Test: `apps/infra/test/container-stack.test.ts`, `apps/infra/test/service-stack.test.ts`

**Interfaces:**
- Produces: metadata keys exactly as Task 5 reads them; env `AGENTCORE_MEMORY_ID` (exists), `MEMORY_BENCHMARK_MEMORY_ID`.

- [ ] **Step 1: Write the failing CDK assertions** — `workspace memory declares the six indexed keys` (`Match.arrayWith([Match.objectLike({ Key: "validFrom", Type: "NUMBER" })])`); `backend task role can rerank`
- [ ] **Step 2: Run → FAIL** — `cd apps/infra && npx jest test/container-stack.test.ts test/service-stack.test.ts -t "indexed keys|rerank"`
- [ ] **Step 3: Implement; if `cdk synth` rejects override or schema properties, keep `indexedKeys` in CDK and apply the rest in `memory-strategy-apply.ts` from the deploy workflow**
- [ ] **Step 4: Run → PASS; `npx cdk synth`; deploy dev; `aws bedrock-agentcore-control get-memory` shows the strategy with overrides**
- [ ] **Step 5: Probe the open question** — send probe 2's two contradictions and three rephrased updates as events to the dev memory; after extraction, `list-memory-records` shows one record per slot with history in the text and `status=contested` on the Ana/Ben case. Record the answer ("merging can/cannot set metadata on the rewritten record") in `docs/research/2026-10-28-beam-baselines.md`; if it cannot, open the self-managed fallback as a follow-up plan and continue.
- [ ] **Step 6: Commit** — `git commit -m "infra(memory): overrides, metadata schema, namespaces, benchmark memory, rerank permission"`

### Task 2: Rerank and expansion clients

**Files:**
- Create: `apps/backend/core/services/memory_models.py`
- Test: `apps/backend/tests/unit/services/test_memory_models.py`

**Interfaces:**
- Produces: `async def rerank(query: str, documents: list[str], top_n: int) -> list[tuple[int, float]] | None` (index into `documents`, score; `None` on any `ClientError`, logged at warning; extra documents beyond 100 dropped from the tail); `async def expand(question: str) -> list[str]` (one Haiku call through the backend's existing Bedrock inference path with `EXPANSION_PROMPT`; returns the non-empty lines; `[question]` when ≤1 line or on error); `EXPANSION_PROMPT: str` ("List the separate facts this question needs, one per line, at most 5; if it needs one, write the question itself").

- [ ] **Step 1: Write the failing tests** — `test_rerank_returns_indices_sorted_by_score`, `test_rerank_failure_returns_none`, `test_rerank_caps_documents_at_100`, `test_expand_single_line_returns_original`, `test_expand_error_returns_original`
- [ ] **Step 2: Run → FAIL** — `cd apps/backend && uv run pytest tests/unit/services/test_memory_models.py -v`
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "feat(memory): Bedrock rerank and question expansion"`

### Task 3: Pure stages: RRF, composition, time windows

**Files:**
- Create: `apps/backend/core/services/memory_stages.py`
- Test: `apps/backend/tests/unit/services/test_memory_stages.py`

**Interfaces:**
- Produces:
  - `@dataclass class Candidate: id: str; kind: str; content: str; occurred_at: datetime; status: str; speaker: str | None; entity: str | None; attribute: str | None; valid_from: datetime | None; valid_to: datetime | None; source_id: str | None; fused: float = 0.0`
  - `def from_record(summary: dict, kind_default: str) -> Candidate` (maps a `memoryRecordSummaries` item: `content.text`, `metadata`, `timestamp`)
  - `def rrf(rankings: list[list[str]], k: int = 60) -> list[tuple[str, float]]`
  - `def compose(candidates: list[Candidate], budget_tokens: int, as_of: datetime) -> dict` (the page; order: contested, then current claims/observations/rules/anchors by `fused`, then turns and entries by `fused`; history parsed from the record text's "was X (who, when)" tail into `history`; stop at the budget; `usedEvidenceTokens` counted on the serialised page)
  - `def time_window(question: str, as_of: datetime) -> tuple[datetime | None, datetime | None]` (month names, "last week", years, ISO dates via `python-dateutil`; unknown → `(None, None)`)

- [ ] **Step 1: Write the failing tests** — `test_rrf_k60_matches_hand_computation`, `test_compose_orders_contested_then_current`, `test_compose_stops_at_budget`, `test_compose_parses_history_tail`, `test_time_window_month_and_year`
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "feat(memory): RRF, composition, time windows"`

### Task 4: Recall over AgentCore

**Files:**
- Create: `apps/backend/core/services/memory_recall.py`
- Modify: `apps/backend/core/services/workspace_memory.py` (export `_client()`; add `namespaces_for(owner_id, user_id | None) -> dict[str, str]` returning `facts`, `turns`, `entries`, `private`)
- Test: `apps/backend/tests/unit/services/test_memory_recall.py` (Stubber on `bedrock-agentcore`; Task 2 functions patched)

**Interfaces:**
- Produces: `async def recall(*, memory_id: str, namespaces: dict[str, str], question: str, as_of: datetime | None = None, budget_tokens: int = 8000, limit: int = 100, include_private: bool = False, use_rerank: bool = True, use_expand: bool = True) -> dict`.
  Stages: (1) `expand`; (2) per part, with `asyncio.gather`: `RetrieveMemoryRecords(namespace=facts, topK=limit, metadataFilters=[status EQUALS_TO current] + as-of filters)`, the same with `status EQUALS_TO contested`, `namespace=turns`, `namespace=entries`, `private` when `include_private`; `ListMemoryRecords(namespace=facts, metadataFilters=[entity EQUALS_TO e])` for each entity `e` whose name (from a `ListMemoryRecords` of distinct entities cached 60 s per workspace) appears in the part; as-of filters when `as_of`: `validFrom LESS_THAN_OR_EQUALS as_of` and (`validTo NOT_EXISTS` or `validTo GREATER_THAN as_of`, two calls merged); `time_window` → `x-amz-agentcore-memory-createdAt` is ingest time, so apply the window to `occurredAt` after fetch instead; (3) `rrf` over every ranking; (4) `rerank` on the top 100 contents, fallback RRF; (5) `compose`. Log stage timings at info.

- [ ] **Step 1: Write the failing tests** — `test_recall_empty_namespace_returns_empty_page`, `test_recall_only_queries_own_namespaces`, `test_recall_as_of_filters_are_sent`, `test_recall_prefers_current_over_replaced`, `test_recall_without_rerank_uses_rrf_order`, `test_recall_multi_part_question_queries_each_part`, `test_recall_entity_listing_adds_all_records_for_entity`
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "feat(memory): five-stage recall over AgentCore"`

### Task 5: Sync between AgentCore and the log

**Files:**
- Create: `apps/backend/core/services/memory_sync.py`
- Modify: `apps/backend/core/services/network_member.py` (expose `call_op(owner_id, op, params, timeout) -> dict | None` returning `None` when the runtime is parked, and `queue_op(owner_id, op, params)` persisted in DynamoDB and drained on the runtime's next start)
- Test: `apps/backend/tests/unit/services/test_memory_sync.py`

**Interfaces:**
- Consumes: cloud member ops from the log plan Task 6: `memory.import {entries}`, `memory.list {since_seq}`, `memory.remember {...}`.
- Produces: `async def sync_workspace(owner_id: str, *, memory_id: str, namespaces: dict[str, str], cloud_member: bool = True) -> dict` → `{"imported": n, "mirrored": n}`: `ListMemoryRecords(facts, updatedAt AFTER watermark)` → `memory.import` entries (author "workspace", kind from metadata, `subject = f"{entity} {attribute}"`, `fact = value`, `when = validFrom`) when the runtime is up, else left for next time; `memory.list {since_seq}` → `BatchCreateMemoryRecords` into `entries` (record id = entry id; tombstones → `BatchDeleteMemoryRecords`); watermarks in DynamoDB under the owner. `async def sync_if_stale(owner_id, …, max_age_s: int = 60) -> None`.

- [ ] **Step 1: Write the failing tests** — `test_sync_imports_new_facts_as_entries`, `test_sync_mirrors_entries_and_tombstones`, `test_sync_skips_private_namespace`, `test_sync_if_stale_noop_within_max_age`, `test_sync_when_parked_leaves_watermark`
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "feat(memory): sync extracted facts into the log and log entries into AgentCore"`

### Task 6: Router: recall, remember, list, benchmark endpoints, tool specs

**Files:**
- Modify: `apps/backend/routers/memory.py`
- Modify: `apps/backend/core/services/network_tools.json` (`recall`, `remember` tool specs; mirrored into the CLI by the log plan Task 3)
- Modify: `apps/backend/core/services/hosted_mcp.py` (map the two tools)
- Test: `apps/backend/tests/unit/routers/test_memory_recall_routes.py`

**Interfaces:**
- Produces:
  - `POST /api/v1/memory/recall {question, as_of?, budget_tokens?=8000, limit?=100}` → page; member auth; `include_private=True` for the caller; `sync_if_stale` first.
  - `POST /api/v1/memory/remember {subject, fact, kind, scope="me"|"team", replaces?}` → `{"queued": bool, "neighbours": [...]}`: neighbours from a `RetrieveMemoryRecords` on `facts` and `entries` by subject; the write forwarded as `memory.remember` (`call_op`, else `queue_op`, `queued=true`).
  - `GET /api/v1/memory` (existing list) adds `status`, `history`, `speaker`, `source` from the record metadata and text; the private list unchanged; `DELETE /{id}` also deletes the `entries` mirror and forwards `forget` to the cloud member.
  - Benchmark (admin token + `MEMORY_BENCHMARK_ENABLED`): `POST /benchmark/projects {name}` → `{workspace_id}`; `POST /benchmark/ingest {workspace_id, idempotency_key, items}` → `CreateEvent` per item on `BenchmarkMemory` (actor `workspace_id`, session = item's BEAM session, `eventTimestamp` = item timestamp, text `f"{author}: {content}"`) and `BatchCreateMemoryRecords` of each item into `/beam/<workspace_id>/turns/` (record id = item id, `timestamp` = item timestamp); repeated key → same `ingestion_id`, nothing re-sent (keys kept in DynamoDB); `GET /benchmark/ingest/{id}` → record count in `facts` stable for 300 s → `{status:"completed", settled:true}`; `POST /benchmark/recall {workspace_id, question, as_of, budget_tokens, limit, extraction: "full"|"none"}` → `recall(...)` with `namespaces` of the benchmark workspace (`extraction="none"` queries `turns` only); `DELETE /benchmark/projects/{workspace_id}` → delete all records in the three namespaces and the actor's events.
  - Tool specs: `recall(question: str)` ("Call when a task starts or you need something a teammate may know"); `remember(subject, fact, kind, scope?, replaces?)` ("Returns what is already known on the subject; call again with `replaces` if yours is a change. Personal unless the person says to share").

- [ ] **Step 1: Write the failing route tests** — `test_recall_requires_member`, `test_recall_calls_sync_then_recall`, `test_remember_returns_neighbours_and_queues_when_parked`, `test_benchmark_routes_404_when_disabled`, `test_benchmark_ingest_idempotent`, `test_benchmark_recall_extraction_none_queries_turns_only`
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "feat(memory): recall, remember, list, benchmark endpoints, tool specs"`

### Task 7: Hermes plugin

**Files:**
- Modify: `apps/infra/hermes/plugins/memory/agentcore/__init__.py`: `sync_turn` adds the speaker name to the event text and writes the turn as a record into `turns` (`BatchCreateMemoryRecords`, id = `<session>:<turn>`); `prefetch`, `_search` and `handle_tool_call` call `POST /api/v1/memory/recall` with the user's text only (strip the turn-context JSON prefix); a `remember` tool schema forwarding to `POST /api/v1/memory/remember`
- Test: `apps/infra/hermes/tests/test_memory_plugin_recall.py`

- [ ] **Step 1: Write the failing tests** — `test_prefetch_query_is_user_text_only`, `test_prefetch_injects_page_lines_under_budget`, `test_sync_turn_writes_turn_record_with_speaker`, `test_remember_tool_forwards`
- [ ] **Step 2: Run → FAIL** — `cd apps/infra/hermes && pytest tests/test_memory_plugin_recall.py -v`
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "feat(memory): room agents recall through the backend; turns mirrored"`

### Task 8: Wipes, import, Memory tab

**Files:**
- Modify: `apps/backend/core/services/workspace_memory.py` (`delete_owner_memory` covers `turns` and `entries`; `delete_member_memory` forwards `memory.forget_author` to the cloud member (log plan Task 6))
- Create: `apps/backend/scripts/memory_import.py` (`import_workspace(owner_id) -> int`: existing `facts` records → `memory.import` with author "workspace (imported)")
- Modify: `apps/frontend/src/components/control/panels/AgentMemoryTab.tsx` (contested badge, history line, contested filter, "pending" state for queued writes)
- Test: `apps/backend/tests/unit/services/test_workspace_memory.py` (extend), `apps/frontend/src/__tests__/components/control/AgentMemoryTab.test.tsx`

- [ ] **Step 1: Write the failing tests** — `test_owner_wipe_covers_three_namespaces`, `test_member_wipe_forwards_forget_author`, `test_import_sends_entries_in_batches`, `renders contested badge and history line`
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "feat(memory): wipes cover the namespaces; one-time import; Memory tab status and history"`

### Task 9: Deploy to dev and run our own workspace on it

- [ ] **Step 1: Deploy dev** (`gh workflow run deploy.yml`; the founder runs prod)
- [ ] **Step 2: Import our workspace** — `uv run scripts/memory_import.py <owner>`
- [ ] **Step 3: Smoke** — a room agent answers from a fact a laptop `remember`ed; the Memory tab shows it with the sharer's name; the runner's Lightbulb arm runs on 100K (runner plan Task 5)
- [ ] **Step 4: Record stage timings, AgentCore extraction throughput and costs for a day in `docs/research/2026-10-28-beam-baselines.md`**
