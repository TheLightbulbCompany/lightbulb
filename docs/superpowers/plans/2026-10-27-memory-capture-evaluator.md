# Memory Capture and Evaluator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record what every agent did on the person's own computer, let them upload it by choice, turn uploaded sessions into proposed lessons and findings through AgentCore's episodic strategy, approve them into memory, show tokens and a timeline in the workspace, and publish the security page.

**Architecture:** The CLI's existing hooks write session and tool-call rows into `state.db`. A signed-in person uploads (metadata or full) to the backend, which stores sessions in S3 and metadata in DynamoDB and writes full sessions as events into an episodic strategy on the memory resource. Reflections become drafts; approving a draft calls `remember` (through the cloud member in a workspace, locally for a solo user). Usage and the timeline read the metadata.

**Tech Stack:** Rust (CLI: `src/hook.rs`, `src/agents/*.rs`, `src/store.rs`, `src/cli.rs`), Python backend (DynamoDB via `core/dynamodb.py`, S3, boto3 `bedrock-agentcore`), CDK (`container-stack.ts` episodic strategy), frontend (Memory tab, Usage, timeline, `/security`).

**Spec:** `docs/superpowers/specs/2026-10-27-memory-layer-design.md` §4.4, §6, §7, §10.

## Global Constraints

- Capture stores: session (id, harness, computer, repo, branch, started, ended, prompt_tokens, completion_tokens) and tool calls (session_id, seq, at, tool, summary ≤200 chars, duration_ms, ok). Never file contents, diffs, command output or prompts. Folders hidden by `lightbulb folder off` are not captured. Summaries pass the memory plan's secret check; a match stores `"[refused: <pattern>]"` as the summary.
- Upload modes: `off` (default), `metadata` (sessions and tool-call rows without summaries), `full` (rows with summaries). Set once at sign-in, changeable with `lightbulb sessions upload <mode>`.
- Retention: free account 30 days, workspace 365 days (S3 lifecycle rules); drafts kept until dismissed or approved.
- Evaluator namespace: episodes `/users/{userId}/episodes/`, reflections `/users/{userId}/`; never cross-user reflection (AWS warns reflections can span actors).
- Free-account evaluator cap: 50 uploaded sessions a month are sent to the episodic strategy; the rest are stored only. Workspace: uncapped.
- No third party ever receives a session.
- The security page never names a competitor and makes only the claims in spec §7.

## Review Focus

1. A tool call whose arguments contain a secret must not reach `state.db` in clear, even in `full` mode. (Task 1: `test_capture_refuses_secret_in_summary`.)
2. Switching upload from `full` to `off` must stop uploads immediately and must not delete what the person already uploaded without asking. (Task 2: `test_upload_mode_off_stops_uploads`.)
3. A draft approved by one member in a workspace becomes a team entry signed by the workspace with the approver's name; a draft approved by a solo user stays on their computer. (Task 4: `test_approve_in_workspace_signs_as_workspace_with_approver`, `test_approve_solo_returns_entry_for_local_store`.)
4. A member wipe removes their sessions, drafts and episodes. (Task 3: `test_member_wipe_removes_sessions_drafts_episodes`.)
5. Capture must never block or slow the agent: the hook writes in under 10 ms or skips, and a failed write is logged, not surfaced. (Task 1: `test_capture_write_failure_is_silent`.)

---

### Task 1: Local capture in the CLI

**Files:**
- Modify: `src/store.rs` (tables `sessions`, `tool_calls` as above), `src/hook.rs` (SessionStart creates the row; PostToolUse appends; Stop updates tokens when the harness reports them; SessionEnd closes), `src/agents/*.rs` (the harnesses that expose tool-call hooks: Claude Code, Codex, Cursor, Grok CLI; OpenCode via its plugin), `src/cli.rs` (`sessions list|show <id>|forget <id>|export`)
- Test: `tests/capture.rs`, `tests/cli.rs` (extend)

**Interfaces:**
- Produces: `capture::record_tool_call(store, session_id, tool, args_json, duration_ms, ok)` (summarises, secret-checks, inserts); `capture::sessions_since(store, after: i64) -> Vec<SessionRow>`; `capture::tool_calls(store, session_id, with_summaries: bool) -> Vec<ToolCallRow>`.

- [ ] **Step 1: Write the failing tests** — `session_rows_follow_hook_lifecycle`, `test_capture_refuses_secret_in_summary`, `hidden_folder_is_not_captured`, `test_capture_write_failure_is_silent`, `sessions_list_shows_harness_repo_tokens`
- [ ] **Step 2: Run → FAIL** — `cargo test --test capture`
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS; `cargo test` whole suite**
- [ ] **Step 5: Commit** — `git commit -m "capture: sessions and tool calls recorded locally"`

### Task 2: Upload by choice

**Files:**
- Modify: `src/cli.rs` (`sessions upload off|metadata|full`; the sign-in flow in `src/login.rs` asks once), `src/config.rs` (`capture_upload`), `src/daemon/mod.rs` (an uploader that sends closed sessions since the last upload, signed with the identity, when signed in and mode ≠ off)
- Backend: Create `apps/backend/routers/sessions.py` (`POST /api/v1/sessions {sessions:[...], tool_calls:[...]}` for a signed-in identity or member; `GET /api/v1/sessions?since=` for the timeline; `DELETE /api/v1/sessions/{id}`); Create `apps/backend/core/services/session_store.py` (`put_sessions(user_id, rows, full: bool)` → S3 `<user>/sessions/<id>.json` + DynamoDB metadata; `list_sessions(user_id | workspace_id, since)`; `delete_sessions(user_id)`); Modify `apps/infra/lib/stacks/service-stack.ts` (bucket lifecycle 30/365 days by prefix)
- Test: `tests/upload.rs`, `apps/backend/tests/unit/routers/test_sessions.py`

**Interfaces:**
- Produces: the endpoints above; metadata row `{id, user_id, workspace_id?, harness, computer, repo, branch, started, ended, prompt_tokens, completion_tokens, full: bool}`.

- [ ] **Step 1: Write the failing tests** — `test_upload_mode_off_stops_uploads`, `upload_metadata_omits_summaries`, `test_sessions_post_requires_identity`, `test_list_sessions_scoped_to_workspace`
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "capture: upload by choice; sessions endpoints and store"`

### Task 3: The evaluator on AgentCore's episodic strategy

**Files:**
- Modify: `apps/infra/lib/stacks/container-stack.ts` (add an `episodicMemoryStrategy` `session_episodes` with the two namespaces; override model `MEMORY_EXTRACT_MODEL`; dev and prod)
- Create: `apps/backend/core/services/evaluator.py`
- Modify: `apps/backend/core/services/workspace_memory.py` (member wipe → `delete_sessions`, `delete_drafts`, delete the user's episodes and reflections)
- Test: `apps/backend/tests/unit/services/test_evaluator.py`, `apps/infra/test/container-stack.test.ts`

**Interfaces:**
- Produces: `async def ingest_session(user_id: str, session: dict, tool_calls: list[dict]) -> None` (writes the session as `CreateEvent`s with `TOOL` role payloads for tool calls, `actorId=user_id`, `sessionId=session["id"]`, timestamps from the rows; skips when the monthly cap is reached for a free account); `async def collect_drafts(user_id: str) -> int` (`ListMemoryRecords` on `/users/{userId}/` for reflections newer than the watermark → DynamoDB `drafts` rows `{id, user_id, workspace_id?, kind: "lesson"|"finding", text, evidence: [session ids], status: "proposed", created}`); `async def list_drafts(user_id | workspace_id) -> list[dict]`; `async def delete_drafts(user_id)`.

- [ ] **Step 1: Write the failing tests** — `test_ingest_session_writes_events_with_tool_role`, `test_free_account_cap_skips_after_50`, `test_collect_drafts_creates_rows_from_new_reflections`, `test_member_wipe_removes_sessions_drafts_episodes`, `episodic strategy declared with per-user namespaces`
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "evaluator: episodic strategy over uploaded sessions; drafts"`

### Task 4: Approve into memory

**Files:**
- Modify: `apps/backend/routers/memory.py` (`GET /api/v1/memory/drafts`, `POST /drafts/{id}/approve {scope}`, `POST /drafts/{id}/dismiss`): in a workspace, approve → cloud member op `memory.remember` with `on_behalf_of` the approver and `kind` lesson, `scope` team by default; for a solo signed-in user, approve returns the entry fields for the CLI to store locally
- Modify: `src/cli.rs` (`memory drafts`, `memory approve <id>`, `memory dismiss <id>` calling the endpoints when signed in, then `memory::remember` locally)
- Modify: `apps/frontend/src/components/control/panels/AgentMemoryTab.tsx` ("Suggested" section with evidence links to the timeline, approve and dismiss)
- Test: `apps/backend/tests/unit/routers/test_memory_drafts.py`, `tests/cli.rs`, the tab test

- [ ] **Step 1: Write the failing tests** — `test_approve_in_workspace_signs_as_workspace_with_approver`, `test_approve_solo_returns_entry_for_local_store`, `test_dismiss_marks_row`, `renders suggested drafts with evidence links`
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "evaluator: approve drafts into memory from the tab and the CLI"`

### Task 5: Usage, timeline, share by link

**Files:**
- Modify: the finance layer's usage service (PRs #4432/#4433 define the tables; add a source `capture` with prompt and completion tokens per agent, person and session)
- Create: `apps/frontend/src/components/control/panels/SessionsTimeline.tsx` (sessions across agents and machines from `GET /api/v1/sessions`)
- Modify: `apps/backend/routers/sessions.py` (`POST /sessions/{id}/share` → signed S3 URL, 7 days; `GET /shared/{token}` renders the metadata and, if full, the tool-call list)
- Test: backend route tests; frontend timeline test

- [ ] **Step 1: Write the failing tests** — `test_usage_includes_capture_tokens_per_agent`, `test_share_link_expires_in_7_days`, `renders sessions grouped by machine`
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "capture: usage tokens, workspace timeline, share by link"`

### Task 6: Security page and docs

**Files:**
- Modify: `apps/frontend/src/app/security/page.tsx` (the claims table from spec §7, each with its evidence line; the "what leaves your machine" band gains memory, capture and upload)
- Modify: CLI `docs/security.md` and `docs/usage.md` (memory, capture, upload, drafts); the Termly privacy rewrite is the founder's
- Test: `apps/frontend/src/__tests__/app/security.test.tsx` (every claim row has an evidence cell; no competitor name on the page)

- [ ] **Step 1: Write the failing test**
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Write the page and docs**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "security: claims with evidence; memory, capture and upload documented"`
