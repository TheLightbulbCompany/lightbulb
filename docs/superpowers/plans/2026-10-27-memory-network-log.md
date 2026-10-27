# Memory Network Log Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Team and personal memory on the free network: signed entries in the engine's SQLite, synced between members like the roster, written and read by every harness through `remember` and `recall`, with nothing on our servers.

**Architecture:** A `memory_entries` table and an FTS5 index in `state.db`; entries signed by their author and numbered per author; peers advertise per-author heads in `hello` and page the gap with a new frame, gated on a `memory` capability so old builds keep messaging. Two MCP tools in the shared tool spec, a session-start block from the hook, and `lightbulb memory` commands. The cloud member exposes IPC ops so the workspace can write entries on a person's behalf.

**Tech Stack:** Rust (the CLI repo at `Desktop/lightbulb-network.nosync`, v0.6.5), rusqlite with FTS5, iroh QUIC, serde; tests in `tests/*.rs` with the existing `tests/common` helpers.

**Spec:** `docs/superpowers/specs/2026-10-27-memory-layer-design.md` §4, §8; `src/roster.rs` for the signing and replay patterns.

## Global Constraints

- Branch from clean `origin/main` of the CLI repo in a fresh worktree.
- Entry size ≤ 16 KB canonical JSON (the roster's `MAX_ENTRY_BYTES`); larger `remember` calls are refused with `too_long`.
- Signing and canonicalisation reuse `identity::{canonical, unsigned, verify}`; never a second implementation.
- Scope rule: `team` entries go to every admitted member; `me` entries go only to computers with the same `person_id` (`roster::Member.person_id`), which without sign-in is this computer alone.
- Session-start block ≤ 400 tokens (approximate: 1,600 characters), team facts for the current repo first, then personal; never injected on `UserPromptSubmit`.
- Secret patterns (refuse, do not scrub): `(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*\S{8,}`, `-----BEGIN [A-Z ]*PRIVATE KEY-----`, `AKIA[0-9A-Z]{16}`, `sk-[A-Za-z0-9_-]{20,}`, `https?://[^\s/]+:[^\s@]+@`, `eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}`.
- Old builds: `Frame` has no unknown-variant tolerance, so memory frames are sent only to peers whose `hello` carried `memory: true`.
- `daemon.log` never logs entry text (as for messages).

## Review Focus

1. A `forget` for an entry the local computer has not received yet must still apply when the entry arrives later (tombstone wins regardless of arrival order). (Task 1: `test_tombstone_before_entry_wins`.)
2. A removed member's computer must stop receiving new entries immediately and must not be able to push entries that others accept. (Task 2: `test_removed_member_entries_refused`.)
3. Two computers of the same signed-in person must sync `me` entries; two different members must not. (Task 2: `test_me_scope_syncs_only_same_person`.)
4. A `remember` whose subject matches an existing fact with a different value and no `replaces` must store the entry and mark both as contested on recall, never drop either. (Task 3: `test_remember_conflict_without_replaces_is_contested`.)
5. An `AGENTS.md`-style repo with hundreds of entries must still produce a session-start block under 1,600 characters, newest first. (Task 4: `test_session_block_caps_at_1600_chars`.)

---

### Task 1: Store: entries, signing, replay, FTS5

**Files:**
- Create: `src/memory.rs`
- Modify: `src/store.rs` (schema: `memory_entries(id TEXT PRIMARY KEY, author TEXT, seq INTEGER, entry TEXT, received_at INTEGER, tombstone INTEGER DEFAULT 0, UNIQUE(author, seq))`; `CREATE VIRTUAL TABLE memory_fts USING fts5(subject, fact, content='memory_entries', content_rowid='rowid')` with the content-sync triggers; `memory_heads` view: `SELECT author, MAX(seq)`)
- Modify: `src/lib.rs` (`pub mod memory;`)
- Test: `tests/memory_store.rs`

**Interfaces:**
- Produces:
  ```rust
  pub enum Kind { Fact, Decision, Lesson, Preference }
  pub enum Scope { Me, Team }
  pub struct Entry { pub v: u32, pub id: String, pub author: String, pub seq: u64, pub op: Op, pub at: i64, pub sig: String }
  pub enum Op { Remember { subject: String, fact: String, kind: Kind, scope: Scope, when: Option<i64>, source: Source, replaces: Vec<String> }, Forget { target: String } }
  pub struct Source { pub harness: String, pub repo: Option<String>, pub session: Option<String> }
  pub fn remember(store: &Store, id: &Identity, op: Op, now: i64) -> Result<Entry>;      // signs, assigns seq = head(author)+1, stores
  pub fn accept(store: &Store, e: &Entry, now: i64) -> Result<bool>;                      // verifies sig, refuses unknown author, applies tombstones, false if already held
  pub fn heads(store: &Store) -> Result<BTreeMap<String, u64>>;
  pub fn since(store: &Store, author: &str, after: u64, limit: usize) -> Result<Vec<Entry>>;
  pub struct Fact { pub id: String, pub subject: String, pub value: String, pub kind: Kind, pub scope: Scope, pub author: String, pub when: i64, pub status: Status, pub history: Vec<(String, String, i64)> }
  pub enum Status { Current, Contested, Replaced }
  pub fn facts(store: &Store, scope: Option<Scope>, repo: Option<&str>) -> Result<Vec<Fact>>; // replays replace chains; contested detection per spec §4.1
  pub fn search(store: &Store, query: &str, limit: usize) -> Result<Vec<Fact>>;             // FTS5 bm25, then the same replay
  pub fn refuse_secret(text: &str) -> Option<&'static str>;                                  // pattern name if matched
  ```

- [ ] **Step 1: Write the failing tests**

```rust
#[test] fn remember_assigns_increasing_seq_and_verifies() { /* two remembers -> seq 1,2; accept(entry) on a second store with the author admitted -> true; tampered fact -> false */ }
#[test] fn replace_chain_yields_one_current_with_history() { /* Postgres 14 then replace -> facts() has one Fact value "Postgres 16", history [("Postgres 14", author, at)] */ }
#[test] fn two_replaces_of_same_target_are_contested() { }
#[test] fn test_tombstone_before_entry_wins() { /* accept(forget X) then accept(remember X) -> facts() empty */ }
#[test] fn search_finds_by_subject_and_fact() { }
#[test] fn refuse_secret_matches_the_six_patterns() { }
#[test] fn entry_over_16kb_is_refused() { }
```

- [ ] **Step 2: Run → FAIL**

Run: `cargo test --test memory_store`

- [ ] **Step 3: Implement `src/memory.rs` and the schema**
- [ ] **Step 4: Run → PASS; `cargo clippy --all-targets` clean**
- [ ] **Step 5: Commit** — `git commit -m "memory: signed entries, replay, FTS5"`

### Task 2: Sync: capability, heads, paged frames, scope

**Files:**
- Modify: `src/net/frames.rs` (`Hello` gains `memory: bool` with `#[serde(default)]`; new `Frame::MemoryHeads { heads: BTreeMap<String, u64> }` and `Frame::Memory { entries: Vec<memory::Entry>, more: bool }`; `check()` accepts them only from members)
- Modify: `src/net/mod.rs` (after roster sync: if the peer's hello has `memory`, send `MemoryHeads`; on `MemoryHeads`, page `since()` for each author where ours is ahead, filtering `Me` entries to peers with the same `person_id`; on `Memory`, `accept` each; pages close below half the frame cap like roster pages)
- Modify: `src/daemon/net_ops.rs` if the hello is built there
- Test: `tests/memory_sync.rs` (two in-process daemons via `tests/common`, as `tests/net_two.rs` does)

**Interfaces:**
- Consumes: Task 1 `heads`, `since`, `accept`.
- Produces: the wire behaviour above; `PROTOCOL` unchanged.

- [ ] **Step 1: Write the failing tests**

```rust
#[tokio::test] async fn team_entry_reaches_the_other_member() { }
#[tokio::test] async fn test_me_scope_syncs_only_same_person() { }
#[tokio::test] async fn old_peer_without_capability_gets_no_memory_frames_and_keeps_mail() { /* hello.memory=false -> no MemoryHeads sent; a message still delivers */ }
#[tokio::test] async fn test_removed_member_entries_refused() { }
#[tokio::test] async fn gap_of_300_entries_pages() { }
```

- [ ] **Step 2: Run → FAIL** (`cargo test --test memory_sync`)
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS; run the whole suite `cargo test`**
- [ ] **Step 5: Commit** — `git commit -m "memory: sync entries between members behind a hello capability"`

### Task 3: Tools: `remember` and `recall`

**Files:**
- Modify: `src/mcp/tools.json` (add the two tools; keep in step with the backend's `network_tools.json`, same text as engine plan Task 9)
- Modify: `src/mcp.rs` and `src/daemon/ipc.rs` (ops `memory.remember {subject, fact, kind, scope?, replaces?, when?}` → `{entry_id, neighbours:[Fact], refused?: reason}`; `memory.recall {question, budget_chars?}` → `{page: String, facts:[Fact]}`)
- Test: `tests/mcp_tools.rs` (extend), `tests/memory_tools.rs`

**Interfaces:**
- Consumes: Task 1.
- Produces: `memory.remember` returns `neighbours` (current facts whose subject matches, FTS over subject) **before** storing when `replaces` is empty and a neighbour has a different value: the response carries `neighbours` and `stored: true`; the agent may follow with a second call carrying `replaces`. Page text format (what the agent reads): `Contested:` lines first ("launch date: 3 March (Ana, 1 Oct) vs 10 March (Ben, 1 Oct)"), then `Current:` lines ("production database: Postgres 16 (Ben, 15 Sep; was Postgres 14, Ana, 2 Aug)"), then `Lessons:`; cut at `budget_chars` (default 1,600).

- [ ] **Step 1: Write the failing tests**

```rust
#[test] fn tools_json_lists_remember_and_recall() { }
#[test] fn test_remember_conflict_without_replaces_is_contested() { }
#[test] fn remember_refuses_secret_with_reason() { }
#[test] fn recall_page_puts_contested_first_and_respects_budget() { }
```

- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "memory: remember and recall tools"`

### Task 4: Session-start block and the agent instruction

**Files:**
- Modify: `src/hook.rs` (on `SessionStart`, after mail: append the recall page for the session's repo, ≤1,600 characters, under a heading `Memory (team and yours):` and one line of instruction: "Call `recall` when a task needs something a teammate may know; call `remember` when you finish a task or are told to remember. Personal unless the person says to share.")
- Test: `tests/hooks.rs` (extend)

- [ ] **Step 1: Write the failing tests** — `session_start_includes_memory_block_for_repo`, `test_session_block_caps_at_1600_chars`, `user_prompt_submit_never_includes_memory`
- [ ] **Step 2: Run → FAIL** (`cargo test --test hooks`)
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "memory: session-start block"`

### Task 5: `lightbulb memory` commands

**Files:**
- Modify: `src/cli.rs` (`memory list [--team|--me] [--repo]`, `memory show <id>` (history and author), `memory forget <id>`, `memory share <id>` (re-remember as `team` with `replaces: [id]`... no: a new `team` entry copying the fact with `source` = the original id; the `me` entry stays), `memory export [--json]`, `memory last` (the last injected block, kept in `state.db` `memory_last`), `memory team on|off` (config flag read by `remember` to refuse `scope: team` when off))
- Modify: `docs/cli.md`, `docs/usage.md`, `docs/security.md` ("Memory" section: what syncs where, what `forget` does, the secret refusal)
- Test: `tests/cli.rs` (extend)

- [ ] **Step 1: Write the failing tests** — `memory_list_shows_scope_author_and_status`, `memory_share_creates_team_entry`, `memory_team_off_refuses_team_scope`, `memory_last_prints_block`
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement; write the docs**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "memory: lightbulb memory commands and docs"`

### Task 6: Cloud member ops for the workspace

**Files:**
- Modify: `src/embed.rs` and `src/daemon/ipc.rs` (cloud-only ops: `memory.remember` with an `on_behalf_of: {name}` carried in `source`, `memory.import { entries: [...] }` (author "workspace (imported)" = the cloud member's key, `source.harness = "import"`), `memory.list { since_seq }` for the Memory tab's reads from the bridge)
- Test: `tests/embed.rs` (extend)

**Interfaces:**
- Produces: the three ops the backend's `network_member.py` calls (engine plan Tasks 9 and 11).

- [ ] **Step 1: Write the failing tests** — `cloud_remember_signs_as_workspace_with_name_in_source`, `cloud_import_assigns_sequential_seq`, `cloud_list_since_returns_new_entries`
- [ ] **Step 2: Run → FAIL** (`cargo test --test embed`)
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "memory: cloud member ops for the workspace"`

### Task 7: Setup suggests sign-in

**Files:**
- Modify: `src/setup/` (the wizard's first screen: "Sign in (free): your memory and sessions follow you across computers; the Claude app reaches your agents; the evaluator can read sessions you upload. Or stay local." with local-only one keypress away; the capture upload choice asked right after sign-in, default off)
- Modify: `docs/usage.md`
- Test: `tests/cli.rs` (extend)

- [ ] **Step 1: Write the failing tests** — `setup_offers_sign_in_first_with_local_one_key_away`, `setup_without_sign_in_sets_upload_off`
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `git commit -m "setup: suggest the free sign-in, local-only one key away"`

### Task 8: Mixed-version test and release

- [ ] **Step 1: Run the two-Mac acceptance from `docs/acceptance-two-mac.md` with one Mac on v0.6.5 and one on this build: messaging works both ways; memory syncs only between new builds**
- [ ] **Step 2: Add the memory rows to `docs/acceptance-public.md`**
- [ ] **Step 3: Release per `docs/releasing.md` (v0.7.0); the npm and Homebrew paths pick it up**
- [ ] **Step 4: Commit** — `git commit -m "release: v0.7.0 (memory on the network)"`
