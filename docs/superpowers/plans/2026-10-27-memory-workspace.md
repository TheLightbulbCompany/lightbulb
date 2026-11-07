# Memory in the Workspace Implementation Plan (stage one, AgentCore only)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Everything the workspace side needs: extraction, storage and search on AgentCore Memory under our instructions and metadata; a five-stage recall in one backend function used by room agents, hosted tools and the Memory tab; sync with the network log through the cloud member; uploaded sessions, the evaluator on AgentCore's episodic strategy, drafts approved into memory; usage, timeline and share; the security page.

**Architecture:** The existing semantic strategy gets override instructions and a metadata schema. Two strategy-less namespaces hold raw room turns and the mirrored log. `memory_recall.recall()` calls AgentCore several ways per sub-question, fuses, reranks through Bedrock and composes a page. `memory_sync` moves extracted facts into the log through the cloud member and log entries into AgentCore. Uploaded sessions go to S3 and DynamoDB and, when full, into an episodic strategy whose reflections become drafts. No new database or service.

**Tech Stack:** Python 3.12 backend (FastAPI, pytest asyncio_mode=auto, uv), boto3 (`bedrock-agentcore`, `bedrock-agentcore-control`, `bedrock-agent-runtime` for `Rerank`, S3, DynamoDB via `core/dynamodb.py`), CDK TypeScript (`container-stack.ts`, `service-stack.ts`), Hermes plugin `apps/infra/hermes/plugins/memory/agentcore/`, frontend (Memory tab, Usage, timeline, `/security`).

**Spec:** `docs/superpowers/specs/2026-10-27-memory-layer-design.md` §5–§8, §10.

## Global Constraints

- Branch from clean `origin/main` in a fresh worktree (CLAUDE.md).
- Namespaces per workspace owner `<o>`: facts `/owners/<o>/` (unchanged), turns `/owners/<o>/turns/`, entries `/owners/<o>/entries/`; private DM scope `/owners/<o>:<user>/` unchanged. Evaluator: episodes `/users/{userId}/episodes/`, reflections `/users/{userId}/`; never cross-user reflection.
- Metadata keys (indexed): `kind` STRING ∈ {claim, observation, rule, mention, anchor}; `entity` STRING; `attribute` STRING; `status` STRING ∈ {current, contested}; `validFrom` NUMBER; `validTo` NUMBER (epoch seconds). Up to 5 filters per query.
- How metadata gets onto merged records, from the AWS docs (relied on, not probed): override consolidation keeps AWS's `AddMemory`/`UpdateMemory` output schema, so it cannot set metadata itself; but "when older memories are consolidated with newer ones … the consolidated record is re-extracted using the current schema and will include the new metadata fields", and schema keys are "populated by the LLM during extraction" from the record text with `LATEST_VALUE` conflict resolution. So our consolidation instructions write the status and dates into the record text ("Contested: …", "since 15 Sep; was … until 15 Sep") and the schema definitions tell the extractor to read `status`, `validFrom` and `validTo` from that text. First deploy to dev smoke-checks this with the probe-2 cases (cents).
- Rerank: `cohere.rerank-v3-5:0`, `us-east-1`, ≤100 documents per call; on any error fall back to RRF order.
- Recall defaults: `budget_tokens=8000`, `limit=100`, RRF `k=60`; expansion only when the model returns more than one line.
- Page shape: `{"asOf": iso, "usedEvidenceTokens": int, "results": [{"rank": int, "kind": "claim|observation|rule|mention|anchor|turn|entry", "content": str, "occurredAt": iso, "status": "current|contested|replaced", "history": [{"value": str, "speaker": str, "occurredAt": iso}], "sources": [{"sourceId": str, "occurredAt": iso, "speaker": str, "excerpt": str}]}]}`; tokens counted with `tiktoken` `o200k_base`.
- Upload retention: free account 30 days, workspace 365 days (S3 lifecycle by prefix). Free-account evaluator cap: 50 sessions a month. No third party ever receives a session.
- AgentCore and Bedrock are stubbed with `botocore.stub.Stubber` in unit tests; no test calls AWS.
- Auth: workspace endpoints use the member auth dependency in `routers/memory.py`; the plugin uses the runtime's existing backend credentials; sessions endpoints accept a signed-in identity or a member.
- `RetrieveMemoryRecords` counts by `searchCriteria.topK`, not `maxResults` (prod audit 24 Sep).
- The security page never names a competitor and makes only the claims in spec §7.

## Review Focus

1. A question with no matching memory returns an empty page quickly: when fewer than 5 candidates score above 0 after rerank, return at most those. (Task 4: `test_recall_empty_namespace_returns_empty_page`.)
2. Two workspaces never see each other's records: every call carries the workspace's namespaces. (Task 4: `test_recall_only_queries_own_namespaces`.)
3. A replaced value never outranks the current one; contested records come first. (Task 3: `test_compose_orders_contested_then_current`.)
4. Rerank failure degrades to RRF order. (Task 2: `test_rerank_failure_returns_none`; Task 4: `test_recall_without_rerank_uses_rrf_order`.)
5. A member wipe removes their private scope, sessions, drafts, episodes and tombstones their entries; an owner wipe empties the three namespaces, and a later recall returns nothing. (Task 8: `test_owner_wipe_covers_three_namespaces`; Task 11: `test_member_wipe_removes_sessions_drafts_episodes`.)
6. A draft approved in a workspace becomes a team entry signed by the workspace with the approver's name. (Task 12: `test_approve_in_workspace_signs_as_workspace_with_approver`.)

---

### Task 1: AgentCore strategy overrides, metadata schema, namespaces, episodic strategy

**Files:**
- Modify: `apps/infra/lib/stacks/container-stack.ts:236-249` (`CfnMemory`): `indexedKeys` for the six keys; on `workspace_facts`, `memoryRecordSchema.metadataSchema` with definitions that tell the extractor to read `status`, `validFrom`, `validTo` from the record text, `allowedValues`, `LATEST_VALUE`; override configuration with the two instruction files and model `MEMORY_EXTRACT_MODEL` (a Haiku-class Bedrock id); an `episodicMemoryStrategy` `session_episodes` with the two per-user namespaces
- Create: `apps/infra/prompts/memory-extraction.md`, `apps/infra/prompts/memory-consolidation.md` (appended to AWS's defaults; consolidation carries one-record-per-slot, current-then-history and contested from spec §5, written into the text in a fixed form the schema definitions name; extraction carries the shapes: speaker prefix, `entity attribute: value`, resolved dates, rules with "applies to")
- Create: `apps/infra/scripts/memory-strategy-apply.ts` (fallback through `UpdateMemory` if `CfnMemory` rejects any field)
- Modify: `apps/infra/lib/stacks/service-stack.ts` (backend task role: `bedrock:Rerank` on `cohere.rerank-v3-5:0`; `bedrock-agentcore:BatchCreateMemoryRecords|BatchDeleteMemoryRecords|ListMemoryRecords|RetrieveMemoryRecords|CreateEvent`; S3 bucket lifecycle 30/365 days by prefix for sessions)
- Test: `apps/infra/test/container-stack.test.ts`, `apps/infra/test/service-stack.test.ts`

- [ ] **Step 1: Write the failing CDK assertions** — `workspace memory declares the six indexed keys`, `episodic strategy declared with per-user namespaces`, `backend task role can rerank`
- [ ] **Step 2: Run → FAIL** — `cd apps/infra && npx jest test/container-stack.test.ts test/service-stack.test.ts -t "indexed keys|episodic|rerank"`
- [ ] **Step 3: Implement; if `cdk synth` rejects override or schema properties, keep `indexedKeys` in CDK and apply the rest in `memory-strategy-apply.ts` from the deploy workflow**
- [ ] **Step 4: Run → PASS; `npx cdk synth`; deploy dev; `aws bedrock-agentcore-control get-memory` shows both strategies**
- [ ] **Step 5: Smoke (cents): send probe 2's two contradictions and three rephrased updates as events; `list-memory-records` shows one record per slot with history in the text, `status=contested` on the Ana/Ben case, `validFrom` set. If the metadata is missing, tighten the schema definitions first; the self-managed fallback only if that fails.**
- [ ] **Step 6: Commit** — `git commit -m "infra(memory): overrides, metadata schema, namespaces, episodic strategy, permissions"`

### Task 2: Rerank and expansion clients

**Files:**
- Create: `apps/backend/core/services/memory_models.py`
- Test: `apps/backend/tests/unit/services/test_memory_models.py`

**Interfaces:**
- Produces: `async def rerank(query: str, documents: list[str], top_n: int) -> list[tuple[int, float]] | None` (`None` on any `ClientError`, logged; documents beyond 100 dropped from the tail); `async def expand(question: str) -> list[str]` (one Haiku call through the backend's existing Bedrock path with `EXPANSION_PROMPT`; `[question]` when ≤1 line or on error); `EXPANSION_PROMPT: str` ("List the separate facts this question needs, one per line, at most 5; if it needs one, write the question itself").

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
- Produces: `@dataclass class Candidate: id, kind, content, occurred_at, status, speaker, entity, attribute, valid_from, valid_to, source_id, fused: float = 0.0`; `def from_record(summary: dict, kind_default: str) -> Candidate`; `def rrf(rankings: list[list[str]], k: int = 60) -> list[tuple[str, float]]`; `def compose(candidates: list[Candidate], budget_tokens: int, as_of: datetime) -> dict` (first `mark_contested`: candidates with the same `entity`+`attribute` (or the same `subject` for entries) and different values, none replacing the other, all get `status="contested"`, whatever namespace they came from; then order: contested, current claims/observations/rules/anchors by `fused`, turns and entries by `fused`; history parsed from the record text's "was X (who, when)" tail; stop at the budget); `def time_window(question: str, as_of: datetime) -> tuple[datetime | None, datetime | None]` (`python-dateutil`; unknown → `(None, None)`).

- [ ] **Step 1: Write the failing tests** — `test_rrf_k60_matches_hand_computation`, `test_compose_orders_contested_then_current`, `test_compose_marks_contested_across_namespaces` (a `facts` record "production database version: 14" and an `entries` record "production database version: 16", neither replacing the other → both `contested`, both on the page first), `test_compose_stops_at_budget`, `test_compose_parses_history_tail`, `test_time_window_month_and_year`
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "feat(memory): RRF, composition, time windows"`

### Task 4: Recall over AgentCore

**Files:**
- Create: `apps/backend/core/services/memory_recall.py`
- Modify: `apps/backend/core/services/workspace_memory.py` (export `_client()`; add `namespaces_for(owner_id, user_id | None) -> dict[str, str]` with `facts`, `turns`, `entries`, `private`)
- Test: `apps/backend/tests/unit/services/test_memory_recall.py`

**Interfaces:**
- Produces: `async def recall(*, memory_id: str, namespaces: dict[str, str], question: str, as_of: datetime | None = None, budget_tokens: int = 8000, limit: int = 100, include_private: bool = False, use_rerank: bool = True, use_expand: bool = True) -> dict`. Stages: (1) `expand`; (2) per part, `asyncio.gather`: `RetrieveMemoryRecords(facts, topK=limit, status EQUALS_TO current + as-of filters)`, the same with `status EQUALS_TO contested`, `turns`, `entries`, `private` when `include_private`; `ListMemoryRecords(facts, entity EQUALS_TO e)` for each entity named in the part (distinct entities cached 60 s per workspace); as-of filters when `as_of`: `validFrom LESS_THAN_OR_EQUALS as_of` and (`validTo NOT_EXISTS` or `validTo GREATER_THAN as_of`, two calls merged); the `time_window` applied to `occurredAt` after fetch; (3) `rrf`; (4) `rerank` on the top 100, fallback RRF; (5) `compose`. Stage timings logged at info.

- [ ] **Step 1: Write the failing tests** — `test_recall_empty_namespace_returns_empty_page`, `test_recall_only_queries_own_namespaces`, `test_recall_as_of_filters_are_sent`, `test_recall_prefers_current_over_replaced`, `test_recall_without_rerank_uses_rrf_order`, `test_recall_multi_part_question_queries_each_part`, `test_recall_entity_listing_adds_all_records_for_entity`
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "feat(memory): five-stage recall over AgentCore"`

### Task 5: Sync between AgentCore and the log

**Files:**
- Create: `apps/backend/core/services/memory_sync.py`
- Modify: `apps/backend/core/services/network_member.py` (`call_op(owner_id, op, params, timeout) -> dict | None`, `None` when parked; `queue_op(owner_id, op, params)` persisted in DynamoDB, drained on the runtime's next start)
- Test: `apps/backend/tests/unit/services/test_memory_sync.py`

**Interfaces:**
- Consumes: the free-network plan's cloud ops `memory.import`, `memory.list`, `memory.remember`, `memory.forget_author`.
- Produces: `async def sync_workspace(owner_id: str, *, memory_id: str, namespaces: dict[str, str]) -> dict` → `{"imported": n, "mirrored": n}`, two directions, one mapping table in DynamoDB (`memory_links`: AgentCore record id ↔ last imported entry id, per owner):
  - **AgentCore → log.** `ListMemoryRecords(facts, updatedAt AFTER watermark)`. A record seen for the first time → `memory.import` a `remember` (author "workspace", `subject = f"{entity} {attribute}"`, `fact = value`, `when = validFrom`, `on_behalf_of` = the speaker parsed from the text). A record already mapped whose text changed → a `replace` entry with `replaces = [the mapped entry id]`; the mapping advances. Only when the runtime is up; otherwise left for next time.
  - **log → AgentCore.** `memory.list {since_seq}`. A `remember` → `BatchCreateMemoryRecords` into `entries` (record id = entry id; metadata `subject`, `status=current`, `kind`). A `replace` → create its record and `BatchUpdateMemoryRecords` on each replaced record's metadata `status=replaced`. A tombstone → `BatchDeleteMemoryRecords` of its record; if the tombstoned entry is mapped to a `facts` record (it was imported), delete that `facts` record too and drop the mapping, so it is not re-imported.
  - Watermarks (`records_after`, `entries_seq`) in DynamoDB. `async def sync_if_stale(owner_id, …, max_age_s: int = 60) -> None`.

- [ ] **Step 1: Write the failing tests** — `test_sync_imports_new_facts_as_entries`, `test_sync_rewrite_becomes_replace_entry` (a mapped record whose text changed → a `replace` with `replaces=[previous entry id]`), `test_sync_mirrors_entries_and_tombstones`, `test_sync_replace_marks_replaced_record_status`, `test_sync_tombstone_of_imported_entry_deletes_facts_record`, `test_sync_skips_private_namespace`, `test_sync_if_stale_noop_within_max_age`, `test_sync_when_parked_leaves_watermark`
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "feat(memory): sync extracted facts into the log and log entries into AgentCore"`

### Task 6: Router: recall, remember, list, tool specs

**Files:**
- Modify: `apps/backend/routers/memory.py`, `apps/backend/core/services/network_tools.json` (`recall`, `remember`; same text as the CLI's `tools.json`), `apps/backend/core/services/hosted_mcp.py`
- Test: `apps/backend/tests/unit/routers/test_memory_recall_routes.py`

**Interfaces:**
- Produces: `POST /api/v1/memory/recall {question, as_of?, budget_tokens?=8000, limit?=100}` → page (member auth; `include_private=True` for the caller; `sync_if_stale` first); `POST /api/v1/memory/remember {subject, fact, kind, scope="me"|"team", replaces?}` → `{"queued": bool, "neighbours": [...]}` (neighbours by subject from `facts` and `entries`; the write forwarded as `memory.remember`, `call_op` else `queue_op`); `GET /api/v1/memory` adds `status`, `history`, `speaker`, `source`; `DELETE /{id}` also deletes the `entries` mirror and forwards `forget`.

- [ ] **Step 1: Write the failing route tests** — `test_recall_requires_member`, `test_recall_calls_sync_then_recall`, `test_remember_returns_neighbours_and_queues_when_parked`, `test_list_includes_status_and_history`
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "feat(memory): recall, remember, list, tool specs"`

### Task 7: Hermes plugin

**Files:**
- Modify: `apps/infra/hermes/plugins/memory/agentcore/__init__.py`: `sync_turn` adds the speaker name to the event text and writes the turn as a record into `turns` (`BatchCreateMemoryRecords`, id `<session>:<turn>`); `prefetch`, `_search`, `handle_tool_call` call `POST /api/v1/memory/recall` with the user's text only (strip the turn-context JSON prefix); a `remember` tool forwarding to `POST /api/v1/memory/remember`
- Test: `apps/infra/hermes/tests/test_memory_plugin_recall.py`

- [ ] **Step 1: Write the failing tests** — `test_prefetch_query_is_user_text_only`, `test_prefetch_injects_page_lines_under_budget`, `test_sync_turn_writes_turn_record_with_speaker`, `test_remember_tool_forwards`
- [ ] **Step 2: Run → FAIL** — `cd apps/infra/hermes && pytest tests/test_memory_plugin_recall.py -v`
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "feat(memory): room agents recall through the backend; turns mirrored"`

### Task 8: Wipes, import, Memory tab

**Files:**
- Modify: `apps/backend/core/services/workspace_memory.py` (`delete_owner_memory` covers `turns` and `entries`; `delete_member_memory` forwards `memory.forget_author`)
- Create: `apps/backend/scripts/memory_import.py` (`import_workspace(owner_id) -> int`: existing `facts` → `memory.import` with author "workspace (imported)")
- Modify: `apps/frontend/src/components/control/panels/AgentMemoryTab.tsx` (contested badge, history line, contested filter, "pending" for queued writes)
- Test: `apps/backend/tests/unit/services/test_workspace_memory.py` (extend), `apps/frontend/src/__tests__/components/control/AgentMemoryTab.test.tsx`

- [ ] **Step 1: Write the failing tests** — `test_owner_wipe_covers_three_namespaces`, `test_member_wipe_forwards_forget_author`, `test_import_sends_entries_in_batches`, `renders contested badge and history line`
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "feat(memory): wipes cover the namespaces; one-time import; Memory tab status and history"`

### Task 9: Sessions endpoints and store

**Files:**
- Create: `apps/backend/routers/sessions.py` (`POST /api/v1/sessions {sessions, tool_calls}` for a signed-in identity or member; `GET /api/v1/sessions?since=`; `DELETE /api/v1/sessions/{id}`), `apps/backend/core/services/session_store.py` (`put_sessions(user_id, rows, full: bool)` → S3 `<user>/sessions/<id>.json` + DynamoDB metadata `{id, user_id, workspace_id?, harness, computer, repo, branch, started, ended, prompt_tokens, completion_tokens, full}`; `list_sessions(user_id | workspace_id, since)`; `delete_sessions(user_id)`)
- Modify: `apps/backend/main.py` (include the router)
- Test: `apps/backend/tests/unit/routers/test_sessions.py`

- [ ] **Step 1: Write the failing tests** — `test_sessions_post_requires_identity`, `test_metadata_rows_have_no_summaries`, `test_list_sessions_scoped_to_workspace`
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "capture: sessions endpoints and store"`

### Task 10: The evaluator on the episodic strategy

**Files:**
- Create: `apps/backend/core/services/evaluator.py`
- Modify: `apps/backend/core/services/workspace_memory.py` (member wipe → `delete_sessions`, `delete_drafts`, delete the user's episodes and reflections)
- Test: `apps/backend/tests/unit/services/test_evaluator.py`

**Interfaces:**
- Produces: `async def ingest_session(user_id: str, session: dict, tool_calls: list[dict]) -> None` (`CreateEvent`s with `TOOL` role payloads for tool calls, `actorId=user_id`, `sessionId=session["id"]`; skipped past the free cap); `async def collect_drafts(user_id: str) -> int` (reflections newer than the watermark → DynamoDB `drafts` `{id, user_id, workspace_id?, kind: "lesson"|"finding", text, evidence: [session ids], status: "proposed", created}`); `async def list_drafts(user_id | workspace_id) -> list[dict]`; `async def delete_drafts(user_id)`.

- [ ] **Step 1: Write the failing tests** — `test_ingest_session_writes_events_with_tool_role`, `test_free_account_cap_skips_after_50`, `test_collect_drafts_creates_rows_from_new_reflections`, `test_member_wipe_removes_sessions_drafts_episodes`
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "evaluator: episodic strategy over uploaded sessions; drafts"`

### Task 11: Approve drafts into memory

**Files:**
- Modify: `apps/backend/routers/memory.py` (`GET /api/v1/memory/drafts`; `POST /drafts/{id}/approve {scope}`: in a workspace → `memory.remember` with `on_behalf_of` the approver, kind lesson, scope team by default; solo signed-in user → returns the entry fields for the CLI; `POST /drafts/{id}/dismiss`)
- Modify: `apps/frontend/src/components/control/panels/AgentMemoryTab.tsx` ("Suggested" section with evidence links, approve and dismiss)
- Test: `apps/backend/tests/unit/routers/test_memory_drafts.py`, the tab test

- [ ] **Step 1: Write the failing tests** — `test_approve_in_workspace_signs_as_workspace_with_approver`, `test_approve_solo_returns_entry_for_local_store`, `test_dismiss_marks_row`, `renders suggested drafts with evidence links`
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "evaluator: approve drafts into memory"`

### Task 12: Usage, timeline, share by link

**Files:**
- Modify: the finance layer's usage service (PRs #4432/#4433 define the tables; add source `capture` with prompt and completion tokens per agent, person and session)
- Create: `apps/frontend/src/components/control/panels/SessionsTimeline.tsx`
- Modify: `apps/backend/routers/sessions.py` (`POST /sessions/{id}/share` → signed S3 URL, 7 days; `GET /shared/{token}`)
- Test: backend route tests; frontend timeline test

- [ ] **Step 1: Write the failing tests** — `test_usage_includes_capture_tokens_per_agent`, `test_share_link_expires_in_7_days`, `renders sessions grouped by machine`
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "capture: usage tokens, workspace timeline, share by link"`

### Task 13: Security page

**Files:**
- Modify: `apps/frontend/src/app/security/page.tsx` (the claims table from spec §7 with an evidence line each; the "what leaves your machine" band gains memory, capture and upload)
- Test: `apps/frontend/src/__tests__/app/security.test.tsx` (every claim row has an evidence cell; no competitor name)

- [ ] **Step 1: Write the failing test**
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Write the page**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "security: claims with evidence; memory, capture and upload"`

### Task 14: Deploy to dev and run our own workspace on it

- [ ] **Step 1: Deploy dev** (`gh workflow run deploy.yml`; the founder runs prod)
- [ ] **Step 2: Import our workspace** — `uv run scripts/memory_import.py <owner>`
- [ ] **Step 3: Smoke** — a room agent answers from a fact a laptop `remember`ed; the Memory tab shows it with the sharer's name; a real session's reflection appears as a draft and approving it produces a team entry; an owner wipe leaves zero records in all three namespaces
- [ ] **Step 4: Record stage timings and costs for a day in `docs/research/2026-10-28-memory-dev-notes.md`**
