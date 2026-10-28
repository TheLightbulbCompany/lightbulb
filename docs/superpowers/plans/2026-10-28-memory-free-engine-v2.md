# Free engine v2 (CLI) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The CLI's memory recalls by meaning, indexes the session's own turns locally, asks the agent to write what it learned at the end of a turn, and fixes the five gaps the 28 Oct code inventory found, at $0 to us with nothing leaving the machine.

**Architecture:** One embedding function behind `embed::text` (a small model downloaded at setup, keyword-only without it); vectors beside the FTS5 rows in `state.db`; recall fuses bm25 and cosine ranks (RRF) per arm and adds a turns arm; the Stop hook nudges once per session; everything else (entries, sync, page, tools) unchanged.

**Tech Stack:** Rust (edition as the crate), rusqlite 0.40 bundled, FTS5, an embedding crate chosen in Task 1 (`fastembed` with int8 ONNX, or `model2vec-rs` static embeddings) — no vector extension, no bundled LLM.

**Spec:** `docs/superpowers/specs/2026-10-28-memory-layer-v6-amendment.md` (§2) over `docs/superpowers/specs/2026-10-27-memory-layer-design.md`. Facts about the code: `docs/research/2026-10-28-inventory-cli-code.md`.

## Global Constraints

- Repository `TheLightbulbCompany/cli`, branch off `origin/main` at or after 8588b81dd; worktree per task; never stack on another agent's branch.
- Nothing leaves the machine: no network call except the one model download at `setup` (from the crate's documented source), skipped with `setup --no-model`.
- `cargo clippy --all-targets -- -D warnings`, `cargo fmt --check`, `cargo test` green on macOS, ubuntu, ubuntu-arm (CI). gitleaks: new fixture secrets go in `.gitleaks.toml`'s allowlist.
- `store.rs` migrations: append only; the next free number (V13 unless another PR took it — check `MIGRATIONS` on `main` before writing; then V14).
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Docs in `docs/` and `src/setup/SKILL.md` change in the task that changes behaviour. No competitor named anywhere.
- Person gate unchanged: new person-only commands go through the same check as `memory forget` (`src/daemon/ipc.rs:899` pattern).
- Secret patterns: reuse `memory::refuse_secret` everywhere a new text is stored.

## Review Focus

1. A `recall` with the model missing (user ran `setup --no-model`, or the download failed) must behave exactly as today: keyword only, no error, no slowdown. Test in Task 3.
2. A turn containing a secret must be stored as `[refused: <name>]`, never embedded, never shown. Test in Task 4.
3. The Stop nudge must never fire when the hook is already handing over waiting messages, and never twice in one session; a session with no tool call gets no nudge. Test in Task 7.
4. An old build (v0.6.6) peer must keep syncing entries: vectors and turns are local tables, never on the wire. Test in Task 2 (no new frame; `memory_sync` tests unchanged).
5. A session started in a subfolder of a repo must get the block for that repo; two orgs' `api` repos must not share a block. Test in Task 6.

---

### Task 1: Embedding backend and the model at setup

**Files:**
- Create: `src/embed_model.rs` (module `embed_model`; `src/embed.rs` already exists for another purpose — do not touch it)
- Modify: `Cargo.toml`, `src/setup/mod.rs`, `src/cli.rs` (the `setup --no-model` flag), `src/paths.rs` (model dir)
- Test: `tests/embed_model.rs`

**Interfaces:**
- Produces: `pub fn text(s: &str) -> Option<Vec<f32>>` — `None` when no model is installed; `pub const DIM: usize` (384 for MiniLM/bge-small; the crate's dimension for model2vec); `pub fn installed() -> bool`; `pub fn install(dir: &Path) -> Result<()>` (download once; idempotent); `pub fn cosine(a: &[f32], b: &[f32]) -> f32`.
- Decision inside the task, recorded in the module doc: `fastembed` (int8 ONNX) vs `model2vec-rs` (static). Acceptance: download ≤ 40 MB; `text` ≤ 20 ms per 200-char entry on the CI runner (measured in a test with a generous bound of 100 ms); release binary growth ≤ 25 MB; builds on all three CI targets with no system library. If `fastembed`'s runtime fails any bound, take `model2vec-rs`; if both fail, STOP and report.

- [ ] **Step 1: Write the failing tests** in `tests/embed_model.rs`:
```rust
#[test] fn without_a_model_text_is_none_and_installed_false()  // fresh state dir
#[test] fn two_paraphrases_are_closer_than_two_unrelated_texts() // needs the model: `#[ignore]` unless LIGHTBULB_TEST_MODEL=1; CI sets it after install
#[test] fn cosine_of_identical_vectors_is_one_and_orthogonal_is_zero()
```
- [ ] **Step 2: Run them** — `cargo test --test embed_model` fails to compile (module missing).
- [ ] **Step 3: Implement `src/embed_model.rs`** with the chosen crate; model files under `paths::state_dir()/model/`; `install` writes to a temp dir then renames.
- [ ] **Step 4: Setup** — `lightbulb setup` calls `install` after the hooks (prints `Memory model installed (NN MB)`; on failure prints the one-line reason and continues: keyword-only); `setup --no-model` skips; `doctor` prints `Memory model   installed | not installed (run lightbulb setup)`.
- [ ] **Step 5: Run tests, clippy, fmt; commit** `embed: a small local model behind embed_model::text; setup installs it`.

### Task 2: Vectors for entries

**Files:**
- Modify: `src/store.rs` (migration), `src/memory.rs` (write path: `remember` and accept-from-peer; read helper), `src/daemon/ipc.rs` (nothing new on the wire)
- Test: `tests/memory_store.rs`

**Interfaces:**
- Consumes: `embed_model::text`, `DIM`.
- Produces: table `memory_vec(id TEXT PRIMARY KEY REFERENCES memory_entries(id), v BLOB NOT NULL)` (little-endian f32s); `memory::embed_missing(store, limit) -> usize` (embeds up to `limit` entries without a vector; the daemon calls it with 200 on start and after each sync page); `memory::vectors(store) -> Vec<(String, Vec<f32>)>` for non-tombstoned current entries.

- [ ] **Step 1: Tests**: `remember_stores_a_vector_when_a_model_is_installed` (ignored without the model), `remember_stores_no_vector_without_a_model`, `forget_drops_the_vector`, `embed_missing_backfills_entries_received_from_peers`.
- [ ] **Step 2: Fail**, **Step 3: Implement** — embed `subject + ": " + fact` on `remember` and on accept; `forget` deletes the row; the migration appends the table (next free number).
- [ ] **Step 4: Pass; commit** `memory: a vector beside each entry`.

### Task 3: Hybrid recall

**Files:**
- Modify: `src/memory.rs` (`search`), `src/daemon/ipc.rs` (recall op unchanged in shape)
- Test: `tests/memory_store.rs`, `tests/memory_tools.rs`

**Interfaces:**
- Produces: `memory::search(store, query, limit) -> Vec<Fact>` now returns the RRF fusion (k = 60) of the bm25 ranking (as today, `src/memory.rs:480-506`) and the cosine ranking of `embed_model::text(query)` against `memory::vectors` (top 50 by cosine, linear scan); with no model, the bm25 list alone. Page composition unchanged.

- [ ] **Step 1: Tests**: `recall_finds_a_paraphrase_keyword_search_misses` (ignored without the model: "how do we ship releases" finds "deploy window: Deploys go out Tuesday mornings"), `recall_without_a_model_is_exactly_the_keyword_ranking`, `rrf_puts_an_entry_ranked_by_both_arms_first` (unit test on the fusion with fixed rank lists).
- [ ] **Step 2–4: Fail, implement, pass; commit** `recall: bm25 and cosine fused by reciprocal rank`.

### Task 4: The session's own turns, locally

**Files:**
- Modify: `src/store.rs` (migration: `capture_turns(session_id, seq, role, text, at)` + `turns_fts` FTS5 porter unicode61 + `turns_vec`), `src/capture.rs`, `src/hook.rs` (UserPromptSubmit: the prompt; Stop: the last assistant text — Claude Code from `transcript_path`'s last assistant message; Codex/Cursor/Grok/OpenCode: whatever their Stop payload carries, else nothing), `src/config.rs` (`[capture] turns = true`, `turns_days = 90`), `src/cli.rs` (`sessions turns on|off`, `sessions show` prints turns), `src/daemon/ipc.rs` (new capture op, agent-allowed like the other capture writes)
- Test: `tests/capture.rs`, `tests/hooks.rs`, `tests/cli.rs`

**Interfaces:**
- Produces: `capture::record_turn(store, session_id, role, text, now) -> Result<bool>` (text cut to 4 KB; `memory::refuse_secret` → stored as `[refused: <name>]`, not embedded); `capture::turns(store, session_id) -> Vec<Turn>`; `capture::prune_turns(store, now, days)` (daemon, daily); hidden folders skip as `record_tool_call` does.

- [ ] **Step 1: Tests**: `a_prompt_and_a_reply_are_recorded_as_turns`, `a_turn_with_a_secret_is_stored_refused_and_not_embedded`, `turns_off_records_nothing`, `turns_older_than_turns_days_are_pruned`, `sessions_show_prints_turns_in_order`, `sessions_turns_is_person_only`, `full_upload_never_sends_turns` (in `tests/upload.rs`).
- [ ] **Step 2–4: Fail, implement, pass; commit** `capture: the session's own turns, on this computer only`.
- [ ] **Step 5: Docs**: `docs/security.md` Capture bullet and `docs/cli.md` sessions table: "records your prompts and replies on your computer only; never uploaded; `sessions turns off`"; `src/setup/SKILL.md` one line.

### Task 5: Turns as a recall arm

**Files:**
- Modify: `src/memory.rs` (`page`: a `Turns:` section after `Lessons:` with up to 5 excerpts of ≤ 240 chars, each as `<role>, <date>: …`), `src/daemon/ipc.rs` (recall op passes the store for turns)
- Test: `tests/memory_tools.rs`

**Interfaces:**
- Produces: `capture::search_turns(store, query, limit) -> Vec<TurnHit>` (bm25 over `turns_fts` fused with cosine over `turns_vec`, same RRF); `recall`'s JSON gains `turns: [{session, seq, role, at, excerpt}]`; the start block never includes turns.

- [ ] **Step 1: Tests**: `recall_returns_an_excerpt_from_a_turn`, `the_start_block_has_no_turns`, `a_refused_turn_is_never_returned`.
- [ ] **Step 2–4: Fail, implement, pass; commit** `recall: excerpts from the session's own turns`.

### Task 6: The five inventory fixes

**Files:**
- Modify: `src/mcp.rs:237-269` (repo: walk up from `cwd` to the first parent with `.git`; key = `owner/name` from the origin URL, both `git@host:owner/name.git` and `https://host/owner/name(.git)`), `src/memory.rs:636-637` (page fill: skip a line that does not fit and keep going, so later sections still appear), `src/memory.rs:337-363` (contested groups keyed by `(scope, subject)`), `src/memory.rs:159-176` (accept-from-peer runs `refuse_secret`; a match is stored as a tombstone with reason, never with text)
- Test: `tests/hooks.rs`, `tests/memory_store.rs`, `tests/memory_sync.rs`

- [ ] **Step 1: Tests**: `a_session_in_a_subfolder_gets_the_repo_block`, `the_repo_key_carries_the_owner`, `a_long_current_section_does_not_hide_lessons`, `a_me_value_does_not_contest_a_team_value`, `an_entry_from_a_peer_with_a_secret_is_refused_on_accept`.
- [ ] **Step 2–4: Fail, implement, pass; commit** one commit per fix.

### Task 7: The end-of-turn nudge

**Files:**
- Modify: `src/hook.rs:357` (Stop), `src/store.rs` (column `capture_sessions.nudged INTEGER DEFAULT 0` in the same migration as Task 4 if unlanded, else the next), `src/daemon/ipc.rs`
- Test: `tests/hooks.rs`

**Interfaces:**
- Rule: on Stop, when the session has ≥ 1 recorded tool call, `nudged = 0`, no entry written by this session yet, and no waiting messages to hand over, answer `{"decision": "block", "reason": NUDGE}` once and set `nudged = 1`; otherwise the existing behaviour. `NUDGE` (exact text): `Before you stop: if this session settled a fact, made a decision or learned a lesson worth keeping, call remember for each (subject, fact, kind; replaces when a fact changed). Personal unless the person said to share. Then stop.` Cursor/OpenCode/Grok: the same reason through their Stop/plugin path (`src/hook.rs:126` already maps Stop to `decision: block`).
- Telemetry: the existing usage counts gain `memory_nudged` and `memory_written_after_nudge` (session-level booleans counted daily).

- [ ] **Step 1: Tests**: `the_first_stop_after_a_tool_call_nudges_once`, `a_stop_with_waiting_messages_hands_them_over_and_does_not_nudge`, `a_session_without_tool_calls_is_not_nudged`, `a_session_that_already_remembered_is_not_nudged`.
- [ ] **Step 2–4: Fail, implement, pass; commit** `hook: once per session, ask the agent to remember what it learned`.

### Task 8: The start block for Cursor, Grok CLI and OpenCode

**Files:**
- Modify: `src/hook.rs:112-118`, `src/agents/{cursor,grok,opencode}.rs` as needed; the OpenCode plugin file under `src/setup/`
- Test: `tests/hooks.rs`

- [ ] **Step 1: Read each harness's own hook docs (Cursor `sessionStart` hook output, Grok `SessionStart`, OpenCode plugin session event) and record in the module doc which can carry context into the model.** Where one can: build the same block. Where none can: leave as is and say so in `docs/usage.md`.
- [ ] **Step 2: Tests** per harness that gains it: `<harness>_session_start_includes_the_memory_block`.
- [ ] **Step 3: Implement, pass; commit.**

### Task 9: Docs, skill, release notes

**Files:** `docs/usage.md` (Memory and Capture), `docs/security.md`, `docs/cli.md`, `docs/connect/*.md` (the model at setup), `src/setup/SKILL.md`, `CHANGELOG.md` if present.

- [ ] **Step 1:** Every behaviour above appears once, in the file that owns it; the "never records prompts" sentence is replaced everywhere (`grep -rn "prompts" docs/`).
- [ ] **Step 2:** `cargo test` (docs tests, if any); commit `docs: recall by meaning, turns on this computer, the end-of-turn nudge`.

The v0.7.0 release (`docs/releasing.md`) is the founder's.
