# Workspace memory, from the code (feat/memory-workspace 83e2edd84, 28 October 2026)

Read-only inventory of the branch code and tests; no docs read. Every line cites path:line.

# Workspace memory: code inventory (feat/memory-workspace @ 83e2edd84, PR #4459)

Read-only, code and tests only. Path prefixes: `B/` = `apps/backend/`, `S/` = `apps/backend/core/services/`, `I/` = `apps/infra/`, `F/` = `apps/frontend/src/`. Test names are in `[brackets]`. "(inference)" marks a conclusion drawn from reading the code that no test pins.

Branch vs `origin/main` (`git diff origin/main...HEAD --stat`, non-doc files): 72 files, +7325/-198. New: `S/memory_api.py` (277), `S/memory_recall.py` (229), `S/memory_stages.py` (263), `S/memory_models.py` (75), `S/memory_sync.py` (489), `S/evaluator.py` (457), `S/session_store.py` (184), `B/routers/sessions.py` (149), `B/scripts/memory_import.py` (29), `F/app/shared/[token]/page.tsx`, `F/components/control/panels/SessionsTimeline.tsx`. Changed: `S/workspace_memory.py` (+120), `B/routers/memory.py` (+137), `S/network_member.py` (+119), `S/network_login.py` (+58), `S/hosted_mcp.py` (+40), `S/network_tools.json` (+60), `I/lib/stacks/container-stack.ts` (+154), `I/lib/stacks/service-stack.ts` (+62), `I/hermes/plugins/memory/agentcore/__init__.py` (+189/-), `I/hermes/bridge/server.py` (+15), `F/app/security/page.tsx` (+110), `F/components/control/panels/AgentMemoryTab.tsx` (+84). `S/session_redact.py` and `B/routers/webhooks_paperclip`-style code are NOT changed. `S/session_redact.py` exists on main but nothing in this branch imports it: its only importer is `S/session_links.py:36`.

Note on scope: the two `I/prompts/*.md` files were read because they are build inputs (`I/lib/stacks/container-stack.ts:26-28` reads them at synth time), not docs. No other .md file was read.

---

## 1. AgentCore resources (CDK)

### Workspace memory (`CfnMemory "WorkspaceMemory"`)
- One resource per env, shared by every tenant, named `lightbulb_<env>_workspace_memory`: `I/lib/stacks/container-stack.ts:267-269`. Isolation between tenants is IAM, not separate resources: `:236-239`.
- Encryption uses an AWS-owned key on purpose (a CMK later means a new name plus a data migration): `:240-243`.
- `eventExpiryDuration: 7` (days): `:272`.
- `memoryExecutionRoleArn` = `WorkspaceMemoryExecutionRole`: `:248-256`, `:273`. It trusts `bedrock-agentcore.amazonaws.com` with `aws:SourceAccount` = this account and `aws:SourceArn` like `arn:aws:bedrock-agentcore:<region>:<acct>:memory/*`. It may call `bedrock:InvokeModel` and `InvokeModelWithResponseStream` on the inference profile `us.anthropic.claude-haiku-4-5-20251001-v1:0` and on `arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0`: `:257-265`. [container-stack.test.ts: "memory execution role is trusted by agentcore and can invoke the extract model"]
- Model constant: `MEMORY_EXTRACT_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"`: `:24`.
- Indexed keys (at most 10 and never removable, so only six are declared): `kind` STRING, `entity` STRING, `attribute` STRING, `status` STRING, `validFrom` NUMBER, `validTo` NUMBER: `:274-283`. [container-stack.test.ts: "workspace memory declares the six indexed keys"]
- Strategy: `customMemoryStrategy` named `workspace_facts`, namespaces `["/owners/{actorId}/"]`, `semanticOverride` with `extraction` and `consolidation` set to `{ appendToPrompt, modelId: MEMORY_EXTRACT_MODEL }`: `:291-299`. The output schema (AddMemory/UpdateMemory) stays AWS's, so the prompts only shape the text: `:286-290`.
- Metadata schema (`memoryRecordSchema.metadataSchema`), `:300-363`:
  - `kind` STRING, allowed values `claim|observation|rule|mention|anchor`: `:302-312`.
  - `entity` STRING ("the <entity> in '<entity> <attribute>: <value>'. Empty for rules."): `:313-321`.
  - `attribute` STRING: `:322-330`.
  - `status` STRING: `contested` when the text begins with `Contested:`, else `current`. `llmExtractionInstruction: "LATEST_VALUE"`, allowed values `current|contested`: `:331-342`. The schema has no `replaced` value. Only the backend's sync writes `replaced` (`S/memory_sync.py:291`).
  - `validFrom` NUMBER, epoch seconds at 00:00 UTC, read from `(since ` or from the conversation date: `:343-352`.
  - `validTo` NUMBER, from `until ` when the text itself has ended: `:353-362`.
- Prompts, appended to AWS's default instructions (`:25-28`):
  - `I/prompts/memory-extraction.md`:
    - Every line starts with `<Name>: ...`, never "the user": `:3`.
    - Claims are written `<entity> <attribute>: <value>` with the same wording each time: `:5`.
    - Relative dates are resolved to YYYY-MM-DD: `:7`.
    - Rules are written `When <condition>, <action>. Applies to <scope>.`: `:9`.
    - One fact per line: `:11`.
    - A request also produces the fact it implies: `:13`.
    - Chatter is dropped: `:15`.
  - `I/prompts/memory-consolidation.md`:
    - Exactly one record per entity and attribute: `:1`.
    - A changed value is rewritten `<Name>: <claim> (since <date>; was <old claim>, <who>, until <date>)`, with history most recent first: `:5-8`.
    - Two people disagreeing gives `Contested: <Name1>: <claim1> (<who>, <date>) vs <Name2>: <claim2> (<who>, <date>)`: `:10-12`.
    - Do not paraphrase these forms, and never rename the AddMemory/UpdateMemory operations: `:14`.

### Sessions memory (`CfnMemory "SessionsMemory"`)
- Named `lightbulb_<env>_sessions_memory`, `eventExpiryDuration: 7`: `I/lib/stacks/container-stack.ts:377-380`.
- `episodicMemoryStrategy` named `session_episodes`, namespaces `["/users/{actorId}/episodes/"]`, `reflectionConfiguration.namespaces ["/users/{actorId}/"]`: `:381-389`.
- No execution role and no model id (the built-in strategy names no model). No runtime grant; only the backend touches it: `:371-376`. [container-stack.test.ts: "episodic strategy lives alone in the sessions memory, with per-user namespaces"]

### Runtime permission ceiling (owner runtimes)
- `BoundaryAgentCoreMemoryUse` allows `CreateEvent`, `ListEvents`, `GetEvent`, `ListSessions`, `RetrieveMemoryRecords` and `ListMemoryRecords` on the workspace memory only. Record get/delete and every Batch* call are left out on purpose: `I/lib/stacks/container-stack.ts:504-519`.
- The actual grant is the per-owner inline policy `owner-memory`: `B/core/billing/bedrock_aip.py:273`. It allows events for `actorId` = owner (`:226`) or `<owner>:user_*` (`:238`), and records for `namespace` = `/owners/<owner>/` (`:245`) or `/owners/<owner>:user_*/` (`:252`). Runtimes therefore cannot read `/turns/<o>/` or `/entries/<o>/`.

### Backend task-role grants (`BackendAgentCore` managed policy, `I/lib/stacks/service-stack.ts:445`)
- `AgentCoreMemoryWipe` on the workspace memory: `ListMemoryRecords`, `BatchDeleteMemoryRecords`, `ListSessions`, `ListEvents`, `DeleteEvent`, `ListActors`: `:552-568`.
- `AgentCoreMemoryReview` on the workspace memory: `GetMemoryRecord`, `BatchCreateMemoryRecords`: `:570-581`.
- `AgentCoreMemorySync` on the workspace memory: `BatchUpdateMemoryRecords`: `:583-591`. [service-stack.test.ts: "backend task role can mark mirrored records replaced"]
- `AgentCoreSessionsMemory` on the sessions memory: `CreateEvent`, `ListMemoryRecords`, `BatchDeleteMemoryRecords`, `ListSessions`, `ListEvents`, `DeleteEvent`: `:593-609`. [service-stack.test.ts: "backend task role reaches the sessions memory, and its id is in the backend env"]
- `AgentCoreMemoryRecall` on the workspace memory: `RetrieveMemoryRecords`, `CreateEvent`: `:611-619`. No backend code calls `create_event` on the workspace memory. The only backend `create_event` is the evaluator's, on the sessions memory (`S/evaluator.py:129`), so this CreateEvent grant has no caller (inference). [service-stack.test.ts: "backend task role can retrieve records and write events on the workspace memory"]
- `MemoryRerank`: `bedrock:Rerank` and `bedrock:InvokeModel` on `arn:aws:bedrock:<region>::foundation-model/cohere.rerank-v3-5:0`: `:620-624`. [service-stack.test.ts: "backend task role can rerank"]
- The expansion's `converse` call is covered by the existing `BedrockInvoke` grant on `foundation-model/*` and `inference-profile/*`: `:1140-1153`.
- S3 `SessionsRW`: Get/Put/DeleteObject on `arn:aws:s3:::lightbulb-<env>-openclaw-replicas/sessions/*`: `:1060-1068`. [service-stack.test.ts: "backend task role reads, writes and deletes uploaded sessions in S3"]

### S3 lifecycle rules
- These sit on bucket `OpenclawReplicasBucket` (`lightbulb-<env>-openclaw-replicas`): `I/lib/stacks/container-stack.ts:82`, `:625-639`. That is the same bucket as the backend's `OWNER_STATE_BUCKET` (`I/lib/stacks/service-stack.ts:1562`).
- `SessionsFree`: prefix `sessions/free/`, expires after 30 days: `container-stack.ts:636`.
- `SessionsWorkspace`: prefix `sessions/workspace/`, expires after 365 days: `:637`. [container-stack.test.ts: "sessions prefixes expire after 30 and 365 days"]

### Env vars handed to the service
- `AGENTCORE_MEMORY_ID` = workspace memory id. It is also stamped into every runtime spec: `I/lib/stacks/service-stack.ts:1570-1572`.
- `AGENTCORE_SESSIONS_MEMORY_ID` = sessions memory id, backend only: `:1573-1574`. It is wired through the stage: `I/lib/lightbulb-stage.ts` (`sessionsMemoryId: container.sessionsMemory.attrMemoryId`).
- `MEMORY_EXPAND_MODEL` is NOT set by CDK. The backend falls back to its default `us.anthropic.claude-haiku-4-5-20251001-v1:0`: `B/core/config.py:390`. Empty `AGENTCORE_SESSIONS_MEMORY_ID` means no evaluator: `B/core/config.py:385-387`.

---

## 2. Recall

Entry point: `memory_api.recall_page(owner_id, user_id, question, as_of, budget_tokens=8000, limit=100, use_expand=True)`, `S/memory_api.py:50-78`.
- The question must be 1-2000 characters after strip, else `ValueError`: `S/memory_api.py:26`, `:42-47`. [test_memory_recall_routes: test_recall_rejects_a_bad_question_before_recall]
- A naive `as_of` is treated as UTC: `:62-63`. [test_recall_treats_a_naive_as_of_as_utc]
- With no `AGENTCORE_MEMORY_ID` it returns an empty page: `:64-66`.
- Namespaces come from `workspace_memory.namespaces_for(owner, user)`: facts `/owners/<o>/`, turns `/turns/<o>/`, entries `/entries/<o>/`, plus `private` `/owners/<o>:<u>/` when a user is given: `S/workspace_memory.py:60-68`. [test_memory_recall: test_namespaces_for]
- Every recall fires a background sync and does not wait for it (section 3): `S/memory_api.py:68`, `:81-94`. [test_recall_returns_without_waiting_for_the_sync, test_a_failed_background_sync_is_logged_not_raised]
- Private scope is included when `user_id is not None`: `:76`.

`memory_recall.recall(...)`, `S/memory_recall.py:159-229`. The five stages:

1. **Expansion.**
   - `memory_models.expand(question)` is a Bedrock `converse` call on `settings.MEMORY_EXPAND_MODEL` with system prompt `"List the separate facts this question needs, one per line, at most 5; if it needs one, write the question itself."` and `maxTokens 200`: `S/memory_models.py:21-23`, `:60-75`.
   - It keeps at most 5 lines. A single line, or any error, returns `[question]`: `:74-75`, `:71-73`. Recall caps the parts at `MAX_PARTS = 5`: `S/memory_recall.py:24`, `:174`.
   - Skipped on the internal (plugin) route (`use_expand=False`): `B/routers/memory.py:192`.
   - Tests: [test_memory_models: test_expand_single_line_returns_original, test_expand_multiple_lines_capped_at_5, test_expand_error_returns_original; test_memory_recall: test_recall_caps_parts_at_five, test_recall_multi_part_question_queries_each_part]
2. **Retrieval arms** (`_search_part`, `S/memory_recall.py:114-149`). Each part runs:
   - Facts namespace via `RetrieveMemoryRecords`, filter `status EQUALS_TO current` and `status EQUALS_TO contested`, each times the as-of variants: `:122-124`.
   - Turns namespace, filtered only by `x-amz-agentcore-memory-createdAt BEFORE <as_of ISO-UTC>` when `as_of` is set: `:127-128`.
   - Entries namespace, with the same created filter: `:129`.
   - Private namespace, as-of variants only and no status filter, when included: `:130-132`.
   - For each workspace entity named in the part (at most `MAX_ENTITIES = 5`): `ListMemoryRecords` on facts with `entity EQUALS_TO <e>` times the variants: `:133-147`, `:25`, `:109-111`.
   - The as-of variants are `[validFrom <= t, validTo NOT_EXISTS]` and `[validFrom <= t, validTo > t]`, with t in epoch seconds: `:50-55`.
   - System fields use `dateTimeValue`: `:39-40`, `:45-47`.
   - `topK = limit` (default 100; route accepts 1-200): `:72-77`, `B/routers/memory.py:100`.
   - The entity set is cached 60 s per (memory, facts ns), and a failed listing is not cached: `S/memory_recall.py:28`, `:96-106`.
   - Listing is capped at `MAX_PAGES = 10`: `:29`, `:80-93`.
   - At most 16 concurrent calls per event loop: `:30-31`, `:58-69`.
   - A failed arm is dropped, but `ParamValidationError` propagates: `:65-69`.
   - Tests: [test_recall_only_queries_own_namespaces, test_recall_private_only_when_included, test_recall_as_of_filters_are_sent, test_as_of_filters_turns_and_entries_by_creation, test_as_of_is_sent_as_iso_utc_and_a_naive_time_is_utc, test_recall_entity_listing_adds_all_records_for_entity, test_recall_caps_entities_at_five_per_part, test_recall_failed_arm_does_not_fail_recall, test_param_validation_error_is_not_swallowed, test_listing_is_capped_in_pages_and_records, test_entity_cache_skips_failed_listing]
   - Post-fetch filtering:
     - Records with status `replaced` are dropped: `:195`.
     - A fact replaced after `as_of` is restored to current: `:152-156`. [test_recall_as_of_keeps_fact_replaced_after_it, test_recall_prefers_current_over_replaced]
     - The question's time window ("today", "yesterday", "last week", "last month", month names, ISO dates, years) narrows only turns and entries, never the current kinds: `:184`, `:192-197`; `S/memory_stages.py:234-263`. [test_recall_time_window_filters_after_fetch, test_time_window_never_drops_a_current_claim, test_memory_stages: test_time_window_month_and_year]
   - `from_record`:
     - Maps metadata and parses the speaker.
     - A record with `logKind` or a log kind becomes `kind="entry"`.
     - Text starting `Contested: ` sets status contested.
     - A `validTo` turns a current record into `replaced`.
     - Lines: `S/memory_stages.py:64-96`. [test_from_record_maps_metadata_and_speaker, test_from_record_parses_contested_form, test_from_record_entry_keeps_subject_and_metadata_author, test_from_record_mirrored_entry_takes_the_entry_branch_by_log_kind]
3. **Fusion.** Reciprocal rank fusion with `k = 60` over every arm's ranking, keeping the top `RERANK_TOP = 100`: `S/memory_stages.py:99-105`, `S/memory_recall.py:203`, `:26`. [test_rrf_k60_matches_hand_computation]
4. **Rerank.**
   - Cohere `cohere.rerank-v3-5:0` through `bedrock-agent-runtime.rerank`, at most 100 documents, `numberOfResults = min(top_n, len)`: `S/memory_models.py:19`, `:31-57`.
   - Skipped when `use_rerank` is False (no caller sets it) or there are no candidates. A failure returns None and keeps the RRF order: `S/memory_recall.py:207-212`.
   - After a rerank, if fewer than `MIN_POSITIVE = 5` results score above 0, only the positive ones are kept: `:213-216`, `:27`.
   - Client: connect 3 s, read 10 s, 1 attempt: `S/memory_models.py:24`.
   - Tests: [test_rerank_returns_indices_sorted_by_score, test_rerank_failure_returns_none, test_rerank_caps_documents_at_100, test_rerank_empty_documents_skips_call, test_rerank_clamps_top_n_to_document_count, test_recall_without_rerank_uses_rrf_order, test_recall_rerank_failure_keeps_rrf_order, test_recall_scores_below_floor_return_only_positive]
5. **Composition** (`compose`, `S/memory_stages.py:195-212`).
   - Order: group 0 is contested, group 1 the current kinds `claim|observation|rule|anchor|mention`, group 2 everything else (`turn`, `entry`). Within a group, by score descending: `:198-203`, `:14`. Deliberate log entries (`entry`) therefore rank after AgentCore claims (inference).
   - "Contested" has two sources. First, the record text starts `Contested: ` (`:70`). Second, `mark_contested` finds more than one distinct value for the same key (`entity attribute`, else the subject before `: `) among non-replaced, non-turn, non-mention candidates, which includes mirrored entries: `:141-165`.
   - Budget: results are added until the estimated tokens of `json.dumps(page)` exceed `budget_tokens`. The estimate is `len // 4`, not tiktoken: `:39-41`, `:187-212`.
   - Tests: [test_compose_orders_contested_then_current, test_compose_marks_contested_across_namespaces, test_compose_stops_at_budget, test_compose_parses_history_tail]
- **Returned shape:** `{"asOf", "usedEvidenceTokens", "results": [{rank, kind, content, occurredAt, status, history[{value, speaker, occurredAt}], sources[{sourceId, occurredAt, speaker, excerpt (280 chars)}]}]}`: `S/memory_stages.py:168-184`, `:204`.
- Only stage timings are logged (`expansionMs` through `composeMs`): `S/memory_recall.py:221-228`.

**Callers**
- Member route `POST /api/v1/memory/recall` (Clerk auth, private scope included, expansion on): `B/routers/memory.py:120-124`, mounted at `B/main.py:1089`. [test_recall_requires_member, test_recall_starts_the_team_sync_and_recalls]
- Internal route `POST /api/v1/internal/memory/recall/{owner_id}`: requires the hook token (`container_auth._require_hook_token`), takes an optional `member` that must match `user_[A-Za-z0-9]+` (else 422), and runs with no expansion: `B/routers/memory.py:183-192`, `:111-113`, mounted at `B/main.py:1090`. [test_internal_recall_runs_as_the_workspace, test_internal_recall_in_a_dm_adds_that_persons_private_scope, test_internal_routes_refuse_without_the_hook_token]
- Hosted MCP tool `recall(question)` calls `recall_page(auth.org_id, auth.user_id, question)`. `ValueError` and `RuntimeError` become `ToolError`: `S/hosted_mcp.py:428-440`. The spec is in `S/network_tools.json` ("Call when a task starts or you need something a teammate may know"). [test_hosted_recall_tool_rejects_a_bad_question_as_a_tool_error]
- Hermes plugin:
  - Passive `prefetch` before each turn: the user's text only, budget 400, at most 1600 characters injected, 3 s backend timeout: `I/hermes/plugins/memory/agentcore/__init__.py:63-67`, `:271-290`.
  - Tool `workspace_memory_search`: budget 1500, output at most 6000 characters: `:340-370`.
  - In a 1:1 DM with a person it sends `member`: `:263-269`, `:244-248`.
  - The plugin no longer reads AgentCore directly; it goes through the backend: `:20-25`.
  - Tests: [test_memory_plugin_recall: test_prefetch_query_is_user_text_only, test_prefetch_injects_page_lines_under_budget, test_prefetch_caps_injected_block, test_dm_recall_names_the_person_and_a_room_does_not, test_recall_query_is_cut_to_the_backends_limit, test_search_tool_reads_through_the_backend]
- The Memory tab does NOT call recall (section 8).

---

## 3. Sync (`S/memory_sync.py`)

**Directions and namespaces** (`:3-15`)
1. facts -> log: AgentCore facts in `/owners/<o>/` become `memory.import` entries in the network log on the cloud member: `:141-221`.
2. log -> entries: `memory.list` entries are mirrored into `/entries/<o>/`, or into the person's private scope for a `me` entry with a `user_` `on_behalf_of`: `:241-363`.
3. events -> turns: workspace-memory events under `actorId = <owner>` (team only) become records in `/turns/<o>/`: `:376-413`.
- Private-scope facts are never imported. `_owned` requires the exact team namespaces: `:421-423`, `:475`. [test_sync_skips_private_namespace]

**State rows** (messaging table, pk `OWNER#<o>`; `B/core/repositories/messaging_repo.py:170-171`), `S/memory_sync.py:17-19`
- `MEMSYNC`: `records_after`, `entries_seq`, `events_after`, `synced_at`, `lease_until`.
- `MEMLINK#<facts rec>`: `entry`, `hash`.
- `MEMENTRY#<entry>`: `arec`, `facts_rec`.
- `MEMTURN#<turn>`: `expiresAt` at 8 days: `:42`, `:409-411`. [test_mirrored_turn_rows_expire]

**Watermarks**
- Facts: `records_after` = the sync start time (`utc_now_iso`), set only when every chunk was sent. The next listing filters `x-amz-agentcore-memory-updatedAt AFTER <ISO Z>` (`dateTimeValue`): `:143-152`, `:220`, `:454`. A record whose SHA-256 text hash equals its MEMLINK hash is skipped: `:157-160`. [test_second_sync_filters_on_the_updated_at_system_field, test_sync_rerun_imports_nothing_new]
- Entries: `entries_seq` is saved after every flushed batch: `:311-351`. [test_entries_seq_is_saved_per_flushed_batch]
- Turns: `events_after` = the newest event timestamp. Events below it are skipped and already-mirrored turns are skipped by their MEMTURN row: `:376-413`. [test_sync_mirrors_events_into_turns, test_repeated_turn_sync_creates_nothing_new]

**Import entry shape**
- `{subject: "<entity> <attribute>", fact: <value>, kind: rule -> decision else fact, scope: "team", when: validFrom (epoch s), key: "<recId>:<hash>", source: {harness: "workspace", repo: null, session: null, on_behalf_of: <speaker display name>}, replaces: [prior entry]}`: `:163-178`, `:45`.
- `on_behalf_of` here carries the speaker's display name from the text (`parse_fact`, `:99-106`), not a user id (inference).
- Tests: [test_sync_imports_new_facts_as_entries, test_sync_rewrite_becomes_replace_entry, test_import_carries_idempotency_key, test_import_maps_every_agentcore_kind_and_reads_a_number_valid_from, test_undated_fact_imports_with_no_when, test_import_sends_entries_in_batches]
- If the member refuses some entries, only the index and reason are logged and the watermark still advances: `:208-214`. [test_import_skips_refused_entries_logs_them_and_advances_the_watermark]

**Idempotency**
- Batches of 100: `:39`.
- `clientToken = sha256(sorted requestIdentifiers)`: `:80-82`, used at `:276-278` and `:406-408`.
- `requestIdentifier` is the entry id (entries) or `sha256(sid:eventId:i)` (turns): `:259`, `:371-373`.
- An entry already mirrored (`arec`) or imported (`facts_rec`) is not created again: `:344-347`. [test_already_mirrored_entry_is_not_created_again, test_imported_fact_is_not_mirrored_back_into_entries]
- `memory.import` itself is idempotent on `key`: `:174`.
- The mappings of the successful records are kept before raising for any failed ones: `:279-283`. [test_partial_create_failure_keeps_mappings_of_the_rest]
- Metadata string values that AgentCore would refuse are dropped: `:47`, `:75-77`. [test_metadata_leaves_out_values_agentcore_would_refuse]

**Mirrored entry record**
- Text `"<subject>: <fact>"`. Metadata `subject`, `status=current`, `kind=entry`, `logKind`, `author`, `entryId`, `validFrom` (NUMBER): `:256-274`.

**`me` vs `team` routing** (`_entry_namespace`, `:241-248`)
- `scope == "me"` with an `on_behalf_of` matching `user_[A-Za-z0-9]+` goes to `/owners/<o>:<u>/`.
- `me` with an `on_behalf_of` that is not a user id is not mirrored.
- Everything else goes to `/entries/<o>/`, including a `me` entry with no `on_behalf_of`. Through the internal route, an agent in a room can call `remember` with `scope:"me"` and no `on_behalf_of` (`I/hermes/plugins/memory/agentcore/__init__.py:376-380`), and that entry is mirrored into the team-searchable namespace (inference). [test_entries_route_by_scope]

**Replace and forget propagation**
- A `remember` op's `replaces` sets the mirrored records' metadata `status=replaced` through `BatchUpdateMemoryRecords`: `:286-293`, `:342`. [test_sync_replace_marks_replaced_record_status]
- A `forget` op deletes the entry's `arec` and `facts_rec` records (a not-found counts as done) and the MEMLINK and MEMENTRY rows: `:296-308`, `:338-340`. [test_sync_mirrors_entries_and_tombstones, test_sync_tombstone_of_imported_entry_deletes_facts_record, test_tombstone_retry_tolerates_already_deleted_records]
- A tombstoned row (no body) is skipped but advances seq: `:336-337`. [test_tombstoned_row_is_skipped_and_the_watermark_still_advances]

**When sync runs**
- On every recall: `sync_if_stale(max_age_s=60)` in the background: `S/memory_api.py:81-88`, `S/memory_sync.py:483-489`.
- Sweep: `run_memory_sweeper` is started in the lifespan (`B/main.py:401`, `:416`). It ticks every 60 s and runs `sweep_once` when `container_repo.try_claim_pass("memory_sweep", interval_s=300)` wins: `S/memory_api.py:28-29`, `:128-139`.
- `sweep_once` scans the whole containers table. Per owner it runs `retry_member_wipes`, then `sync_if_stale(max_age_s=300)`, then `evaluator.collect_due()`: `:97-125`. [test_sweep_syncs_only_stale_workspaces, test_the_sweep_retries_owed_wipes_for_every_workspace]
- Lock: a per-owner lease `lease_until` (300 s) set by conditional update. A concurrent caller returns zeros at once. The lease is released in `finally` and also by the state put: `S/memory_sync.py:426-480`, `:465-468`. [test_concurrent_sync_returns_at_once_while_lease_is_held, test_sync_releases_lease_even_when_it_fails]
- Parked member: steps 1-2 stop and `synced_at` is not set, so the next recall or sweep retries. Turns are still mirrored: `:452-469`. [test_sync_when_parked_leaves_watermark, test_sync_if_stale_noop_within_max_age]
- Each sync first drains queued ops: `:456`. Queued ops are also drained when `network.sessions` refreshes a warm member: `S/network_member.py:143`.
- One-time import script: `B/scripts/memory_import.py:17-25`. It calls `memory_sync.sync_workspace` (same lease and watermark, safe to re-run) and returns `imported`. It runs as `uv run python -m scripts.memory_import <owner_id>`. No test.

**Bridge RPC to the cloud member**
- `network_member.call_op` goes over `container_rpc.call` only when the runtime is warm (`_session_is_warm`), and returns None when parked; it never cold-starts: `S/network_member.py:536-547`, `:165-171`. The timeout floor is `AGENTCORE_INGRESS_TIMEOUT_S`, sent as `timeoutMs`: `S/container_rpc.py:67`, `:76`. [test_call_op_returns_none_when_parked, test_call_op_sends_over_the_runtime_rpc_when_warm]
- The bridge registers `memory.import`, `memory.list`, `memory.remember`, `memory.forget` and `memory.forget_author`. Params pass through to `network.ask(method, params, timeout_ms/1000)`: `I/hermes/bridge/server.py:531-540`. [test_network_rpc: test_memory_ops_reach_the_engine_with_their_params_unchanged, test_memory_op_refused_by_the_engine_is_ok_false, test_memory_ops_when_off_answer_unavailable, test_memory_op_passes_the_backends_timeout_to_the_engine]
- Payload shapes, as the backend sends and reads them:
  - `memory.import`: `{"entries": [<import entry>]}` with a 60 s timeout. Answer `{"ids": [id|null] (same length), "refused": [{index, reason}]}`: `S/memory_sync.py:202-215`.
  - `memory.list`: `{"since_seq": n}` with a 30 s timeout. Answer `{"entries": [{id, seq, at, author, tombstone?, op: {t: remember|forget, subject, fact, kind, scope, when, source{on_behalf_of}, replaces, target}}], "next"}`: `:356-363`; `S/memory_api.py:214-226`.
  - `memory.remember`: `{subject, fact, kind, scope, replaces?, on_behalf_of?}`. Answer `{"stored": bool, "refused": "secret:<name>"|"too_long"|"team_off"}`: `S/memory_api.py:177-190`, `:33-39`.
  - `memory.forget`: `{"entry": id}`: `:204-205`.
  - `memory.forget_author`: `{"author": key}`: `:275-277`.
- Op queue (`MEMOP#<ms>#<rand>` under `OWNER#<o>`):
  - Params are stored as JSON text and expire after 30 days: `S/network_member.py:550-563`. [test_a_queued_op_expires_after_thirty_days]
  - Each row is claimed for 120 s with a conditional write. A parked member stops the drain. Up to 5 attempts, then the op is dropped with a warning. The drain never raises: `:561-633`. [test_queue_op_then_drain_sends_in_order_and_deletes, test_drain_skips_unclaimable_rows, test_drain_retries_then_drops_a_failing_op_and_never_raises, test_drain_swallows_storage_errors]

---

## 4. Remember and import

- `memory_api.remember(owner, subject, fact, kind, scope="me", replaces, on_behalf_of)`, `S/memory_api.py:164-190`:
  - Reads up to 5 neighbours each from facts and entries via `RetrieveMemoryRecords`, dropping replaced ones: `:142-161`, `:27`.
  - Sends `memory.remember` to the member. When parked, it raises `Refused("secret")` if `subject\nfact` matches the loose `evaluator._SECRET` regex (`secret|credential|token|\bkeys?\b|api[_ -]?key|password`, case-insensitive; `S/evaluator.py:39`), otherwise it queues the op.
  - A member answer with `stored: false` raises `Refused(<reason>)`.
  - Returns `{"queued", "neighbours"}`.
  - Tests: [test_remember_returns_neighbours_and_queues_when_parked, test_remember_forwards_when_up, test_remember_answers_the_members_refusal_as_422, test_parked_remember_of_a_secret_is_refused_and_never_queued]
- Member route `POST /api/v1/memory/remember` (`RememberBody`):
  - `subject` 1-500, `fact` 1-16000, `kind` one of `fact|decision|lesson|preference`, `scope` `me|team` (default `me`), `replaces` at most 50: `B/routers/memory.py:103-108`.
  - `on_behalf_of = auth.user_id`: `:127-130`.
  - `Refused` is a `ValueError`, so `_guard` returns 422 with `"Not remembered: <reason>"`. A `RuntimeError` returns 502: `:87-93`; `S/memory_api.py:33-39`.
- Hosted MCP tool `remember`:
  - Same kinds and scopes as typed `Literal`s, but no length bounds; it relies on the member's `too_long`: `S/hosted_mcp.py:443-464`.
  - Requires `WRITE_SCOPE`, `on_behalf_of = auth.user_id`, and a refusal becomes a `ToolError`.
  - Spec description: "Returns what is already known on the subject; call again with `replaces` if yours is a change. Personal unless the person says to share" (`S/network_tools.json`). [test_hosted_remember_tool_raises_the_refusal_as_a_tool_error; test_hosted_mcp_spec (2 lines added)]
- Plugin channel:
  - The tool `remember` runs `redact_for_egress` on subject and fact, so secrets are scrubbed, not refused.
  - Default scope is `me` in a DM, `team` in a room. In a DM it sets `on_behalf_of` to the member.
  - It posts to `POST /api/v1/internal/memory/remember/{owner}`.
  - A 422 is shown to the agent as `{"error": <detail>}`.
  - Lines: `I/hermes/plugins/memory/agentcore/__init__.py:320-338`, `:372-396`. [test_remember_redacts_subject_and_fact, test_remember_tool_forwards, test_remember_refusal_is_surfaced_to_the_agent]
- Internal hook-token routes:
  - `/recall/{owner_id}` (section 2) and `/remember/{owner_id}` (`InternalRememberBody` = `RememberBody` + `on_behalf_of` at most 200 characters, taken from the payload unvalidated): `B/routers/memory.py:116-117`, `:195-198`. [test_internal_remember_takes_on_behalf_of_from_the_payload]
- Event-write path (not a remember): the plugin's `sync_turn` writes every turn as a `CreateEvent` under actor `<owner>` or `<owner>:<member>` (1:1 DM with a person), with text redacted and capped at 100,000 characters: `I/hermes/plugins/memory/agentcore/__init__.py:296-318`. [test_memory_agentcore: test_sync_turn_redacts_secrets_before_they_reach_shared_memory, test_a_dm_turn_from_a_person_writes_their_private_actor, test_every_other_turn_writes_the_team_actor]
- Secret handling summary (inference):
  - The member refuses secrets on `memory.remember` and `memory.import` (the engine's list).
  - The backend refuses only on the parked path, with the loose regex.
  - The plugin scrubs.
  - Event text that AgentCore extracts from, and mirrored turns, are scrubbed by `redact_for_egress`, not refused.
- Review acts on records:
  - `GET /memory` lists the private and team facts namespaces only (newest first, cap 200 per scope, `truncated`): `S/workspace_memory.py:312`, `:345-398`.
  - `DELETE /memory/{id}`: the id must match `mem-[A-Za-z0-9_-]{36,46}`, else 404. It first forgets the linked log entry (via MEMLINK), then deletes the record (scope-checked by `GetMemoryRecord`), then drops the mirror: `B/routers/memory.py:27`, `:52-65`; `S/workspace_memory.py:361-413`.
  - `POST /memory/{id}/share` and `/confirm`: a vouched copy into team with the prefix `"Confirmed by a person: "` and a deterministic clientToken, then the original is deleted. Over 16000 characters gives 422: `B/routers/memory.py:68-84`; `S/workspace_memory.py:316-317`, `:416-473`.
  - Tests: [test_memory_router: 9 tests; test_workspace_memory_review: 17 tests; test_delete_forgets_the_entry_and_drops_the_mirror, test_delete_sends_the_forget_before_deleting_the_record_and_survives_a_failing_call, test_delete_without_mirror_rows_sends_no_forget, test_forget_queues_when_parked, test_forget_queues_when_the_call_raises]
  - (inference) `entry_for` reads only MEMLINK rows, which exist only for imported facts (`S/memory_sync.py:183-192`, `:224-226`). Deleting a private-scope record that mirrors a `me` log entry therefore removes the search mirror only. The log entry stays on the member and on computers, and its MEMENTRY `arec` row keeps it from being re-mirrored.

---

## 5. Sessions

- Routes, mounted at `B/main.py:1093-1096`:
  - `POST /api/v1/sessions`: `B/routers/sessions.py:72-91`.
  - `GET /api/v1/sessions?since=`: `:94-97`.
  - `DELETE /api/v1/sessions/{id}`: `:100-112`.
  - `POST /api/v1/sessions/{id}/share`: `:122-132`.
  - `GET /api/v1/shared/{token}` (no auth): `:135-149`.
- Caller: an `X-Lightbulb-Proof` signed computer, via `network_login.computer_identity`, which allows no workspace (`org_id=None`). Otherwise Clerk: `B/routers/sessions.py:64-69`; `S/network_login.py:456-457`, `:467-469`. [test_sessions_post_requires_identity, test_free_identity_uploads_without_workspace, test_body_user_and_workspace_ids_are_ignored]
- Validation:
  - id `^[A-Za-z0-9._:-]{1,128}$`, text and summary fields at most 2000, at most 200 sessions and at most 5000 tool calls: `B/routers/sessions.py:31-61`.
  - A 422 on `/api/v1/sessions*` returns a fixed body `{"detail": "Invalid sessions upload."}` so summaries are not echoed back: `B/main.py:628-630`.
  - Tests: [test_bad_ids_are_refused, test_over_limit_upload_is_422]
- Tiers:
  - S3 key `sessions/<free|workspace>/<user>/<id>.json` in `OWNER_STATE_BUCKET`: `S/session_store.py:43-44`.
  - Retention 30 days free, 365 in a workspace, as row `expiresAt` (the table's TTL). Expired rows read as gone before DynamoDB deletes them: `:29`, `:55-63`.
  - Tests: [test_full_upload_goes_to_s3_under_the_tier_prefix_and_is_idempotent, test_rows_expire_with_their_retention_and_read_as_gone_once_past_it]
- Storage:
  - Row `SESSIONS#<user>/S#<id>`, plus `WSESSIONS#<ws>/S#<user>#<id>` in a workspace. Fields: id, harness, computer, repo, branch, started, ended, prompt and completion tokens, user_id, full, uploaded_at, expiresAt, workspace_id: `S/session_store.py:30-32`, `:86-100`.
  - The S3 object `{session, tool_calls}` is written only for `full`. A metadata re-upload deletes it: `:90-95`.
  - A re-upload under another tier or workspace deletes the old copies first: `:73-77`. [test_reupload_under_other_tier_leaves_no_orphans, test_metadata_rows_have_no_summaries, test_mode_full_without_tool_calls_is_stored_full]
  - Workspace uploads add token deltas to usage `member:<user>:<YYYY-MM>` (`capture_prompt_tokens` and `capture_completion_tokens`): `:78-85`; `B/core/repositories/usage_repo.py:67-75`. They are shown in UsagePanel ("From your agents' sessions"). [test_usage_includes_capture_tokens_per_agent]
  - List: the workspace partition if in a workspace, else the user's own; newest first, cap 500: `S/session_store.py:28`, `:104-112`. [test_list_sessions_scoped_to_workspace]
- Full uploads trigger `evaluator.ingest_session` per session. A failure never fails the upload: `B/routers/sessions.py:80-90`. [test_upload_full_triggers_ingest_and_metadata_does_not]
- Share link:
  - Payload `"<user>|<sid>|<exp = now + 7 days>"`, base64url, followed by `.` and `HMAC-SHA256(PAPERCLIP_SERVICE_TOKEN_KEY, "session-share|" + body)`. URL `FRONTEND_URL/shared/<token>`: `B/routers/sessions.py:27`, `:115-132`.
  - Only the uploader may share (others get 404), and there is no link without the key.
  - `/shared/{token}` checks with `compare_digest`, checks expiry, then `read_shared`. The response leaves out user and workspace ids. It returns the tool calls when the session was uploaded full, and 404 when the S3 object has aged out: `:135-149`; `S/session_store.py:162-184`.
  - Tests: [test_share_link_expires_in_7_days, test_share_requires_the_uploader, test_a_share_link_whose_object_aged_out_is_404, test_a_cli_session_id_with_a_colon_round_trips_through_upload_list_and_share; shared page.test.tsx: 2 tests]
  - (inference) The share response is not redacted. `S/session_redact.py` is not imported by sessions code, so tool-call summaries, repo, branch and computer name go to anyone holding the link for 7 days. A link cannot be revoked except by deleting the session.
- Delete and its cascade:
  - Only the uploader may delete (a teammate's session is 404). Both rows and the object are removed: `S/session_store.py:153-159`, `:115-122`.
  - Then `evaluator.forget_session` runs (section 6). Its failure still returns 204: `B/routers/sessions.py:106-112`.
  - Tests: [test_delete_uploader_only, test_delete_forgets_the_session_in_the_evaluator_and_its_failure_is_still_204, test_delete_sessions_removes_everything_a_user_uploaded]
- Who may do what:
  - Upload, list and delete within the caller's workspace or their own.
  - Read via `get_session`: own sessions, or the workspace's for a member: `S/session_store.py:144-150`.
  - Share and delete: the uploader only.
  - The frontend Share button appears on every row, teammates' included, and the backend 404s those (`F/components/control/panels/SessionsTimeline.tsx:78-83`).

---

## 6. Evaluator (`S/evaluator.py`)

- **Ingest** (`ingest_session`, `:105-154`):
  - Only with `AGENTCORE_SESSIONS_MEMORY_ID` set.
  - Marker `EVAL#<user>/S#<sid>`, written with a conditional put, so a re-upload is not re-ingested: `:113-117`.
  - Free accounts (`workspace_id None`) reserve one slot of `MEMCAP#<user>#<YYYY-MM>/COUNT` atomically. The cap is `FREE_MONTHLY_CAP = 50` and the row expires after 62 days: `:35`, `:37`, `:118-121`, `:187-201`. Workspace uploads have no cap.
  - Events:
    - A `USER` head line `"<harness> session in <repo> on <branch>, started <started>"`.
    - One `TOOL` payload per tool call, `"<tool>: <summary>"`, with text capped at 9000: `:88-102`.
    - Sent in `CreateEvent`s of at most 100 payloads, stamped at the first payload's time, `actorId=<user>`, `sessionId=event_session_id(sid)`: `:126-131`.
  - Session id mapping: characters outside `[A-Za-z0-9_-]` become `-`. If anything changed or the id is longer than 100, it becomes 90 characters + `-` + 8 hex of the sha256, with an `s` prefix if it does not start alphanumeric: `:70-78`.
  - On failure: written chunks are deleted, the marker is removed and the cap slot is refunded: `:133-144`.
  - Then a due row is put and `collect_drafts` runs, best effort: `:146-154`.
  - No secret check on summaries before they go to AgentCore (inference).
  - Tests: [test_ingest_session_writes_events_with_tool_role, test_event_session_id_is_a_valid_agentcore_session_id, test_a_cli_session_id_is_mapped_for_agentcore_and_kept_in_our_rows, test_ingest_batches_at_100_payloads, test_free_account_cap_skips_after_50, test_free_ingest_counts_once, test_cap_reservation_is_atomic, test_concurrent_same_session_ingests_once, test_a_failed_chunk_drops_the_chunks_already_written, test_cap_rows_expire_after_their_month, test_an_ingest_collects_once_and_marks_the_person_due, test_a_failed_collect_never_fails_the_ingest]
- **EVALDUE**: row `EVALDUE/U#<user>#W#<ws|free>` with `expiresAt` = now + 7 days. The sweep's `collect_due` lists the partition and collects for each unexpired row: `:36`, `:38`, `:157-173`.
- **Draft collection** (`collect_drafts`, `:210-254`):
  - Lists reflections in `/users/<user>/`, keeping only records whose own `namespaces` include that exact namespace, which excludes episodes: `:204-207`. [test_collect_drafts_ignores_episode_subnamespace]
  - Takes records newer than the watermark `EVAL#<user>/WATERMARK.at`. The watermark is set to the newest `createdAt` collected: `:215-218`, `:251-253`.
  - Evidence: session ids from `SESSIONS#<user>` that appear, as the CLI id or as the AgentCore id, as a whole token in the reflection text or metadata values: `:219-234`. [test_a_reflection_naming_the_agentcore_session_id_cites_the_cli_session]
  - Classification: `kind = "finding"` if `_secret_shaped(text)`, else `"lesson"`. `_secret_shaped` is the keyword regex `_SECRET` (`:39`) OR any of `SECRET_PATTERNS`, copied from the CLI's `src/memory.rs`: `:40-61`. The patterns:
    - `credential` `(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*\S{8,}`
    - `private_key` `-----BEGIN [A-Z ]*PRIVATE KEY-----`
    - `aws_key` `AKIA[0-9A-Z]{16}`
    - `api_key` `sk-[A-Za-z0-9_-]{20,}`
    - `vendor_key` (Stripe `sk_/rk_live|test_`, GitHub `gh[pousr]_`, `github_pat_`, Slack `xox[abprs]-`, Google `AIza...`, GitLab `glpat-`)
    - `url_credentials` `https?://[^\s/]+:[^\s@]+@`
    - `jwt` `eyJ...\....`
  - Row `DRAFTS#<user>/D#<recId>` with `{id, user_id, kind, text, evidence, status: "proposed", created, workspace_id?, mirrored}`: `:235-245`.
  - `mirrored` = it is a lesson AND it cites at least one of the person's sessions in this workspace. Only then is `WDRAFTS#<ws>/D#<user>#<id>` written: `:241-250`.
  - An already-collected draft is never reset, but a missing mirror is repaired: `:246-250`.
  - Tests: [test_collect_drafts_creates_rows_from_new_reflections, test_a_workspace_collect_mirrors_only_lessons_drawn_from_its_sessions, test_a_credential_shaped_reflection_is_a_finding_never_mirrored, test_a_missing_workspace_mirror_is_repaired_on_the_next_collect]
- **Listing**:
  - `GET /api/v1/memory/drafts` (caller = computer or Clerk) returns `WDRAFTS#<ws>` plus the caller's own unmirrored drafts in this workspace, or `DRAFTS#<user>` when free. Status `proposed`, newest first: `B/routers/memory.py:145-147`; `S/evaluator.py:261-275`.
  - `find_draft`: `:278-288`. Another caller's draft or a settled one is 404: `B/routers/memory.py:137-142`. [test_other_callers_draft_is_404]
- **Approve** (`POST /memory/drafts/{id}/approve`, body `{scope?}`), `B/routers/memory.py:150-173`:
  - Lessons only (a finding gives 422).
  - Default scope is team in a workspace, me when solo.
  - Subject = the repo of the first evidence session, else its harness, else `"lesson"`.
  - The status flips to `approved` first. It is conditional on the author row being `proposed`, the mirror is forced to the same status, and a loser gets 409: `S/evaluator.py:291-329`.
  - In a workspace, `memory_api.remember(org, subject, fact=draft text, kind="lesson", scope, on_behalf_of=approver)` writes a network-log entry on the cloud member, which the sync mirrors to `/entries/<o>/` (team) or `/owners/<o>:<approver>/` (me). On any exception, including cancellation, the draft reverts to `proposed`.
  - Solo: nothing is written. The route returns `{subject, fact, kind, scope}` for the CLI to store locally.
  - Tests: [test_approve_in_workspace_signs_as_workspace_with_approver, test_approve_in_workspace_failure_reverts_both_rows_to_proposed, test_approve_refused_by_the_member_reverts_the_draft_to_proposed, test_approve_races_to_409_and_remembers_once, test_approve_solo_returns_entry_for_local_store, test_a_finding_cannot_be_approved, test_mirror_out_of_step_is_forced_to_the_author_rows_status]
- **Dismiss**: a conditional flip to `dismissed`, 204, or 409: `B/routers/memory.py:176-180`. [test_dismiss_marks_row]
- **forget_session**: removes the marker `EVAL#<user>/S#<sid>`, the session's events in the sessions memory, and the sid from the evidence of the person's drafts (free plus this workspace). A draft left with no evidence is deleted, both copies. Reflections stay. It never raises: `S/evaluator.py:356-401`. [test_forget_session_drops_its_marker_events_and_evidence, test_forget_session_never_raises_and_still_drops_the_marker]

---

## 7. Wipes

- **Member leaves** (Clerk `organizationMembership.deleted`): `delete_member_memory(org, user, still_gone=still_current)` in its own try. A failure raises so the event is redelivered: `B/routers/webhooks.py:601-614`. Steps run via `_run_all`, so every step runs and the error is raised afterwards (`S/workspace_memory.py:207-216`), at `:257-289`:
  - Wipe the AgentCore records in `/owners/<o>:<u>/` now, raising on any failure. The tail (events of actor `<o>:<u>`, then a re-sweep 180 s later) runs only while `still_gone()` holds: `:149-205`, `:282-288`, `:47`.
  - `session_store.delete_sessions(user, workspace_id=o)`.
  - `evaluator.delete_drafts(user, workspace_id=o)`: drafts in that workspace, both copies, and the due row: `S/evaluator.py:336-353`.
  - `_forget_authored`: `memory.forget_author` for every key in `network_login.computer_keys(user)` (the NETKEY registry, plus live routes, plus signed-out keys), then `forward_on_behalf_of`: `S/workspace_memory.py:248-254`; `S/network_login.py:331-338`.
  - Team memory, episodes and computer keys stay. A non-`user_` id is a no-op: `:270-272`.
  - Tests: [test_member_wipe_deletes_only_that_members_private_scope, test_member_tail_runs_only_while_the_member_is_still_gone, test_member_wipe_skips_ids_that_can_have_no_private_scope, test_member_wipe_runs_every_step_when_one_fails, test_member_wipe_forwards_forget_author, test_member_wipe_removes_only_what_belongs_to_that_workspace; test_webhooks_paperclip: test_membership_deleted_calls_archive_member]
- **Key registry**:
  - `NETKEY#<user>/NETKEY#<key>` with `first_seen`, written by a conditional put. It never raises: `S/network_login.py:300-320`.
  - Written on approve (`:203`), refresh (`:226`) and route connect (`S/network_route.py:256`).
  - `forget_keys` deletes the rows: `S/network_login.py:323-328`.
  - Tests: [test_proof_registers_member_key, test_computer_keys_union_registry_live_and_signed_out]
- **MEMWIPE retry and wake**:
  - `forward_on_behalf_of` pages `memory.list` and sends `memory.forget` for each `remember` whose `source.on_behalf_of == user`: `S/memory_api.py:208-226`.
  - If the member is parked or the call fails, it puts `MEMWIPE#<o>/U#<user>` with `expiresAt` 30 days and calls `network_member.wake(o)`. It never raises: `:229-252`; `S/network_member.py:470-480`.
  - `retry_member_wipes` (per owner, each sweep) retries each unexpired row and deletes the row when it succeeds: `S/memory_api.py:255-272`. [test_a_parked_member_is_owed_the_wipe_forward_and_the_sweep_finishes_it, test_forget_on_behalf_of_pages_the_log_and_forgets_only_that_persons_entries]
  - (inference) AgentCore-imported team facts carry `on_behalf_of = <display name>` (`S/memory_sync.py:175`), so the leaver's spoken facts are not matched. That is consistent with "team memory stays".
- **Owner wipe**, `delete_owner_memory`, called last and unswallowed in runtime teardown with `delete_profiles`: `S/hermes_provisioning.py:663-670`. Contents at `S/workspace_memory.py:219-245`:
  - `evaluator.delete_workspace_drafts(o)`, run first because it finds people from the WDRAFTS and WSESSIONS rows: `S/evaluator.py:404-415`.
  - `session_store.delete_workspace_sessions(o)`: `S/session_store.py:136-141`.
  - Records in facts, turns, entries, every `/owners/<o>:*` namespace found by a `namespacePath="/owners/"` scan, and every `ListActors` `<o>:*`. The events tail covers the owner and members, with a re-sweep at 180 s: `S/workspace_memory.py:124-146`, `:228-236`.
  - It does not send `memory.forget` to the member or the computers. The log copies on members' computers are not touched by this function (inference).
  - The sync and queue rows under `OWNER#<o>` (MEMSYNC, MEMLINK, MEMENTRY, MEMTURN, MEMOP) are removed only by account wipe's `delete_owner_partition` (`S/account_wipe.py:1050`), not by `delete_owner_memory`. `MEMWIPE#<o>` sits in its own partition and only expires (inference).
  - Tests: [test_owner_wipe_covers_three_namespaces, test_owner_wipe_also_deletes_every_member_scope_and_nothing_of_a_prefix_neighbour, test_a_record_that_fails_to_delete_fails_the_wipe, test_wipe_refuses_an_owner_id_that_would_widen_the_namespace, test_owner_wipe_deletes_the_workspaces_sessions_and_drafts_without_memory, test_owner_wipe_deletes_every_members_workspace_sessions_and_drafts_directly; test_webhooks_paperclip: test_last_member_leaving_a_parked_workspace_still_reaches_the_teardown]
- **Account deletion**, `delete_person_memory(user)`, `S/workspace_memory.py:292-308`. It deletes all sessions, all drafts, the EVAL rows, the due rows, the current month's cap row, the episodes (records in `/users/<u>/` and `/users/<u>/episodes/`, events under actor `<u>`, re-sweep tail), and the NETKEY registry: `S/evaluator.py:336-353`, `:418-457`. It is called from:
  - Clerk `user.deleted` (a failure fails the event): `B/routers/webhooks.py:938-942`.
  - Admin account wipe step `person_memory`: `S/account_wipe.py:1065-1073`, preview at `:704-711`.
  - `DELETE /debug/user-data`: `B/routers/debug.py:279`.
  - Tests: [test_person_wipe_removes_everything_of_the_persons_own, test_delete_episodes_removes_events_and_records; test_account_wipe: test_person_memory_is_its_own_step_and_fails_alone; test_webhooks_paperclip: test_user_deleted_fails_the_event_when_the_person_wipe_fails; test_debug (person-memory stub)]
  - It does not forward forgets for the person's log entries in workspaces (inference).
- **Sweep**: see section 3. Wipe tails run in-process and are lost if the task stops within about 180 s: `S/workspace_memory.py:199-204`.

---

## 8. Frontend Memory tab (`F/components/control/panels/AgentMemoryTab.tsx`)

- Calls:
  - `GET /memory`: `:57-61`.
  - `GET /memory/drafts`: `:62-66`.
  - `GET /sessions`, only when there are drafts, so evidence ids can link to the viewer's own sessions: `:70-75`.
  - `POST /memory/{id}/share`: `:141`.
  - `POST /memory/{id}/confirm`: `:145`.
  - `DELETE /memory/{id}`, after a `window.confirm`: `:147-155`.
  - `POST /memory/drafts/{id}/approve|dismiss` with `{}`: `:110-120`. Approve therefore always uses the default scope; the tab has no scope picker.
- Shows:
  - "Suggested" drafts, with a kind chip, evidence links and Approve (lessons only) / Dismiss: `:167-187`, `:213-219`.
  - "Only you" (private) with Share with team: `:220`.
  - "Team" with Confirm when not confirmed: `:221`.
  - Badges for Confirmed, Contested and Pending, and history lines ("Was" or "Also:"): `:136-162`.
  - A "Contested only" filter: `:207-210`.
  - "Showing 200 of them." when truncated: `:222`.
- A module-level 90 s optimistic overlay per `org:user` scope, with one refetch at 90 s: `:20-46`, `:80-107`.
- It does not call recall. `GET /memory` lists only `/owners/<o>/` and `/owners/<o>:<u>/` (`S/workspace_memory.py:384-398`). Team log entries (approved team lessons, `remember scope:"team"`) are mirrored to `/entries/<o>/`, which the tab never lists. The code comment "The approved entry reaches the list above ~60 s later" (`:109`) holds only for `scope:"me"` approvals (inference).
- Mounted as the inspector's "memory" tab. A new "Sessions" tab renders `SessionsTimeline` (`GET /sessions`, `POST /sessions/{id}/share`): `F/components/control/panels/AgentInspectorPanel.tsx`, `F/components/control/panels/SessionsTimeline.tsx:30-48`. The public viewer `F/app/shared/[token]/page.tsx:22-27` fetches `BACKEND_URL/shared/<token>`.
- Tests: AgentMemoryTab.test.tsx has 20 tests, e.g. ["renders suggested drafts with evidence links", "approves a draft, removes it locally and refetches the drafts", "renders contested badge and history line", "does not show org A's overlay rows in org B"]. SessionsTimeline.test.tsx has 1 test.

---

## 9. Security page claims (CLAIMS, `F/app/security/page.tsx:21-76`)

The header comment says these rows depend on a privacy-policy rewrite that "must land before this page ships": `:11-13`. Tests in `F/__tests__/app/securityPage.test.tsx` (4 tests): "carries the claims table with evidence on every row", "says what leaves your machine: memory, capture and upload", "names no competitor and claims no compliance".

| line | claim (verbatim) | evidence text (verbatim, abridged only where marked) | code check |
|---|---|---|---|
| 23 | Your keys never leave your computer | "The computer's key is a file only you can read and it is never uploaded. A cloud member's key comes from a secret at boot and is never written to storage." | CLI/engine code; not in this repo's diff |
| 28 | Relays see ciphertext, keys, addresses, timing and sizes, never text | "Messages are encrypted end to end between member keys. A relay carries the ciphertext when a direct route fails." | not in diff |
| 33 | On the free network, memory lives on members' computers and moves only between them | "...In a workspace, the workspace's always-on member keeps a copy, and the facts it mirrors for search are reachable only by that member and our backend. A workspace wipe deletes them." | Mirrors in `/entries` and `/turns` are reachable only by the backend role (runtime policy names only `/owners/...`, `B/core/billing/bedrock_aip.py:245-252`). Team facts in `/owners/<o>/` are readable by the runtime role. The owner wipe deletes the mirrors (`S/workspace_memory.py:225-236`) |
| 38 | Nothing from a laptop session leaves without sign-in and a choice | "Capture is local by default. Upload is a setting you choose once at sign-in: keep local, metadata only, or full. You can change it with lightbulb sessions upload." | upload requires proof or Clerk (`B/routers/sessions.py:64-69`); the setting lives in the CLI |
| 43 | Secrets are refused before they are stored | "...its text is checked against a list of patterns: API keys, tokens, private-key blocks and credentials in URLs. A match is refused with the reason, not saved with the secret scrubbed out." | Holds for log entries the member checks. Not for AgentCore events and turns (scrubbed by `redact_for_egress`, plugin `:305`), plugin remembers (scrubbed, `:376`), session uploads and evaluator events (no check, `S/evaluator.py:88-131`), or queued ops (the loose regex only, `S/memory_api.py:185`) |
| 48 | Every fact carries who, where and when; disagreements are shown, not settled | "Each entry is signed by its author and records the tool, repo and session it came from, and when. When two people hold different current values, recall shows both with names and dates and marks the fact contested. Neither overwrites the other." | contested ordering and marking: `S/memory_stages.py:150-212`; prompt `memory-consolidation.md:10-12` |
| 53 | You can see exactly what your agent was told | "lightbulb memory last shows what was injected at the last session start. The block is logged on your computer." | CLI; not in this repo |
| 58 | Forgetting reaches every copy that comes online | "A forget is a signed tombstone that keeps the id and drops the content on every copy as each computer receives it. The one limit: a computer that stays offline forever keeps what it held before the forget..." | the backend mirror drops on a forget (`S/memory_sync.py:296-308`); see the section 4 inference on private-record delete |
| 63 | Agents ask before they spend | "Spend gates and approvals are shipped." | not memory |
| 67 | Teammates see presence, not your sessions or files | "...In a workspace, uploaded sessions appear in the workspace timeline to its members, because the workspace owns that record." | `S/session_store.py:104-112` |
| 72 | In a workspace, a wipe deletes everything, including the extracted facts | "A workspace wipe removes the workspace's memory, its uploaded sessions and the drafts the evaluator suggested, along with the rest of its data." | `S/workspace_memory.py:219-245` |

The "What leaves your machine" section (`:172-200`) adds that uploads are kept "30 days on a free account and a year in a workspace". This matches `S/session_store.py:29` and `I/lib/stacks/container-stack.ts:636-637`.

---

## Open at deploy time (only a deployed run can confirm)

1. **Custom metadata on `BatchCreateMemoryRecords`.** `S/workspace_memory.py:314` and `:420` say "AgentCore drops custom record metadata (spike S2)". Yet the sync writes `status`, `kind`, `logKind`, `author`, `entryId`, `subject` and `validFrom` on entry records (`S/memory_sync.py:262-273`) and `speaker`/`session` on turns (`:402`). It also relies on `BatchUpdateMemoryRecords` metadata `status=replaced` (`:291`). If S2 still holds:
   - replaced entries keep showing;
   - `logKind` and `author` are lost;
   - the private-scope as-of filter (`validFrom <=`) drops every mirrored `me` entry.
   No test can tell; the tests use stubs.
2. **System-field filters.** It is unconfirmed whether `x-amz-agentcore-memory-createdAt BEFORE` and `x-amz-agentcore-memory-updatedAt AFTER` with `dateTimeValue` are accepted (`S/memory_recall.py:39-40`, `:127`; `S/memory_sync.py:146-152`). The same goes for `NOT_EXISTS` and `GREATER_THAN` on NUMBER `validTo` and `EQUALS_TO` on `status` and `entity` (`S/memory_recall.py:50-55`, `:124`, `:143`).
3. **`namespace` exact vs prefix.** The code comments disagree. `S/workspace_memory.py:26-27` says "NOT a prefix in practice". `S/memory_sync.py:155` and `S/workspace_memory.py:346` say "the listing matches by prefix". Both filter defensively. Recall's `RetrieveMemoryRecords` does no namespace post-filter (`S/memory_recall.py:72-77`). If `/owners/<o>/` matched as a prefix, it would also pull in `/owners/<o>:<u>/` private records in team recall. The `/turns` and `/entries` siblings were chosen so they cannot collide (`S/workspace_memory.py:62-63`).
4. **Consolidation output.** It is unconfirmed whether `customMemoryStrategy.semanticOverride` with `appendToPrompt` plus `memoryRecordSchema`/`indexedKeys` deploys through CFN, and whether the model writes the `Contested:` and `(since ...; was ...)` forms that `S/memory_stages.py:15-17` and `:108-138` parse, and fills `status`, `validFrom` and `validTo` (`I/lib/stacks/container-stack.ts:274-363`).
5. **Model ids and regions.**
   - `us.anthropic.claude-haiku-4-5-20251001-v1:0` for extraction (`container-stack.ts:24`) and expansion (`B/core/config.py:390`, not set in the service env).
   - `cohere.rerank-v3-5:0` availability in `AWS_REGION` through `bedrock-agent-runtime.rerank` (`S/memory_models.py:36`), under `bedrock:Rerank` and `InvokeModel` (`service-stack.ts:620-624`).
   - The extraction role's foundation-model ARN drops the `us.` prefix (`container-stack.ts:262`).
6. **Episodic reflections.**
   - When they appear (the sweep collects for 7 days, `S/evaluator.py:36`).
   - Whether they land in `/users/<u>/` with `namespaces` that the filter at `:207` accepts.
   - Whether their text or metadata names session ids. With no ids, `evidence` is empty, so `mirrored` is false, no workspace draft ever appears, and the approve subject falls back to `"lesson"` (`:228-242`; `B/routers/memory.py:157-159`).
7. **Engine ops on the cloud member.** The CLI engine in the deployed image must implement `memory.import`, `memory.list`, `memory.remember`, `memory.forget` and `memory.forget_author` with the payloads in section 3 (`I/hermes/bridge/server.py:531-540`). It must also answer `refused` reasons as `secret:<name>`, `too_long` or `team_off`. The `memory.import` 60 s timeout runs against the RPC ingress floor (`S/container_rpc.py:67`).
8. **Warm detection.** `_session_is_warm` (`S/network_member.py:165-171`) decides between send and queue. A parked workspace never imports facts or mirrors entries until it is warm. Turns still mirror.
9. **TTL.** The messaging table must have its TTL attribute on `expiresAt` for MEMOP, MEMTURN, MEMWIPE, EVALDUE, MEMCAP and session rows (`S/network_member.py:563`, `S/memory_sync.py:411`).
10. **Env and secrets.**
    - `AGENTCORE_MEMORY_ID` and `AGENTCORE_SESSIONS_MEMORY_ID` (`service-stack.ts:1572-1574`).
    - `OWNER_STATE_BUCKET` (`:1562`).
    - `PAPERCLIP_SERVICE_TOKEN_KEY` and `FRONTEND_URL` for share links (`B/routers/sessions.py:118`, `:128-132`).
    - The plugin's `bridge.API` and `bridge.HOOK_TOKEN` (`I/hermes/plugins/memory/agentcore/__init__.py:135-145`).
11. **Throughput.** Each recall can fan out to about 2-4 facts arms, plus turns, entries and private, plus up to 10 entity listings per part, across up to 5 parts, capped at 16 concurrent calls (`S/memory_recall.py:30`, `:114-149`). The plugin allows 3 s (`I/hermes/plugins/memory/agentcore/__init__.py:67`) and the internal route has no expansion. AgentCore throttling and latency are unknown.
12. **Leftover permissions.** The runtime ceiling still grants `RetrieveMemoryRecords` and `ListMemoryRecords` on the workspace memory (`container-stack.ts:508-519`), but the plugin no longer calls them. The backend's `CreateEvent` grant on the workspace memory has no caller (`service-stack.ts:617`).

---

## Test files and counts

Counts are of `def test_` / `it(`/`test(` in each file.

**Memory (recall, models, stages, routes, review, wipe)**
- `B/tests/unit/services/test_memory_recall.py`: 22
- `B/tests/unit/services/test_memory_stages.py`: 10
- `B/tests/unit/services/test_memory_models.py`: 8
- `B/tests/unit/routers/test_memory_recall_routes.py`: 23
- `B/tests/unit/routers/test_memory_router.py`: 9
- `B/tests/unit/services/test_workspace_memory_review.py`: 17
- `B/tests/unit/services/test_workspace_memory.py`: 13
- `B/tests/unit/services/agentcore_filters.py`: helper, 20 lines
- `I/hermes/tests/test_memory_plugin_recall.py`: 15
- `I/hermes/tests/test_memory_agentcore.py`: 26
- `F/components/control/panels/__tests__/AgentMemoryTab.test.tsx`: 20
- `F/__tests__/app/securityPage.test.tsx`: 4
- `I/test/container-stack.test.ts`: 29 in the file, 4 added for memory
- `I/test/service-stack.test.ts`: 79 in the file, 5 added in "workspace memory recall"
- Subtotal for the memory-specific backend and plugin files: 143

**Evaluator**
- `B/tests/unit/services/test_evaluator.py`: 24
- `B/tests/unit/routers/test_memory_drafts.py`: 9
- Subtotal: 33

**Sync** (including the op queue, key registry and MEMWIPE)
- `B/tests/unit/services/test_memory_sync.py`: 39
- `I/hermes/tests/test_network_rpc.py`: 12 in the file, 4 for memory ops

**Sessions**
- `B/tests/unit/routers/test_sessions.py`: 20
- `F/app/shared/[token]/__tests__/page.test.tsx`: 2
- `F/components/control/panels/__tests__/SessionsTimeline.test.tsx`: 1

**Wipe callers touched**
- `B/tests/unit/routers/test_webhooks_paperclip.py`: 3 new
- `B/tests/unit/services/test_account_wipe.py`: 1 new
- `B/tests/unit/routers/test_debug.py`: stub added
- `B/tests/unit/services/test_workspace_wipe.py` and `test_hermes_provisioning_teardown.py`: existing

No test covers `B/scripts/memory_import.py`. No test exercises real AgentCore, Bedrock or the CLI engine; all of them use stubs.
