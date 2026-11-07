# Memory on the Free Network (the engine) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Everything the CLI engine needs for memory and capture with no account and nothing on our servers: signed memory entries synced between members like the roster, `remember` and `recall` for every harness, a session-start block, local capture of sessions, `lightbulb memory` and `lightbulb sessions`, setup that suggests the free sign-in, upload by choice when signed in, and the cloud-member ops the workspace plan calls.

**Architecture:** A `memory_entries` table with FTS5 and `sessions`/`tool_calls` tables in `state.db`; entries signed by their author and numbered per author; peers advertise per-author heads in `hello` and page the gap with a new frame, gated on a `memory` capability so old builds keep messaging. Two MCP tools in the shared tool spec, hooks for the block and for capture, CLI commands, and IPC ops for the bridge.

**Tech Stack:** Rust (the CLI repo at `Desktop/lightbulb-network.nosync`, v0.6.5), rusqlite with FTS5, iroh QUIC, serde; tests in `tests/*.rs` with the existing `tests/common` helpers.

**Spec:** `docs/superpowers/specs/2026-10-27-memory-layer-design.md` §4, §6 (capture half), §10; `src/roster.rs` for signing and replay.

## Global Constraints

- Branch from clean `origin/main` of the CLI repo in a fresh worktree.
- Entry size ≤ 16 KB canonical JSON (the roster's `MAX_ENTRY_BYTES`); larger `remember` calls are refused with `too_long`.
- Signing and canonicalisation reuse `identity::{canonical, unsigned, verify}`; never a second implementation.
- Scope rule: `team` entries go to every admitted member; `me` entries go only to computers with the same `person_id` (`roster::Member.person_id`), which without sign-in is this computer alone.
- Session-start block ≤ 1,600 characters (~400 tokens), team facts for the current repo first, then personal; never on `UserPromptSubmit`.
- Secret patterns (refuse, never scrub): `(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*\S{8,}`, `-----BEGIN [A-Z ]*PRIVATE KEY-----`, `AKIA[0-9A-Z]{16}`, `sk-[A-Za-z0-9_-]{20,}`, `https?://[^\s/]+:[^\s@]+@`, `eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}`. Used by `remember` and by capture summaries.
- Capture stores: session (id, harness, computer, repo, branch, started, ended, prompt_tokens, completion_tokens) and tool calls (session_id, seq, at, tool, summary ≤200 chars, duration_ms, ok). Never file contents, diffs, command output or prompts. Folders hidden by `lightbulb folder off` are not captured. A summary that matches a secret pattern is stored as `"[refused: <pattern>]"`.
- Upload modes: `off` (default), `metadata` (rows without summaries), `full`. Asked once at sign-in; `lightbulb sessions upload <mode>` changes it. Uploads go only to the Lightbulb backend, signed with the identity.
- Old builds: `Frame` has no unknown-variant tolerance, so memory frames are sent only to peers whose `hello` carried `memory: true`.
- `daemon.log` never logs entry text or tool-call summaries.

## Review Focus

1. A `forget` that arrives before its entry must still win when the entry arrives later. (Task 1: `test_tombstone_before_entry_wins`.)
2. A removed member's computer stops receiving entries immediately and cannot push entries others accept. (Task 2: `test_removed_member_entries_refused`.)
3. Two computers of the same signed-in person sync `me` entries; two different members do not. (Task 2: `test_me_scope_syncs_only_same_person`.)
4. A `remember` whose subject matches an existing fact with a different value and no `replaces` stores the entry and both show as contested, never dropped. (Task 3: `test_remember_conflict_without_replaces_is_contested`.)
5. A tool call whose arguments contain a secret never reaches `state.db` in clear; capture never slows the agent (a write over 10 ms is skipped and logged). (Task 9: `test_capture_refuses_secret_in_summary`, `test_capture_write_failure_is_silent`.)
6. Switching upload to `off` stops uploads at once and deletes nothing already uploaded without asking. (Task 10: `test_upload_mode_off_stops_uploads`.)

---

### Task 1: Store: entries, signing, replay, FTS5

**Files:**
- Create: `src/memory.rs`
- Modify: `src/store.rs` (schema: `memory_entries(id TEXT PRIMARY KEY, author TEXT, seq INTEGER, entry TEXT, received_at INTEGER, tombstone INTEGER DEFAULT 0, UNIQUE(author, seq))`; `CREATE VIRTUAL TABLE memory_fts USING fts5(subject, fact, content='memory_entries', content_rowid='rowid')` with content-sync triggers; a `memory_heads` view `SELECT author, MAX(seq)`)
- Modify: `src/lib.rs` (`pub mod memory;`)
- Test: `tests/memory_store.rs`

**Interfaces:**
- Produces:
  ```rust
  pub enum Kind { Fact, Decision, Lesson, Preference }
  pub enum Scope { Me, Team }
  pub struct Entry { pub v: u32, pub id: String, pub author: String, pub seq: u64, pub op: Op, pub at: i64, pub sig: String }
  pub enum Op { Remember { subject: String, fact: String, kind: Kind, scope: Scope, when: Option<i64>, source: Source, replaces: Vec<String> }, Forget { target: String } }
  pub struct Source { pub harness: String, pub repo: Option<String>, pub session: Option<String>, pub on_behalf_of: Option<String> }
  pub fn remember(store: &Store, id: &Identity, op: Op, now: i64) -> Result<Entry>;      // signs, seq = head(author)+1, stores
  pub fn accept(store: &Store, e: &Entry, now: i64) -> Result<bool>;                      // verifies, refuses unknown author, applies tombstones, false if held
  pub fn heads(store: &Store) -> Result<BTreeMap<String, u64>>;
  pub fn since(store: &Store, author: &str, after: u64, limit: usize) -> Result<Vec<Entry>>;
  pub struct Fact { pub id: String, pub subject: String, pub value: String, pub kind: Kind, pub scope: Scope, pub author: String, pub when: i64, pub status: Status, pub history: Vec<(String, String, i64)> }
  pub enum Status { Current, Contested, Replaced }
  pub fn facts(store: &Store, scope: Option<Scope>, repo: Option<&str>) -> Result<Vec<Fact>>; // replays replace chains; contested per spec §4.1
  pub fn search(store: &Store, query: &str, limit: usize) -> Result<Vec<Fact>>;             // FTS5 bm25, then the same replay
  pub fn refuse_secret(text: &str) -> Option<&'static str>;                                  // pattern name if matched
  ```

- [ ] **Step 1: Write the failing tests** — `remember_assigns_increasing_seq_and_verifies` (two remembers → seq 1, 2; `accept` on a second store with the author admitted → true; tampered fact → false), `replace_chain_yields_one_current_with_history`, `two_replaces_of_same_target_are_contested`, `test_tombstone_before_entry_wins`, `search_finds_by_subject_and_fact`, `refuse_secret_matches_the_six_patterns`, `entry_over_16kb_is_refused`
- [ ] **Step 2: Run → FAIL** — `cargo test --test memory_store`
- [ ] **Step 3: Implement `src/memory.rs` and the schema**
- [ ] **Step 4: Run → PASS; `cargo clippy --all-targets` clean**
- [ ] **Step 5: Commit** — `git commit -m "memory: signed entries, replay, FTS5"`

### Task 2: Sync: capability, heads, paged frames, scope

**Files:**
- Modify: `src/net/frames.rs` (`Hello` gains `memory: bool` with `#[serde(default)]`; `Frame::MemoryHeads { heads: BTreeMap<String, u64> }`; `Frame::Memory { entries: Vec<memory::Entry>, more: bool }`; `check()` accepts them only from members)
- Modify: `src/net/mod.rs` (after roster sync: if the peer's hello has `memory`, send `MemoryHeads`; on `MemoryHeads`, page `since()` per author where ours is ahead, filtering `Me` entries to peers with the same `person_id`; on `Memory`, `accept` each; pages close below half the frame cap like roster pages)
- Test: `tests/memory_sync.rs` (two in-process daemons via `tests/common`, as `tests/net_two.rs`)

- [ ] **Step 1: Write the failing tests** — `team_entry_reaches_the_other_member`, `test_me_scope_syncs_only_same_person`, `old_peer_without_capability_gets_no_memory_frames_and_keeps_mail`, `test_removed_member_entries_refused`, `gap_of_300_entries_pages`
- [ ] **Step 2: Run → FAIL** — `cargo test --test memory_sync`
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS; whole suite `cargo test`**
- [ ] **Step 5: Commit** — `git commit -m "memory: sync entries between members behind a hello capability"`

### Task 3: Tools: `remember` and `recall`

**Files:**
- Modify: `src/mcp/tools.json` (the two tools; same text as the backend's `network_tools.json`: `recall(question)` "Call when a task starts or you need something a teammate may know"; `remember(subject, fact, kind, scope?, replaces?)` "Returns what is already known on the subject; call again with `replaces` if yours is a change. Personal unless the person says to share")
- Modify: `src/mcp.rs`, `src/daemon/ipc.rs` (ops `memory.remember {subject, fact, kind, scope?, replaces?, when?}` → `{entry_id, neighbours: [Fact], stored: bool, refused?: reason}`; `memory.recall {question, budget_chars?}` → `{page: String, facts: [Fact]}`)
- Test: `tests/mcp_tools.rs` (extend), `tests/memory_tools.rs`

**Interfaces:**
- Produces: `memory.remember` returns `neighbours` (current facts whose subject matches, FTS over subject) and stores the entry; a neighbour with a different value and no `replaces` leaves both current and recall marks them contested. Page text: `Contested:` lines first ("launch date: 3 March (Ana, 1 Oct) vs 10 March (Ben, 1 Oct)"), then `Current:` ("production database: Postgres 16 (Ben, 15 Sep; was Postgres 14, Ana, 2 Aug)"), then `Lessons:`; cut at `budget_chars` (default 1,600).

- [ ] **Step 1: Write the failing tests** — `tools_json_lists_remember_and_recall`, `test_remember_conflict_without_replaces_is_contested`, `remember_refuses_secret_with_reason`, `recall_page_puts_contested_first_and_respects_budget`
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "memory: remember and recall tools"`

### Task 4: Session-start block

**Files:**
- Modify: `src/hook.rs` (on `SessionStart`, after mail: the recall page for the session's repo under `Memory (team and yours):` plus one instruction line: "Call `recall` when a task needs something a teammate may know; call `remember` when you finish a task or are told to remember. Personal unless the person says to share."; the block saved to `memory_last` in `state.db`)
- Test: `tests/hooks.rs` (extend)

- [ ] **Step 1: Write the failing tests** — `session_start_includes_memory_block_for_repo`, `test_session_block_caps_at_1600_chars`, `user_prompt_submit_never_includes_memory`
- [ ] **Step 2: Run → FAIL** — `cargo test --test hooks`
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "memory: session-start block"`

### Task 5: `lightbulb memory` commands

**Files:**
- Modify: `src/cli.rs` (`memory list [--team|--me] [--repo]`, `memory show <id>` (history and author), `memory forget <id>`, `memory share <id>` (a new `team` entry copying the fact with `source.session` = the original id; the `me` entry stays), `memory export [--json]`, `memory last`, `memory team on|off` (a config flag; `remember` refuses `scope: team` when off))
- Modify: `docs/cli.md`, `docs/usage.md`, `docs/security.md` ("Memory": what syncs where, what `forget` does, the secret refusal)
- Test: `tests/cli.rs` (extend)

- [ ] **Step 1: Write the failing tests** — `memory_list_shows_scope_author_and_status`, `memory_share_creates_team_entry`, `memory_team_off_refuses_team_scope`, `memory_last_prints_block`
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement; write the docs**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "memory: lightbulb memory commands and docs"`

### Task 6: Cloud member ops for the workspace

**Files:**
- Modify: `src/embed.rs`, `src/daemon/ipc.rs` (cloud-only ops: `memory.remember` with `on_behalf_of` carried in `source`; `memory.import { entries }` (author = the cloud member's key, `source.harness = "import"` or `"workspace"`); `memory.list { since_seq }`; `memory.forget_author { author }` (tombstones every entry by that author: member wipe))
- Test: `tests/embed.rs` (extend)

**Interfaces:**
- Produces: the four ops the workspace plan's backend calls through the bridge.

- [ ] **Step 1: Write the failing tests** — `cloud_remember_signs_as_workspace_with_name_in_source`, `cloud_import_assigns_sequential_seq`, `cloud_list_since_returns_new_entries`, `cloud_forget_author_tombstones_all`
- [ ] **Step 2: Run → FAIL** — `cargo test --test embed`
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "memory: cloud member ops for the workspace"`

### Task 7: Setup suggests sign-in

**Files:**
- Modify: `src/setup/` (first screen: "Sign in (free): your memory and sessions follow you across computers; the Claude app reaches your agents; the evaluator can read sessions you upload. Or stay local." with local-only one keypress away; the upload choice asked right after sign-in, default off)
- Modify: `docs/usage.md`
- Test: `tests/cli.rs` (extend)

- [ ] **Step 1: Write the failing tests** — `setup_offers_sign_in_first_with_local_one_key_away`, `setup_without_sign_in_sets_upload_off`
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "setup: suggest the free sign-in, local-only one key away"`

### Task 8: Local capture

**Files:**
- Modify: `src/store.rs` (`sessions`, `tool_calls` per Global Constraints), `src/hook.rs` (SessionStart creates the row; PostToolUse appends; Stop updates tokens when the harness reports them; SessionEnd closes), `src/agents/*.rs` (the harnesses with tool-call hooks: Claude Code, Codex, Cursor, Grok CLI; OpenCode via its plugin), `src/cli.rs` (`sessions list|show <id>|forget <id>|export`)
- Test: `tests/capture.rs`, `tests/cli.rs` (extend)

**Interfaces:**
- Produces: `capture::record_tool_call(store, session_id, tool, args_json, duration_ms, ok)`; `capture::sessions_since(store, after: i64) -> Vec<SessionRow>`; `capture::tool_calls(store, session_id, with_summaries: bool) -> Vec<ToolCallRow>`.

- [ ] **Step 1: Write the failing tests** — `session_rows_follow_hook_lifecycle`, `test_capture_refuses_secret_in_summary`, `hidden_folder_is_not_captured`, `test_capture_write_failure_is_silent`, `sessions_list_shows_harness_repo_tokens`
- [ ] **Step 2: Run → FAIL** — `cargo test --test capture`
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS; `cargo test`**
- [ ] **Step 5: Commit** — `git commit -m "capture: sessions and tool calls recorded locally"`

### Task 9: Upload by choice, drafts from the CLI

**Files:**
- Modify: `src/cli.rs` (`sessions upload off|metadata|full`; `memory drafts`, `memory approve <id>`, `memory dismiss <id>`), `src/login.rs` (asks the upload mode once), `src/config.rs` (`capture_upload`), `src/daemon/mod.rs` (an uploader sending closed sessions since the last upload, signed with the identity, when signed in and mode ≠ off)
- Test: `tests/upload.rs`, `tests/cli.rs` (extend)

**Interfaces:**
- Consumes: the workspace plan's `POST /api/v1/sessions`, `GET /api/v1/memory/drafts`, `POST /api/v1/memory/drafts/{id}/approve|dismiss`. For a solo signed-in user, approve returns the entry fields and the CLI stores them with `memory::remember` locally.

- [ ] **Step 1: Write the failing tests** — `test_upload_mode_off_stops_uploads`, `upload_metadata_omits_summaries`, `uploader_signs_batches`, `memory_approve_stores_locally_for_solo_user`
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "capture: upload by choice; drafts from the CLI"`

### Task 10: Mixed-version test and release

- [ ] **Step 1: Run the two-Mac acceptance from `docs/acceptance-two-mac.md` with one Mac on v0.6.5 and one on this build: messaging both ways; memory syncs only between new builds; capture rows appear; nothing uploads with mode off**
- [ ] **Step 2: Add the memory and capture rows to `docs/acceptance-public.md`**
- [ ] **Step 3: Release per `docs/releasing.md` (v0.7.0)**
- [ ] **Step 4: Commit** — `git commit -m "release: v0.7.0 (memory and capture on the network)"`
